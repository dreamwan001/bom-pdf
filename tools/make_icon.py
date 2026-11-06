"""bom-pdf 앱 아이콘 생성 (코드로 그린 원본 그림).

모양: 귀퉁이가 접힌 흰 문서 + 아래에서 돋아나는 새싹. 평면 디자인.
작은 크기(16–32px)는 글줄을 빼고 테두리·새싹을 굵게 그린 단순판을 쓴다.

사용: .venv/Scripts/python.exe tools/make_icon.py
산출(Git 포함): assets/bom-pdf.ico(16·20·24·32·40·48·64·128·256), assets/icon-master.png(1024),
              assets/installer-side.bmp(설치 프로그램 시작·마침 화면 왼쪽 그림),
              assets/icon-preview.png(256)
"""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)

INK = (31, 94, 63, 255)          # 짙은 초록 테두리·줄기
PAGE_TOP = (255, 255, 255, 255)
PAGE_BOTTOM = (236, 246, 239, 255)
FOLD = (176, 221, 189, 255)
LEAF_DARK = (46, 158, 87, 255)
LEAF_LIGHT = (126, 204, 104, 255)
LINE = (190, 204, 196, 255)


def _leaf(base, tip, width, bend):
    """base→tip 잎 모양 다각형. bend 는 잎이 휘는 정도(폭에 대한 비율)."""
    bx, by = base
    tx, ty = tip
    dx, dy = tx - bx, ty - by
    length = math.hypot(dx, dy)
    nx, ny = -dy / length, dx / length
    left, right = [], []
    steps = 48
    for i in range(steps + 1):
        t = i / steps
        cx = bx + dx * t + nx * width * bend * math.sin(math.pi * t)
        cy = by + dy * t + ny * width * bend * math.sin(math.pi * t)
        half = width * math.sin(math.pi * t) ** 0.9 * (1 - 0.25 * t)
        left.append((cx + nx * half, cy + ny * half))
        right.append((cx - nx * half, cy - ny * half))
    return left + right[::-1]


def _curve(p0, p1, p2, steps=40):
    pts = []
    for i in range(steps + 1):
        t = i / steps
        x = (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0]
        y = (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1]
        pts.append((x, y))
    return pts


