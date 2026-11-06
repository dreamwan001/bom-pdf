"""O4 파일 위치·디스크·느린 저장소: 긴 경로, 해시 캐시 재사용·지연, 원격 파일 메모리 열기·취소, 여유 공간, 쓰기 실패.

실제 사건 자료·실제 네트워크 폴더를 쓰지 않는다 — 합성 PDF, 원격은 BOM_PDF_SIM_REMOTE, 느림은 BOM_PDF_SIM_SLOW_READ 흉내.
"""
from __future__ import annotations

import errno
import os
import shutil
import time
from pathlib import Path

import pymupdf
import pytest

from bom_pdf import paths, storage
from bom_pdf.engine.exportstatus import TMP_SUFFIX
from bom_pdf.services import diskspace
from bom_pdf.services import document as ds
from conftest import Events, make_marked_pdf, make_text_pdf, qt_wait, sha256, snapshot

L = paths.long_path


def _deep_dir(base: Path, min_len: int = 300) -> str:
    d = str(base)
    k = 0
    while len(d) < min_len:
        d = os.path.join(d, f"아주긴사건폴더이름_{k:02d}_" + "x" * 16)
        k += 1
    os.makedirs(L(d))
    return d


def _ls(folder: str) -> list[str]:
    return sorted(os.listdir(L(folder)))


def _big_pdf(path: Path, mb: int = 4) -> Path:
    """첨부 파일(무작위 바이트)로 크기를 키운 합성 PDF — 느린 읽기·취소 시험용."""
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "big synthetic", fontsize=20)
    doc.embfile_add("pad.bin", os.urandom(mb * 2**20))
    doc.save(path)
    doc.close()
    return path


# --------------------------------------------------------------------------- 긴 경로


def test_long_path_helper():
    short = r"C:\a\b.pdf"
    assert L(short) == short
    longp = "C:\\" + "\\".join(["d" * 20] * 15) + "\\x.pdf"
    assert L(longp).startswith("\\\\?\\C:\\") and paths.plain_path(L(longp)) == longp
    unc = "\\\\srv\\share\\" + "\\".join(["d" * 20] * 15)
    assert L(unc).startswith("\\\\?\\UNC\\srv\\share\\") and paths.plain_path(L(unc)) == unc
    assert L(L(longp)) == L(longp)


def test_open_render_annotate_export_build_with_300_char_paths(tmp_path, events, service):
    src_dir = _deep_dir(tmp_path / "src")
    out_dir = _deep_dir(tmp_path / "out")
    src = os.path.join(src_dir, "합성 원본.pdf")
    assert len(src) > 300
    make_text_pdf(tmp_path / "s.pdf")
    shutil.copyfile(tmp_path / "s.pdf", L(src))
    before = (sha256(Path(L(src))), os.stat(L(src)).st_mtime_ns, _ls(src_dir))

    doc_id = service.open(src)
    opened = events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    assert opened.doc.page_count == 14
    service.request_pages(doc_id, 1.0, [0], 0)
    events.wait(lambda e: isinstance(e, ds.PageImage) and e.doc_id == doc_id)
    h = events.wait(lambda e: isinstance(e, ds.DocumentHashed))
    assert h.sha256 == before[0]

    # 메모 포함 PDF(작업자)
    out = os.path.join(out_dir, "합성 원본_메모.pdf")
    item = {"id": "a", "type": "box", "page": 0, "rect": [72, 72, 200, 140], "color": [1, 0, 0], "note": "",
            "tags": [], "label": "box"}
    service.export_annotated(doc_id, out, [item])
    done = events.wait(lambda e: isinstance(e, ds.ExportDone), timeout=60)
    assert done.result["status"] == ds.EXPORT_OK and os.path.isfile(L(out))

    # 쪽 편집 저장(작업 프로세스)
    out2 = os.path.join(out_dir, "합성 원본_편집.pdf")
    job = {"sources": [{"path": src, "password": None, "sha256": h.sha256}],
           "outputs": [{"path": out2, "pages": [[0, 2, 90], [0, 0, 0]]}], "toc": True}
    jid = service.build_pages(job)
    jd = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == jid, timeout=90)
    assert jd.result["status"] == ds.EXPORT_OK
    with pymupdf.open(L(out2)) as d:
        assert d.page_count == 2

    # 압축본(작업 프로세스)
    out3 = os.path.join(out_dir, "합성 원본_압축.pdf")
    jid = service.compress(doc_id, out3, "clean")
    jd = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == jid, timeout=90)
    assert jd.result["status"] in (ds.EXPORT_OK, ds.JOB_NOT_SMALLER)
    assert not any(n.endswith(TMP_SUFFIX) for n in _ls(out_dir))
    assert (sha256(Path(L(src))), os.stat(L(src)).st_mtime_ns, _ls(src_dir)) == before


