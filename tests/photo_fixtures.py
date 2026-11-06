"""합성 사진 픽스처 (1-5). 실제 사진 아님 — 색 블록·잡음·가짜 EXIF(촬영시각·GPS·기기 이름)를 넣어 만든다.

"바로 선 모습"(왼쪽 위에 빨간 블록)을 먼저 그리고, EXIF Orientation 값에 맞게 화소를 거꾸로 돌려 저장한다.
앱이 회전을 바르게 적용하면 결과 그림의 왼쪽 위가 빨갛고 가로·세로가 바로 선 모습과 같다.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

FAKE_MAKE = "SyntheticCam-4417"
FAKE_GPS = {1: "N", 2: (37.0, 33.0, 12.5), 3: "E", 4: (126.0, 58.0, 41.0)}
RED = (220, 20, 20)

# Orientation → 저장할 화소를 만드는 변환(바로 선 모습에 적용). 앱은 그 반대로 돌려 바로 세운다.
_RAW_FROM_UPRIGHT = {
    1: None,
    3: Image.Transpose.ROTATE_180,
    6: Image.Transpose.ROTATE_90,   # 6 = 보여 줄 때 시계 방향 90° → 저장은 반시계 90°
    8: Image.Transpose.ROTATE_270,
}


def upright(size=(600, 400), noise: bool = False) -> Image.Image:
    w, h = size
    if noise:
        g = Image.effect_noise((w, h), 60).convert("L")
        im = Image.merge("RGB", (g, Image.linear_gradient("L").resize((w, h)), g.transpose(Image.Transpose.FLIP_LEFT_RIGHT)))
    else:
        im = Image.new("RGB", (w, h), (235, 235, 235))
    ImageDraw.Draw(im).rectangle((0, 0, w // 4, h // 4), fill=RED)
    return im


def make_exif(orientation: int = 1, taken: str | None = None, gps: bool = True) -> Image.Exif:
    ex = Image.Exif()
    ex[0x010F] = FAKE_MAKE  # Make
    if orientation != 1:
        ex[0x0112] = orientation
    if taken:  # "YYYY:MM:DD HH:MM:SS" → Exif IFD DateTimeOriginal
        ex.get_ifd(0x8769)[0x9003] = taken
    if gps:
        ex.get_ifd(0x8825).update(FAKE_GPS)
    return ex


def make_photo(path: Path, size=(600, 400), orientation: int = 1, taken: str | None = None, gps: bool = True,
               noise: bool = False, image: Image.Image | None = None) -> Path:
    im = image if image is not None else upright(size, noise)
    t = _RAW_FROM_UPRIGHT[orientation]
    raw = im.transpose(t) if t is not None else im
    ex = make_exif(orientation, taken, gps).tobytes()
    ext = path.suffix.lower()
    if ext in (".jpg", ".jpeg"):
        raw.save(path, "JPEG", quality=90, exif=ex)
    elif ext == ".png":
        raw.save(path, "PNG", exif=ex)
    elif ext == ".webp":
        raw.save(path, "WEBP", quality=90, exif=ex)
    elif ext in (".heic", ".heif"):
        import pillow_heif

        pillow_heif.register_heif_opener()
        raw.save(path, quality=90, exif=ex)
    else:
        raise ValueError(ext)
    return path


def document_photo(size=(1200, 900)) -> Image.Image:
    """어두운 책상 위에 비스듬히 놓인 흰 종이(글자 줄 몇 개) — 문서 사진 모드가 테두리를 찾아야 한다."""
    w, h = size
    im = Image.new("RGB", (w, h), (60, 50, 45))
    d = ImageDraw.Draw(im)
    quad = [(int(w * 0.30), int(h * 0.06)), (int(w * 0.70), int(h * 0.10)),
            (int(w * 0.66), int(h * 0.95)), (int(w * 0.26), int(h * 0.91))]  # 세로로 긴 종이, 조금 기울어짐
    d.polygon(quad, fill=(245, 245, 240))
    for k in range(8):
        y = int(h * (0.20 + k * 0.08))
        d.line((int(w * 0.36), y, int(w * 0.60), y + 3), fill=(30, 30, 30), width=4)
    return im


def plain_photo(size=(1200, 900)) -> Image.Image:
    """테두리가 없는 사진(부드러운 그러데이션) — 문서 사진 모드는 원본을 그대로 둬야 한다."""
    w, h = size
    g = Image.linear_gradient("L").resize((w, h))
    return Image.merge("RGB", (g, g.rotate(90).resize((w, h)), Image.new("L", (w, h), 128)))
