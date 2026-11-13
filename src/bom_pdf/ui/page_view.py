"""연속 스크롤 PageView.

- 보이는 쪽 ±2 쪽만 렌더를 요청한다. 해상도 = 화면 배율(96 논리 DPI × 확대율) × devicePixelRatio.
- 확대(O2): 한 쪽이 TILE_MIN_PX 장치 화소를 넘으면 쪽 전체 대신 보이는 부분 + 여유를 TILE_PX 타일로 그린다
  (화질은 같다 — 작업자가 같은 배율로 잘라 그린다). 타일이 오는 동안은 낮은 해상도 미리보기(PREVIEW_PX)나
  다른 배율 그림을 늘려 보인다. 보이는 타일과 그 대체 그림은 캐시에 고정한다.
- 메모리(O3): 사용 가능 메모리가 적으면(sysinfo.memory_low) 미리 그리기 없이 보이는 것만 요청한다.
- 요청은 바뀔 때마다 그 문서의 대기열을 통째로 바꾼다(지나간 쪽은 작업자가 버린다).
- 확대·축소 직후에는 다른 배율로 그려 둔 이미지를 늘려 임시로 보여 주고 새로 그린 것이 오면 바꾼다.
- 이 위젯은 PyMuPDF 를 모른다. 쪽 크기는 서비스의 Document 에서 받는다.
- 보기마다 렌더 통로(lane)가 따로 있어 분할 창에서 같은 문서를 두 보기가 봐도 요청이 서로를 지우지 않는다.
  같은 문서의 렌더 결과는 그 문서의 모든 보기가 받는다(캐시 공유, ack 공유).
- 검색 강조: 검색 패널이 가진 {쪽: [SearchHit]} 를 참조로 받아 보이는 쪽에만 칠한다.
- 주석(1-3): 사이드카 저장소(AnnotationStore)를 참조로 받아 보이는 쪽에 그린다. 원본 PDF 에 이미 있던 주석은
  엔진이 쪽 그림에 그려 주므로 따로 다루지 않는다(표시만). 도구: 선택(글자 선택·주석 고르기)·형광펜·밑줄·메모·사각형.
  보기는 저장소를 직접 바꾸지 않고 createRequested 등 신호만 낸다 — 색·메모 입력·되돌리기는 주 창이 맡는다.
- 글자 선택은 작업자가 준 글자 상자(TextLayer)로 한다. 선택한 글자는 기록하지 않는다.
"""
from __future__ import annotations

import bisect
import itertools
import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QMenu

from bom_pdf import sysinfo
from bom_pdf.services import annotations as an
from bom_pdf.services.document import TILE_PX, Document, DocumentService
from bom_pdf.ui.page_cache import PageCache

LOGICAL_DPI = 96.0
GAP = 10  # 배율을 직접 정했을 때 맨 위·쪽 사이·맨 아래 (논리 px)
FIT_GAP = 4  # 폭·쪽 맞춤의 쪽 사이 — 맨 위·맨 아래는 0(맞춘 쪽이 화면을 꽉 채움), 쪽 경계는 알아볼 만큼만
MARGIN = 12  # 배율을 직접 정했을 때 쪽 양옆 배경 — 폭·쪽 맞춤에서는 0(맞춘 쪽 옆에 회색 띠를 남기지 않음)
PREFETCH = 2  # 보이는 쪽 앞뒤로 미리 그릴 쪽 수
TILE_MIN_PX = 24_000_000  # 쪽 하나가 이 장치 화소(RGB32 ≈ 92MB)를 넘으면 보이는 부분만 타일로
TILE_MARGIN_PX = TILE_PX // 2  # 보이는 부분 둘레로 미리 그릴 여유(장치 px)
PREVIEW_PX = 4_000_000  # 타일이 오는 동안 늘려 보일 쪽 전체 미리보기 화소(≈16MB)
ZOOM_MIN, ZOOM_MAX = 0.1, 8.0
ZOOM_STEPS = [0.1, 0.25, 0.33, 0.5, 0.67, 0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0, 4.0, 6.0, 8.0]

FIT_NONE, FIT_WIDTH, FIT_PAGE = "none", "width", "page"

BG = QColor(0x5f, 0x63, 0x68)
PAGE_BORDER = QColor(0, 0, 0, 70)
PLACEHOLDER = QColor(0xfa, 0xfa, 0xfa)
ERROR_FILL = QColor(0xf3, 0xe5, 0xe5)
HIT_FILL = QColor(255, 214, 0, 90)
CUR_HIT_FILL = QColor(255, 120, 0, 110)
CUR_HIT_PEN = QColor(230, 90, 0)
SEL_FILL = QColor(40, 110, 230, 70)
SEL_ANNOT_PEN = QColor(20, 90, 220)

TOOL_SELECT, TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_NOTE, TOOL_BOX = "select", an.HIGHLIGHT, an.UNDERLINE, an.NOTE, an.BOX
DRAG_MIN_PX = 3  # 이보다 적게 움직이면 클릭으로 본다


def _qcolor(name: str, alpha: int = 255) -> QColor:
    r, g, b = an.color_rgb(name)
    return QColor(round(r * 255), round(g * 255), round(b * 255), alpha)

_lanes = itertools.count(1)


