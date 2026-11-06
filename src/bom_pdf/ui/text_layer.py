"""쪽별 글자 상자 캐시(선택용). 작업자에게 필요한 쪽만 요청하고 결과를 보기들이 함께 쓴다.

글자 자체는 메모리에만 두고 기록(log)하지 않는다.
"""
from __future__ import annotations

from collections import OrderedDict

from bom_pdf.services.document import DocumentService, PageChars


class TextLayer:
    MAX_PAGES = 64

    def __init__(self, service: DocumentService):
        self.service = service
        self._pages: OrderedDict = OrderedDict()  # (doc, page) -> [(c, x0, y0, x1, y1, line)]
        self._pending: set = set()

    def get(self, doc_id: int, page: int) -> list | None:
        """있으면 글자 목록(빈 목록 = 글자 없음), 없으면 요청하고 None — 도착하면 PageChars 이벤트."""
        key = (doc_id, page)
        chars = self._pages.get(key)
        if chars is not None:
            self._pages.move_to_end(key)
            return chars
        if key not in self._pending:
            self._pending.add(key)
            self.service.request_chars(doc_id, page)
        return None

    def on_chars(self, ev: PageChars) -> None:
        key = (ev.doc_id, ev.page)
        self._pending.discard(key)
        self._pages[key] = ev.chars or []
        while len(self._pages) > self.MAX_PAGES:
            self._pages.popitem(last=False)

    def drop_doc(self, doc_id: int) -> None:
        for k in [k for k in self._pages if k[0] == doc_id]:
            del self._pages[k]
        self._pending = {k for k in self._pending if k[0] != doc_id}

    def on_engine_restarted(self) -> None:
        self._pending.clear()  # 보낸 요청이 사라졌다 — 다음 get 에서 다시 요청
