"""Phase 0 합성 픽스처 생성 — 실제 사건 자료를 쓰지 않는다.

만드는 것 (spike/out/fixtures/):
- big_scan_891.pdf   : 891쪽, 쪽마다 서로 다른 200DPI JPEG (카톡 캡처 묶음 유사)
- big_text_891.pdf   : 891쪽, 한글 텍스트 PDF (검색·추출 시험용)
- pii_text.pdf       : 가짜 개인정보(주민번호·전화·계좌·카드·이메일)가 든 텍스트 PDF
- corrupt_*.pdf      : 손상 PDF 변형들
"""
from __future__ import annotations

import io
import random
import sys
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).parent / "out" / "fixtures"
FONT = r"C:\Windows\Fonts\malgun.ttf"
A4 = pymupdf.paper_rect("a4")  # 595 x 842 pt

FAKE_LINES = [
    "원고 홍길동: 내일 오후 3시에 계약서 가지고 갈게요",
    "피고 김철수: 네 알겠습니다. 잔금은 그때 드릴게요",
    "원고 홍길동: 지난번 말씀하신 금액 맞으시죠?",
    "피고 김철수: 네, 오천만 원 맞습니다",
    "원고 홍길동: 사진 보내드립니다",
]


def make_chat_image(i: int, rng: random.Random, font: ImageFont.FreeTypeFont) -> bytes:
    w, h = 1654, 2339  # A4 @ 200DPI
    img = Image.new("RGB", (w, h), (178, 199, 217))
    d = ImageDraw.Draw(img)
    y = 80
    d.text((60, 20), f"합성 대화 캡처 {i + 1}쪽", font=font, fill=(0, 0, 0))
    while y < h - 200:
        line = rng.choice(FAKE_LINES) + f" ({rng.randint(1, 12)}:{rng.randint(0, 59):02d})"
        right = line.startswith("피고")
        tw = int(d.textlength(line, font=font)) + 40
        x = w - tw - 60 if right else 60
        d.rounded_rectangle((x, y, x + tw, y + 90), 20, fill=(255, 235, 51) if right else (255, 255, 255))
        d.text((x + 20, y + 18), line, font=font, fill=(0, 0, 0))
        y += rng.randint(110, 170)
    # 노이즈 조금 (JPEG 크기를 현실적으로)
    for _ in range(400):
        px, py = rng.randrange(w), rng.randrange(h)
        d.point((px, py), fill=(rng.randrange(256),) * 3)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=80)
    return buf.getvalue()


def make_big_scan(n: int = 891) -> Path:
    path = OUT / f"big_scan_{n}.pdf"
    rng = random.Random(8)
    font = ImageFont.truetype(FONT, 44)
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_image(page.rect, stream=make_chat_image(i, rng, font))
        if i % 100 == 0:
            print(f"  scan {i}/{n}", flush=True)
    doc.save(path, garbage=3, deflate=True)
    return path


def make_big_text(n: int = 891) -> Path:
    path = OUT / f"big_text_{n}.pdf"
    rng = random.Random(9)
    doc = pymupdf.open()
    for i in range(n):
        page = doc.new_page(width=A4.width, height=A4.height)
        page.insert_font(fontname="malgun", fontfile=FONT)
        text = "\n".join(rng.choice(FAKE_LINES) for _ in range(30))
        if i == 777:
            text += "\n찾을말: 임대차보증금반환청구"
        page.insert_textbox(pymupdf.Rect(50, 50, 545, 800), text, fontname="malgun", fontsize=11)
    doc.save(path, garbage=3, deflate=True)
    return path


PII_TEXT = """가짜 개인정보 시험 문서 (모두 합성 값)
주민등록번호: 000000-1000000
연락처: 010-0000-0000, 02-000-0000
계좌: 국민은행 000000-00-000000
카드: 0000-0000-0000-0000
이메일: tester@example.com
본문: 위 사람은 2026. 1. 1. 계약을 체결하였다."""


def make_pii() -> Path:
    path = OUT / "pii_text.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="malgun", fontfile=FONT)
    page.insert_textbox(pymupdf.Rect(50, 50, 545, 400), PII_TEXT, fontname="malgun", fontsize=12)
    # 텍스트 아래 이미지도 깔아 이미지 가림까지 시험
    img = Image.new("RGB", (400, 100), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    page.insert_image(pymupdf.Rect(50, 420, 450, 520), stream=buf.getvalue())
    page.insert_textbox(pymupdf.Rect(60, 440, 440, 500), "이미지 위 주민번호 000000-2000000",
                        fontname="malgun", fontsize=12)
    doc.set_metadata({"author": "가짜작성자", "title": "PII 시험", "creator": "spike"})
    doc.save(path)
    return path


def make_corrupt(src: Path) -> list[Path]:
    data = src.read_bytes()
    rng = random.Random(3)
    variants: dict[str, bytes] = {
        "truncated_half": data[: len(data) // 2],
        "truncated_tail": data[:-2000],
        "no_xref": data.replace(b"startxref", b"startxrXX"),
        "zero_bytes": b"",
        "not_pdf": b"This is not a PDF at all\n" * 100,
        "header_only": b"%PDF-1.7\n",
    }
    flipped = bytearray(data)
    for _ in range(300):
        pos = rng.randrange(200, len(flipped))
        flipped[pos] = rng.randrange(256)
    variants["byteflip_300"] = bytes(flipped)
    # 암호화본 (열기 시 비밀번호 필요)
    enc = OUT / "corrupt_encrypted.pdf"
    d = pymupdf.open(src)
    d.save(enc, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    out = [enc]
    for name, b in variants.items():
        p = OUT / f"corrupt_{name}.pdf"
        p.write_bytes(b)
        out.append(p)
    return out


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    which = set(sys.argv[1:]) or {"scan", "text", "pii", "corrupt"}
    if "text" in which or "corrupt" in which:
        print("text:", make_big_text())
    if "pii" in which:
        print("pii:", make_pii())
    if "corrupt" in which:
        # 손상본은 중간 크기 텍스트 PDF(앞 50쪽)로 만든다
        mid = OUT / "mid_text_50.pdf"
        d = pymupdf.open(OUT / "big_text_891.pdf")
        d.select(range(50))
        d.save(mid, garbage=3, deflate=True)
        for p in make_corrupt(mid):
            print("corrupt:", p.name, p.stat().st_size)
    if "scan" in which:
        print("scan:", make_big_scan())


if __name__ == "__main__":
    main()