class PageView(QAbstractScrollArea):
    pageChanged = Signal(int, int)  # 현재 쪽(0부터), 전체 쪽 수
    zoomChanged = Signal(float)
    activated = Signal()  # 포커스를 받음 — 분할 창에서 단축키·사이드바가 따라갈 보기
    createRequested = Signal(dict)  # 새 주석 자료(type·page·quads/rect·text) — 색·메모는 주 창이 채운다
    annotationClicked = Signal(str)  # 고른 주석 id ('' = 고른 것 없음)
    annotationEditRequested = Signal(str)
    annotationDeleteRequested = Signal(str)

    def __init__(self, service: DocumentService, cache: PageCache, doc: Document, parent=None):
        super().__init__(parent)
        self.service, self.cache, self.doc = service, cache, doc
        self._zoom = 1.0
        self._fit = FIT_WIDTH
        self._tops: list[int] = []
        self._rects: list[tuple[int, int, int, int]] = []  # x, y, w, h (논리 px, 내용 좌표)
        self._content_w = self._content_h = 0
        self._layout_vw = 0
        self._ack = 0
        self._last_request = None
        self._errors: set[int] = set()
        self._dead = False
        self._current = -1
        self.lane = next(_lanes)
        self._hits: dict | None = None  # {쪽: [SearchHit]} — 검색 패널 소유, 읽기만
        self._cur_hit = None
        # 주석·선택 (1-3)
        self.store: an.AnnotationStore | None = None  # 주 창이 sha256 을 안 뒤 넣는다
        self.text_layer = None  # ui.text_layer.TextLayer (주 창이 넣는다)
        self.tool = TOOL_SELECT
        self.selected_id: str | None = None
        self._drag: dict | None = None  # {"kind": "text"|"box", "page", "a": (x, y) pt, "b", "moved", "hit", "press"}
        self._sel: tuple[int, list, str] | None = None  # (쪽, 줄별 사각형 pt, 글자)
        self.setFrameShape(QAbstractScrollArea.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setAutoFillBackground(False)
        self.verticalScrollBar().setSingleStep(48)
        self.horizontalScrollBar().setSingleStep(48)
        self._relayout(keep_anchor=False)

    # ------------------------------------------------------------------ 배율·배치
    @property
    def zoom(self) -> float:
        return self._zoom

    @property
    def fit_mode(self) -> str:
        return self._fit

    def _dpr(self) -> float:
        return float(self.devicePixelRatioF())

    def render_scale(self) -> float:
        """PDF pt → 장치 픽셀 배율 (작업자에게 보내는 값)."""
        return round(self._zoom * LOGICAL_DPI / 72.0 * self._dpr(), 4)

    def _px(self) -> float:
        return self._zoom * LOGICAL_DPI / 72.0

    def _side(self) -> int:
        return MARGIN if self._fit == FIT_NONE else 0

    def _top(self) -> int:
        """맨 위(·맨 아래) 빈 공간 — go_to_page 는 쪽 위로 이만큼 남긴다."""
        return GAP if self._fit == FIT_NONE else 0

    def _gap(self) -> int:
        return GAP if self._fit == FIT_NONE else FIT_GAP

    def _fit_zoom(self) -> float | None:
        if self._fit == FIT_NONE or not self.doc.page_sizes:
            return None
        vw = max(50, self.viewport().width() - 2 * self._side())
        vh = max(50, self.viewport().height() - 2 * self._top())
        cur = max(0, self._current)
        pw, ph = self.doc.page_sizes[cur]
        if self._fit == FIT_WIDTH:  # 보는 쪽 기준 — 가장 넓은 쪽 기준이면 큰 쪽 하나가 섞인 문서에서 나머지가 작아진다
            z = vw / (pw * LOGICAL_DPI / 72.0)
        else:
            z = min(vw / (pw * LOGICAL_DPI / 72.0), vh / (ph * LOGICAL_DPI / 72.0))
        return max(ZOOM_MIN, min(ZOOM_MAX, z))

    def _relayout(self, keep_anchor: bool = True) -> None:
        anchor = self._anchor() if keep_anchor and self._tops else None
        fx = self._anchor_x(anchor[0]) if anchor is not None and self._fit == FIT_NONE else 0.5  # 맞춤은 늘 가운데
        fz = self._fit_zoom()
        if fz is not None:
            self._zoom = fz
        k = self._px()
        widths = [max(1, round(w * k)) for w, _ in self.doc.page_sizes]
        side = self._side()
        self._content_w = (max(widths) if widths else 0) + 2 * side
        vw = self._layout_vw = self.viewport().width()
        top, gap = self._top(), self._gap()
        y = top
        self._tops, self._rects = [], []
        for (w, h), pw in zip(self.doc.page_sizes, widths):
            ph = max(1, round(h * k))
            x = max(side, (max(vw, self._content_w) - pw) // 2)
            self._tops.append(y)
            self._rects.append((x, y, pw, ph))
            y += ph + gap
        self._content_h = y - gap + top  # 맨 아래 빈 공간 = 맨 위와 같게
        vs, hs = self.verticalScrollBar(), self.horizontalScrollBar()
        vs.setRange(0, max(0, self._content_h - self.viewport().height()))
        vs.setPageStep(max(1, self.viewport().height() - 40))
        hs.setRange(0, max(0, self._content_w - vw))
        hs.setPageStep(max(1, vw))
        if anchor is not None:
            page, frac = anchor
            x, y0, w, h = self._rects[page]
            vs.setValue(int(y0 + frac * h - self.viewport().height() / 2))
            hs.setValue(round(x + fx * w - vw / 2))  # 쪽 크기가 섞인 문서: 보던 쪽이 옆으로 밀리지 않게
        else:
            hs.setValue((self._content_w - vw) // 2)  # 쪽은 모두 내용 폭 가운데에 있다
        self.viewport().update()
        self.zoomChanged.emit(self._zoom)

    def _anchor(self) -> tuple[int, float]:
        """화면 가운데의 (쪽, 쪽 안 상대 위치)."""
        cy = self.verticalScrollBar().value() + self.viewport().height() / 2
        i = max(0, bisect.bisect_right(self._tops, cy) - 1)
        x, y, w, h = self._rects[i]
        return i, min(1.0, max(0.0, (cy - y) / h))

    def _anchor_x(self, page: int) -> float:
        """화면 가로 가운데가 그 쪽 폭의 어디인지(0~1) — 마지막으로 배치한 화면 폭 기준."""
        x, y, w, h = self._rects[page]
        cx = self.horizontalScrollBar().value() + self._layout_vw / 2
        return min(1.0, max(0.0, (cx - x) / w))

    def set_zoom(self, z: float) -> None:
        self._fit = FIT_NONE
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if abs(z - self._zoom) > 1e-4:
            self._zoom = z
            self._relayout()

    def set_fit(self, mode: str) -> None:
        """맞춘 뒤 보던 쪽의 맨 위로 — 화면 가운데 기준으로 두면 위아래 쪽이 걸쳐 보인다."""
        page = self._anchor()[0] if self._tops else None
        self._fit = mode
        self._relayout(keep_anchor=page is not None)
        if page is not None:
            self.go_to_page(page)

    def zoom_in(self) -> None:
        self.set_zoom(next((s for s in ZOOM_STEPS if s > self._zoom + 1e-3), ZOOM_MAX))

    def zoom_out(self) -> None:
        self.set_zoom(next((s for s in reversed(ZOOM_STEPS) if s < self._zoom - 1e-3), ZOOM_MIN))

    # ------------------------------------------------------------------ 이동
    def current_page(self) -> int:
        return max(0, self._current)

    def visible_pages(self) -> range:
        if not self._tops:
            return range(0)
        top = self.verticalScrollBar().value()
        bottom = top + self.viewport().height()
        first = max(0, bisect.bisect_right(self._tops, top) - 1)
        if self._tops[first] + self._rects[first][3] <= top:  # 쪽 사이 틈에 걸림(아랫변이 딱 맞닿은 쪽도 안 보임)
            first = min(first + 1, len(self._tops) - 1)
        last = max(first, bisect.bisect_left(self._tops, bottom) - 1)
        return range(first, min(last, len(self._tops) - 1) + 1)

    def go_to_page(self, page: int) -> None:
        if self._tops:
            page = max(0, min(page, len(self._tops) - 1))
            self.verticalScrollBar().setValue(self._tops[page] - self._top())

    def content_height(self) -> int:
        return self._content_h

    # ------------------------------------------------------------------ 확대 타일 (O2)
    def _dev_size(self, p: int) -> tuple[float, float]:
        """그 쪽을 지금 배율로 그린 장치 px 크기(작업자 그림과 ±1)."""
        pw, ph = self.doc.page_sizes[p]
        s = self.render_scale()
        return pw * s, ph * s

    def is_tiled(self, p: int) -> bool:
        w, h = self._dev_size(p)
        return w * h > TILE_MIN_PX

    def _visible_part(self, p: int) -> QRectF | None:
        """쪽 p 의 보이는 부분 — 쪽 안 논리 좌표."""
        x, y, w, h = self._rects[p]
        vp = self.viewport()
        r = QRectF(self.horizontalScrollBar().value() - x, self.verticalScrollBar().value() - y,
                   vp.width(), vp.height()).intersected(QRectF(0, 0, w, h))
        return r if not r.isEmpty() else None

    def _tiles(self, p: int, margin: int = 0) -> list[tuple[int, int]]:
        """보이는 부분(+ margin 장치 px)을 덮는 타일 (tx, ty) — 화면 가운데에 가까운 것부터."""
        part = self._visible_part(p)
        if part is None:
            return []
        dpr = self._dpr()
        w, h = self._dev_size(p)
        nx, ny = max(1, math.ceil(w / TILE_PX - 1e-6)), max(1, math.ceil(h / TILE_PX - 1e-6))
        x0, y0 = part.left() * dpr - margin, part.top() * dpr - margin
        x1, y1 = part.right() * dpr + margin, part.bottom() * dpr + margin
        tx0, ty0 = max(0, int(x0 // TILE_PX)), max(0, int(y0 // TILE_PX))
        tx1, ty1 = min(nx - 1, int((x1 - 1) // TILE_PX)), min(ny - 1, int((y1 - 1) // TILE_PX))
        cx, cy = part.center().x() * dpr, part.center().y() * dpr
        tiles = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]
        tiles.sort(key=lambda t: ((t[0] + .5) * TILE_PX - cx) ** 2 + ((t[1] + .5) * TILE_PX - cy) ** 2)
        return tiles

    def _preview_scale(self, p: int) -> float:
        pw, ph = self.doc.page_sizes[p]
        return round(min(self.render_scale(), math.sqrt(PREVIEW_PX / max(1.0, pw * ph))), 3)

    def _fallback_item(self, p: int) -> tuple[int, float]:
        """타일이 오는 동안 늘려 보일 쪽 전체 그림 항목 (쪽, 배율) — 이미 쓸 만한 그림이 있으면 그것(요청 안 됨, 고정만)."""
        key = self.cache.find_any_key(self.doc.doc_id, p)
        ps = self._preview_scale(p)
        return (p, key[2]) if key is not None and key[2] >= 0.75 * ps else (p, ps)

    def _item_key(self, item, scale: float) -> tuple:
        """요청 항목 → 캐시 키 (doc, page, scale, tile)."""
        did = self.doc.doc_id
        if isinstance(item, int):
            return (did, item, scale, None)
        if len(item) == 3:
            return (did, item[0], scale, (item[1], item[2]))
        return (did, item[0], item[1], None)

    def _pin_keys(self) -> list[tuple]:
        """지금 보이는 그림의 캐시 키 — 타일 쪽은 보이는 타일 + 그동안 보일 대체 그림."""
        did, scale = self.doc.doc_id, self.render_scale()
        keys = []
        for p in self.visible_pages():
            if self.is_tiled(p):
                keys.extend((did, p, scale, t) for t in self._tiles(p))
                fb = self.cache.find_any_key(did, p)
                if fb is not None:
                    keys.append(fb)
            else:
                keys.append((did, p, scale, None))
        return keys

    def _tiled_view_bytes(self, tiled: list[int]) -> int:
        """확대 쪽들을 보일 때 고정될 바이트의 위쪽 어림 — 스크롤 위치와 무관하게 같은 값이어야
        미리 그릴 몫이 스크롤마다 들쭉날쭉하지 않는다(그러면 넣었다 밀어냈다를 되풀이한다).
        보이는 타일 최대 수(창 크기 기준, 쪽 경계가 걸리면 한 줄 더) + 쪽마다 대체 그림."""
        dpr, vp = self._dpr(), self.viewport()
        cols = math.ceil(vp.width() * dpr / TILE_PX) + 1
        rows = math.ceil(vp.height() * dpr / TILE_PX) + len(tiled)
        total = cols * rows * TILE_PX * TILE_PX * 4
        for p in tiled:
            img = self.cache.peek(self._item_key(self._fallback_item(p), self.render_scale()))
            total += img.sizeInBytes() if img is not None else PREVIEW_PX * 4
        return total

    def coverage(self) -> tuple[float, float]:
        """보이는 쪽 면적 중 (지금 배율 그림으로 덮인 몫, 아무 그림도 없는 몫) — 측정·시험용."""
        did, scale, dpr = self.doc.doc_id, self.render_scale(), self._dpr()
        total = sharp = blank = 0.0
        for p in self.visible_pages():
            part = self._visible_part(p)
            if part is None:
                continue
            a = part.width() * part.height()
            total += a
            if p in self._errors:
                sharp += a
                continue
            if self.is_tiled(p):
                imgs = [self.cache.peek((did, p, scale, t)) for t in self._tiles(p)]
                if all(img is not None for img in imgs):
                    cov = a
                else:
                    cov = 0.0
                    for img in imgs:
                        if img is not None:
                            o = img.offset()
                            r = QRectF(o.x() / dpr, o.y() / dpr, img.width() / dpr, img.height() / dpr)
                            r = r.intersected(part)
                            cov += r.width() * r.height()
                    cov = min(cov, a * 0.999)
                sharp += cov
                if self.cache.find_any_key(did, p) is None:
                    blank += a - cov
            elif self.cache.contains(did, p, scale):
                sharp += a
            elif self.cache.find_any_key(did, p) is None:
                blank += a
        return (sharp / total, blank / total) if total else (1.0, 0.0)

    # ------------------------------------------------------------------ 렌더 요청·결과
    def _wanted(self) -> list:
        """요청 항목(앞쪽이 먼저): 쪽 번호 | (쪽, tx, ty) 타일 | (쪽, 배율) 미리보기."""
        vis = self.visible_pages()
        if not vis:
            return []
        n = len(self._tops)
        scale = self.render_scale()
        tiled = [p for p in vis if self.is_tiled(p)]
        order: list = [self._fallback_item(p) for p in tiled]
        for p in vis:
            if p in tiled:
                order.extend((p, tx, ty) for tx, ty in self._tiles(p))
            else:
                order.append(p)
        if sysinfo.memory_low():  # 메모리가 모자라면 보이는 것만
            return order
        if tiled:  # 확대 중: 보이는 부분 둘레 타일 + 앞뒤 쪽 미리보기 — 고정한 것 뒤 캐시에 남는 만큼만
            room = self.cache.max_bytes - self._tiled_view_bytes(tiled)
            for p in (vis.stop, vis.start - 1):  # 다음·앞 쪽으로 넘어갈 때 빈 칸 대신 보일 미리보기
                if 0 <= p < n and self.is_tiled(p):
                    item = self._fallback_item(p)
                    img = self.cache.peek(self._item_key(item, scale))
                    size = img.sizeInBytes() if img is not None else PREVIEW_PX * 4
                    if room >= size:
                        order.append(item)
                        room -= size
            tile_bytes = TILE_PX * TILE_PX * 4
            for p in tiled:
                near = set(self._tiles(p))
                for tx, ty in self._tiles(p, TILE_MARGIN_PX):
                    if (tx, ty) not in near and room >= tile_bytes:
                        order.append((p, tx, ty))
                        room -= tile_bytes
            return order
        # 미리 그릴 쪽은 캐시에 들어가는 만큼만 — 넘치면 서로 밀어내며 끝없이 다시 그린다(큰 화면)
        x, y, w, h = self._rects[vis.start]
        dpr = self._dpr()
        one = max(1, int(w * dpr) * int(h * dpr) * 4)
        room = max(0, self.cache.max_bytes // one - len(vis))
        after = list(range(vis.stop, min(n, vis.stop + PREFETCH)))
        before = list(range(max(0, vis.start - PREFETCH), vis.start))[::-1]
        for a, b in zip(after + [None] * len(before), before + [None] * len(after)):
            for p in (a, b):
                if p is not None and room > 0:
                    order.append(p)
                    room -= 1
        return order

    def _update_requests(self, force: bool = False) -> None:
        if self._dead:
            return
        scale = self.render_scale()
        did = self.doc.doc_id
        wanted = self._wanted()
        # 보이는 것 + 지금 요청하는 것(미리 그릴 것)을 고정 — 개수는 _wanted 가 캐시 상한 안으로 맞춘다.
        # 고정하지 않으면 아직 안 보인 미리 그린 것이 LRU 에서 가장 오래되어 먼저 밀려나고 다시 요청되기를 되풀이한다.
        pins = self._pin_keys() + [self._item_key(it, scale) for it in wanted]
        self.cache.set_pinned(self.lane, pins if self.isVisible() else ())
        needed = [it for it in wanted
                  if (it if isinstance(it, int) else it[0]) not in self._errors
                  and not self.cache.has(self._item_key(it, scale))]
        key = (scale, tuple(needed))
        if force or key != self._last_request:
            self._last_request = key
            self.service.request_pages(did, scale, needed, self._ack, self.lane)

    def on_page_ready(self, page: int, scale: float, seq: int, image: QImage, tile=None) -> None:
        """tile: None(쪽 전체) | (tx, ty, x, y) — 확대 타일(image.offset() 이 (x, y))."""
        self._ack = max(self._ack, seq)
        if self._dead:
            return
        if scale == self.render_scale():
            image.setDevicePixelRatio(self._dpr())
        self.cache.put(self.doc.doc_id, page, scale, image, (tile[0], tile[1]) if tile else None)
        if page in self.visible_pages():
            self.viewport().update()
        self._update_requests()

    def on_page_error(self, page: int, seq: int) -> None:
        self._ack = max(self._ack, seq)
        self._errors.add(page)
        self.viewport().update()
        self._update_requests()

    def on_engine_restarted(self) -> None:
        """작업자가 다시 떴다 — 보내 둔 요청이 사라졌으니 다시 보낸다."""
        self._ack = 0
        self._last_request = None
        self._update_requests(force=True)

    def set_dead(self) -> None:
        """엔진이 이 문서에서 멈췄다 — 더 요청하지 않는다. 이미 그린 쪽은 계속 보인다."""
        self._dead = True
        self.viewport().update()

    def release(self) -> None:
        """이 보기를 버린다 — 남은 요청을 비운다. 캐시는 같은 문서의 다른 보기가 쓸 수 있어 두고, 문서를 닫을 때 비운다."""
        if not self._dead:
            self.service.request_pages(self.doc.doc_id, self.render_scale(), [], self._ack, self.lane)
        self._dead = True
        self.cache.set_pinned(self.lane, ())

    # ------------------------------------------------------------------ 검색 강조
    def set_search_hits(self, hits_by_page: dict | None, current=None) -> None:
        self._hits = hits_by_page
        self._cur_hit = current
        self.viewport().update()

    def reveal(self, page: int, rect=None) -> None:
        """쪽(과 그 안의 사각형 pt)이 보이도록 스크롤한다. 이미 보이면 움직이지 않는다."""
        if not self._tops or not 0 <= page < len(self._tops):
            return
        if rect is None:
            self.go_to_page(page)
            return
        x, y, w, h = self._rects[page]
        k = self._px()
        top, bottom = y + rect[1] * k, y + rect[3] * k
        vs, hs = self.verticalScrollBar(), self.horizontalScrollBar()
        vh, vw = self.viewport().height(), self.viewport().width()
        if top < vs.value() or bottom > vs.value() + vh:
            vs.setValue(int(top - vh / 3))
        left, right = x + rect[0] * k, x + rect[2] * k
        if left < hs.value() or right > hs.value() + vw:
            hs.setValue(int((left + right) / 2 - vw / 2))
        self.viewport().update()

    def _paint_hits(self, qp: QPainter, page: int, target: QRectF) -> None:
        k = self._px()
        hits = self._hits.get(page) if self._hits else None
        cur = self._cur_hit if self._cur_hit is not None and self._cur_hit.page == page else None
        if not hits and cur is None:
            return
        qp.save()
        qp.setPen(Qt.PenStyle.NoPen)
        qp.setBrush(QBrush(HIT_FILL))
        for hit in hits or ():
            if hit is cur:
                continue
            for x0, y0, x1, y1 in hit.rects:
                qp.drawRect(QRectF(target.x() + x0 * k, target.y() + y0 * k, (x1 - x0) * k, (y1 - y0) * k))
        if cur is not None:
            qp.setBrush(QBrush(CUR_HIT_FILL))
            qp.setPen(QPen(CUR_HIT_PEN, 1.5))
            for x0, y0, x1, y1 in cur.rects:
                qp.drawRect(QRectF(target.x() + x0 * k - 1, target.y() + y0 * k - 1,
                                   (x1 - x0) * k + 2, (y1 - y0) * k + 2))
        qp.restore()

    # ------------------------------------------------------------------ Qt 이벤트
    def paintEvent(self, ev) -> None:  # noqa: N802
        qp = QPainter(self.viewport())
        qp.fillRect(self.viewport().rect(), BG)
        dx = -self.horizontalScrollBar().value()
        dy = -self.verticalScrollBar().value()
        scale = self.render_scale()
        did = self.doc.doc_id
        qp.setPen(QPen(PAGE_BORDER, 1))
        for p in self.visible_pages():
            x, y, w, h = self._rects[p]
            target = QRectF(x + dx, y + dy, w, h)
            qp.drawRect(target.adjusted(-1, -1, 0, 0))
            if p not in self._errors and self.is_tiled(p):
                self._paint_tiled(qp, p, target, scale)
                self._paint_overlays(qp, p, target)
                continue
            img = self.cache.get(did, p, scale)
            if img is not None:
                qp.drawImage(QPointF(target.x(), target.y()), img)
                self._paint_overlays(qp, p, target)
                continue
            if p in self._errors:
                qp.fillRect(target, ERROR_FILL)
                qp.drawText(target, Qt.AlignmentFlag.AlignCenter, "이 쪽을 그릴 수 없습니다")
                continue
            other = self.cache.find_any(did, p)
            if other is not None:
                qp.drawImage(target, other, QRectF(other.rect()))
                self._paint_overlays(qp, p, target)
            else:
                qp.fillRect(target, PLACEHOLDER)
        qp.end()
        self._update_requests()
        self._emit_page()

    def _paint_tiled(self, qp: QPainter, p: int, target: QRectF, scale: float) -> None:
        """확대 쪽: 보이는 타일을 제자리에. 빠진 타일이 있으면 그 밑에 대체 그림(없으면 빈 칸)을 먼저 깐다."""
        did, dpr = self.doc.doc_id, self._dpr()
        vr = QRectF(self.viewport().rect()).intersected(target)
        if vr.isEmpty():
            return
        tiles = [self.cache.get(did, p, scale, t) for t in self._tiles(p)]
        if any(img is None for img in tiles):
            fb = self.cache.find_any(did, p)
            if fb is not None:  # 보이는 부분만 원본 화소에서 잘라 늘린다
                sx, sy = fb.width() / target.width(), fb.height() / target.height()
                src = QRectF((vr.x() - target.x()) * sx, (vr.y() - target.y()) * sy, vr.width() * sx, vr.height() * sy)
                qp.drawImage(vr, fb, src)
            else:
                qp.fillRect(vr, PLACEHOLDER)
        for img in tiles:
            if img is not None:
                o = img.offset()
                qp.drawImage(QPointF(target.x() + o.x() / dpr, target.y() + o.y() / dpr), img)

    def _emit_page(self) -> None:
        if not self._tops:
            return
        cur, _ = self._anchor()
        vis = self.visible_pages()
        if vis and self.verticalScrollBar().value() <= 0:
            cur = 0
        if cur != self._current:
            self._current = cur
            self.pageChanged.emit(cur, len(self._tops))

    def scrollContentsBy(self, dx: int, dy: int) -> None:  # noqa: N802
        self.viewport().update()

    def hideEvent(self, ev) -> None:  # noqa: N802
        """다른 탭으로 넘어가면 이 보기의 고정을 푼다 — 숨은 탭이 캐시를 붙잡지 않게."""
        self.cache.set_pinned(self.lane, ())
        super().hideEvent(ev)

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        if self._fit == FIT_PAGE and self._tops and self._current >= 0:  # 쪽 맞춤은 창 크기가 바뀌어도 한 쪽만
            page = self._anchor()[0]
            self._relayout(keep_anchor=False)
            self.go_to_page(page)
            return
        self._relayout(keep_anchor=self._current >= 0)

    def wheelEvent(self, ev) -> None:  # noqa: N802
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if ev.angleDelta().y() > 0:
                self.zoom_in()
            elif ev.angleDelta().y() < 0:
                self.zoom_out()
            ev.accept()
            return
        super().wheelEvent(ev)

    def focusInEvent(self, ev) -> None:  # noqa: N802
        super().focusInEvent(ev)
        self.activated.emit()

    def next_page(self) -> None:
        """맨 위에 걸친 쪽의 다음 쪽 맨 위로."""
        vis = self.visible_pages()
        if vis:
            self.go_to_page(vis.start + 1)

    def prev_page(self) -> None:
        """맨 위에 걸친 쪽의 머리가 잘려 있으면 그 쪽 맨 위로, 아니면 앞 쪽으로."""
        vis = self.visible_pages()
        if not vis:
            return
        first = vis.start
        if self._tops[first] - self._top() < self.verticalScrollBar().value() - 2:
            self.go_to_page(first)
        else:
            self.go_to_page(first - 1)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        key = ev.key()
        plain = not (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier))
        if key == Qt.Key.Key_Home:
            self.verticalScrollBar().setValue(0)
        elif key == Qt.Key.Key_End:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())
        elif key == Qt.Key.Key_PageDown and plain:
            self.next_page()
        elif key == Qt.Key.Key_PageUp and plain:
            self.prev_page()
        elif key == Qt.Key.Key_Delete and plain and self.selected_id:
            self.annotationDeleteRequested.emit(self.selected_id)
        elif key == Qt.Key.Key_Escape and plain and (self._sel or self._drag):
            self.clear_selection()
        elif key == Qt.Key.Key_Space and plain:  # 한 화면씩 (Shift = 위로)
            sb = self.verticalScrollBar()
            step = sb.pageStep() * (-1 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1)
            sb.setValue(sb.value() + step)
        else:
            super().keyPressEvent(ev)

    # ------------------------------------------------------------------ 주석 그리기
    def set_annotation_store(self, store) -> None:
        self.store = store
        if store is None or (self.selected_id and store.get(self.selected_id) is None):
            self.selected_id = None
        self.viewport().update()

    def set_tool(self, tool: str) -> None:
        self.tool = tool
        self._drag = None
        if tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE):
            cursor = Qt.CursorShape.IBeamCursor
        elif tool in (TOOL_NOTE, TOOL_BOX):
            cursor = Qt.CursorShape.CrossCursor
        else:
            cursor = Qt.CursorShape.ArrowCursor
        self.viewport().setCursor(cursor)

    def set_selected(self, item_id: str | None) -> None:
        if item_id != self.selected_id:
            self.selected_id = item_id
            self.viewport().update()

    def _paint_overlays(self, qp: QPainter, page: int, target: QRectF) -> None:
        self._paint_annots(qp, page, target)
        self._paint_hits(qp, page, target)
        self._paint_selection(qp, page, target)

    def _paint_annots(self, qp: QPainter, page: int, target: QRectF) -> None:
        if self.store is None:
            return
        items = self.store.on_page(page)
        if not items:
            return
        k = self._px()
        ox, oy = target.x(), target.y()

        def rect(r) -> QRectF:
            return QRectF(ox + r[0] * k, oy + r[1] * k, (r[2] - r[0]) * k, (r[3] - r[1]) * k)

        qp.save()
        qp.setClipRect(target)
        for it in items:
            kind, color = it["type"], it["color"]
            if kind == an.HIGHLIGHT:  # 곱하기 합성 — 글자는 검게 남는다
                qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_Multiply)
                qp.setPen(Qt.PenStyle.NoPen)
                qp.setBrush(QBrush(_qcolor(color, 220)))
                for q in it["quads"]:
                    qp.drawRect(rect(q))
                qp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            elif kind == an.UNDERLINE:
                qp.setPen(QPen(_qcolor(color), max(1.0, 1.3 * k)))
                for q in it["quads"]:
                    r = rect(q)
                    qp.drawLine(QPointF(r.left(), r.bottom()), QPointF(r.right(), r.bottom()))
            elif kind == an.BOX:
                qp.setPen(QPen(_qcolor(color), max(1.0, 1.5 * k)))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawRect(rect(it["rect"]))
            elif kind == an.NOTE:
                r = rect(it["rect"])
                qp.setPen(QPen(QColor(0, 0, 0, 140), 1))
                qp.setBrush(QBrush(_qcolor(color)))
                qp.drawRoundedRect(r, 2, 2)
                qp.setPen(QPen(QColor(0, 0, 0, 150), max(1.0, 0.8 * k)))
                for f in (0.35, 0.55, 0.75):  # 줄 세 개 — 메모 아이콘
                    y = r.top() + r.height() * f
                    qp.drawLine(QPointF(r.left() + r.width() * 0.2, y), QPointF(r.right() - r.width() * 0.2, y))
            if it["id"] == self.selected_id:
                qp.setPen(QPen(SEL_ANNOT_PEN, 1.5, Qt.PenStyle.DashLine))
                qp.setBrush(Qt.BrushStyle.NoBrush)
                qp.drawRect(rect(an.item_bounds(it)).adjusted(-3, -3, 3, 3))
        qp.restore()

    def _paint_selection(self, qp: QPainter, page: int, target: QRectF) -> None:
        k = self._px()
        sel = self._sel if self._sel is not None and self._sel[0] == page else None
        d = self._drag
        box = d if d is not None and d["kind"] == "box" and d["page"] == page and d["moved"] else None
        if sel is None and box is None:
            return
        qp.save()
        if sel is not None:
            qp.setPen(Qt.PenStyle.NoPen)
            qp.setBrush(QBrush(SEL_FILL))
            for x0, y0, x1, y1 in sel[1]:
                qp.drawRect(QRectF(target.x() + x0 * k, target.y() + y0 * k, (x1 - x0) * k, (y1 - y0) * k))
        if box is not None:
            (ax, ay), (bx, by) = box["a"], box["b"]
            qp.setPen(QPen(SEL_ANNOT_PEN, 1, Qt.PenStyle.DashLine))
            qp.setBrush(Qt.BrushStyle.NoBrush)
            qp.drawRect(QRectF(target.x() + min(ax, bx) * k, target.y() + min(ay, by) * k,
                               abs(bx - ax) * k, abs(by - ay) * k))
        qp.restore()

    # ------------------------------------------------------------------ 좌표·찾기
    def page_at(self, pos) -> int | None:
        """화면 위치(viewport 좌표) 아래의 쪽."""
        cx = pos.x() + self.horizontalScrollBar().value()
        cy = pos.y() + self.verticalScrollBar().value()
        for p in self.visible_pages():
            x, y, w, h = self._rects[p]
            if x <= cx <= x + w and y <= cy <= y + h:
                return p
        return None

    def to_page_point(self, pos, page: int) -> tuple[float, float]:
        """화면 위치 → 그 쪽의 pt 좌표(쪽 밖이면 쪽 가장자리로 맞춤)."""
        cx = pos.x() + self.horizontalScrollBar().value()
        cy = pos.y() + self.verticalScrollBar().value()
        x, y, w, h = self._rects[page]
        k = self._px()
        pw, ph = self.doc.page_sizes[page]
        return (min(max((cx - x) / k, 0.0), pw), min(max((cy - y) / k, 0.0), ph))

    def page_point_to_view(self, page: int, x: float, y: float) -> QPointF:
        """쪽 pt → viewport 좌표 (시험용)."""
        rx, ry, w, h = self._rects[page]
        k = self._px()
        return QPointF(rx + x * k - self.horizontalScrollBar().value(),
                       ry + y * k - self.verticalScrollBar().value())

    def annotation_at(self, page: int, x: float, y: float) -> str | None:
        """(x, y) pt 아래의 주석 id — 사각형은 테두리 근처만(안쪽은 글자 선택에 쓴다)."""
        if self.store is None:
            return None
        pad, edge = 2.0, 4.0
        for it in reversed(self.store.on_page(page)):
            if it["type"] == an.BOX:
                x0, y0, x1, y1 = it["rect"]
                inside = x0 - edge <= x <= x1 + edge and y0 - edge <= y <= y1 + edge
                if inside and min(abs(x - x0), abs(x - x1), abs(y - y0), abs(y - y1)) <= edge:
                    return it["id"]
                continue
            rects = it["quads"] if "quads" in it else [it["rect"]]
            if any(r[0] - pad <= x <= r[2] + pad and r[1] - pad <= y <= r[3] + pad for r in rects):
                return it["id"]
        return None

    # ------------------------------------------------------------------ 글자 선택
    def selected_text(self) -> str:
        return self._sel[2] if self._sel else ""

    def selection(self) -> tuple[int, list, str] | None:
        return self._sel

    def clear_selection(self) -> None:
        self._sel = None
        self._drag = None
        self.viewport().update()

    def _update_text_selection(self) -> None:
        d = self._drag
        if d is None or d["kind"] != "text" or not d["moved"] or self.text_layer is None:
            return
        chars = self.text_layer.get(self.doc.doc_id, d["page"])
        if chars is None:
            return  # 요청함 — 도착하면 on_chars 가 다시 부른다
        i = an.char_at(chars, *d["a"])
        j = an.char_at(chars, *d["b"])
        self._sel = None
        if i is not None and j is not None:
            rects, text = an.selection(chars, i, j)
            if rects:
                self._sel = (d["page"], rects, text)
        self.viewport().update()

    def on_chars(self, page: int) -> None:
        """그 쪽의 글자 상자가 도착했다 — 끌던 중이면 선택을 다시 계산하고, 이미 놓았으면 마무리."""
        d = self._drag
        if d is None or d["kind"] != "text" or d["page"] != page:
            return
        self._update_text_selection()
        if d.get("released"):
            self._finish_text_drag()

    def _finish_text_drag(self) -> None:
        d = self._drag
        self._drag = None
        if d is None or not d["moved"]:
            return
        if self.text_layer is not None and self.text_layer.get(self.doc.doc_id, d["page"]) is None:
            d["released"] = True  # 글자 상자를 기다린다
            self._drag = d
            return
        if self.tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE) and self._sel is not None:
            page, rects, text = self._sel
            self._sel = None
            self.createRequested.emit({"type": self.tool, "page": page, "quads": rects, "text": text})
        self.viewport().update()

    # ------------------------------------------------------------------ 마우스
    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self.setFocus(Qt.FocusReason.MouseFocusReason)
        if ev.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(ev)
            return
        pos = ev.position().toPoint()
        page = self.page_at(pos)
        if page is None:
            if self._sel is not None:
                self.clear_selection()
            if self.tool == "select" and self.selected_id:
                self.annotationClicked.emit("")
            return
        pt = self.to_page_point(pos, page)
        hit = self.annotation_at(page, *pt)
        if self.tool == TOOL_NOTE:
            if hit:  # 이미 있는 주석 위 — 새 메모를 겹쳐 만들지 않고 그 주석을 고른다(두 번 누르면 편집)
                self.annotationClicked.emit(hit)
                return
            if self.store is not None:
                pw, ph = self.doc.page_sizes[page]
                x, y = min(pt[0], pw - an.NOTE_SIZE), min(pt[1], ph - an.NOTE_SIZE)
                self.createRequested.emit({"type": an.NOTE, "page": page,
                                           "rect": [x, y, x + an.NOTE_SIZE, y + an.NOTE_SIZE]})
            return
        self._sel = None
        kind = "box" if self.tool == TOOL_BOX else "text"
        self._drag = {"kind": kind, "page": page, "a": pt, "b": pt, "moved": False, "hit": hit, "press": pos}
        if kind == "text" and self.text_layer is not None:
            self.text_layer.get(self.doc.doc_id, page)  # 미리 요청(이미 있으면 그대로)
        self.viewport().update()

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        d = self._drag
        if d is None or d.get("released") or not (ev.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(ev)
            return
        pos = ev.position().toPoint()
        d["b"] = self.to_page_point(pos, d["page"])
        if not d["moved"] and (pos - d["press"]).manhattanLength() >= DRAG_MIN_PX:
            d["moved"] = True
        if d["kind"] == "text":
            self._update_text_selection()
        else:
            self.viewport().update()

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        d = self._drag
        if d is None or ev.button() != Qt.MouseButton.LeftButton or d.get("released"):
            super().mouseReleaseEvent(ev)
            return
        pos = ev.position().toPoint()
        d["b"] = self.to_page_point(pos, d["page"])
        if not d["moved"] and (pos - d["press"]).manhattanLength() >= DRAG_MIN_PX:
            d["moved"] = True
        if d["kind"] == "box" and not d["moved"] and d["hit"]:  # 사각형 도구로 주석을 누르기만 함 — 고르기
            self._drag = None
            self.annotationClicked.emit(d["hit"])
            self.viewport().update()
            return
        if d["kind"] == "box":
            self._drag = None
            (ax, ay), (bx, by) = d["a"], d["b"]
            if d["moved"] and abs(bx - ax) >= 4 and abs(by - ay) >= 4 and self.store is not None:
                self.createRequested.emit({"type": an.BOX, "page": d["page"],
                                           "rect": [min(ax, bx), min(ay, by), max(ax, bx), max(ay, by)]})
            self.viewport().update()
            return
        if not d["moved"]:  # 클릭 — 주석 위면 고르기(어느 도구든), 선택 도구로 빈 곳이면 고른 것 해제
            self._drag = None
            if d["hit"] or self.tool == TOOL_SELECT:
                self.annotationClicked.emit(d["hit"] or "")
            self.viewport().update()
            return
        self._update_text_selection()
        self._finish_text_drag()

    def mouseDoubleClickEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position().toPoint()
        page = self.page_at(pos)
        if page is not None:  # 어느 도구든 기존 주석을 두 번 누르면 편집
            hit = self.annotation_at(page, *self.to_page_point(pos, page))
            if hit:
                self._drag = None
                self.annotationClicked.emit(hit)
                self.annotationEditRequested.emit(hit)
                return
        super().mouseDoubleClickEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        page = self.page_at(ev.pos())
        hit = self.annotation_at(page, *self.to_page_point(ev.pos(), page)) if page is not None else None
        writable = self.store is not None and not self.store.read_only
        menu = QMenu(self)
        if hit:
            self.annotationClicked.emit(hit)
            menu.addAction("메모·태그 편집…", lambda: self.annotationEditRequested.emit(hit)).setEnabled(writable)
            menu.addAction("주석 삭제", lambda: self.annotationDeleteRequested.emit(hit)).setEnabled(writable)
        elif self._sel is not None:
            sel = self._sel
            for kind in (an.HIGHLIGHT, an.UNDERLINE):
                a = menu.addAction(an.TYPE_LABELS[kind], lambda k=kind: self.create_from_selection(k, sel))
                a.setEnabled(writable)
            menu.addAction("복사", self.copy_selection)
        else:
            return
        menu.exec(ev.globalPos())

    def create_from_selection(self, kind: str, sel=None) -> bool:
        sel = sel or self._sel
        if sel is None:
            return False
        page, rects, text = sel
        self._sel = None
        self.createRequested.emit({"type": kind, "page": page, "quads": rects, "text": text})
        self.viewport().update()
        return True

    def copy_selection(self) -> bool:
        if not self._sel:
            return False
        QGuiApplication.clipboard().setText(self._sel[2])
        return True
