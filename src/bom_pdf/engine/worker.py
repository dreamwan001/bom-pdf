"""렌더 작업자 프로세스와 UI 쪽 클라이언트 (결정 D3).

PyMuPDF `get_pixmap`은 GIL을 놓지 않으므로 렌더·텍스트 추출은 별도 프로세스 하나에서 한다.
통신은 단방향 파이프 두 개(명령 / 결과). shared memory 는 쓰지 않는다(Phase 0 에서 이득 없음).

명령 (UI → 작업자)
    ("open",  doc_id, path, password)
    ("close", doc_id)
    ("want",  doc_id, scale, pages, ack, lane)  그 (문서, 통로)의 대기열을 통째로 바꾼다 — 최신 요청만 유효
              pages 의 항목: 쪽 번호 int(그 배율로 쪽 전체) | (쪽, tx, ty)(그 배율의 TILE_PX 타일, O2)
                             | (쪽, 다른 배율)(쪽 전체 — 확대 중 임시로 보일 낮은 해상도 미리보기)
    ("text",  doc_id, page, req_id)
    ("chars", doc_id, page, req_id)        글자 상자(선택용) — 텍스트와 같은 우선순위
    ("outline", doc_id)
    ("search", doc_id, search_id, needle, ignore_space)  진행 중인 검색은 무엇이든 이것으로 대체
    ("search_cancel", search_id)
    ("export", doc_id, req_id, out_path, expect_sha256, items)  메모 포함 PDF(새 파일) — 명령처럼 바로 처리
    ("quit",)
    ("_crash", doc_id)                     시험용: 그 문서 작업 중 급사 흉내
결과 (작업자 → UI)
    ("busy",   doc_id)                     작업 시작 직전 — 급사 시 원인 문서 판정용
    ("loading", doc_id, done, total)       (O4) 원격 파일을 메모리로 읽는 중(바이트)
    ("opened", doc_id, status, reason, repaired, page_sizes, sha256, stat, embedded_annots, embedded_notes)
              sha256·stat((크기, mtime_ns)): 메모리로 읽어 연 경우에만, 아니면 None
    ("page",   doc_id, page, scale, seq, w, h, stride, lane, tile)  바로 뒤에 send_bytes(RGB 픽셀)
              tile: None(쪽 전체) | (tx, ty, x, y) — x, y 는 쪽 전체 그림 안에서 타일의 왼쪽 위 장치 px
    ("page_error", doc_id, page, scale, seq, lane)
    ("text",   doc_id, page, req_id, text | None)
    ("chars",  doc_id, page, req_id, [(c, x0, y0, x1, y1, line)] | None)
    ("exported", doc_id, req_id, result)   result: pdf.export_annotated 의 dict
    ("outline", doc_id, [(level, title, page)])
    ("search_hits", doc_id, search_id, [(page, [hit, ...])], pages_done, page_count)
    ("search_done", doc_id, search_id, hit_count, status)   status: done / truncated / cancelled

통로(lane): 보기 하나(분할 창의 각 쪽)마다 0 이상의 번호, 썸네일·편집 격자·인쇄는 음수(THUMB_LANE·EDIT_LANE·
PRINT_LANE) — 음수 통로는 썸네일 우선순위이고, 도착 예정 쪽(`ack`)을 통로마다 따로 센다.
우선순위: 명령 > 보기 렌더(가장 최근에 요청한 통로부터) > 텍스트 > 검색 조각·썸네일(번갈아).
검색은 SEARCH_SLICE_S 동안 몇 쪽씩만 하고 명령을 다시 확인하므로 렌더·취소가 막히지 않는다.

`ack`: UI 가 그 문서에 대해 이미 받아 처리한 결과의 seq 최댓값. 이미 보냈지만 UI 가 아직
받지 못한 쪽(seq > ack)은 새 요청에 들어 있어도 다시 그리지 않는다(곧 도착한다).
보기 통로들은 같은 문서의 결과를 모두 받으므로 서로의 도착 예정 쪽을 공유한다. 썸네일은 따로 센다.

원격 파일(O4, storage.is_remote — 네트워크 드라이브·클라우드 온라인 전용)로 storage.MEM_OPEN_MAX 이하이면 작업자가 1MB 씩
순서대로 한 번 읽어(sha256 도 함께) 메모리에서 연다 — MuPDF 의 잘게 나눈 읽기(쪽 수만큼 수천 번)가 네트워크 왕복을 쌓지
않게. 읽는 동안 "loading" 을 보내고, 그 문서의 "close" 가 오면 그만둔다(다른 명령은 읽은 뒤 차례대로 처리).
메모리로 연 문서 합이 MEM_OPEN_TOTAL 을 넘을 것 같으면 경로로 연다.

이 모듈의 클라이언트 쪽은 PyMuPDF·PySide6 를 import하지 않는다.
"""
from __future__ import annotations

