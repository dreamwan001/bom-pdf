"""문서 열기·쪽 정보·식별값(sha256)·상태 문구 (D11). PySide6 를 import하지 않는다.

- 열기·렌더·텍스트는 렌더 작업자 프로세스(engine.worker)가 한다. 이 프로세스는 PyMuPDF 문서를 갖지 않는다.
- sha256(O4): 캐시(`(경로, 크기, mtime)`)에 있으면 열 때 바로. 작업자가 원격 파일을 메모리로 읽었으면 그 읽기에서 함께.
  둘 다 아니면 첫 쪽이 보인 뒤 낮은 입출력 우선순위 스레드로 전체를 읽는다(렌더와 읽기 다툼을 줄임).
- 원본은 읽기만 한다. 이 모듈이 쓰는 파일은 데이터 폴더의 캐시 하나뿐이다.
- 이벤트 콜백은 백그라운드 스레드에서 불린다.
"""
from __future__ import annotations

import itertools
import logging
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from bom_pdf import paths, storage
from bom_pdf.engine import jobs
from bom_pdf.engine import worker as eng

log = logging.getLogger(__name__)

# 열기 실패 종류
NEEDS_PASSWORD = "needs_password"
WRONG_PASSWORD = "wrong_password"
CANNOT_OPEN = "cannot_open"
ENGINE_CRASH = "engine_crash"

MSG_REPAIRED = ("파일에 손상된 부분이 있어 복구해서 열었습니다. 원본 파일은 바꾸지 않았습니다. "
                "일부 쪽이 원래와 다르게 보일 수 있습니다.")
MSG_NEEDS_PASSWORD = "암호가 걸린 PDF입니다. 암호를 입력해야 열 수 있습니다."
MSG_WRONG_PASSWORD = "암호가 맞지 않습니다."
MSG_ENGINE_CRASH = ("이 문서를 처리하던 중 PDF 엔진이 멈췄습니다. 원본 파일은 바뀌지 않았고, "
                    "다른 문서는 계속 쓸 수 있습니다.")
_CANNOT_OPEN_REASONS = {
    "not_found": "파일을 찾을 수 없습니다.",
    "no_permission": "파일을 읽을 권한이 없습니다.",
    "empty": "빈 파일(0바이트)입니다.",
    "damaged": "PDF 형식이 아니거나 너무 많이 손상되어 열 수 없습니다.",
    "no_pages": "쪽이 하나도 없는 PDF입니다.",
}


def cannot_open_message(reason: str) -> str:
    return "PDF를 열 수 없습니다. " + _CANNOT_OPEN_REASONS.get(reason, "알 수 없는 오류입니다.")


# --------------------------------------------------------------------------- 모델·이벤트


@dataclass
class Document:
    doc_id: int
    path: str
    page_sizes: list[tuple[float, float]]  # pt, 회전 반영
    repaired: bool = False
    sha256: str | None = None
    password: str | None = field(default=None, repr=False)
    embedded_annots: int = 0  # 원본 PDF 에 이미 든 주석 수 — 엔진이 그리기만 하고 bom-pdf 로는 편집하지 않는다
    embedded_notes: list = field(default_factory=list, repr=False)  # 그중 메모 글이 든 것(읽기만, 기록 금지)

    @property
    def page_count(self) -> int:
        return len(self.page_sizes)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


@dataclass
class DocumentOpened:
    doc: Document
    notice: str | None  # 복구해서 연 경우 안내 문구


@dataclass
class DocumentOpenFailed:
    doc_id: int
    path: str
    kind: str  # NEEDS_PASSWORD / WRONG_PASSWORD / CANNOT_OPEN / ENGINE_CRASH
    message: str


@dataclass
class DocumentLoading:
    """(O4) 원격 파일을 작업자가 메모리로 읽는 중 — done/total 바이트."""
    doc_id: int
    done: int
    total: int


@dataclass
class DocumentHashed:
    doc_id: int
    sha256: str


@dataclass
class DocumentCrashed:
    """이미 열린 문서를 처리하다 엔진이 멈춤 — 그 문서는 더 이상 렌더하지 않는다."""
    doc_id: int
    message: str


@dataclass
class EngineRestarted:
    """작업자가 다시 떴다. 보기는 필요한 쪽을 다시 요청해야 한다."""
    pid: int


@dataclass
class EngineFailed:
    message: str


