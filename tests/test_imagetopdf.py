"""1-5 사진 → PDF: 정렬(카톡 번호 → EXIF 촬영시각 → 파일 이름), EXIF 회전, GPS·EXIF 부재(D9), 쪽 배치·쪽 수,
캡션(TTF 임베드·subset, D10), 문서 사진 모드(보정·실패 시 원본 유지), 예상 용량, 산출물 이름·폴더(D4),
작업 프로세스(취소 = 종료 + 임시 파일 정리), 원본 사진 sha256·폴더 목록 불변. 합성 사진만 쓴다."""
from __future__ import annotations

import io
import os
from pathlib import Path

import pymupdf
import pytest
from PIL import Image

from bom_pdf.engine import images, jobs
from bom_pdf.engine.exportstatus import TMP_SUFFIX
from bom_pdf.services import document as ds
from bom_pdf.services import imagetopdf as itp
from bom_pdf.services import outputs
from conftest import Events, snapshot
from photo_fixtures import FAKE_MAKE, RED, document_photo, make_photo, plain_photo


def _photo(path: str) -> itp.Photo:
    r = images.scan([path])["items"][0]
    return itp.Photo(path=r["path"], width=r["width"], height=r["height"], taken=r["taken"], thumb=r["thumb"])


def _pdf_images(path: Path) -> list[tuple[Image.Image, bytes, pymupdf.Rect]]:
    """쪽 순서대로 (그림, 넣은 원래 바이트, 쪽 위 사각형)."""
    out = []
    with pymupdf.open(path) as d:
        for page in d:
            for info in page.get_images(full=True):
                xref = info[0]
                raw = d.xref_stream_raw(xref)
                img = Image.open(io.BytesIO(d.extract_image(xref)["image"]))
                img.load()
                out.append((img, raw, page.get_image_rects(xref)[0]))
    return out


def _is_red(px) -> bool:
    r, g, b = px[:3]
    return r > 170 and g < 90 and b < 90


@pytest.fixture
def folders(tmp_path):
    src = tmp_path / "현장 사진"
    src.mkdir()
    out = tmp_path / "산출물"
    out.mkdir()
    return src, out


def _snap_all(paths):
    return {str(p): snapshot(Path(p)) for p in paths}


# --------------------------------------------------------------------------- 프로세스 경계