import itertools
import logging
import multiprocessing
import os
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Callable

from bom_pdf.engine.exportstatus import EXPORT_INTERRUPTED
from bom_pdf.engine.textnorm import normalize_query

log = logging.getLogger(__name__)

_SENT_LOG_MAX = 512
TILE_PX = 1024  # 확대 타일 한 변(장치 px) — RGB32 4MB. 보기(UI)와 작업자가 같은 격자를 쓴다
_DLIST_MAX = 2  # 타일용으로 해석해 둔 쪽(display list) 수
THUMB_LANE = -1  # 사이드바 썸네일
EDIT_LANE = -2  # 1-4 페이지 편집 격자 썸네일
PRINT_LANE = -3  # 1-4 인쇄용 고해상도 렌더
SEARCH_SLICE_S = 0.015  # 검색이 명령 확인 없이 한 번에 쓰는 최대 시간
MAX_SEARCH_HITS = 5000
MEM_OPEN_TOTAL = 96 * 2**20  # 메모리로 연 문서 합 상한(작업자 메모리 — D13 600MB 안)
LOADING_EVERY_S = 0.2

SEARCH_DONE = "done"
SEARCH_TRUNCATED = "truncated"  # MAX_SEARCH_HITS 에서 멈춤
SEARCH_CANCELLED = "cancelled"
SEARCH_INTERRUPTED = "interrupted"  # 작업자 급사 (클라이언트가 만든다)


# --------------------------------------------------------------------------- 작업자 프로세스


def worker_main(cmd_conn, res_conn) -> None:
    """작업자 프로세스 본체. 부모가 사라지면(파이프 끊김) 조용히 끝난다."""
    try:
        _worker_loop(cmd_conn, res_conn)
    except (EOFError, OSError):
        pass


class _Search:
    __slots__ = ("doc_id", "sid", "needle", "ignore_space", "page", "hits")

    def __init__(self, doc_id, sid, needle, ignore_space):
        self.doc_id, self.sid, self.needle, self.ignore_space = doc_id, sid, needle, ignore_space
        self.page = 0
        self.hits = 0


