"""사진 → PDF (1-5, system-design §4-5). PySide6·PyMuPDF·Pillow·OpenCV 를 import하지 않는다.

- 입력 모으기(파일·폴더), 정렬(카톡 번호 → EXIF 촬영시각 → 파일명), 쪽 배치 계산, 예상 용량, 작업 계획을 만든다.
- 사진을 실제로 읽고(EXIF 회전·GPS 제거), 문서 사진 보정(OpenCV), PDF 를 만드는 일은 작업 프로세스
  (engine.images, engine.jobs)가 한다. 이 모듈의 쪽 배치 계산은 작업 프로세스도 그대로 쓴다(예상 용량과 실제가 같은 식).
- 원본 사진은 읽기만 한다. 산출물 기본 폴더는 사진 폴더가 아니다(D4, services.outputs).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif")
SUFFIX_PHOTOS = "_사진"  # 기본 이름 `<첫 사진 폴더 이름>_사진.pdf` (§3-3 에 없어 1-5 에서 정함 — 질문 대기)

A4 = (595.28, 841.89)  # pt
MM = 72 / 25.4
PAGE_A4 = "a4"              # A4 세로, 사진은 비율 유지하며 맞춤 (기본)
PAGE_ORIGINAL = "original"  # 쪽 모양을 사진 비율로 (쪽당 1장만)

CAPTION_NUMBER = "number"
CAPTION_NAME = "name"
CAPTION_DATE = "date"
CAPTION_PT = 9.0
CAPTION_H = CAPTION_PT * 1.5 + 3  # 사진 아래 캡션 자리(pt)
SLOT_GAP_MM = 6.0                 # 쪽당 2장일 때 두 칸 사이

# 화질: 사진이 놓일 칸 크기 기준 해상도(줄이기만, 키우지 않음)와 JPEG 품질
QUALITY_PRESETS = {
    "submit": {"dpi": 150, "quality": 75},   # 제출용 (기본) — 1-4 압축 "제출용"과 같은 값
    "quality": {"dpi": 300, "quality": 90},  # 화질 우선
}
DEFAULT_PRESET = "submit"
# 예상 용량: 사진 표본을 실제로 줄여 인코딩한 바이트/화소(작업 프로세스가 잰 값). 못 재면 이 값
DEFAULT_BPP = {"submit": 0.22, "quality": 0.45}
PAGE_OVERHEAD = 1500     # 쪽마다 PDF 객체(바이트, 대략)
FONT_OVERHEAD = 40_000   # 캡션 글꼴 부분 집합(바이트, 대략)

FONT_CANDIDATES = ("malgun.ttf", "NanumGothic.ttf", "NanumBarunGothic.ttf")


@dataclass
class Photo:
    path: str
    width: int = 0              # EXIF 회전을 적용한 뒤 화소
    height: int = 0
    taken: str | None = None    # "YYYY-MM-DD HH:MM:SS" (EXIF 촬영시각), 없으면 None
    rotate: int = 0             # 사용자가 더 돌린 각도 (0/90/180/270, 시계 방향)
    doc_mode: bool = False      # 문서 사진 모드(테두리 찾아 반듯하게)
    thumb: bytes | None = field(default=None, repr=False)  # 작은 JPEG (화면용)

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def shown_size(self) -> tuple[int, int]:
        if self.rotate % 180:
            return self.height, self.width
        return self.width, self.height


@dataclass
class Options:
    page: str = PAGE_A4
    per_page: int = 1
    margin_mm: float = 10.0
    caption: tuple[str, ...] = ()
    preset: str = DEFAULT_PRESET

    @property
    def effective_per_page(self) -> int:
        return 1 if self.page == PAGE_ORIGINAL else max(1, min(2, self.per_page))


# --------------------------------------------------------------------------- 입력·정렬


def is_image(path: str) -> bool:
    return path.lower().endswith(IMAGE_EXTS)


def natural_key(s: str) -> tuple:
    """'사진2' < '사진10' 처럼 숫자를 수로 비교하는 정렬 열쇠."""
    out = []
    for p in re.split(r"(\d+)", s.casefold()):
        if not p:
            continue
        out.append((0, int(p), p) if p.isdigit() else (1, 0, p))
    return tuple(out)


_KAKAO = re.compile(r"^kakaotalk", re.IGNORECASE)


def kakao_key(name: str) -> tuple[str, tuple[int, ...]] | None:
    """카카오톡 저장 파일 이름이면 (보낸 시각 숫자 17자리, 묶음 안 번호), 아니면 None.

    예: KakaoTalk_20231015_123456789_01.jpg → ("20231015123456789", (1,)),
        KakaoTalk_Photo_2023-10-15-12-34-56 003.jpeg → ("20231015123456000", (3,)).
    앞 숫자 묶음을 14자리(년월일시분초) 이상이 될 때까지 이어 시각으로, 나머지를 번호로 본다 — 두 이름 꼴이 섞여도
    시각으로 비교된다. 번호가 없는 첫 장(…_123456789.jpg)은 같은 묶음의 _01 보다 앞에 온다.
    """
    stem = os.path.splitext(name)[0]
    if not _KAKAO.match(stem):
        return None
    groups = re.findall(r"\d+", stem)
    if not groups:
        return None
    ts, k = "", 0
    while k < len(groups) and len(ts) < 14:
        ts += groups[k]
        k += 1
    return ts.ljust(17, "0")[:17], tuple(int(g) for g in groups[k:])


def sort_key(p: Photo) -> tuple:
    """카톡 이름(묶음 번호) → EXIF 촬영시각 → 파일 이름 순. 섞여 있으면 카톡 → 촬영시각 있는 것 → 나머지."""
    k = kakao_key(p.name)
    if k is not None:
        return (0, k, "", natural_key(p.name))
    if p.taken:
        return (1, (), p.taken, natural_key(p.name))
    return (2, (), "", natural_key(p.name))


def sort_photos(photos: list[Photo]) -> list[Photo]:
    return sorted(photos, key=sort_key)


def collect_inputs(paths: list[str]) -> list[str]:
    """파일·폴더 → 사진 파일 경로(중복 없이). 폴더는 바로 안쪽만(하위 폴더는 보지 않는다)."""
    out: list[str] = []
    seen: set[str] = set()

    def add(p: str) -> None:
        p = os.path.abspath(p)
        k = os.path.normcase(p)
        if k not in seen and is_image(p) and os.path.isfile(p):
            seen.add(k)
            out.append(p)

    for p in paths:
        if os.path.isdir(p):
            try:
                names = sorted(os.listdir(p), key=natural_key)
            except OSError:
                continue
            for n in names:
                add(os.path.join(p, n))
        else:
            add(p)
    return out


# --------------------------------------------------------------------------- 쪽 배치


Rect = tuple[float, float, float, float]


def fit_rect(w: float, h: float, box: Rect) -> Rect:
    """w×h 를 비율 유지하며 box 가운데에 맞춘 사각형."""
    bx0, by0, bx1, by1 = box
    bw, bh = bx1 - bx0, by1 - by0
    if w <= 0 or h <= 0 or bw <= 0 or bh <= 0:
        return box
    s = min(bw / w, bh / h)
    rw, rh = w * s, h * s
    x0 = bx0 + (bw - rw) / 2
    y0 = by0 + (bh - rh) / 2
    return (x0, y0, x0 + rw, y0 + rh)


def layout(sizes: list[tuple[int, int]], opts: Options) -> list[dict]:
    """사진 크기(돌린 뒤 화소) 목록 → 쪽 목록 [{"size": (w, h) pt, "slots": [(사진 번호, 사진 사각형)]}].

    캡션을 켜면 사진 아래 CAPTION_H 만큼 자리를 남긴다(사진 사각형 바로 아래에 쓴다).
    """
    m = max(0.0, opts.margin_mm) * MM
    cap = CAPTION_H if opts.caption else 0.0
    pages: list[dict] = []
    if opts.page == PAGE_ORIGINAL:
        long_side = A4[1] - 2 * m - cap
        for i, (w, h) in enumerate(sizes):
            s = long_side / max(w, h, 1)
            iw, ih = max(1.0, w * s), max(1.0, h * s)
            pw, ph = iw + 2 * m, ih + 2 * m + cap
            pages.append({"size": (pw, ph), "slots": [(i, (m, m, m + iw, m + ih))]})
        return pages
    pw, ph = A4
    per = opts.effective_per_page
    gap = SLOT_GAP_MM * MM if per == 2 else 0.0
    slot_h = (ph - 2 * m - gap * (per - 1)) / per
    for start in range(0, len(sizes), per):
        slots = []
        for k, i in enumerate(range(start, min(start + per, len(sizes)))):
            y0 = m + k * (slot_h + gap)
            box = (m, y0, pw - m, y0 + slot_h - cap)
            w, h = sizes[i]
            slots.append((i, fit_rect(w, h, box)))
        pages.append({"size": (pw, ph), "slots": slots})
    return pages


def target_pixels(w: int, h: int, rect: Rect, preset: str) -> tuple[int, int]:
    """사진이 놓일 사각형과 화질 기준 해상도로 정한 출력 화소 (줄이기만)."""
    dpi = QUALITY_PRESETS.get(preset, QUALITY_PRESETS[DEFAULT_PRESET])["dpi"]
    need_w = (rect[2] - rect[0]) / 72 * dpi
    s = min(1.0, need_w / max(w, 1))
    return max(1, round(w * s)), max(1, round(h * s))


def caption_text(index: int, photo: Photo, parts: tuple[str, ...]) -> str:
    out = []
    if CAPTION_NUMBER in parts:
        out.append(f"사진 {index + 1}")
    if CAPTION_NAME in parts:
        out.append(photo.name)
    if CAPTION_DATE in parts and photo.taken:
        out.append(photo.taken[:16])
    return " · ".join(out)


def estimate_size(photos: list[Photo], opts: Options, bpp: dict | None = None) -> int:
    """만들 PDF 의 대략 용량(바이트). 문서 사진 모드의 잘라내기는 셈하지 않는다(그만큼 더 작아질 수 있음)."""
    if not photos:
        return 0
    b = (bpp or {}).get(opts.preset) or DEFAULT_BPP.get(opts.preset, DEFAULT_BPP[DEFAULT_PRESET])
    sizes = [p.shown_size for p in photos]
    total = 0.0
    pages = layout(sizes, opts)
    for pg in pages:
        total += PAGE_OVERHEAD
        for i, rect in pg["slots"]:
            tw, th = target_pixels(*sizes[i], rect, opts.preset)
            total += tw * th * b
    if opts.caption:
        total += FONT_OVERHEAD
    return int(total)


def page_count(n_photos: int, opts: Options) -> int:
    per = opts.effective_per_page
    return (n_photos + per - 1) // per


# --------------------------------------------------------------------------- 산출물·작업 계획


def default_name(photos: list[Photo]) -> str:
    """`<첫 사진 폴더 이름>_사진.pdf` — 폴더 이름을 못 얻으면 `사진.pdf`."""
    if not photos:
        return "사진.pdf"
    folder = os.path.basename(os.path.dirname(os.path.abspath(photos[0].path)))
    folder = folder.strip().rstrip(".")
    return f"{folder}{SUFFIX_PHOTOS}.pdf" if folder else "사진.pdf"


def find_font() -> str | None:
    """캡션용 한국어 TTF (D10: 임베드 + subset, 내장 CJK `korea` 는 쓰지 않는다)."""
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows"
    for name in FONT_CANDIDATES:
        p = Path(windir) / "Fonts" / name
        if p.is_file():
            return str(p)
    return None


def plan(photos: list[Photo], opts: Options, out_path: str, protected: list[str],
         font_path: str | None = None) -> dict:
    """작업 프로세스(engine.images.build_pdf)에 넘길 작업. 경로·설정만 — 사진 내용은 작업 프로세스가 다시 읽는다."""
    items = []
    for i, p in enumerate(photos):
        items.append({
            "path": p.path,
            "rotate": p.rotate % 360,
            "doc_mode": bool(p.doc_mode),
            "caption": caption_text(i, p, opts.caption) if opts.caption else "",
        })
    return {
        "out": os.path.abspath(out_path),
        "items": items,
        "page": opts.page,
        "per_page": opts.effective_per_page,
        "margin_mm": float(opts.margin_mm),
        "caption": bool(opts.caption),
        "preset": opts.preset if opts.preset in QUALITY_PRESETS else DEFAULT_PRESET,
        "font": font_path if opts.caption else None,
        "protected": [os.path.abspath(p) for p in protected] + [os.path.abspath(p.path) for p in photos],
    }
