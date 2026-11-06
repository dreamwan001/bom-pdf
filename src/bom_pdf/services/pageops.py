"""페이지 편집 "가상 문서" (1-4, system-design §4-4). PySide6·PyMuPDF 를 import하지 않는다.

- 가상 문서 = 쪽 참조 목록 `[PageRef(원본 번호, 쪽, 추가 회전)]`. 여러 열린 문서·파일에 걸칠 수 있다(병합).
- 회전·삭제·순서 바꾸기·병합은 이 목록만 바꾼다 — 어떤 파일도 건드리지 않는다. 바꿀 때마다 되돌리기 기록.
- 저장·추출·분할은 `plan()` 이 만든 작업 하나를 작업 프로세스(engine.build)가 한 번에 새 파일로 만든다.
- 분할 계산(쪽 범위 / N쪽마다 / 최상위 책갈피)은 가상 문서의 쪽 번호(0부터) 묶음을 돌려주는 순수 함수.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

HISTORY_MAX = 100

SUFFIX_EDIT = "_편집"      # §3-3 페이지 편집
SUFFIX_EXTRACT = "_추출"   # 고른 쪽만 새 파일로 (§3-3 에 없어 1-4 에서 정함)
SUFFIX_COMPRESS = "_압축"  # 용량 압축본


@dataclass(frozen=True)
class PageRef:
    src: int          # PageOps.sources 의 번호
    page: int         # 원본 쪽 (0부터)
    rotate: int = 0   # 원본 회전에 더할 회전 (0/90/180/270)


@dataclass
class Source:
    path: str
    page_sizes: list[tuple[float, float]]  # pt, 원본 회전 반영
    password: str | None = field(default=None, repr=False)
    sha256: str | None = None
    doc_id: int | None = None  # 화면 쪽(UI)이 렌더 요청에 쓰는 열린 문서 번호

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def page_count(self) -> int:
        return len(self.page_sizes)


class PageOps:
    def __init__(self, primary: Source):
        self.sources: list[Source] = [primary]
        self.pages: list[PageRef] = [PageRef(0, i) for i in range(primary.page_count)]
        self._saved: tuple = tuple(self.pages)
        self._undo: list[tuple[str, tuple]] = []
        self._redo: list[tuple[str, tuple]] = []

    # ---- 원본
    @property
    def primary(self) -> Source:
        return self.sources[0]

    def add_source(self, src: Source) -> int:
        """원본을 더한다(같은 경로면 있는 것). 번호를 돌려준다. 쪽은 더하지 않는다."""
        key = os.path.normcase(os.path.abspath(src.path))
        for i, s in enumerate(self.sources):
            if os.path.normcase(os.path.abspath(s.path)) == key:
                if s.doc_id is None:
                    s.doc_id = src.doc_id
                if s.sha256 is None:
                    s.sha256 = src.sha256
                return i
        self.sources.append(src)
        return len(self.sources) - 1

    def source_of(self, i: int) -> Source:
        return self.sources[self.pages[i].src]

    def page_size(self, i: int) -> tuple[float, float]:
        """화면에 보일 크기(추가 회전 반영)."""
        ref = self.pages[i]
        w, h = self.sources[ref.src].page_sizes[ref.page]
        return (h, w) if ref.rotate % 180 else (w, h)

    def __len__(self) -> int:
        return len(self.pages)

    # ---- 기록
    @property
    def dirty(self) -> bool:
        return tuple(self.pages) != self._saved

    def mark_saved(self) -> None:
        self._saved = tuple(self.pages)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> str | None:
        if not self._undo:
            return None
        label, before = self._undo.pop()
        self._redo.append((label, tuple(self.pages)))
        self.pages = list(before)
        return label

    def redo(self) -> str | None:
        if not self._redo:
            return None
        label, after = self._redo.pop()
        self._undo.append((label, tuple(self.pages)))
        self.pages = list(after)
        return label

    def _commit(self, new: list[PageRef], label: str) -> bool:
        if new == self.pages:
            return False
        self._undo.append((label, tuple(self.pages)))
        del self._undo[:-HISTORY_MAX]
        self._redo.clear()
        self.pages = new
        return True

    # ---- 편집 (indices: 가상 문서 쪽 번호, 0부터)
    def _clean(self, indices) -> list[int]:
        n = len(self.pages)
        return sorted({i for i in indices if 0 <= i < n})

    def rotate(self, indices, delta: int) -> bool:
        idx = set(self._clean(indices))
        if not idx or delta % 90:
            return False
        new = [replace(r, rotate=(r.rotate + delta) % 360) if i in idx else r for i, r in enumerate(self.pages)]
        return self._commit(new, "회전")

    def delete(self, indices) -> bool:
        """고른 쪽을 지운다. 모든 쪽은 지울 수 없다(빈 문서 저장 방지)."""
        idx = set(self._clean(indices))
        if not idx or len(idx) >= len(self.pages):
            return False
        return self._commit([r for i, r in enumerate(self.pages) if i not in idx], "삭제")

    def move(self, indices, dest: int) -> list[int]:
        """고른 쪽들을 (지금 목록 기준) dest 자리 앞으로 옮긴다(고른 순서 유지). 옮긴 뒤 그 쪽들의 번호."""
        idx = self._clean(indices)
        if not idx:
            return []
        dest = max(0, min(dest, len(self.pages)))
        moving = [self.pages[i] for i in idx]
        rest = [r for i, r in enumerate(self.pages) if i not in set(idx)]
        at = dest - sum(1 for i in idx if i < dest)
        new = rest[:at] + moving + rest[at:]
        self._commit(new, "순서")
        return list(range(at, at + len(moving)))

    def merge(self, order: list) -> bool:
        """병합: order 의 차례로 이어 붙인다. None = 지금 편집 중인 쪽들, int = 그 원본 전체."""
        new: list[PageRef] = []
        for o in order:
            if o is None:
                new.extend(self.pages)
            else:
                new.extend(PageRef(o, p) for p in range(self.sources[o].page_count))
        if not new:
            return False
        return self._commit(new, "병합")

    # ---- 저장 계획
    def plan(self, outputs: list[tuple[str, list[int]]], annots: list | None = None, toc: bool = True) -> dict:
        """작업 프로세스로 보낼 작업. outputs = [(산출물 경로, 가상 문서 쪽 번호들)].

        annots: 원본마다 1-3 주석 항목 목록(AnnotationStore.export_items 모양) 또는 None — 표준 주석으로 넣는다.
        """
        return {
            "sources": [{"path": s.path, "password": s.password, "sha256": s.sha256} for s in self.sources],
            "outputs": [{"path": os.path.abspath(p),
                         "pages": [[self.pages[i].src, self.pages[i].page, self.pages[i].rotate] for i in idx]}
                        for p, idx in outputs],
            "annots": annots,
            "toc": toc,
        }


# --------------------------------------------------------------------------- 분할


def parse_ranges(text: str, n: int) -> list[list[int]]:
    """"1-3, 4-7, 8-" → [[0,1,2], [3..6], [7..n-1]]. 쪽 번호는 1부터, "-" 뒤를 비우면 끝까지.

    잘못된 입력은 ValueError(사용자에게 보일 문구).
    """
    groups = []
    parts = [p.strip() for p in text.replace("~", "-").split(",") if p.strip()]
    if not parts:
        raise ValueError("쪽 범위를 입력하세요. 예: 1-3, 4-10, 11-")
    for part in parts:
        a, sep, b = part.partition("-")
        try:
            start = int(a) if a.strip() else 1
            end = (int(b) if b.strip() else n) if sep else start
        except ValueError:
            raise ValueError(f"'{part}' 를 쪽 범위로 읽을 수 없습니다.") from None
        if not (1 <= start <= end <= n):
            raise ValueError(f"'{part}' 는 1–{n}쪽 밖이거나 앞뒤가 바뀌었습니다.")
        groups.append(list(range(start - 1, end)))
    return groups


def split_every(n: int, k: int) -> list[list[int]]:
    if k < 1:
        raise ValueError("나눌 쪽 수는 1 이상이어야 합니다.")
    return [list(range(i, min(n, i + k))) for i in range(0, n, k)]


def split_by_bookmarks(ops: PageOps, outlines: dict[int, list]) -> list[list[int]]:
    """최상위(수준 1) 책갈피가 가리키는 쪽에서 나눈다. outlines = {원본 번호: [(수준, 제목, 쪽)]}.

    지운 쪽을 가리키는 책갈피는 건너뛴다. 첫 책갈피 앞쪽들은 첫 묶음이 된다. 책갈피가 없으면 ValueError.
    """
    where: dict[tuple[int, int], int] = {}
    for i, r in enumerate(ops.pages):
        where.setdefault((r.src, r.page), i)
    starts = set()
    for si, items in outlines.items():
        for lvl, _title, page in items or ():
            if lvl == 1 and page >= 0 and (si, page) in where:
                starts.add(where[(si, page)])
    if not starts:
        raise ValueError("나눌 기준이 되는 최상위 책갈피가 없습니다.")
    starts.add(0)
    cuts = sorted(starts) + [len(ops.pages)]
    return [list(range(a, b)) for a, b in zip(cuts, cuts[1:]) if b > a]


def split_names(original: str, count: int) -> list[str]:
    """§3-3 분할 이름: `<원본>_1.pdf`, `<원본>_2.pdf` …"""
    stem = Path(original).stem
    return [f"{stem}_{i}.pdf" for i in range(1, count + 1)]