def test_photo_pdf_with_300_char_paths(tmp_path):
    from PIL import Image

    from bom_pdf.engine import images
    from bom_pdf.services import imagetopdf as itp

    d = _deep_dir(tmp_path / "photos")
    p = os.path.join(d, "사진 1.jpg")
    Image.new("RGB", (800, 600), (200, 100, 50)).save(L(p), "JPEG")
    r = images.scan([p])
    assert len(r["items"]) == 1 and not r["failed"]
    out = os.path.join(_deep_dir(tmp_path / "out"), "사진.pdf")
    photo = itp.Photo(path=p, width=800, height=600)
    res = images.build_pdf(itp.plan([photo], itp.Options(), out, [], None))
    assert res["status"] == "ok" and os.path.isfile(L(out))


# --------------------------------------------------------------------------- 해시 캐시·지연


def test_reopen_uses_hash_cache_without_reading(tmp_path, events, service, sample_pdf):
    service.hash_cache.sha256_of(str(sample_pdf))
    n0 = storage.hash_bytes_read
    service.open(str(sample_pdf))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    assert ev.doc.sha256 == sha256(sample_pdf)  # 열 때 바로(읽지 않고)
    events.wait(lambda e: isinstance(e, ds.DocumentHashed))
    assert storage.hash_bytes_read == n0


def test_uncached_hash_waits_for_first_page(tmp_path, events, service):
    pdf = make_text_pdf(tmp_path / "fresh.pdf")
    n0 = storage.hash_bytes_read
    doc_id = service.open(str(pdf))
    events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    time.sleep(0.6)
    assert storage.hash_bytes_read == n0  # 첫 쪽 전에는 전체 읽기를 시작하지 않는다
    service.request_pages(doc_id, 1.0, [0], 0)
    h = events.wait(lambda e: isinstance(e, ds.DocumentHashed), timeout=10)
    assert h.sha256 == sha256(pdf) and storage.hash_bytes_read > n0


def test_source_unchanged_uses_cache_and_detects_change(tmp_path, monkeypatch):
    calls = []
    real = storage.file_sha256
    monkeypatch.setattr(storage, "file_sha256", lambda p, *a, **k: calls.append(p) or real(p))
    pdf = make_text_pdf(tmp_path / "a.pdf")
    digest = sha256(pdf)
    cache_file = paths.data_dir() / "hash_cache.json"
    storage.HashCache(cache_file).put(storage.HashCache.key(str(pdf)), digest)
    assert storage.source_unchanged(str(pdf), digest) and calls == []  # 캐시 적중 — 읽지 않음
    pdf.write_bytes(pdf.read_bytes() + b"\n%changed")
    assert not storage.source_unchanged(str(pdf), digest) and len(calls) == 1  # 바뀌면 읽어서 비교
    assert storage.source_unchanged(str(pdf), None)


# --------------------------------------------------------------------------- 원격(느린) 파일


@pytest.fixture
def remote_service(tmp_path, monkeypatch):
    """모든 파일을 원격으로 보고(작은 파일은 메모리로) 읽기를 1MB/s 로 늦춘 작업자."""
    monkeypatch.setenv(storage.ENV_SIM_REMOTE, "1")
    monkeypatch.setenv("BOM_PDF_SIM_SLOW_READ", "1:0")
    ev = Events()
    s = ds.DocumentService(ev, data_dir=tmp_path / "home")
    s.start()
    yield s, ev
    s.shutdown()


def test_remote_small_file_read_once_with_progress_and_hash(tmp_path, remote_service):
    s, events = remote_service
    pdf = _big_pdf(tmp_path / "remote.pdf", 2)
    doc_id = s.open(str(pdf))
    loading = events.wait(lambda e: isinstance(e, ds.DocumentLoading), timeout=20)
    assert loading.doc_id == doc_id and loading.total == pdf.stat().st_size
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened), timeout=30)
    assert ev.doc.sha256 == sha256(pdf)  # 메모리로 읽으며 함께 계산 — 따로 다시 읽지 않음
    assert s.hash_cache.cached(str(pdf)) == ev.doc.sha256
    s.request_pages(doc_id, 1.0, [0], 0)
    events.wait(lambda e: isinstance(e, ds.PageImage) and e.doc_id == doc_id, timeout=20)