def _worker_loop(cmd_conn, res_conn) -> None:
    from bom_pdf import sysinfo
    from bom_pdf.engine import pdf  # 작업자 프로세스에서만 PyMuPDF 를 불러온다

    spec = sysinfo.slow_read_spec()
    meter = None
    if spec:  # 시험·측정 전용 느린 저장소 근사(O4) — psutil 이 없는 포장본에서는 꺼진다
        try:
            meter = sysinfo.SlowReadMeter(spec)
        except ImportError:
            meter = None

    def io_begin() -> None:
        if meter is not None:
            meter.begin()

    def io_settle() -> None:
        if meter is not None:
            meter.settle()

    docs: dict = {}
    sources: dict[int, tuple[str, str | None]] = {}  # 내보내기 때 원본을 다시 열 경로·암호
    queues: dict[tuple[int, int], tuple[float, deque]] = {}  # (doc, lane) -> (scale, pages)
    order: list[tuple[int, int]] = []  # 가장 최근에 요청받은 (문서, 통로)가 앞
    texts: deque = deque()
    sent: OrderedDict = OrderedDict()  # (doc, 항목, scale, 통로 묶음 min(lane, 0)) -> seq
    dlists: OrderedDict = OrderedDict()  # (doc, page) -> DisplayList (타일 렌더용, 최근 _DLIST_MAX 개)
    seq = 0
    search: _Search | None = None
    search_turn = True  # 검색 조각과 썸네일을 번갈아
    backlog: deque = deque()  # 메모리로 읽는 동안 받은 다른 명령 — 읽은 뒤 차례대로
    mem_sizes: dict[int, int] = {}  # 메모리로 연 문서 → 바이트

    def read_into_memory(doc_id: int, path: str):
        """(bytes, sha256, (크기, mtime_ns)) | None(이 문서가 닫힘 — 취소) | "path"(메모리로 열지 않음)."""
        from bom_pdf import paths as _paths
        from bom_pdf import storage

        try:
            if not storage.is_remote(path):
                return "path"
            st = os.stat(_paths.long_path(path))
        except OSError:
            return "path"
        if (st.st_size <= 0 or st.st_size > storage.MEM_OPEN_MAX
                or sum(mem_sizes.values()) + st.st_size > MEM_OPEN_TOTAL):
            return "path"
        import hashlib

        h = hashlib.sha256()
        buf = bytearray()
        last = 0.0
        try:
            with open(_paths.long_path(path), "rb") as f:
                while True:
                    io_begin()
                    b = f.read(storage.CHUNK)
                    io_settle()
                    if not b:
                        break
                    h.update(b)
                    buf += b
                    now = time.monotonic()
                    if now - last >= LOADING_EVERY_S:
                        last = now
                        res_conn.send(("loading", doc_id, len(buf), st.st_size))
                    while cmd_conn.poll(0):
                        m = cmd_conn.recv()
                        if m[0] == "close" and m[1] == doc_id:
                            return None
                        backlog.append(m)
        except OSError:
            return "path"  # 경로로 열면 open_pdf 가 이유(없음·권한)를 가린다
        st2 = os.stat(_paths.long_path(path))
        if (st2.st_size, st2.st_mtime_ns) != (st.st_size, st.st_mtime_ns) or len(buf) != st.st_size:
            return "path"  # 읽는 중 바뀜 — 보통 경로로
        return bytes(buf), h.hexdigest(), (st.st_size, st.st_mtime_ns)

    def handle(msg) -> bool:
        nonlocal search
        op = msg[0]
        if op == "want":
            _, doc_id, scale, pages, ack, *rest = msg
            lane = rest[0] if rest else 0
            group = min(lane, 0)  # 보기 통로는 도착 예정 쪽을 공유(0), 음수 통로(썸네일·편집·인쇄)는 따로
            key = (doc_id, lane)
            q = deque(p for p in pages if sent.get((doc_id, p, scale, group), 0) <= ack)
            if q:
                queues[key] = (scale, q)
                if key in order:
                    order.remove(key)
                order.insert(0, key)
            else:
                queues.pop(key, None)
                if key in order:
                    order.remove(key)
        elif op in ("text", "chars"):
            texts.append(msg)
        elif op == "export":
            _, doc_id, req_id, out_path, expect, items = msg
            src = sources.get(doc_id)
            if src is None:
                result = {"status": pdf.EXPORT_ERROR, "reason": "not_open"}
            else:
                res_conn.send(("busy", doc_id))
                io_begin()
                try:
                    result = pdf.export_annotated(src[0], src[1], out_path, items, expect)
                except Exception as e:  # 경로·본문이 든 메시지는 보내지 않는다
                    result = {"status": pdf.EXPORT_ERROR, "reason": type(e).__name__}
                io_settle()
            res_conn.send(("exported", doc_id, req_id, result))
        elif op == "outline":
            doc_id = msg[1]
            doc = docs.get(doc_id)
            if doc is not None:
                res_conn.send(("busy", doc_id))
                res_conn.send(("outline", doc_id, pdf.outline(doc)))
        elif op == "search":
            _, doc_id, sid, needle, ignore_space = msg
            search = _Search(doc_id, sid, needle, ignore_space)  # 앞 검색은 말없이 버린다
        elif op == "search_cancel":
            if search is not None and search.sid == msg[1]:
                res_conn.send(("search_done", search.doc_id, search.sid, search.hits, SEARCH_CANCELLED))
                search = None
        elif op == "open":
            _, doc_id, path, password = msg
            res_conn.send(("busy", doc_id))
            got = read_into_memory(doc_id, path)
            if got is None:
                return True  # 읽는 중 닫힘 — 답하지 않는다(클라이언트는 이미 잊었다)
            sha = stat = None
            io_begin()
            if got == "path":
                r = pdf.open_pdf(path, password)
            else:
                data, sha, stat = got
                r = pdf.open_pdf(path, password, data=data)
            io_settle()
            old = docs.pop(doc_id, None)
            if old is not None:
                old.close()
            mem_sizes.pop(doc_id, None)
            if r.doc is not None:
                docs[doc_id] = r.doc
                sources[doc_id] = (path, password)
                if sha is not None:
                    mem_sizes[doc_id] = stat[0]
            res_conn.send(("opened", doc_id, r.status, r.reason, r.repaired, r.page_sizes,
                           sha if r.doc is not None else None, stat if r.doc is not None else None,
                           r.embedded_annots, r.embedded_notes))
        elif op == "close":
            doc_id = msg[1]
            for key in [k for k in queues if k[0] == doc_id]:
                del queues[key]
            order[:] = [k for k in order if k[0] != doc_id]
            if search is not None and search.doc_id == doc_id:
                search = None
            d = docs.pop(doc_id, None)
            sources.pop(doc_id, None)
            mem_sizes.pop(doc_id, None)
            if d is not None:
                d.close()
            for k in [k for k in sent if k[0] == doc_id]:
                del sent[k]
            for k in [k for k in dlists if k[0] == doc_id]:
                del dlists[k]
        elif op == "quit":
            return False
        elif op == "_crash":
            res_conn.send(("busy", msg[1]))
            os._exit(70)
        return True

    def search_slice() -> None:
        """검색을 SEARCH_SLICE_S 동안만 진행하고 찾은 것을 한 번에 보낸다."""
        nonlocal search
        s = search
        doc = docs.get(s.doc_id)
        if doc is None:
            search = None
            res_conn.send(("search_done", s.doc_id, s.sid, s.hits, SEARCH_CANCELLED))
            return
        res_conn.send(("busy", s.doc_id))
        n = doc.page_count
        found = []
        io_begin()
        end = time.perf_counter() + SEARCH_SLICE_S
        while s.page < n and s.hits < MAX_SEARCH_HITS:
            try:
                hits = pdf.search_page(doc, s.page, s.needle, s.ignore_space, MAX_SEARCH_HITS - s.hits)
            except Exception:
                hits = []  # 이 쪽 글자를 읽을 수 없음 — 건너뛴다
            if hits:
                found.append((s.page, hits))
                s.hits += len(hits)
            s.page += 1
            if time.perf_counter() >= end:
                break
        io_settle()
        res_conn.send(("search_hits", s.doc_id, s.sid, found, s.page, n))
        if s.page >= n or s.hits >= MAX_SEARCH_HITS:
            status = SEARCH_DONE if s.page >= n else SEARCH_TRUNCATED
            res_conn.send(("search_done", s.doc_id, s.sid, s.hits, status))
            search = None
        pdf.trim_store()

    def render(key) -> None:
        nonlocal seq
        doc_id, lane = key
        scale, q = queues[key]
        item = q.popleft()
        if not q:
            del queues[key]
            order.remove(key)
        doc = docs.get(doc_id)
        if doc is None:
            queues.pop(key, None)
            if key in order:
                order.remove(key)
            return
        res_conn.send(("busy", doc_id))
        seq += 1
        page, rscale, tile = item, scale, None
        io_begin()
        try:
            if isinstance(item, tuple) and len(item) == 3:  # 확대 타일
                page, tx, ty = item
                dk = (doc_id, page)
                dl = dlists.pop(dk, None)
                if dl is None:
                    dl = pdf.display_list(doc, page)
                dlists[dk] = dl
                while len(dlists) > _DLIST_MAX:
                    dlists.popitem(last=False)
                pix = pdf.render_tile(dl, scale, tx, ty, TILE_PX)
                tile = (tx, ty, pix.x, pix.y)
            else:
                if isinstance(item, tuple):  # 미리보기: (쪽, 배율)
                    page, rscale = item
                pix = pdf.render_page(doc, page, rscale)
        except Exception:
            io_settle()
            res_conn.send(("page_error", doc_id, page, rscale, seq, lane))
            return
        io_settle()
        res_conn.send(("page", doc_id, page, rscale, seq, pix.width, pix.height, pix.stride, lane, tile))
        res_conn.send_bytes(pix.samples_mv)
        del pix
        pdf.trim_store()
        sk = (doc_id, item, scale, min(lane, 0))
        sent[sk] = seq
        sent.move_to_end(sk)
        while len(sent) > _SENT_LOG_MAX:
            sent.popitem(last=False)

    while True:
        if backlog:
            if not handle(backlog.popleft()):
                return
            continue
        view_key = next((k for k in order if k[1] >= 0), None)
        thumb_key = next((k for k in order if k[1] < 0), None)
        has_work = view_key is not None or thumb_key is not None or bool(texts) or search is not None
        if not has_work or cmd_conn.poll(0):
            if not handle(cmd_conn.recv()):  # 할 일이 없으면 여기서 기다린다
                return
            continue  # 쌓인 명령을 모두 반영한 뒤 다음 작업

        if view_key is not None:
            render(view_key)
            continue

        if texts:
            op, doc_id, page, req_id = texts.popleft()
            doc = docs.get(doc_id)
            out = None
            if doc is not None:
                res_conn.send(("busy", doc_id))
                io_begin()
                try:
                    out = pdf.page_text(doc, page) if op == "text" else pdf.page_chars(doc, page)
                except Exception:
                    out = None
                io_settle()
            res_conn.send((op, doc_id, page, req_id, out))
            continue

        if search is not None and (search_turn or thumb_key is None):
            search_turn = False
            search_slice()
            continue
        search_turn = True
        render(thumb_key)