# PageImage / PageError / PageText 는 engine.worker 의 것을 그대로 넘긴다.
PageImage = eng.PageImage
PageError = eng.PageError
PageText = eng.PageText
Outline = eng.Outline
PageChars = eng.PageChars
ExportDone = eng.ExportDone
from bom_pdf.engine.exportstatus import (  # noqa: E402,F401  재수출
    EXPORT_CHANGED, EXPORT_ERROR, EXPORT_INTERRUPTED, EXPORT_OK, EXPORT_REFUSED, JOB_CANCELLED, JOB_NOT_SMALLER,
)
JobProgress = jobs.JobProgress
JobDone = jobs.JobDone
KIND_BUILD, KIND_COMPRESS = jobs.KIND_BUILD, jobs.KIND_COMPRESS
EDIT_LANE = eng.EDIT_LANE
PRINT_LANE = eng.PRINT_LANE
SearchHit = eng.SearchHit
SearchResults = eng.SearchResults
SearchDone = eng.SearchDone
THUMB_LANE = eng.THUMB_LANE
TILE_PX = eng.TILE_PX
SEARCH_DONE, SEARCH_TRUNCATED = eng.SEARCH_DONE, eng.SEARCH_TRUNCATED
SEARCH_CANCELLED, SEARCH_INTERRUPTED = eng.SEARCH_CANCELLED, eng.SEARCH_INTERRUPTED


# --------------------------------------------------------------------------- sha256


# (O4) sha256·해시 캐시는 bom_pdf.storage 로 옮겼다 — 작업자·작업 프로세스도 같이 쓴다. 이름은 그대로 재수출.
file_sha256 = storage.file_sha256
HashCache = storage.HashCache
HASH_WAIT_FIRST_PAGE_S = 10.0  # 캐시에 없으면 첫 그림(쪽·썸네일)이 온 뒤(또는 이만큼 뒤)에 낮은 우선순위로 전체를 읽는다


# --------------------------------------------------------------------------- 서비스