def test_cancel_slow_remote_open_frees_worker(tmp_path, remote_service):
    s, events = remote_service
    slow = _big_pdf(tmp_path / "slow.pdf", 6)  # 1MB/s 흉내로 6초
    small = make_text_pdf(tmp_path / "small.pdf")
    slow_id = s.open(str(slow))
    events.wait(lambda e: isinstance(e, ds.DocumentLoading), timeout=20)
    t0 = time.monotonic()
    s.close(slow_id)
    s.open(str(small))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened), timeout=20)
    assert ev.doc.path == str(small.resolve()) or os.path.samefile(ev.doc.path, small)
    assert time.monotonic() - t0 < 3.0  # 6초 읽기를 끝까지 기다리지 않았다
    assert not any(isinstance(e, ds.DocumentOpened) and e.doc.doc_id == slow_id for e in events.drain(0.3))


def test_slow_open_shows_loading_and_cancel_closes_tab(qapp, tmp_path, monkeypatch):
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    monkeypatch.setenv(storage.ENV_SIM_REMOTE, "1")
    monkeypatch.setenv("BOM_PDF_SIM_SLOW_READ", "1:0")
    bridge = ServiceBridge()
    service = ds.DocumentService(bridge, data_dir=tmp_path / "home")
    service.start()
    win = MainWindow(service, bridge, RecentDocuments(tmp_path / "home"))
    win.show()
    try:
        pdf = _big_pdf(tmp_path / "slow.pdf", 6)
        win.open_paths([str(pdf)])
        tab = win.tabs.currentWidget()
        qt_wait(qapp, lambda: tab.cancel_open.isVisible() and "MB" in tab.status_label.text(), timeout=20)
        assert "불러오는 중" in tab.status_label.text()
        tab.cancel_open.click()
        qt_wait(qapp, lambda: win.tabs.count() == 0, timeout=5)
    finally:
        win.close()
        service.shutdown()


# --------------------------------------------------------------------------- 여유 공간


def test_space_check_numbers(tmp_path, monkeypatch):
    assert diskspace.required(100 * 2**20) == int(120 * 2**20) + diskspace.TEMP_MARGIN
    monkeypatch.setattr(diskspace, "free_bytes", lambda f: 10 * 2**20)
    c = diskspace.check(str(tmp_path / "없는 폴더"), 50 * 2**20)
    assert not c.ok and "부족" in diskspace.message(c) and "10MB" in diskspace.message(c)
    monkeypatch.setattr(diskspace, "free_bytes", lambda f: None)
    assert diskspace.check(str(tmp_path), 10**12).ok  # 모르면 막지 않는다
    monkeypatch.undo()
    assert diskspace.free_bytes(str(tmp_path / "a" / "b")) > 0  # 없는 하위 폴더는 있는 상위로


def test_estimate_build_scales_with_pages(tmp_path):
    a = make_marked_pdf(tmp_path / "a.pdf", pages=8)
    size = a.stat().st_size
    job = {"sources": [{"path": str(a)}], "outputs": [{"path": "x", "pages": [[0, i, 0] for i in range(4)]}]}
    assert diskspace.estimate_build(job, [8]) == pytest.approx(size / 2, rel=0.01)


def test_not_enough_space_refuses_before_start(qapp, window, tmp_path, monkeypatch):
    from bom_pdf.ui import main_window as mw

    (tmp_path / "orig").mkdir()
    pdf = make_marked_pdf(tmp_path / "orig" / "a.pdf")
    snap = snapshot(pdf)
    window.open_paths([str(pdf)])
    qt_wait(qapp, lambda: window.current_view() is not None and window.current_store() is not None)
    outdir = tmp_path / "out"
    outdir.mkdir()
    monkeypatch.setattr(mw.diskspace, "free_bytes", lambda f: 1024)
    started = []
    monkeypatch.setattr(window.service, "compress", lambda *a, **k: started.append("compress"))
    monkeypatch.setattr(window.service, "export_annotated", lambda *a, **k: started.append("export"))
    monkeypatch.setattr(window.service, "build_pages", lambda *a, **k: started.append("build"))
    monkeypatch.setattr(window.service, "images_to_pdf", lambda *a, **k: started.append("photos"))

    window.ask_compress = lambda doc: "submit"
    window.ask_output_path = lambda title, d: str(outdir / "c.pdf")
    assert window.compress_document() is None
    assert "여유 공간이 부족" in window.last_notice

    store = window.current_store()
    store.add({"type": "box", "page": 0, "rect": [72, 72, 200, 140]})
    window.ask_export_path = lambda d: str(outdir / "e.pdf")
    window.last_notice = None
    window.export_annotated()
    assert "여유 공간이 부족" in window.last_notice

    ed = window.open_page_editor()
    ed.ask_save_path = lambda title, d: str(outdir / "s.pdf")
    ed.save()
    assert ed.job_id is None and "여유 공간이 부족" in ed.last_notice
    ed.force_close()
    assert started == [] and os.listdir(outdir) == [] and snapshot(pdf) == snap