def _thick_line(draw, pts, width, fill):
    draw.line(pts, fill=fill, width=int(width), joint="curve")
    r = width / 2
    for x, y in (pts[0], pts[-1]):
        draw.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def draw_icon(px: int, simple: bool) -> Image.Image:
    """px 크기 정사각 RGBA. simple=True 는 16–32px 용 단순판(글줄·흙 선 없음, 굵은 선)."""
    s = px
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    left, right = 0.15 * s, 0.85 * s
    top, bottom = 0.05 * s, 0.95 * s
    if simple:
        left, right, top, bottom = 0.10 * s, 0.90 * s, 0.03 * s, 0.97 * s
    fold = (0.26 if simple else 0.22) * s
    radius = 0.08 * s
    stroke = (0.075 if simple else 0.04) * s

    # 문서: 바깥 모양을 짙은 초록으로 칠하고 테두리 두께만큼 안쪽 모양에 옅은 그라데이션을 덮는다.
    # 오른쪽 위는 접힌 귀퉁이 대각선(x - y <= c) 바깥을 잘라 낸다.
    def shape(inset: float) -> Image.Image:
        m = Image.new("L", (s, s), 0)
        md = ImageDraw.Draw(m)
        md.rounded_rectangle((left + inset, top + inset, right - inset, bottom - inset),
                             radius=max(radius - inset, 1), fill=255)
        c = (right - fold) - top - inset * math.sqrt(2)
        md.polygon([(c + top - s, top - s), (right + s, top - s), (right + s, right + s - c)], fill=0)
        return m

    img.paste(Image.new("RGBA", (s, s), INK), (0, 0), shape(0))
    grad = Image.new("RGBA", (1, s))
    for y in range(s):
        t = min(max((y - top) / (bottom - top), 0), 1)
        grad.putpixel((0, y), tuple(int(a + (b - a) * t) for a, b in zip(PAGE_TOP, PAGE_BOTTOM)))
    img.paste(grad.resize((s, s)), (0, 0), shape(stroke))

    # 접힌 귀퉁이
    d.polygon([(right - fold, top), (right - fold, top + fold), (right, top + fold)], fill=FOLD)
    d.line([(right - fold, top), (right - fold, top + fold), (right, top + fold)], fill=INK,
           width=int(stroke), joint="curve")

    # 글줄(큰 크기만)
    if not simple:
        lw = 0.035 * s
        for i, (x0, x1) in enumerate(((0.27, 0.56), (0.27, 0.73), (0.27, 0.66))):
            y = (0.21 + 0.085 * i) * s
            _thick_line(d, [(x0 * s, y), (x1 * s, y)], lw, LINE)

    # 새싹: 아래 가운데에서 위로 굽은 줄기 + 잎 두 장
    cx = 0.5 * s
    base_y = (0.86 if simple else 0.85) * s
    top_y = (0.52 if simple else 0.56) * s
    stem_w = (0.085 if simple else 0.05) * s
    stem = _curve((cx, base_y), (cx - 0.02 * s, (base_y + top_y) / 2), (cx + 0.01 * s, top_y))
    leaf_w = (0.14 if simple else 0.115) * s
    joint = stem[len(stem) * 2 // 3]
    d.polygon(_leaf(joint, (cx - (0.33 if simple else 0.27) * s, joint[1] - (0.16 if simple else 0.12) * s),
                    leaf_w * 0.85, 0.35), fill=LEAF_LIGHT)
    tip = stem[-1]
    d.polygon(_leaf(tip, (cx + (0.30 if simple else 0.27) * s, tip[1] - (0.12 if simple else 0.15) * s),
                    leaf_w, -0.35), fill=LEAF_DARK)
    _thick_line(d, stem, stem_w, INK)

    # 바닥 흙 선(큰 크기만)
    if not simple:
        _thick_line(d, [(0.36 * s, base_y + 0.015 * s), (0.64 * s, base_y + 0.015 * s)], 0.03 * s, LEAF_DARK)
    return img


def render(size: int) -> Image.Image:
    """size 픽셀 아이콘 — 8배로 그려 줄여 가장자리를 매끄럽게."""
    simple = size <= 32
    big = draw_icon(max(size * 8, 512), simple)
    return big.resize((size, size), Image.Resampling.LANCZOS)


def installer_side(master: Image.Image) -> Image.Image:
    """NSIS 시작·마침 화면 왼쪽 그림(164×314 의 2배 — 고배율 화면에서 늘려도 덜 흐리게). 옅은 초록 바탕 + 아이콘."""
    w, h = 328, 628
    img = Image.new("RGB", (w, h))
    top, bottom = (232, 245, 236), (198, 230, 207)
    for y in range(h):
        t = y / (h - 1)
        ImageDraw.Draw(img).line([(0, y), (w, y)], fill=tuple(int(a + (b - a) * t) for a, b in zip(top, bottom)))
    icon = master.resize((200, 200), Image.Resampling.LANCZOS)
    img.paste(icon, ((w - 200) // 2, 150), icon)
    return img


def main() -> int:
    ASSETS.mkdir(exist_ok=True)
    master = draw_icon(1024 * 2, simple=False).resize((1024, 1024), Image.Resampling.LANCZOS)
    master.save(ASSETS / "icon-master.png", optimize=True)
    frames = {n: render(n) for n in ICO_SIZES}
    frames[256].save(ASSETS / "icon-preview.png", optimize=True)
    # Pillow ICO: 가장 큰 그림을 주고 append_images 로 크기별 그림을 넣는다(작은 크기 단순판이 그대로 들어감)
    frames[256].save(ASSETS / "bom-pdf.ico", format="ICO", sizes=[(n, n) for n in ICO_SIZES],
                     append_images=[frames[n] for n in ICO_SIZES if n != 256])
    installer_side(master).save(ASSETS / "installer-side.bmp")
    print({p.name: p.stat().st_size for p in sorted(ASSETS.iterdir())})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
