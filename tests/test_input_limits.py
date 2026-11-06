"""O5 입력 한도: 큰 사진은 줄여 읽거나(JPEG draft·reduce) 이유와 함께 건너뛴다. 합성 그림만 쓴다.

실제 2억 화소·사진 500장·1만 쪽 수치는 tools/measure_inputs.py 로 잰다(시간이 오래 걸려 여기서는 한도를 낮춰 같은 길을 확인).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from PIL import Image

from bom_pdf.engine import images
from bom_pdf.services import imagetopdf as itp


def test_check_size_rules():
    jpeg = SimpleNamespace(size=(16384, 12288), format="JPEG")  # 약 2억 화소
    png = SimpleNamespace(size=(16384, 12288), format="PNG")
    images._check_size(jpeg, (320, 320))  # 1/8 로 줄여 읽으면 약 300만 화소 — 됨
    with pytest.raises(images.TooLarge) as e:
        images._check_size(png, (320, 320))  # 줄여 읽기 없음 — 한도 넘음
    assert e.value.megapixels == 201
    with pytest.raises(images.TooLarge):
        images._check_size(jpeg, None)  # 원래 크기로 풀어야 하면 한도 넘음
    with pytest.raises(images.TooLarge):
        images._check_size(SimpleNamespace(size=(40000, 30000), format="JPEG"), (320, 320))  # 10억 화소 넘음
    images._check_size(SimpleNamespace(size=(4000, 3000), format="PNG"), None)


def test_pillow_bomb_limit_replaced():
    assert Image.MAX_IMAGE_PIXELS is None  # 우리 한도로 직접 거른다(약 1.79억 화소 오류 대신)


@pytest.fixture
def small_limits(monkeypatch):
    """한도를 200만 화소로 낮춰 작은 합성 그림으로 큰 사진 길을 확인한다."""
    monkeypatch.setattr(images, "FULL_DECODE_MAX_PX", 2_000_000)
    monkeypatch.setattr(images, "DOC_MODE_MAX_SIDE", 800)


def test_big_jpeg_decoded_downscaled_and_big_png_skipped_with_reason(tmp_path, small_limits):
    jpg = tmp_path / "big.jpg"
    png = tmp_path / "big.png"
    Image.new("RGB", (3200, 2400), (120, 60, 30)).save(jpg, "JPEG")  # 768만 화소 > 한도, 1/2 로 줄여 읽으면 됨
    Image.new("RGB", (2000, 1500), (30, 60, 120)).save(png, "PNG")  # 300만 화소 > 한도, 줄여 읽기 없음
    r = images.scan([str(jpg), str(png)])
    assert [it["path"] for it in r["items"]] == [str(jpg)]
    assert r["items"][0]["width"] == 3200  # 정보는 원래 크기
    assert r["failed"] == [str(png)]
    assert r["skipped"] == [{"path": str(png), "reason": images.SKIP_TOO_LARGE, "mp": 3}]

    out = tmp_path / "out.pdf"
    photos = [itp.Photo(path=str(jpg), width=3200, height=2400), itp.Photo(path=str(png), width=2000, height=1500)]
    res = images.build_pdf(itp.plan(photos, itp.Options(), str(out), [], None))
    assert res["status"] == "ok" and res["images"] == 1 and res["too_large"] == 1 and res["failed"] == 1


def test_load_image_reduces_to_needed_size(tmp_path):
    png = tmp_path / "p.png"
    Image.new("RGB", (4000, 3000), (1, 2, 3)).save(png, "PNG")
    im = images.load_image(str(png), 0, (900, 700))
    assert im.size == (1000, 750)  # 정수 배(4)로 줄임 — 필요한 크기 이상은 남긴다
    assert images.load_image(str(png), 0, None).size == (4000, 3000)


def test_doc_mode_works_on_reduced_copy(tmp_path, small_limits):
    jpg = tmp_path / "doc.jpg"
    Image.new("RGB", (3000, 2000), (240, 240, 240)).save(jpg, "JPEG")
    stats = {"failed": 0, "too_large": 0, "doc_fixed": 0, "doc_kept": 0, "caption_skipped": 0}
    im = images._prepare({"path": str(jpg), "rotate": 0, "doc_mode": True}, itp.Options(), stats)
    assert max(im.size) <= 800 * 1.2


def test_exif_rotation_kept_with_reduce(tmp_path):
    from photo_fixtures import make_photo

    p = make_photo(tmp_path / "r6.jpg", (1200, 800), 6)
    im = images.load_image(str(p), 0, (300, 200))
    assert im.size == (300, 200)  # 저장된 화소는 세로(800x1200) — 줄여 읽어도 EXIF 6(90°)이 적용돼 가로


def test_dialog_skip_message_names_reason():
    from bom_pdf.ui.image_pdf_dialog import skipped_message

    msg = skipped_message(["C:/x/a.png", "C:/x/b.jpg"],
                          [{"path": "C:/x/a.png", "reason": "too_large", "mp": 201},
                           {"path": "C:/x/b.jpg", "reason": "unreadable"}])
    assert "너무 큰 사진 1장" in msg and "a.png(약 201백만 화소)" in msg and "읽지 못한 사진 1장" in msg