class DocumentService:
    def __init__(self, on_event: Callable[[object], None], data_dir: Path | None = None):
        self._on_event = on_event
        base = data_dir or paths.data_dir()
        self.data_dir = base
        self.hash_cache = HashCache(base / "hash_cache.json")
        self._lock = threading.Lock()
        self._docs: dict[int, Document] = {}
        self._pending: dict[int, tuple[str, str | None]] = {}
        self._ids = itertools.count(1)
        self._client = eng.EngineClient(self._on_engine_event)
        self._jobs = jobs.JobRunner(self._emit)
        self._closed = False
        self._first_page: dict[int, threading.Event] = {}  # 해시 스레드가 기다리는 첫 쪽 도착
        self._hash_cancel: dict[int, threading.Event] = {}

    # ---- 수명
    def start(self) -> None:
        self._client.start()

    def shutdown(self) -> None:
        self._closed = True
        self._jobs.shutdown()
        self._client.shutdown()

    @property
    def engine(self) -> eng.EngineClient:
        return self._client

    # ---- 문서
    def open(self, path: str, password: str | None = None, doc_id: int | None = None) -> int:
        """비동기 열기. 결과는 DocumentOpened / DocumentOpenFailed 이벤트. doc_id 를 주면 암호 재시도."""
        path = os.path.abspath(path)
        if doc_id is None:
            doc_id = next(self._ids)
        with self._lock:
            self._pending[doc_id] = (path, password)
        log.info("open doc=%s path=%s", doc_id, path)
        self._client.open(doc_id, path, password)
        return doc_id

    def close(self, doc_id: int) -> None:
        with self._lock:
            self._docs.pop(doc_id, None)
            self._pending.pop(doc_id, None)
            cancel = self._hash_cancel.pop(doc_id, None)
            first = self._first_page.pop(doc_id, None)
        if cancel is not None:
            cancel.set()
        if first is not None:
            first.set()
        self._client.close_doc(doc_id)

    def document(self, doc_id: int) -> Document | None:
        return self._docs.get(doc_id)

    def request_pages(self, doc_id: int, scale: float, pages: list[int], ack: int, lane: int = 0) -> None:
        """그 (문서, 통로)의 렌더 요청을 이 목록으로 바꾼다(앞쪽이 우선). 지나간 요청은 버려진다.

        lane: 보기마다 다른 0 이상의 번호 — 분할 창의 두 보기가 같은 문서를 봐도 서로의 요청을 지우지 않는다.
        """
        self._client.want(doc_id, scale, pages, ack, lane)

    def request_thumbs(self, doc_id: int, scale: float, pages: list[int], ack: int) -> None:
        """썸네일 요청 — 보기 렌더보다 늦게 그린다. 결과는 lane == THUMB_LANE 인 PageImage."""
        self._client.want_thumbs(doc_id, scale, pages, ack)

    def request_text(self, doc_id: int, page: int) -> int:
        return self._client.request_text(doc_id, page)

    def request_chars(self, doc_id: int, page: int) -> int:
        """쪽의 글자 상자(선택용) — 결과는 PageChars 이벤트."""
        return self._client.request_chars(doc_id, page)

    def export_annotated(self, doc_id: int, out_path: str, items: list) -> int | None:
        """메모 포함 PDF(새 파일) — 결과는 ExportDone. 원본이 연 뒤에 바뀌었으면(sha256) 작업자가 거절한다.

        sha256 을 아직 모르면 None(내보내지 않음). out_path 는 호출자가 원본이 아닌지 확인한 경로.
        """
        doc = self._docs.get(doc_id)
        if doc is None or doc.sha256 is None:
            return None
        log.info("export annotated doc=%s items=%d", doc_id, len(items))
        return self._client.export_annotated(doc_id, os.path.abspath(out_path), doc.sha256, items)

    # ---- 1-4 긴 파일 작업(작업 프로세스) — 결과는 JobProgress … JobDone
    def build_pages(self, job: dict) -> int:
        """페이지 편집 결과(저장·추출·분할·병합)를 새 파일로. job 은 services.pageops.PageOps.plan() 의 것.

        산출물 경로는 호출자가 열린 원본이 아닌지 확인한 것. 작업자도 원본 경로·바뀐 원본을 다시 거절한다.
        """
        outs = [o["path"] for o in job["outputs"]]
        log.info("build pages outputs=%d pages=%d", len(outs), sum(len(o["pages"]) for o in job["outputs"]))
        return self._jobs.start(jobs.KIND_BUILD, (job,), outs)

    def compress(self, doc_id: int, out_path: str, preset: str) -> int | None:
        """용량 압축본(새 파일). sha256 을 아직 모르면 None."""
        doc = self._docs.get(doc_id)
        if doc is None or doc.sha256 is None:
            return None
        out_path = os.path.abspath(out_path)
        log.info("compress doc=%s preset=%s", doc_id, preset)
        return self._jobs.start(jobs.KIND_COMPRESS, (doc.path, doc.password, doc.sha256, out_path, preset),
                                [out_path])

    # ---- 1-5 사진 → PDF (작업 프로세스) — 결과는 JobProgress … JobDone
    def scan_images(self, paths: list[str]) -> int:
        """사진 정보(크기·촬영시각)·미리보기·예상 용량 표본. 산출물 없음."""
        log.info("scan images n=%d", len(paths))
        return self._jobs.start(jobs.KIND_IMAGE_SCAN, (list(paths),), [])

    def images_to_pdf(self, job: dict) -> int:
        """services.imagetopdf.plan() 의 작업 → 새 PDF. 산출물 경로는 호출자가 원본이 아닌지 확인한 것."""
        log.info("images to pdf n=%d per_page=%s page=%s preset=%s", len(job["items"]), job["per_page"],
                 job["page"], job["preset"])
        return self._jobs.start(jobs.KIND_IMAGE_PDF, (job,), [job["out"]])

    def cancel_job(self, job_id: int) -> None:
        self._jobs.cancel(job_id)

    def running_jobs(self) -> list[int]:
        return self._jobs.running()

    def request_outline(self, doc_id: int) -> None:
        """책갈피(TOC) — 결과는 Outline 이벤트."""
        self._client.request_outline(doc_id)

    def search(self, doc_id: int, query: str, ignore_space: bool = False) -> int:
        """문서 안 검색(작업자). SearchResults 가 조각마다 오고 SearchDone 으로 끝난다. 새 검색은 앞 검색을 대체."""
        return self._client.search(doc_id, query, ignore_space)

    def cancel_search(self, search_id: int) -> None:
        self._client.cancel_search(search_id)

    # ---- 엔진 이벤트 → 서비스 이벤트
    def _emit(self, ev) -> None:
        if not self._closed:
            self._on_event(ev)

    def _on_engine_event(self, ev) -> None:
        if isinstance(ev, (eng.PageImage, eng.PageError)):
            first = self._first_page.get(ev.doc_id)
            if first is not None:
                first.set()
        if isinstance(ev, eng.Opened):
            self._on_opened(ev)
        elif isinstance(ev, eng.Loading):
            self._emit(DocumentLoading(ev.doc_id, ev.done, ev.total))
        elif isinstance(ev, eng.WorkerRestarted):
            self._on_restarted(ev)
        elif isinstance(ev, eng.WorkerFailed):
            self._emit(EngineFailed(ev.message))
        else:  # PageImage / PageError / PageText / Outline / SearchResults / SearchDone
            self._emit(ev)

    def _on_opened(self, ev: eng.Opened) -> None:
        if ev.reopen:
            if ev.status != "ok":
                with self._lock:
                    known = self._docs.pop(ev.doc_id, None)
                if known is not None:
                    self._emit(DocumentCrashed(ev.doc_id, cannot_open_message(ev.reason)))
            return
        with self._lock:
            pending = self._pending.pop(ev.doc_id, None)
        if pending is None:
            return  # 그사이 닫힘
        path, password = pending
        if ev.status == "ok":
            doc = Document(ev.doc_id, path, [tuple(s) for s in ev.page_sizes], ev.repaired, password=password,
                           embedded_annots=ev.embedded_annots, embedded_notes=list(ev.embedded_notes or []))
            if ev.sha256 and ev.stat:  # 작업자가 메모리로 읽으며 함께 계산(원격 파일)
                doc.sha256 = ev.sha256
                self.hash_cache.put(storage.stat_key(path, *ev.stat), ev.sha256)
            else:
                doc.sha256 = self.hash_cache.cached(path)  # 다시 연 파일 — 읽지 않고 바로
            with self._lock:
                self._docs[ev.doc_id] = doc
                if doc.sha256 is None:
                    self._first_page[ev.doc_id] = threading.Event()
                    self._hash_cancel[ev.doc_id] = threading.Event()
            log.info("opened doc=%s pages=%d repaired=%s memory=%s sha=%s", ev.doc_id, doc.page_count, ev.repaired,
                     bool(ev.sha256), "known" if doc.sha256 else "later")
            self._emit(DocumentOpened(doc, MSG_REPAIRED if ev.repaired else None))
            if doc.sha256 is not None:
                self._emit(DocumentHashed(doc.doc_id, doc.sha256))
            else:
                threading.Thread(target=self._hash_job, args=(doc,), daemon=True, name="bom-pdf-sha256").start()
            return
        if ev.status == "needs_password":
            kind, msg = NEEDS_PASSWORD, MSG_NEEDS_PASSWORD
        elif ev.status == "wrong_password":
            kind, msg = WRONG_PASSWORD, MSG_WRONG_PASSWORD
        else:
            kind, msg = CANNOT_OPEN, cannot_open_message(ev.reason)
        log.warning("open failed doc=%s kind=%s reason=%s path=%s", ev.doc_id, kind, ev.reason, path)
        self._emit(DocumentOpenFailed(ev.doc_id, path, kind, msg))

    def _on_restarted(self, ev: eng.WorkerRestarted) -> None:
        culprit = ev.culprit_doc
        if culprit is not None:
            with self._lock:
                pending = self._pending.pop(culprit, None)
                doc = self._docs.pop(culprit, None)
            if pending is not None:
                self._emit(DocumentOpenFailed(culprit, pending[0], ENGINE_CRASH, MSG_ENGINE_CRASH))
            elif doc is not None:
                self._emit(DocumentCrashed(culprit, MSG_ENGINE_CRASH))
        self._emit(EngineRestarted(ev.pid))

    def _hash_job(self, doc: Document) -> None:
        """캐시에 없는 파일: 첫 쪽이 보일 때까지(최대 HASH_WAIT_FIRST_PAGE_S) 기다렸다가 낮은 우선순위로 전체를 읽는다."""
        with self._lock:
            first = self._first_page.get(doc.doc_id)
            cancel = self._hash_cancel.get(doc.doc_id)
        if first is not None:
            first.wait(HASH_WAIT_FIRST_PAGE_S)
        if cancel is not None and cancel.is_set():
            return
        storage.background_io()
        try:
            digest = self.hash_cache.sha256_of(doc.path, cancel=cancel)
        except OSError:
            log.exception("sha256 failed doc=%s", doc.doc_id)
            return
        finally:
            with self._lock:
                self._first_page.pop(doc.doc_id, None)
                self._hash_cancel.pop(doc.doc_id, None)
        if digest is None:
            return
        doc.sha256 = digest
        if doc.doc_id in self._docs:
            self._emit(DocumentHashed(doc.doc_id, digest))
