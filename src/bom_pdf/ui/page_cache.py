"""렌더된 쪽 이미지 LRU 캐시 — 바이트 상한. 모든 탭이 하나를 같이 쓴다.

지금 화면에 보이는 쪽은 보기마다 `set_pinned` 로 고정해 밀어내지 않는다. 큰 화면(예: 4K·배율 150%)에서는
쪽 하나가 70MB를 넘어 보이는 두 쪽만으로 상한을 넘는데, 고정하지 않으면 두 쪽이 서로를 밀어내고 다시 그려지며
번갈아 깜빡인다. 고정된 쪽 때문에 상한을 잠시 넘는 것은 허용한다(보이는 만큼만).

키는 (doc_id, page, scale, tile) — tile 은 쪽 전체면 None, 확대 타일(O2)이면 (tx, ty).

상한(O3)은 `adaptive_budget` 이 화면 크기와 RAM 등급으로 정한다: 폭 맞춤 한 쪽 × 3(최소 64MB)을 RAM 등급 상한으로 자름.
"""
from __future__ import annotations

import math
from collections import OrderedDict

from PySide6.QtGui import QImage

from bom_pdf import sysinfo

MB = 1024 * 1024
DEFAULT_MAX_BYTES = 128 * MB  # 상한을 주지 않을 때(시험·썸네일 외 기본)
MIN_BYTES = 64 * MB
PAGES_IN_BUDGET = 3  # 보이는 두 쪽 + 미리 그릴 한 쪽
# RAM 등급별 상한 — (총 RAM GB 이하, 상한). 8GB PC 기준을 먼저 정했다(UI+작업자 600MB, D13)
TIER_CAPS = ((8.5, 128 * MB), (16.5, 256 * MB), (math.inf, 384 * MB))
A4_RATIO = 297 / 210


def fit_width_page_bytes(screen_w: float, dpr: float) -> int:
    """그 화면을 다 쓰는 창에서 폭 맞춤 A4 한 쪽(RGB32)의 바이트 — 위쪽 어림(창 테두리·사이드바를 빼지 않음)."""
    w = max(1.0, screen_w * dpr)
    return int(w * w * A4_RATIO * 4)


def tier_cap(ram_gb: float) -> int:
    return next(cap for limit, cap in TIER_CAPS if ram_gb <= limit)


def adaptive_budget(screens: list[tuple[float, float, float]], ram_gb: float) -> int:
    """screens: [(논리 폭, 논리 높이, 배율)]. 가장 큰 화면 기준(창을 옮길 수 있다)."""
    page = max((fit_width_page_bytes(w, dpr) for w, _h, dpr in screens), default=0)
    return min(tier_cap(ram_gb), max(MIN_BYTES, PAGES_IN_BUDGET * page))


def real_screens() -> list[tuple[float, float, float]]:
    """연결된 화면 [(논리 폭, 논리 높이, 배율)] — QApplication 이 있어야 한다."""
    from PySide6.QtGui import QGuiApplication

    return [(s.size().width(), s.size().height(), s.devicePixelRatio()) for s in QGuiApplication.screens()]


def budget_screens() -> list[tuple[float, float, float]]:
    """캐시 상한 계산에 쓸 화면 — 시험용 흉내(BOM_PDF_SIM_SCREEN)가 있으면 그것."""
    return sysinfo.simulated_screens() or real_screens()


def default_budget() -> int:
    return adaptive_budget(budget_screens(), sysinfo.total_ram_gb())


class PageCache:
    def __init__(self, max_bytes: int = DEFAULT_MAX_BYTES):
        self.max_bytes = max_bytes
        self._d: OrderedDict[tuple, QImage] = OrderedDict()
        self._bytes = 0
        self._pins: dict[object, frozenset[tuple]] = {}
        self._pinned: frozenset[tuple] = frozenset()

    @property
    def bytes_used(self) -> int:
        return self._bytes

    def __len__(self) -> int:
        return len(self._d)

    def get(self, doc_id: int, page: int, scale: float, tile=None) -> QImage | None:
        key = (doc_id, page, scale, tile)
        img = self._d.get(key)
        if img is not None:
            self._d.move_to_end(key)
        return img

    def contains(self, doc_id: int, page: int, scale: float, tile=None) -> bool:
        return (doc_id, page, scale, tile) in self._d

    def has(self, key: tuple) -> bool:
        return key in self._d

    def peek(self, key: tuple) -> QImage | None:
        """LRU 순서를 바꾸지 않고 본다(측정용)."""
        return self._d.get(key)

    def find_any_key(self, doc_id: int, page: int) -> tuple | None:
        """다른 배율로라도 그려 둔 쪽 전체 그림의 키 — 가장 큰 배율. 타일은 고르지 않는다."""
        best = None
        for key in self._d:
            d, p, s, t = key
            if d == doc_id and p == page and t is None and (best is None or s > best[2]):
                best = key
        return best

    def find_any(self, doc_id: int, page: int) -> QImage | None:
        """다른 배율로라도 그려 둔 것 (확대·축소 직후 임시 표시용). 가장 큰 것."""
        key = self.find_any_key(doc_id, page)
        return self._d[key] if key is not None else None

    def set_pinned(self, owner: object, keys) -> None:
        """owner(보기)가 지금 보여 주는 (doc_id, page, scale, tile) 들 — 밀어내지 않는다. 빈 것이면 풀기."""
        keys = frozenset(keys)
        if keys:
            self._pins[owner] = keys
        else:
            self._pins.pop(owner, None)
        self._pinned = frozenset().union(*self._pins.values()) if self._pins else frozenset()
        self._shrink()

    def put(self, doc_id: int, page: int, scale: float, img: QImage, tile=None) -> None:
        key = (doc_id, page, scale, tile)
        old = self._d.pop(key, None)
        if old is not None:
            self._bytes -= old.sizeInBytes()
        self._d[key] = img
        self._bytes += img.sizeInBytes()
        self._shrink(keep=key)

    def _shrink(self, keep=None) -> None:
        """상한까지 오래된 것부터 버린다 — 고정된 쪽과 방금 넣은 쪽은 남긴다."""
        if self._bytes <= self.max_bytes:
            return
        for k in [k for k in self._d if k not in self._pinned and k != keep]:
            if self._bytes <= self.max_bytes:
                break
            self._bytes -= self._d.pop(k).sizeInBytes()

    def drop_doc(self, doc_id: int) -> None:
        for key in [k for k in self._d if k[0] == doc_id]:
            self._bytes -= self._d.pop(key).sizeInBytes()