def test_ui_process_does_not_import_image_engines():
    """사진 → PDF 창·서비스를 불러와도 UI 프로세스에 PyMuPDF·OpenCV·Pillow 가 없다(무거운 일은 작업 프로세스)."""
    import subprocess
    import sys

    code = ("import sys; import bom_pdf.services.imagetopdf, bom_pdf.ui.main_window, bom_pdf.ui.image_pdf_dialog; "
            "bad=[m for m in sys.modules if m.split('.')[0] in "
            "('pymupdf','fitz','cv2','PIL','pillow_heif','numpy')]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**os.environ, "PYTHONPATH": "src", "QT_QPA_PLATFORM": "offscreen"})
    assert out.stdout.strip() == "[]"


# --------------------------------------------------------------------------- 정렬·입력 (순수)


def test_sort_kakao_then_exif_then_natural_name():
    P = itp.Photo
    photos = [
        P("/x/사진10.jpg"), P("/x/사진2.jpg"),
        P("/x/IMG_9.jpg", taken="2024-05-02 09:00:00"), P("/x/IMG_1.jpg", taken="2024-05-03 08:00:00"),
        P("/x/KakaoTalk_20241015_123456789_10.jpg", taken="2020-01-01 00:00:00"),
        P("/x/KakaoTalk_20241015_123456789_02.jpg"),
        P("/x/KakaoTalk_20241015_123456789.jpg"),
        P("/x/KakaoTalk_20241014_235959000_01.jpg"),
        P("/x/KakaoTalk_Photo_2024-10-15-13-00-00 003.jpeg"),
        P("/x/KakaoTalk_Photo_2024-10-15-13-00-00 001.jpeg"),
    ]
    names = [p.name for p in itp.sort_photos(photos)]
    assert names == [
        "KakaoTalk_20241014_235959000_01.jpg",
        "KakaoTalk_20241015_123456789.jpg", "KakaoTalk_20241015_123456789_02.jpg",
        "KakaoTalk_20241015_123456789_10.jpg",  # 카톡 이름이면 EXIF 시각은 보지 않는다
        "KakaoTalk_Photo_2024-10-15-13-00-00 001.jpeg", "KakaoTalk_Photo_2024-10-15-13-00-00 003.jpeg",
        "IMG_9.jpg", "IMG_1.jpg",               # 촬영시각 순 (이름 순과 반대)
        "사진2.jpg", "사진10.jpg",                # 자연 정렬
    ]


def test_collect_inputs_folders_dedup_and_filter(folders):
    src, _ = folders
    for n in ("b.JPG", "a.png", "c.webp", "d.heic", "note.txt", "x.pdf"):
        (src / n).write_bytes(b"x")
    sub = src / "하위"
    sub.mkdir()
    (sub / "e.jpg").write_bytes(b"x")
    got = itp.collect_inputs([str(src), str(src / "a.png")])
    assert [os.path.basename(p) for p in got] == ["a.png", "b.JPG", "c.webp", "d.heic"]


def test_layout_pages_and_original_aspect():
    sizes = [(4000, 3000), (3000, 4000), (1000, 1000)]
    one = itp.layout(sizes, itp.Options())
    two = itp.layout(sizes, itp.Options(per_page=2))
    assert len(one) == 3 and len(two) == 2 and len(two[0]["slots"]) == 2 and len(two[1]["slots"]) == 1
    for pg in one + two:
        assert pg["size"] == itp.A4
        for _, (x0, y0, x1, y1) in pg["slots"]:
            m = 10 * itp.MM
            assert x0 >= m - 0.01 and y0 >= m - 0.01 and x1 <= itp.A4[0] - m + 0.01 and y1 <= itp.A4[1] - m + 0.01
    # 쪽당 2장: 위 칸과 아래 칸이 겹치지 않는다
    assert two[0]["slots"][0][1][3] < two[0]["slots"][1][1][1]
    orig = itp.layout(sizes, itp.Options(page=itp.PAGE_ORIGINAL, per_page=2, margin_mm=0))
    assert len(orig) == 3  # 원본 비율은 쪽당 1장
    w, h = orig[0]["size"]
    assert abs(w / h - 4 / 3) < 0.01 and abs(max(w, h) - itp.A4[1]) < 0.5


def test_default_name_and_folder_avoid_photo_folders(tmp_path):
    a, b, docs = tmp_path / "사건A 사진", tmp_path / "다른", tmp_path / "문서"
    for d in (a, b, docs):
        d.mkdir()
    photos = [itp.Photo(str(a / "1.jpg")), itp.Photo(str(b / "2.jpg"))]
    assert itp.default_name(photos) == "사건A 사진_사진.pdf"
    assert itp.default_name([]) == "사진.pdf"
    # 마지막 내보낸 폴더가 사진 폴더 중 하나면 쓰지 않는다
    assert outputs.default_export_dir_for([p.path for p in photos], str(b), str(docs)) == str(docs)
    assert outputs.default_export_dir_for([p.path for p in photos], None, str(a)) == os.path.expanduser("~")
    (docs / "사건A 사진_사진.pdf").write_bytes(b"x")
    assert outputs.unique_path(docs, itp.default_name(photos)).name == "사건A 사진_사진(2).pdf"


def test_plan_protects_photos_and_captions():
    photos = [itp.Photo("/p/a.jpg", 10, 10, taken="2024-01-02 03:04:05"), itp.Photo("/p/b.jpg", 10, 10)]
    job = itp.plan(photos, itp.Options(caption=(itp.CAPTION_NUMBER, itp.CAPTION_NAME, itp.CAPTION_DATE)),
                   "/o/x.pdf", ["/o/open.pdf"], "font.ttf")
    assert [i["caption"] for i in job["items"]] == ["사진 1 · a.jpg · 2024-01-02 03:04", "사진 2 · b.jpg"]
    assert os.path.abspath("/p/a.jpg") in job["protected"] and os.path.abspath("/o/open.pdf") in job["protected"]
    assert itp.plan(photos, itp.Options(), "/o/x.pdf", [])["font"] is None


# --------------------------------------------------------------------------- 엔진 (작업 프로세스 코드를 직접)


@pytest.mark.parametrize("ext", [".jpg", ".png", ".webp", ".heic"])
def test_scan_reads_orientation_size_and_time(folders, ext):
    src, _ = folders
    p = make_photo(src / f"o6{ext}", (600, 400), orientation=6 if ext != ".png" else 1,
                   taken="2024:03:04 05:06:07")
    r = images.scan([str(p)])
    assert r["failed"] == [] and set(r["bpp"]) == set(itp.QUALITY_PRESETS)
    it = r["items"][0]
    assert (it["width"], it["height"]) == (600, 400)
    assert it["taken"] == "2024-03-04 05:06:07"
    th = Image.open(io.BytesIO(it["thumb"]))
    assert th.width > th.height and _is_red(th.getpixel((2, 2)))
    assert "exif" not in th.info


def test_scan_reports_unreadable(folders):
    src, _ = folders
    bad = src / "깨진.jpg"
    bad.write_bytes(b"not a jpeg")
    good = make_photo(src / "ok.jpg")
    r = images.scan([str(bad), str(good)])
    assert r["failed"] == [str(bad)] and len(r["items"]) == 1


def test_build_orientation_order_no_metadata_originals_unchanged(folders):
    """EXIF 회전 1·3·6·8 + png·webp·heic, GPS·기기 이름 든 사진 → 순서·회전·쪽 수, 결과에 EXIF·GPS 없음, 원본 불변."""
    src, out_dir = folders
    files = [
        make_photo(src / "KakaoTalk_20241015_100000000_03.jpg", orientation=8, taken="2024:01:01 00:00:03"),
        make_photo(src / "KakaoTalk_20241015_100000000_01.jpg", orientation=1, taken="2024:01:01 00:00:09"),
        make_photo(src / "KakaoTalk_20241015_100000000_02.jpg", orientation=6),
        make_photo(src / "KakaoTalk_20241015_100000000_04.jpg", orientation=3),
        make_photo(src / "KakaoTalk_20241015_100000000_05.png"),
        make_photo(src / "KakaoTalk_20241015_100000000_06.webp", orientation=6),
        make_photo(src / "KakaoTalk_20241015_100000000_07.heic", orientation=8),
    ]
    snaps = _snap_all(files)
    for f in files:  # 픽스처가 정말 GPS·기기 이름을 품고 있는지(시험이 헛돌지 않게)
        with Image.open(f) as im:
            ex = im.getexif()
            assert ex.get(0x010F) == FAKE_MAKE and ex.get_ifd(0x8825).get(1) == "N", f.name
    photos = itp.sort_photos([_photo(str(f)) for f in files])
    assert [p.name[-6:-4] for p in photos[:4]] == ["01", "02", "03", "04"]
    out = out_dir / "결과.pdf"
    r = images.build_pdf(itp.plan(photos, itp.Options(), str(out), []))
    assert r["status"] == ds.EXPORT_OK and r["pages"] == 7 and r["images"] == 7 and r["failed"] == 0
    with pymupdf.open(out) as d:
        assert d.page_count == 7
        assert all((p.rect.width, p.rect.height) == pytest.approx(itp.A4, abs=0.1) for p in d)
        meta = d.metadata
        assert not d.get_xml_metadata()
        assert not any(v and FAKE_MAKE in str(v) for v in meta.values())
    imgs = _pdf_images(out)
    assert len(imgs) == 7
    for img, raw, rect in imgs:
        assert img.width > img.height, "바로 선 모습은 가로"  # 회전이 적용됐다
        assert _is_red(img.getpixel((3, 3))), "왼쪽 위가 빨강"
        assert rect.width > rect.height
        assert b"Exif" not in raw and b"GPS" not in raw and FAKE_MAKE.encode() not in raw
        assert not img.getexif() and "icc_profile" not in img.info
    data = out.read_bytes()
    assert FAKE_MAKE.encode() not in data and b"Exif\x00\x00" not in data
    assert _snap_all(files) == snaps
    assert not list(out_dir.glob("*" + TMP_SUFFIX))


def test_manual_rotation_two_per_page_and_original_aspect(folders):
    src, out_dir = folders
    files = [make_photo(src / f"p{i}.jpg", (600, 400)) for i in range(5)]
    photos = [_photo(str(f)) for f in files]
    photos[1].rotate = 90
    out = out_dir / "two.pdf"
    r = images.build_pdf(itp.plan(photos, itp.Options(per_page=2, margin_mm=5), str(out), []))
    assert r["status"] == ds.EXPORT_OK and r["pages"] == 3
    imgs = _pdf_images(out)
    assert len(imgs) == 5
    turned = imgs[1][0]
    assert turned.height > turned.width and _is_red(turned.getpixel((turned.width - 3, 3)))  # 시계 방향 90°
    out2 = out_dir / "orig.pdf"
    r = images.build_pdf(itp.plan(photos[:2], itp.Options(page=itp.PAGE_ORIGINAL, margin_mm=0), str(out2), []))
    assert r["pages"] == 2
    with pymupdf.open(out2) as d:
        assert d[0].rect.width / d[0].rect.height == pytest.approx(1.5, abs=0.01)
        assert d[1].rect.height / d[1].rect.width == pytest.approx(1.5, abs=0.01)


@pytest.mark.skipif(itp.find_font() is None, reason="한국어 TTF(맑은 고딕·나눔고딕) 없음")
def test_caption_uses_embedded_subset_ttf(folders):
    src, out_dir = folders
    files = [make_photo(src / f"현장{i}.jpg", taken="2024:05:06 07:08:09") for i in range(3)]
    photos = [_photo(str(f)) for f in files]
    out = out_dir / "cap.pdf"
    opts = itp.Options(per_page=2, caption=(itp.CAPTION_NUMBER, itp.CAPTION_NAME, itp.CAPTION_DATE))
    r = images.build_pdf(itp.plan(photos, opts, str(out), [], itp.find_font()))
    assert r["status"] == ds.EXPORT_OK and r["caption_skipped"] == 0
    with pymupdf.open(out) as d:
        text = d[0].get_text()
        assert "사진 1 · 현장0.jpg · 2024-05-06 07:08" in text and "사진 2" in text
        fonts = d[0].get_fonts()
        assert fonts and all(f[1] == "ttf" or f[1] == "cff" for f in fonts), fonts
        assert all("+" in f[3] for f in fonts), "subset 이름(ABCDEF+…)"
        assert not any("korea" in f[3].lower() or f[1] == "n/a" for f in fonts)
    assert out.stat().st_size < 400_000  # subset 이 없으면 맑은 고딕만 수 MB


def test_caption_without_font_skips_korean_but_keeps_ascii(folders):
    src, out_dir = folders
    photos = [_photo(str(make_photo(src / "a.jpg"))), _photo(str(make_photo(src / "b.jpg")))]
    job = itp.plan(photos, itp.Options(caption=(itp.CAPTION_NAME,)), str(out_dir / "x.pdf"), [], None)
    job["items"][1]["caption"] = "한글 캡션"
    r = images.build_pdf(job)
    assert r["status"] == ds.EXPORT_OK and r["caption_skipped"] == 1
    with pymupdf.open(out_dir / "x.pdf") as d:
        assert "a.jpg" in d[0].get_text() and not d[1].get_text().strip()


def test_document_mode_straightens_or_keeps_original(folders):
    src, out_dir = folders
    doc_p = make_photo(src / "문서.jpg", image=document_photo())
    plain_p = make_photo(src / "풍경.jpg", image=plain_photo())
    snaps = _snap_all([doc_p, plain_p])
    # 엔진 함수: 종이를 찾아 잘라내고, 테두리 없는 사진은 None
    fixed = images.straighten_document(Image.open(doc_p).convert("RGB"))
    assert fixed is not None and fixed.height > fixed.width  # 종이는 세로로 길다
    assert fixed.width < 1200 * 0.75 and fixed.getpixel((fixed.width // 2, 5))[0] > 180  # 어두운 책상이 잘려 나감
    assert images.straighten_document(Image.open(plain_p).convert("RGB")) is None
    photos = [_photo(str(doc_p)), _photo(str(plain_p))]
    for p in photos:
        p.doc_mode = True
    out = out_dir / "doc.pdf"
    r = images.build_pdf(itp.plan(photos, itp.Options(), str(out), []))
    assert r["status"] == ds.EXPORT_OK and r["pages"] == 2 and r["doc_fixed"] == 1 and r["doc_kept"] == 1
    imgs = _pdf_images(out)
    assert imgs[0][0].height > imgs[0][0].width and imgs[1][0].width > imgs[1][0].height
    assert _snap_all([doc_p, plain_p]) == snaps


def test_document_mode_survives_opencv_error(folders, monkeypatch):
    src, out_dir = folders
    p = _photo(str(make_photo(src / "a.jpg")))
    p.doc_mode = True
    import cv2

    monkeypatch.setattr(cv2, "Canny", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    r = images.build_pdf(itp.plan([p], itp.Options(), str(out_dir / "x.pdf"), []))
    assert r["status"] == ds.EXPORT_OK and r["doc_kept"] == 1


def test_submit_preset_downscales_and_estimate_is_close(folders):
    src, out_dir = folders
    files = [make_photo(src / f"n{i}.jpg", (3000, 2000), noise=True) for i in range(4)]
    scan = images.scan([str(f) for f in files])
    photos = [itp.Photo(path=d["path"], width=d["width"], height=d["height"]) for d in scan["items"]]
    sizes = {}
    for preset in ("submit", "quality"):
        opts = itp.Options(preset=preset)
        out = out_dir / f"{preset}.pdf"
        r = images.build_pdf(itp.plan(photos, opts, str(out), []))
        assert r["status"] == ds.EXPORT_OK
        sizes[preset] = r["size"]
        est = itp.estimate_size(photos, opts, scan["bpp"])
        assert 0.6 < est / r["size"] < 1.6, (preset, est, r["size"])
        img = _pdf_images(out)[0][0]
        dpi = itp.QUALITY_PRESETS[preset]["dpi"]
        want = round((itp.A4[0] - 2 * 10 * itp.MM) / 72 * dpi)
        assert abs(img.width - min(3000, want)) <= 2
    assert sizes["submit"] < sizes["quality"]


def test_refuses_output_on_photo_or_open_pdf(folders):
    src, _ = folders
    p = make_photo(src / "a.jpg")
    before = snapshot(p)
    photos = [_photo(str(p))]
    r = images.build_pdf(itp.plan(photos, itp.Options(), str(p), []))
    assert r["status"] == ds.EXPORT_REFUSED
    other = src / "열린.pdf"
    other.write_bytes(b"%PDF-1.7 synthetic")
    r = images.build_pdf(itp.plan(photos, itp.Options(), str(other), [str(other)]))
    assert r["status"] == ds.EXPORT_REFUSED and other.read_bytes() == b"%PDF-1.7 synthetic"
    assert snapshot(p)[:2] == before[:2]


def test_unreadable_photo_is_skipped_not_fatal(folders):
    src, out_dir = folders
    good = _photo(str(make_photo(src / "a.jpg")))
    gone = itp.Photo(str(src / "없음.jpg"), 10, 10)
    r = images.build_pdf(itp.plan([good, gone], itp.Options(), str(out_dir / "x.pdf"), []))
    assert r["status"] == ds.EXPORT_OK and r["pages"] == 1 and r["failed"] == 1
    r = images.build_pdf(itp.plan([gone], itp.Options(), str(out_dir / "y.pdf"), []))
    assert r["status"] == ds.EXPORT_ERROR and not (out_dir / "y.pdf").exists()


# --------------------------------------------------------------------------- 작업 프로세스(서비스 경유)


def test_service_scan_and_build_in_job_process(service, events, folders):
    src, out_dir = folders
    files = [make_photo(src / f"사진{i}.jpg", orientation=6, taken=f"2024:01:0{9 - i} 00:00:00") for i in range(3)]
    listing = sorted(os.listdir(src))
    snaps = _snap_all(files)
    sid = service.scan_images([str(f) for f in files])
    done = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == sid)
    assert done.kind == jobs.KIND_IMAGE_SCAN and len(done.result["items"]) == 3
    photos = itp.sort_photos([itp.Photo(path=d["path"], width=d["width"], height=d["height"], taken=d["taken"])
                              for d in done.result["items"]])
    assert [p.name for p in photos] == ["사진2.jpg", "사진1.jpg", "사진0.jpg"]  # 촬영시각 순
    out = out_dir / "결과.pdf"
    jid = service.images_to_pdf(itp.plan(photos, itp.Options(), str(out), []))
    done = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == jid, 60)
    assert done.result["status"] == ds.EXPORT_OK and done.result["pages"] == 3
    assert sorted(os.listdir(src)) == listing and _snap_all(files) == snaps


def test_cancel_image_job_removes_temp_and_output(folders):
    src, out_dir = folders
    big = make_photo(src / "big.jpg", (3000, 2000), noise=True)
    photos = [itp.Photo(str(big), 3000, 2000) for _ in range(80)]
    out = out_dir / "x.pdf"
    ev = Events()
    runner = jobs.JobRunner(ev)
    jid = runner.start(jobs.KIND_IMAGE_PDF, (itp.plan(photos, itp.Options(preset="quality"), str(out), []),),
                       [str(out)])
    ev.wait(lambda e: isinstance(e, jobs.JobProgress) and e.job_id == jid and e.done >= 1, 30)
    runner.cancel(jid)
    done = ev.wait(lambda e: isinstance(e, jobs.JobDone) and e.job_id == jid, 30)
    assert done.result["status"] == ds.JOB_CANCELLED
    assert not out.exists() and not list(out_dir.iterdir())
    assert sorted(os.listdir(src)) == ["big.jpg"]