# --------------------------------------------------------------------------- 이벤트


@dataclass
class Opened:
    doc_id: int
    status: str
    reason: str
    repaired: bool
    page_sizes: list | None
    sha256: str | None = None  # (O4) 메모리로 읽어 연 경우 그 읽기에서 계산
    stat: tuple | None = None  # 그때의 (크기, mtime_ns)
    embedded_annots: int = 0  # 원본에 이미 든 주석 수(표시만)
    embedded_notes: list | None = None  # 그중 메모 글이 든 것(읽기만)
    reopen: bool = False  # 작업자 재기동 뒤 다시 연 것


@dataclass
class Loading:
    doc_id: int
    done: int
    total: int


@dataclass
class PageImage:
    doc_id: int
    page: int
    scale: float
    seq: int
    width: int
    height: int
    stride: int
    data: bytes  # RGB888
    lane: int = 0  # THUMB_LANE 이면 썸네일
    tile: tuple | None = None  # (tx, ty, x, y) — 확대 타일이면 쪽 전체 그림 안의 위치(장치 px)


@dataclass
class PageError:
    doc_id: int
    page: int
    scale: float
    seq: int
    lane: int = 0


@dataclass
class PageText:
    doc_id: int
    page: int
    req_id: int
    text: str | None


@dataclass
class PageChars:
    doc_id: int
    page: int
    req_id: int
    chars: list | None  # [(글자, x0, y0, x1, y1, 줄 번호)] 회전 반영 쪽 좌표, 읽을 수 없으면 None


