"""왼쪽 사이드바: 썸네일(쪽 그림)·책갈피(TOC). 활성 보기의 문서를 따라간다.

- 썸네일은 보이는 것 ±THUMB_PREFETCH 만 낮은 배율로 요청한다. 작업자는 썸네일 통로를 보기 렌더가
  없을 때만 그리므로(THUMB_LANE) 화면의 쪽보다 앞서지 않는다. 캐시는 사이드바 전용 작은 LRU.
- 책갈피는 문서가 열릴 때 한 번 요청해 문서별로 보관한다. 제목은 기록하지 않는다.
- 클릭하면 pageRequested(쪽) — 주 창이 활성 보기를 옮긴다. 현재 쪽은 강조한다.
"""
from __future__ import annotations

import bisect

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractScrollArea, QLabel, QStackedWidget, QTabWidget, QTreeWidget, QTreeWidgetItem,
)

from bom_pdf.services.document import Document, DocumentService
from bom_pdf.ui.page_cache import PageCache

THUMB_W = 110  # 썸네일 칸 폭 (논리 px)
THUMB_GAP = 8
LABEL_H = 16
SIDE_MARGIN = 14
THUMB_PREFETCH = 4
THUMB_CACHE_BYTES = 24 * 1024 * 1024

SIDE_BG = QColor(0xee, 0xef, 0xf1)
CUR_PEN = QColor(0x1a, 0x73, 0xe8)
PLACEHOLDER = QColor(0xfa, 0xfa, 0xfa)
BORDER = QColor(0, 0, 0, 60)
ERROR_FILL = QColor(0xf3, 0xe5, 0xe5)


