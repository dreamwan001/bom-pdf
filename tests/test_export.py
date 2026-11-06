"""1-3 작업자: 글자 상자, 메모 포함 PDF 내보내기(새 파일, 표준 주석, 원본 불변), 원본의 기존 주석은 표시만."""
from __future__ import annotations

import os
import shutil

import pymupdf
import pytest

from bom_pdf.engine import pdf as engine_pdf
from bom_pdf.services import annotations as an
from bom_pdf.services import document as ds
from conftest import sha256


def _open(service, events, path):
    doc_id = service.open(str(path))
    ev = events.wait(lambda e: isinstance(e, ds.DocumentOpened) and e.doc.doc_id == doc_id)
    events.wait(lambda e: isinstance(e, ds.DocumentHashed) and e.doc_id == doc_id)
    return ev.doc


def _chars(service, events, doc_id, page):
    req = service.request_chars(doc_id, page)
    return events.wait(lambda e: isinstance(e, ds.PageChars) and e.req_id == req).chars


def _export(service, events, doc_id, out, items):
    req = service.export_annotated(doc_id, str(out), items)
    assert req is not None
    return events.wait(lambda e: isinstance(e, ds.ExportDone) and e.req_id == req, timeout=60).result


def test_page_chars_rotation_and_selection(service, events, korean_pdf):
    doc = _open(service, events, korean_pdf)
    chars = _chars(service, events, doc.doc_id, 1)
    text = "".join(c[0] for c in chars)
    assert "임대차보증금반환청구" in text
    w, h = doc.page_sizes[1]
    assert all(0 <= c[1] <= c[3] <= w and 0 <= c[2] <= c[4] <= h for c in chars)
    i = text.index("임대차")
    rects, sel = an.selection(chars, i, i + len("임대차보증금반환청구") - 1)
    assert sel == "임대차보증금반환청구" and len(rects) == 1
    # 회전 쪽: 회전 반영 좌표(가로로 누운 쪽 크기 안)
    rot = _chars(service, events, doc.doc_id, 4)
    rw, rh = doc.page_sizes[4]
    assert rw > rh and rot and all(0 <= c[1] <= rw and 0 <= c[2] <= rh for c in rot)
    assert _chars(service, events, doc.doc_id, 999) is None  # 없는 쪽은 None(작업자 생존)
    assert service.engine.is_alive()


def _items(doc):
    store_items = [
        {"type": "highlight", "page": 0, "quads": [[72, 80, 300, 102]], "note": "강조 메모", "tags": ["쟁점1"]},
        {"type": "underline", "page": 1, "quads": [[72, 80, 200, 100], [72, 104, 150, 120]], "note": ""},
        {"type": "note", "page": 2, "rect": [400, 300, 420, 320], "note": "한글 메모 — 팝업", "tags": []},
        {"type": "box", "page": 3, "rect": [50, 50, 250, 150], "note": "", "tags": ["가", "나"]},
        {"type": "highlight", "page": 4, "quads": [[600, 60, 700, 80]], "note": "회전 쪽"},
    ]
    out = []  # AnnotationStore.export_items 와 같은 모양
    for it in store_items:
        n = an.normalize_item(it)
        d = {"type": n["type"], "page": n["page"], "color": an.color_rgb("green"), "note": n["note"],
             "tags": n["tags"], "label": an.TYPE_LABELS[n["type"]]}
        d.update({"quads": n["quads"]} if "quads" in n else {"rect": n["rect"]})
        out.append(d)
    return out