@dataclass
class ExportDone:
    doc_id: int
    req_id: int
    result: dict  # status: ok / changed / refused / error / interrupted, 수: added·before·after·by_type


@dataclass
class Outline:
    doc_id: int
    items: list  # [(수준, 제목, 쪽 0부터 | -1)]


@dataclass
class SearchHit:
    page: int
    rects: list  # [(x0, y0, x1, y1)] pt, 회전 반영 쪽 좌표, 줄마다 하나
    before: str
    match: str
    after: str


@dataclass
class SearchResults:
    """검색 중간 결과 — 한 조각(여러 쪽)에서 찾은 것. 진행률도 같이 온다."""
    doc_id: int
    search_id: int
    hits: list  # [SearchHit]
    pages_done: int
    page_count: int


@dataclass
class SearchDone:
    doc_id: int
    search_id: int
    hit_count: int
    status: str  # SEARCH_DONE / SEARCH_TRUNCATED / SEARCH_CANCELLED / SEARCH_INTERRUPTED


@dataclass
class WorkerRestarted:
    culprit_doc: int | None  # 급사 당시 작업 중이던 문서 (없으면 None)
    pid: int


@dataclass
class WorkerFailed:
    message: str


# --------------------------------------------------------------------------- 클라이언트


class EngineClient:
    """UI 프로세스 쪽. 이벤트 콜백은 **수신 스레드**에서 불린다 — UI 는 신호로 넘겨 받는다."""

    RESTART_LIMIT = 3  # RESTART_WINDOW_S 안에 이만큼 죽으면 포기
    RESTART_WINDOW_S = 30.0

    def __init__(self, on_event: Callable[[object], None]):
        self._on_event = on_event
        self._lock = threading.RLock()
        self._cmd = None
        self._proc = None
        self._gen = 0
        self._busy_doc: int | None = None
        self._docs: dict[int, tuple[str, str | None]] = {}  # 열린 문서 (재기동 시 다시 연다)
        self._opening: dict[int, tuple[str, str | None]] = {}
        self._reopening: set[int] = set()
        self._restarts: deque = deque()
        self._closing = False
        self._failed = False
        self._req_ids = itertools.count(1)
        self._search_ids = itertools.count(1)
        self._search: tuple[int, int] | None = None  # 진행 중 (doc_id, search_id)
        self._exports: dict[int, int] = {}  # 진행 중 내보내기 req_id -> doc_id

    # ---- 수명
    def start(self) -> None:
        with self._lock:
            self._spawn()

    @property
    def pid(self) -> int | None:
        p = self._proc
        return p.pid if p is not None else None

    def is_alive(self) -> bool:
        p = self._proc
        return p is not None and p.is_alive()

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._lock:
            self._closing = True
            proc, cmd = self._proc, self._cmd
            self._send(("quit",))
        if proc is not None:
            proc.join(timeout)
            if proc.is_alive():
                proc.terminate()
                proc.join(1.0)
        if cmd is not None:
            try:
                cmd.close()
            except OSError:
                pass

    # ---- 명령
    def open(self, doc_id: int, path: str, password: str | None = None) -> None:
        with self._lock:
            self._opening[doc_id] = (path, password)
            self._send(("open", doc_id, path, password))

    def close_doc(self, doc_id: int) -> None:
        with self._lock:
            self._docs.pop(doc_id, None)
            self._opening.pop(doc_id, None)
            self._reopening.discard(doc_id)
            self._send(("close", doc_id))

    def want(self, doc_id: int, scale: float, pages: list, ack: int, lane: int = 0) -> None:
        """pages: 쪽 번호 | (쪽, tx, ty) 타일 | (쪽, 배율) 미리보기 — 모듈 설명 참고."""
        self._send(("want", doc_id, scale, list(pages), ack, lane))

    def want_thumbs(self, doc_id: int, scale: float, pages: list[int], ack: int) -> None:
        """썸네일 — 보기 렌더가 없을 때만 그린다."""
        self.want(doc_id, scale, pages, ack, THUMB_LANE)

    def request_text(self, doc_id: int, page: int) -> int:
        req_id = next(self._req_ids)
        self._send(("text", doc_id, page, req_id))
        return req_id

    def request_chars(self, doc_id: int, page: int) -> int:
        """그 쪽의 글자 상자 — 결과는 PageChars."""
        req_id = next(self._req_ids)
        self._send(("chars", doc_id, page, req_id))
        return req_id

    def export_annotated(self, doc_id: int, out_path: str, expect_sha256: str | None, items: list) -> int:
        """메모 포함 PDF 를 out_path 에 새로 만든다 — 결과는 ExportDone. 경로·내용은 기록하지 않는다."""
        req_id = next(self._req_ids)
        with self._lock:
            self._exports[req_id] = doc_id
            self._send(("export", doc_id, req_id, out_path, expect_sha256, items))
        return req_id

    def request_outline(self, doc_id: int) -> None:
        self._send(("outline", doc_id))

    def search(self, doc_id: int, query: str, ignore_space: bool = False) -> int:
        """검색 시작 — 진행 중인 검색(어느 문서든)은 버려진다. 검색어는 기록하지 않는다."""
        sid = next(self._search_ids)
        with self._lock:
            self._search = (doc_id, sid)
            self._send(("search", doc_id, sid, normalize_query(query, ignore_space), ignore_space))
        return sid

    def cancel_search(self, search_id: int) -> None:
        self._send(("search_cancel", search_id))

    def _debug_crash(self, doc_id: int) -> None:
        """시험용: 작업자가 doc_id 작업 중에 급사한 것처럼 만든다."""
        self._send(("_crash", doc_id))

    # ---- 내부
    def _send(self, msg) -> None:
        with self._lock:
            if self._cmd is None or self._failed:
                return
            try:
                self._cmd.send(msg)
            except (OSError, ValueError):
                pass  # 작업자 급사 — 수신 스레드가 처리한다

    def _spawn(self) -> None:
        ctx = multiprocessing.get_context("spawn")
        cmd_r, cmd_w = ctx.Pipe(duplex=False)
        res_r, res_w = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=worker_main, args=(cmd_r, res_w), daemon=True, name="bom-pdf-render")
        proc.start()
        cmd_r.close()
        res_w.close()
        self._gen += 1
        self._cmd, self._proc = cmd_w, proc
        self._busy_doc = None
        threading.Thread(target=self._read_loop, args=(res_r, self._gen), daemon=True,
                         name=f"bom-pdf-render-reader-{self._gen}").start()
        log.info("render worker started pid=%s", proc.pid)

    def _read_loop(self, conn, gen: int) -> None:
        try:
            while True:
                msg = conn.recv()
                kind = msg[0]
                if kind == "busy":
                    self._busy_doc = msg[1]
                    continue
                self._busy_doc = None
                if kind == "page":
                    _, doc_id, page, scale, seq, w, h, stride, lane, tile = msg
                    data = conn.recv_bytes()
                    self._emit(PageImage(doc_id, page, scale, seq, w, h, stride, data, lane, tile))
                elif kind == "page_error":
                    self._emit(PageError(*msg[1:]))
                elif kind == "search_hits":
                    _, doc_id, sid, found, done, total = msg
                    hits = [SearchHit(page, rects, b, m, a) for page, hs in found for rects, b, m, a in hs]
                    self._emit(SearchResults(doc_id, sid, hits, done, total))
                elif kind == "search_done":
                    with self._lock:
                        if self._search is not None and self._search[1] == msg[2]:
                            self._search = None
                    self._emit(SearchDone(*msg[1:]))
                elif kind == "text":
                    self._emit(PageText(*msg[1:]))
                elif kind == "chars":
                    self._emit(PageChars(*msg[1:]))
                elif kind == "exported":
                    with self._lock:
                        self._exports.pop(msg[2], None)
                    self._emit(ExportDone(*msg[1:]))
                elif kind == "outline":
                    self._emit(Outline(*msg[1:]))
                elif kind == "opened":
                    self._on_opened(Opened(*msg[1:]))
                elif kind == "loading":
                    self._busy_doc = msg[1]
                    with self._lock:
                        wanted = msg[1] in self._opening
                    if wanted:
                        self._emit(Loading(*msg[1:]))
        except (EOFError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        self._on_dead(gen)

    def _on_opened(self, ev: Opened) -> None:
        with self._lock:
            info = self._opening.pop(ev.doc_id, None)
            if ev.doc_id in self._reopening:
                self._reopening.discard(ev.doc_id)
                ev.reopen = True
                if ev.status != "ok":
                    self._docs.pop(ev.doc_id, None)
            if ev.status == "ok" and info is not None:
                self._docs[ev.doc_id] = info
            elif info is None and not ev.reopen:
                return  # 그사이 닫힌 문서
        self._emit(ev)

    def _emit(self, ev) -> None:
        try:
            self._on_event(ev)
        except Exception:
            log.exception("engine event handler failed")

    def _on_dead(self, gen: int) -> None:
        with self._lock:
            if gen != self._gen or self._closing:
                return
            culprit = self._busy_doc
            proc = self._proc
            code = proc.exitcode if proc is not None else None
            if proc is not None:
                proc.join(0.5)
                code = proc.exitcode
            log.error("render worker died pid=%s exit=%s culprit_doc=%s",
                      proc.pid if proc else None, code, culprit)
            if culprit is not None:
                self._docs.pop(culprit, None)
                self._opening.pop(culprit, None)
            lost_search, self._search = self._search, None  # 새 작업자는 검색을 이어 가지 않는다
            lost_exports, self._exports = self._exports, {}
            now = time.monotonic()
            while self._restarts and now - self._restarts[0] > self.RESTART_WINDOW_S:
                self._restarts.popleft()
            if len(self._restarts) >= self.RESTART_LIMIT:
                self._failed = True
                self._cmd = None
                fail = WorkerFailed("PDF 엔진이 계속 멈춰 다시 시작하지 않았습니다. 프로그램을 다시 실행해 주세요.")
            else:
                fail = None
                self._restarts.append(now)
                self._spawn()
                self._reopening = set(self._docs)
                for doc_id, (path, pw) in self._docs.items():
                    self._send(("open", doc_id, path, pw))
                for doc_id, (path, pw) in self._opening.items():
                    self._send(("open", doc_id, path, pw))
            pid = self.pid or 0
        if lost_search is not None:
            self._emit(SearchDone(lost_search[0], lost_search[1], -1, SEARCH_INTERRUPTED))
        for req_id, doc_id in lost_exports.items():
            self._emit(ExportDone(doc_id, req_id, {"status": EXPORT_INTERRUPTED}))
        if fail is not None:
            log.error("render worker restart limit reached")
            self._emit(fail)
        else:
            self._emit(WorkerRestarted(culprit, pid))