class ThumbnailView(QAbstractScrollArea):
    pageClicked = Signal(int)

    def __init__(self, service: DocumentService, cache: PageCache, doc: Document, parent=None):
        super().__init__(parent)
        self.service, self.cache, self.doc = service, cache, doc
        self._ack = 0
        self._last_request = None
        self._errors: set[int] = set()
        self._current = 0
        self._dead = False
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.verticalScrollBar().setSingleStep(40)
        self._layout()

    # ---- 배치
    def scale(self) -> float:
        """한 문서에 배율 하나 — 가장 넓은 쪽이 THUMB_W 에 맞는다."""
        widest = max(w for w, _ in self.doc.page_sizes)
        return round(THUMB_W / widest * self.devicePixelRatioF(), 4)

    def _layout(self) -> None:
        k = THUMB_W / max(w for w, _ in self.doc.page_sizes)
        y = THUMB_GAP
        self._tops, self._rects = [], []
        for w, h in self.doc.page_sizes:
            tw, th = max(1, round(w * k)), max(1, round(h * k))
            self._tops.append(y)
            self._rects.append((SIDE_MARGIN + (THUMB_W - tw) // 2, y, tw, th))
            y += th + LABEL_H + THUMB_GAP
        self._content_h = y
        vs = self.verticalScrollBar()
        vs.setRange(0, max(0, y - self.viewport().height()))
        vs.setPageStep(max(1, self.viewport().height()))

    def sizeHint(self):  # noqa: N802
        s = super().sizeHint()
        s.setWidth(THUMB_W + 2 * SIDE_MARGIN + self.verticalScrollBar().sizeHint().width())
        return s

    def visible_pages(self) -> range:
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = max(0, bisect.bisect_right(self._tops, top) - 1)
        last = max(first, bisect.bisect_left(self._tops, bottom) - 1)
        return range(first, min(last, len(self._tops) - 1) + 1)

    def page_at(self, y: int) -> int | None:
        cy = y + self.verticalScrollBar().value()
        i = bisect.bisect_right(self._tops, cy) - 1
        if 0 <= i < len(self._tops) and cy <= self._tops[i] + self._rects[i][3] + LABEL_H:
            return i
        return None

    # ---- 현재 쪽
    def set_current(self, page: int) -> None:
        if page == self._current or not 0 <= page < len(self._tops):
            return
        self._current = page
        top = self._tops[page]
        bottom = top + self._rects[page][3] + LABEL_H
        vs = self.verticalScrollBar()
        if top < vs.value() or bottom > vs.value() + self.viewport().height():
            vs.setValue(int(top - self.viewport().height() / 3))
        self.viewport().update()

    # ---- 요청·결과
    def _update_requests(self, force: bool = False) -> None:
        if self._dead or not self.isVisible():
            return
        vis = self.visible_pages()
        n = len(self._tops)
        order = list(vis) + list(range(vis.stop, min(n, vis.stop + THUMB_PREFETCH)))
        order += range(max(0, vis.start - THUMB_PREFETCH), vis.start)
        scale = self.scale()
        did = self.doc.doc_id
        needed = [p for p in order if p not in self._errors and not self.cache.contains(did, p, scale)]
        key = (scale, tuple(needed))
        if force or key != self._last_request:
            self._last_request = key
            self.service.request_thumbs(did, scale, needed, self._ack)

    def stop_requests(self) -> None:
        """숨겨질 때 — 남은 썸네일 요청을 비운다."""
        if self._last_request and self._last_request[1]:
            self.service.request_thumbs(self.doc.doc_id, self.scale(), [], self._ack)
        self._last_request = None

    def on_thumb_ready(self, page: int, scale: float, seq: int, image) -> None:
        self._ack = max(self._ack, seq)
        if self._dead:
            return
        if scale == self.scale():
            image.setDevicePixelRatio(self.devicePixelRatioF())
        self.cache.put(self.doc.doc_id, page, scale, image)
        if page in self.visible_pages():
            self.viewport().update()
        self._update_requests()

    def on_thumb_error(self, page: int, seq: int) -> None:
        self._ack = max(self._ack, seq)
        self._errors.add(page)
        self.viewport().update()
        self._update_requests()

    def on_engine_restarted(self) -> None:
        self._ack = 0
        self._last_request = None
        self._update_requests(force=True)

    def set_dead(self) -> None:
        self._dead = True

    # ---- Qt
    def paintEvent(self, ev) -> None:  # noqa: N802
        qp = QPainter(self.viewport())
        qp.fillRect(self.viewport().rect(), SIDE_BG)
        dy = -self.verticalScrollBar().value()
        scale = self.scale()
        did = self.doc.doc_id
        bold = QFont(self.font())
        bold.setBold(True)
        for p in self.visible_pages():
            x, y, w, h = self._rects[p]
            target = QRectF(x, y + dy, w, h)
            img = self.cache.get(did, p, scale)
            if img is not None:
                qp.drawImage(QPointF(target.x(), target.y()), img)
            elif p in self._errors:
                qp.fillRect(target, ERROR_FILL)
            else:
                qp.fillRect(target, PLACEHOLDER)
            cur = p == self._current
            qp.setPen(QPen(CUR_PEN, 3) if cur else QPen(BORDER, 1))
            qp.drawRect(target.adjusted(-1, -1, 0, 0) if not cur else target.adjusted(-2, -2, 1, 1))
            qp.setPen(CUR_PEN if cur else QColor(0x40, 0x40, 0x40))
            qp.setFont(bold if cur else self.font())
            qp.drawText(QRectF(0, y + dy + h, self.viewport().width(), LABEL_H),
                        Qt.AlignmentFlag.AlignCenter, str(p + 1))
        qp.end()
        self._update_requests()

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        self.viewport().update()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._layout()

    def hideEvent(self, ev) -> None:  # noqa: N802
        self.stop_requests()
        super().hideEvent(ev)

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            p = self.page_at(int(ev.position().y()))
            if p is not None:
                self.pageClicked.emit(p)
        super().mousePressEvent(ev)


class Sidebar(QTabWidget):
    """썸네일·책갈피 탭. 문서마다 ThumbnailView 하나(쌓아 두고 활성 문서 것만 보인다)."""

    pageRequested = Signal(int)

    def __init__(self, service: DocumentService, parent=None):
        super().__init__(parent)
        self.service = service
        self.cache = PageCache(THUMB_CACHE_BYTES)
        self._thumbs: dict[int, ThumbnailView] = {}
        self._outlines: dict[int, list] = {}
        self._doc: Document | None = None
        self._current = 0
        self._outline_index: list[tuple[int, QTreeWidgetItem]] = []

        self.thumb_stack = QStackedWidget()
        self.thumb_empty = QLabel("문서가 없습니다")
        self.thumb_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.thumb_stack.addWidget(self.thumb_empty)
        self.addTab(self.thumb_stack, "쪽")

        self.outline_stack = QStackedWidget()
        self.outline_empty = QLabel("책갈피가 없습니다")
        self.outline_empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.outline_empty.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemClicked.connect(self._on_outline_clicked)
        self.tree.itemActivated.connect(self._on_outline_clicked)
        self.outline_stack.addWidget(self.outline_empty)
        self.outline_stack.addWidget(self.tree)
        self.addTab(self.outline_stack, "책갈피")

    # ---- 문서
    @property
    def document(self) -> Document | None:
        return self._doc

    def thumbnails(self, doc_id: int) -> ThumbnailView | None:
        return self._thumbs.get(doc_id)

    def set_document(self, doc: Document | None, current: int = 0) -> None:
        if doc is self._doc:
            self.set_current_page(current)
            return
        self._doc = doc
        if doc is None:
            self.thumb_stack.setCurrentWidget(self.thumb_empty)
        else:
            tv = self._thumbs.get(doc.doc_id)
            if tv is None:
                tv = ThumbnailView(self.service, self.cache, doc)
                tv.pageClicked.connect(self.pageRequested)
                self._thumbs[doc.doc_id] = tv
                self.thumb_stack.addWidget(tv)
            self.thumb_stack.setCurrentWidget(tv)
        self._rebuild_outline()
        self._current = -1
        self.set_current_page(current)

    def drop_document(self, doc_id: int) -> None:
        tv = self._thumbs.pop(doc_id, None)
        if tv is not None:
            tv.set_dead()
            self.thumb_stack.removeWidget(tv)
            tv.deleteLater()
        self._outlines.pop(doc_id, None)
        self.cache.drop_doc(doc_id)
        if self._doc is not None and self._doc.doc_id == doc_id:
            self.set_document(None)

    def set_current_page(self, page: int) -> None:
        if self._doc is None or page == self._current:
            return
        self._current = page
        tv = self._thumbs.get(self._doc.doc_id)
        if tv is not None:
            tv.set_current(page)
        self._select_outline_for(page)

    # ---- 엔진 결과
    def on_thumb(self, ev) -> None:
        tv = self._thumbs.get(ev.doc_id)
        if tv is not None:
            tv.on_thumb_ready(ev.page, ev.scale, ev.seq, ev.image)

    def on_thumb_error(self, ev) -> None:
        tv = self._thumbs.get(ev.doc_id)
        if tv is not None:
            tv.on_thumb_error(ev.page, ev.seq)

    def set_outline(self, doc_id: int, items: list) -> None:
        self._outlines[doc_id] = items
        if self._doc is not None and self._doc.doc_id == doc_id:
            self._rebuild_outline()
            self._select_outline_for(self._current)

    def outline(self, doc_id: int) -> list | None:
        """받아 둔 책갈피(없으면 None — 아직 안 옴)."""
        return self._outlines.get(doc_id)

    def has_outline(self, doc_id: int) -> bool:
        return doc_id in self._outlines

    def set_doc_dead(self, doc_id: int) -> None:
        tv = self._thumbs.get(doc_id)
        if tv is not None:
            tv.set_dead()

    def on_engine_restarted(self) -> None:
        for doc_id, tv in self._thumbs.items():
            tv.on_engine_restarted()
            if doc_id not in self._outlines:
                self.service.request_outline(doc_id)

    # ---- 책갈피
    def _rebuild_outline(self) -> None:
        self.tree.clear()
        self._outline_index = []
        items = self._outlines.get(self._doc.doc_id) if self._doc is not None else None
        if not items:
            self.outline_empty.setText("책갈피를 읽는 중…" if self._doc is not None and items is None
                                       else "책갈피가 없습니다")
            self.outline_stack.setCurrentWidget(self.outline_empty)
            return
        parents: list[QTreeWidgetItem] = []
        for level, title, page in items:
            del parents[max(0, level - 1):]
            parent = parents[-1] if parents else None
            it = QTreeWidgetItem([title or "(제목 없음)"])
            it.setData(0, Qt.ItemDataRole.UserRole, page)
            if page >= 0:
                it.setToolTip(0, f"{page + 1}쪽")
            if parent is None:
                self.tree.addTopLevelItem(it)
            else:
                parent.addChild(it)
            parents.append(it)
            if page >= 0:
                self._outline_index.append((page, it))
        self.tree.expandToDepth(0)
        self.outline_stack.setCurrentWidget(self.tree)

    def _select_outline_for(self, page: int) -> None:
        """현재 쪽 이전의 마지막 책갈피를 선택(문서 순서). 신호는 막는다."""
        best = None
        for p, it in self._outline_index:
            if p <= page:
                best = it
        self.tree.blockSignals(True)
        if best is None:
            self.tree.clearSelection()
        else:
            self.tree.setCurrentItem(best)
        self.tree.blockSignals(False)

    def _on_outline_clicked(self, item: QTreeWidgetItem, _col: int = 0) -> None:
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(page, int) and page >= 0:
            self.pageRequested.emit(page)