def test_export_writes_new_file_with_standard_annotations(service, events, tmp_path, korean_pdf):
    case = tmp_path / "case"
    case.mkdir()
    src = case / "원본.pdf"
    shutil.copy(korean_pdf, src)
    before = (sha256(src), src.stat().st_mtime_ns, sorted(os.listdir(case)))
    doc = _open(service, events, src)
    items = _items(doc)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    out = out_dir / "원본_메모.pdf"
    r = _export(service, events, doc.doc_id, out, items)
    assert r["status"] == ds.EXPORT_OK
    assert r["added"] == 5 and r["before"] == 0 and r["after"] == 5 and r["skipped"] == 0
    assert r["by_type"] == {"Highlight": 2, "Underline": 1, "Text": 1, "Square": 1}

    chk = pymupdf.open(out)  # 다시 열어 확인
    assert chk.page_count == 5
    pages = [chk[i] for i in range(chk.page_count)]  # 쪽 객체를 붙잡아 두어야 주석을 읽을 수 있다
    annots = {p.number: list(p.annots()) for p in pages}
    hl0 = annots[0][0]
    assert hl0.type[1] == "Highlight" and hl0.info["content"] == "강조 메모\n태그: 쟁점1"
    assert hl0.info["title"] == engine_pdf.EXPORT_AUTHOR
    assert hl0.colors["stroke"] == pytest.approx(an.color_rgb("green"), abs=0.01)
    assert annots[2][0].type[1] == "Text" and annots[2][0].info["content"] == "한글 메모 — 팝업"
    assert annots[3][0].type[1] == "Square" and annots[3][0].info["content"] == "태그: 가, 나"
    # 회전 쪽: 저장은 회전 반영 좌표 — 원래 좌표로 돌려 넣었으므로 다시 돌리면 같은 자리
    rot_page = chk[4]
    pts = [pymupdf.Point(v) * rot_page.rotation_matrix for v in next(rot_page.annots()).vertices]
    bbox = (min(q.x for q in pts), min(q.y for q in pts), max(q.x for q in pts), max(q.y for q in pts))
    assert bbox == pytest.approx((600, 60, 700, 80), abs=0.5)
    chk.close()
    # 원본 불변·원본 폴더에 아무것도 없음, 임시 파일 없음
    assert (sha256(src), src.stat().st_mtime_ns, sorted(os.listdir(case))) == before
    assert sorted(os.listdir(out_dir)) == ["원본_메모.pdf"]


def test_export_keeps_existing_annotations_and_counts(service, events, tmp_path, sample_pdf):
    src = tmp_path / "with_annot.pdf"
    d = pymupdf.open(sample_pdf)
    a = d[0].add_rect_annot(pymupdf.Rect(10, 10, 100, 100))
    a.set_colors(stroke=(1, 0, 0))
    a.update()
    d.save(src)
    d.close()
    doc = _open(service, events, src)
    items = [{"type": "box", "page": 0, "rect": [200, 200, 300, 300], "color": (0, 0, 1), "note": "",
              "tags": [], "label": "사각형"},
             {"type": "box", "page": 500, "rect": [0, 0, 10, 10], "color": (0, 0, 1), "note": "", "tags": []}]
    r = _export(service, events, doc.doc_id, tmp_path / "o.pdf", items)
    assert r["status"] == ds.EXPORT_OK and r["before"] == 1 and r["added"] == 1 and r["after"] == 2
    assert r["skipped"] == 1


def test_export_refuses_original_path_and_changed_original(service, events, tmp_path, sample_pdf):
    case = tmp_path / "case"
    case.mkdir()
    src = case / "s.pdf"
    shutil.copy(sample_pdf, src)
    h = sha256(src)
    doc = _open(service, events, src)
    item = [{"type": "note", "page": 0, "rect": [1, 1, 21, 21], "color": (1, 1, 0), "note": "x", "tags": []}]
    r = _export(service, events, doc.doc_id, src, item)
    assert r["status"] == ds.EXPORT_REFUSED and sha256(src) == h
    r = _export(service, events, doc.doc_id, str(src).upper(), item)  # 대소문자만 다른 같은 파일
    assert r["status"] == ds.EXPORT_REFUSED and sha256(src) == h
    doc.sha256 = "0" * 64  # 연 뒤 원본이 바뀐 것처럼
    out = case / "o.pdf"
    r = _export(service, events, doc.doc_id, out, item)
    assert r["status"] == ds.EXPORT_CHANGED and not out.exists()
    assert sorted(os.listdir(case)) == ["s.pdf"]  # 임시 파일도 없음


def test_existing_pdf_annotations_are_rendered(tmp_path, sample_pdf):
    """원본에 이미 있는 주석은 엔진이 쪽 그림에 그린다(표시만) — 렌더 함수 기본값 확인."""
    src = tmp_path / "a.pdf"
    d = pymupdf.open(sample_pdf)
    a = d[0].add_rect_annot(pymupdf.Rect(300, 300, 400, 400))
    a.set_colors(stroke=(1, 0, 0), fill=(1, 0, 0))
    a.update()
    d.save(src)
    d.close()
    r = engine_pdf.open_pdf(str(src))
    pix = engine_pdf.render_page(r.doc, 0, 1.0)
    assert pix.pixel(350, 350)[:3] == (255, 0, 0)
    r.doc.close()
