"""DocumentService: 열기·쪽 정보·sha256(캐시)·D11 문구·암호·텍스트 추출."""
from __future__ import annotations

import sys

import pytest

from bom_pdf.services import document as ds
from conftest import SECRET_WORD, sha256


def test_service_layer_does_not_import_qt():
    import subprocess

    code = ("import sys; import bom_pdf.services.document, bom_pdf.services.recent; "
            "bad=[m for m in sys.modules if m.startswith(('PySide6','pymupdf','fitz'))]; print(bad)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True,
                         env={**__import__("os").environ, "PYTHONPATH": "src"})
    assert out.stdout.strip() == "[]"


def test_open_reports_pages_sizes_and_hash(service, events, sample_pdf):
    doc_id = service.open(str(sample_pdf))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    doc = ev.doc
    assert doc.doc_id == doc_id and ev.notice is None
    assert doc.page_count == 14
    assert doc.page_sizes[0] == pytest.approx((595, 842))
    assert doc.page_sizes[12] == pytest.approx((842, 595))
    assert doc.page_sizes[13] == pytest.approx((842, 595))  # 90도 회전 반영
    h = events.wait(lambda e: isinstance(e, ds.DocumentHashed))
    assert h.sha256 == sha256(sample_pdf)
    assert service.document(doc_id).sha256 == h.sha256


def test_hash_cache_reused_and_invalidated(tmp_path, sample_pdf, monkeypatch):
    from bom_pdf import storage

    calls = []
    real = storage.file_sha256
    monkeypatch.setattr(storage, "file_sha256", lambda p, *a, **k: calls.append(p) or real(p))
    cache = ds.HashCache(tmp_path / "hash_cache.json")
    a = cache.sha256_of(str(sample_pdf))
    b = ds.HashCache(tmp_path / "hash_cache.json").sha256_of(str(sample_pdf))  # 새로 읽어도 적중
    assert a == b == sha256(sample_pdf) and len(calls) == 1
    copy = tmp_path / "copy.pdf"
    copy.write_bytes(sample_pdf.read_bytes())
    cache.sha256_of(str(copy))
    copy.write_bytes(sample_pdf.read_bytes() + b"\n%extra")
    assert cache.sha256_of(str(copy)) == sha256(copy)  # 크기·mtime 이 바뀌면 다시 계산
    assert len(calls) == 3


@pytest.mark.parametrize("name, kind", [
    ("corrupt_zero_bytes.pdf", ds.CANNOT_OPEN),
    ("corrupt_not_pdf.pdf", ds.CANNOT_OPEN),
    ("corrupt_header_only.pdf", ds.CANNOT_OPEN),
    ("corrupt_encrypted.pdf", ds.NEEDS_PASSWORD),
    ("does_not_exist.pdf", ds.CANNOT_OPEN),
])
def test_open_failures_have_distinct_messages(service, events, fixtures_dir, name, kind):
    service.open(str(fixtures_dir / name))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpenFailed))
    assert ev.kind == kind
    if kind == ds.NEEDS_PASSWORD:
        assert ev.message == ds.MSG_NEEDS_PASSWORD
    else:
        assert ev.message.startswith("PDF를 열 수 없습니다.")
    assert service.engine.is_alive()


def test_damaged_specific_reasons(service, events, fixtures_dir):
    service.open(str(fixtures_dir / "corrupt_zero_bytes.pdf"))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpenFailed))
    assert "빈 파일" in ev.message
    service.open(str(fixtures_dir / "nope.pdf"))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpenFailed))
    assert "찾을 수 없습니다" in ev.message


def test_every_corrupt_fixture_opens_or_fails_cleanly(service, events, fixtures_dir):
    files = sorted(fixtures_dir.glob("corrupt_*.pdf"))
    assert len(files) == 8
    ids = {service.open(str(f)): f.name for f in files}
    results = {}
    while len(results) < len(ids):
        ev = events.wait(lambda e: isinstance(e, (ds.DocumentOpened, ds.DocumentOpenFailed)))
        did = ev.doc.doc_id if isinstance(ev, ds.DocumentOpened) else ev.doc_id
        results[ids[did]] = ev
    # 열린 것은 전 쪽 렌더 요청 — 쪽 오류는 괜찮지만 엔진은 살아 있어야 한다
    for did, name in ids.items():
        ev = results[name]
        if isinstance(ev, ds.DocumentOpened):
            if ev.doc.repaired:
                assert ev.notice == ds.MSG_REPAIRED
            service.request_pages(did, 1.0, list(range(ev.doc.page_count)), 0)
            for _ in range(ev.doc.page_count):
                events.wait(lambda e, did=did: isinstance(e, (ds.PageImage, ds.PageError)) and e.doc_id == did)
    assert service.engine.is_alive()
    assert isinstance(results["corrupt_encrypted.pdf"], ds.DocumentOpenFailed)
    assert isinstance(results["corrupt_no_xref.pdf"], ds.DocumentOpened)
    assert results["corrupt_no_xref.pdf"].doc.repaired


def test_password_retry(service, events, fixtures_dir):
    path = str(fixtures_dir / "corrupt_encrypted.pdf")
    doc_id = service.open(path)
    assert events.wait(lambda e: isinstance(e, ds.DocumentOpenFailed)).kind == ds.NEEDS_PASSWORD
    service.open(path, "wrong", doc_id=doc_id)
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpenFailed))
    assert ev.kind == ds.WRONG_PASSWORD and ev.message == ds.MSG_WRONG_PASSWORD
    service.open(path, "x", doc_id=doc_id)  # make_fixtures 의 user_pw
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    assert ev.doc.doc_id == doc_id and ev.doc.page_count == 14


def test_text_extraction_runs_in_worker(service, events, sample_pdf):
    doc_id = service.open(str(sample_pdf))
    events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    req = service.request_text(doc_id, 3)
    ev = events.wait(lambda e: isinstance(e, ds.PageText) and e.req_id == req)
    assert SECRET_WORD in ev.text
    req = service.request_text(doc_id, 999)  # 없는 쪽 → None, 작업자는 산다
    assert events.wait(lambda e: isinstance(e, ds.PageText) and e.req_id == req).text is None
    assert service.engine.is_alive()


def test_close_drops_pending_and_late_results(service, events, sample_pdf):
    doc_id = service.open(str(sample_pdf))
    events.wait(lambda e: isinstance(e, ds.DocumentOpened))
    service.close(doc_id)
    assert service.document(doc_id) is None
    service.request_pages(doc_id, 1.0, [0, 1], 0)  # 닫힌 문서 요청은 무시
    assert not [e for e in events.drain(0.5) if isinstance(e, ds.PageImage)]