# --------------------------------------------------------------------------- 쓰다가 실패(네트워크 끊김·공간 부족 흉내)


def _failing_save(real_save):
    def save(self, filename, *a, **k):
        if str(filename).endswith(TMP_SUFFIX):
            with open(filename, "wb") as f:  # 반쯤 쓰다가
                f.write(b"%PDF-1.7 partial")
            raise OSError(errno.ENOSPC, "simulated network/disk failure")
        return real_save(self, filename, *a, **k)
    return save


@pytest.mark.parametrize("what", ["build", "compress", "export", "photos"])
def test_write_failure_mid_save_keeps_original_and_existing_output(tmp_path, monkeypatch, what):
    from PIL import Image

    from bom_pdf.engine import build, images
    from bom_pdf.engine import pdf as epdf
    from bom_pdf.services import imagetopdf as itp

    src_dir = tmp_path / "src"
    src_dir.mkdir()
    src = make_marked_pdf(src_dir / "a.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "결과.pdf"
    out.write_bytes(b"existing output - must stay")  # 사용자가 덮어쓰기로 고른 기존 파일
    snap = snapshot(src)
    monkeypatch.setattr(pymupdf.Document, "save", _failing_save(pymupdf.Document.save))
    with pytest.raises(OSError):
        if what == "build":
            build.build({"sources": [{"path": str(src), "password": None, "sha256": sha256(src)}],
                         "outputs": [{"path": str(out), "pages": [[0, 0, 0], [0, 1, 0]]}]})
        elif what == "compress":
            build.compress(str(src), None, sha256(src), str(out), "submit")
        elif what == "export":
            epdf.export_annotated(str(src), None, str(out), [], sha256(src))
        else:
            ph = src_dir / "p.jpg"
            Image.new("RGB", (400, 300), (10, 20, 30)).save(ph, "JPEG")
            snap = snapshot(src)
            images.build_pdf(itp.plan([itp.Photo(path=str(ph), width=400, height=300)], itp.Options(), str(out),
                                      [], None))
    assert out.read_bytes() == b"existing output - must stay"
    assert sorted(os.listdir(out_dir)) == ["결과.pdf"]  # 임시 파일 없음
    assert snapshot(src) == snap


def test_replace_failure_cleans_temp(tmp_path, monkeypatch):
    from bom_pdf.engine import build

    src = make_marked_pdf(tmp_path / "a.pdf")
    out_dir = tmp_path / "out"
    out_dir.mkdir()

    def boom(a, b):
        raise OSError(errno.ENETRESET, "simulated network drop at replace")

    monkeypatch.setattr(build, "replace_with_retry", boom)
    with pytest.raises(OSError):
        build.build({"sources": [{"path": str(src), "password": None, "sha256": None}],
                     "outputs": [{"path": str(out_dir / "x.pdf"), "pages": [[0, 0, 0]]}]})
    assert os.listdir(out_dir) == []


def test_default_export_dir_follows_redirected_documents(tmp_path):
    from bom_pdf.services import outputs

    onedrive_docs = tmp_path / "OneDrive" / "문서"
    onedrive_docs.mkdir(parents=True)
    orig = tmp_path / "case" / "a.pdf"
    assert outputs.default_export_dir(str(orig), None, str(onedrive_docs)) == str(onedrive_docs)
    gone = tmp_path / "OneDrive-unlinked" / "문서"  # OneDrive 연결을 끊어 폴더가 없는 경우
    assert outputs.default_export_dir(str(orig), None, str(gone)) == os.path.expanduser("~")


def test_about_shows_version_and_update_note(window):
    from bom_pdf import __version__

    text = window.about_text()
    assert f"bom-pdf {__version__}" in text and "덮어써" in text and "AGPL" in text
    assert window.act_about.text().startswith("bom-pdf 정보")
