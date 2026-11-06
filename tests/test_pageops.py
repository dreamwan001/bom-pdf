"""1-4 서비스·작업 프로세스: PageOps 가상 문서, 저장(회전·삭제·순서), 병합, 분할 3방식, 추출, 책갈피·링크 다시 잇기,
되돌리기, 원본 불변, 원본 경로·바뀐 원본 거절, 메모 넣기, 용량 압축본, 작업 취소·급사."""
from __future__ import annotations

import os
import shutil

import pymupdf
import pytest

from bom_pdf.engine import build as engine_build
from bom_pdf.engine import jobs
from bom_pdf.engine.exportstatus import TMP_SUFFIX
from bom_pdf.services import document as ds
from bom_pdf.services import pageops as po
from conftest import make_marked_pdf, page_marks, snapshot


# --------------------------------------------------------------------------- 순수 PageOps


def _ops(n=8, extra=()):
    ops = po.PageOps(po.Source("C:/x/a.pdf", [(595, 842)] * n))
    for path, k in extra:
        ops.add_source(po.Source(path, [(842, 595)] * k))
    return ops


def test_edit_undo_redo_and_dirty():
    ops = _ops()
    assert not ops.dirty and not ops.can_undo()
    assert ops.rotate([0, 2], 90) and ops.pages[0].rotate == 90 and ops.pages[2].rotate == 90
    assert ops.rotate([0], -90) and ops.pages[0].rotate == 0
    assert ops.rotate([2], 270) and ops.pages[2].rotate == 0
    assert ops.page_size(1) == (595, 842)
    ops.rotate([1], 90)
    assert ops.page_size(1) == (842, 595)
    assert ops.delete([3, 4]) and [r.page for r in ops.pages] == [0, 1, 2, 5, 6, 7]
    assert not ops.delete(range(6))  # 모든 쪽은 못 지움
    assert ops.dirty
    assert ops.undo() == "삭제" and len(ops) == 8
    assert ops.redo() == "삭제" and len(ops) == 6
    ops.undo()
    ops.rotate([5], 180)  # 새 편집은 다시 하기 기록을 지운다
    assert not ops.can_redo()
    while ops.undo():
        pass
    assert not ops.dirty and ops.pages == [po.PageRef(0, i) for i in range(8)]
    ops.rotate([0], 90)
    ops.mark_saved()
    assert not ops.dirty
    assert not ops.rotate([0], 45) and not ops.rotate([99], 90)


def test_move_keeps_selection_order():
    ops = _ops(6)
    assert ops.move([4, 1], 0) == [0, 1] and [r.page for r in ops.pages] == [1, 4, 0, 2, 3, 5]
    ops = _ops(6)
    assert ops.move([0, 1], 6) == [4, 5] and [r.page for r in ops.pages] == [2, 3, 4, 5, 0, 1]
    ops = _ops(6)
    assert ops.move([1], 4) == [3] and [r.page for r in ops.pages] == [0, 2, 3, 1, 4, 5]  # 4번 자리 쪽 앞으로
    assert ops.move([3], 3) == [3] and ops.move([3], 4) == [3]  # 제자리 — 기록 안 함
    assert ops.undo() == "순서" and not ops.can_undo()
    assert [r.page for r in ops.pages] == list(range(6))


def test_merge_and_plan():
    ops = _ops(3, [("C:/y/b.pdf", 2)])
    assert ops.add_source(po.Source("c:/Y/B.pdf", [(1, 1)])) == 1  # 같은 경로(대소문자)는 같은 원본
    ops.rotate([0], 90)
    assert ops.merge([1, None])
    assert [(r.src, r.page, r.rotate) for r in ops.pages] == [(1, 0, 0), (1, 1, 0), (0, 0, 90), (0, 1, 0), (0, 2, 0)]
    assert ops.page_size(0) == (842, 595)
    job = ops.plan([("C:/out/o.pdf", [2, 0])], annots=[None, None])
    assert job["outputs"][0]["pages"] == [[0, 0, 90], [1, 0, 0]]
    assert [s["path"] for s in job["sources"]] == ["C:/x/a.pdf", "C:/y/b.pdf"]


def test_split_helpers():
    assert po.parse_ranges("1-3, 4-5, 6-", 8) == [[0, 1, 2], [3, 4], [5, 6, 7]]
    assert po.parse_ranges("2, 4~5", 8) == [[1], [3, 4]]
    for bad in ("", "0-2", "3-1", "1-9", "가"):
        with pytest.raises(ValueError):
            po.parse_ranges(bad, 8)
    assert po.split_every(7, 3) == [[0, 1, 2], [3, 4, 5], [6]]
    with pytest.raises(ValueError):
        po.split_every(7, 0)
    ops = _ops(8)
    outline = [(1, "a", 0), (2, "b", 1), (1, "c", 2), (1, "d", 5)]
    assert po.split_by_bookmarks(ops, {0: outline}) == [[0, 1], [2, 3, 4], [5, 6, 7]]
    ops.delete([2])  # 책갈피가 가리키던 쪽을 지우면 그 기준은 빠진다
    assert po.split_by_bookmarks(ops, {0: outline}) == [[0, 1, 2, 3], [4, 5, 6]]
    with pytest.raises(ValueError):
        po.split_by_bookmarks(ops, {0: [(2, "x", 3)]})
    assert po.split_names("C:/a/기록.pdf", 3) == ["기록_1.pdf", "기록_2.pdf", "기록_3.pdf"]


# --------------------------------------------------------------------------- 작업 프로세스로 저장


def _open(service, events, path):
    doc_id = service.open(str(path))
    doc = events.wait(lambda e: isinstance(e, ds.DocumentOpened) and e.doc.doc_id == doc_id).doc
    events.wait(lambda e: isinstance(e, ds.DocumentHashed) and e.doc_id == doc_id)
    return doc


def _source(doc):
    return po.Source(doc.path, list(doc.page_sizes), doc.password, doc.sha256, doc.doc_id)


def _run(service, events, job, timeout=60):
    job_id = service.build_pages(job)
    return events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == job_id, timeout=timeout).result


@pytest.fixture
def originals(tmp_path):
    """원본 폴더(쓰면 안 됨)와 산출물 폴더."""
    src_dir = tmp_path / "사건 폴더"
    src_dir.mkdir()
    out_dir = tmp_path / "산출물"
    out_dir.mkdir()
    a = make_marked_pdf(src_dir / "기록A.pdf", 8, "A")
    b = make_marked_pdf(src_dir / "기록B.pdf", 6, "B")
    return a, b, out_dir


def test_save_rotate_delete_reorder_keeps_toc_and_links(service, events, originals):
    a, _b, out_dir = originals
    before = snapshot(a)
    ops = po.PageOps(_source(_open(service, events, a)))
    ops.rotate([0], 90)
    ops.rotate([2], 90)  # 원래 90° 쪽 → 180°
    ops.delete([3])      # A4 (책갈피 "둘-1" 이 가리키던 쪽)
    ops.move([4], 0)     # A6(최상위 책갈피 "셋") 을 맨 앞으로
    out = out_dir / "기록A_편집.pdf"
    r = _run(service, events, ops.plan([(str(out), list(range(len(ops))))]))
    assert r["status"] == ds.EXPORT_OK and r["outputs"][0]["pages"] == 7
    assert page_marks(out) == ["MARK-A6", "MARK-A1", "MARK-A2", "MARK-A3", "MARK-A5", "MARK-A7", "MARK-A8"]
    with pymupdf.open(out) as d:
        assert [p.rotation for p in d] == [0, 90, 0, 180, 0, 0, 0]
        toc = d.get_toc(simple=True)
        # 지운 쪽 항목("둘-1")은 빠지고, 그 자식("둘-1-가")은 남은 조상 바로 아래로
        assert [(lvl, title, page) for lvl, title, page in toc] == [
            (1, "A 하나", 2), (2, "A 하나-1", 3), (1, "A 둘", 4), (2, "A 둘-1-가", 5), (1, "A 셋", 1)]
        links = d[2].get_links()  # 원래 2쪽(A2) → 지금 3번째 쪽
        goto = [lk for lk in links if lk["kind"] == pymupdf.LINK_GOTO]
        uri = [lk for lk in links if lk["kind"] == pymupdf.LINK_URI]
        assert len(goto) == 1 and goto[0]["page"] == 4  # A5 는 지금 5번째 쪽(0부터 4)
        assert len(uri) == 1 and uri[0]["uri"] == "https://example.invalid/"
    assert not list(out_dir.glob("*" + TMP_SUFFIX))
    assert snapshot(a) == before


def test_link_to_deleted_page_is_dropped(service, events, originals):
    a, _b, out_dir = originals
    ops = po.PageOps(_source(_open(service, events, a)))
    ops.delete([4])  # 링크 대상 A5
    out = out_dir / "x.pdf"
    assert _run(service, events, ops.plan([(str(out), list(range(len(ops))))]))["status"] == ds.EXPORT_OK
    with pymupdf.open(out) as d:
        kinds = [lk["kind"] for lk in d[1].get_links()]
    assert kinds == [pymupdf.LINK_URI]


def test_merge_two_documents_in_chosen_order(service, events, originals):
    a, b, out_dir = originals
    snaps = {p: snapshot(p) for p in (a, b)}
    ops = po.PageOps(_source(_open(service, events, a)))
    ops.delete([1, 2, 3, 4, 5, 6])  # A1, A8 만
    si = ops.add_source(_source(_open(service, events, b)))
    assert ops.merge([si, None])  # B 전체 다음 편집 중인 A
    ops.move([7], 0)  # A8 을 맨 앞으로
    out = out_dir / "합본.pdf"
    r = _run(service, events, ops.plan([(str(out), list(range(len(ops))))]))
    assert r["status"] == ds.EXPORT_OK
    assert page_marks(out) == ["MARK-A8", *[f"MARK-B{i}" for i in range(1, 7)], "MARK-A1"]
    with pymupdf.open(out) as d:
        assert d.page_count == 8 and d[3].rotation == 90  # B3 은 원래 회전 유지
        titles = [t for _l, t, _p in d.get_toc(simple=True)]
    # 원본마다 결과에 처음 나오는 차례(A8 이 맨 앞 → A 의 남은 책갈피 먼저, 그다음 B 전체)
    assert titles == ["A 하나", "B 하나", "B 하나-1", "B 둘", "B 둘-1", "B 둘-1-가", "B 셋"]
    for p, s in snaps.items():
        assert snapshot(p) == s


@pytest.mark.parametrize("mode", ["ranges", "every", "bookmarks"])
def test_split_three_modes(service, events, originals, mode):
    a, _b, out_dir = originals
    before = snapshot(a)
    doc = _open(service, events, a)
    ops = po.PageOps(_source(doc))
    if mode == "ranges":
        groups = po.parse_ranges("1-2, 3-5, 6-", len(ops))
    elif mode == "every":
        groups = po.split_every(len(ops), 3)
    else:
        outline = [(1, "", 0), (2, "", 1), (1, "", 2), (2, "", 3), (3, "", 4), (1, "", 5)]
        groups = po.split_by_bookmarks(ops, {0: outline})
    names = po.split_names(str(a), len(groups))
    outs = [(str(out_dir / n), g) for n, g in zip(names, groups)]
    r = _run(service, events, ops.plan(outs))
    assert r["status"] == ds.EXPORT_OK and len(r["outputs"]) == len(groups)
    marks = [page_marks(out_dir / n) for n in names]
    assert sum(marks, []) == [f"MARK-A{i}" for i in range(1, 9)]
    expect = {"ranges": [2, 3, 3], "every": [3, 3, 2], "bookmarks": [2, 3, 3]}[mode]
    assert [len(m) for m in marks] == expect
    assert names[0] == "기록A_1.pdf"
    assert snapshot(a) == before


def test_extract_subset(service, events, originals):
    a, _b, out_dir = originals
    before = snapshot(a)
    ops = po.PageOps(_source(_open(service, events, a)))
    ops.rotate([6], 180)
    out = out_dir / "기록A_추출.pdf"
    r = _run(service, events, ops.plan([(str(out), [6, 1])]))
    assert r["status"] == ds.EXPORT_OK
    assert page_marks(out) == ["MARK-A7", "MARK-A2"]
    with pymupdf.open(out) as d:
        assert [p.rotation for p in d] == [180, 0]
        assert d.get_toc(simple=True) == [[1, "A 하나-1", 2]]  # 부모(A1)가 빠진 하위 항목은 1단계로
    assert snapshot(a) == before


def test_refuses_original_path_and_changed_original(service, events, originals, tmp_path):
    a, _b, out_dir = originals
    doc = _open(service, events, a)
    ops = po.PageOps(_source(doc))
    ops.rotate([0], 90)
    before = snapshot(a)
    same_upper = str(a).upper() if os.name == "nt" else str(a)
    for target in (str(a), same_upper):
        r = _run(service, events, ops.plan([(target, [0, 1])]))
        assert r["status"] == ds.EXPORT_REFUSED
    # 분할 산출물 가운데 하나만 원본이어도 아무것도 만들지 않는다
    r = _run(service, events, ops.plan([(str(out_dir / "ok.pdf"), [0]), (str(a), [1])]))
    assert r["status"] == ds.EXPORT_REFUSED and not (out_dir / "ok.pdf").exists()
    assert snapshot(a) == before
    # 연 뒤에 원본이 바뀜(사본으로 흉내) → changed
    c = tmp_path / "사건 폴더" / "copy.pdf"
    shutil.copy(a, c)
    ops2 = po.PageOps(_source(_open(service, events, c)))
    with open(c, "ab") as f:
        f.write(b"\n% changed\n")
    r = _run(service, events, ops2.plan([(str(out_dir / "c.pdf"), [0])]))
    assert r["status"] == ds.EXPORT_CHANGED and not (out_dir / "c.pdf").exists()


def test_burn_in_sidecar_annotations_follow_pages(service, events, originals):
    a, _b, out_dir = originals
    ops = po.PageOps(_source(_open(service, events, a)))
    items = [
        {"type": "box", "page": 2, "rect": [100, 100, 200, 150], "color": (1, 0, 0), "note": "회전 쪽 메모",
         "tags": [], "label": "사각형"},
        {"type": "note", "page": 3, "rect": [300, 300, 320, 320], "color": (1, 1, 0), "note": "지울 쪽", "tags": [],
         "label": "메모"},
        {"type": "highlight", "page": 0, "quads": [[72, 80, 200, 104]], "color": (1, 1, 0), "note": "",
         "tags": ["쟁점"], "label": "형광펜"},
    ]
    ops.delete([3])
    ops.move([2], 0)  # 원래 회전 90° 쪽(A3)을 맨 앞으로
    ops.rotate([0], 90)
    out = out_dir / "메모.pdf"
    r = _run(service, events, ops.plan([(str(out), list(range(len(ops))))], annots=[items]))
    assert r["status"] == ds.EXPORT_OK and r["outputs"][0]["annots"] == 2
    with pymupdf.open(out) as d:
        p0 = d[0]
        assert p0.rotation == 180
        annots = list(p0.annots())
        assert [x.type[1] for x in annots] == ["Square"] and annots[0].info["content"] == "회전 쪽 메모"
        # 쪽을 돌려도 주석은 쪽 내용에 붙어 같이 돈다: 쪽 원래 좌표에서의 자리는 그대로
        unrot = annots[0].rect
        assert unrot.width > 0 and unrot.height > 0
        p1 = d[1]
        assert [x.type[1] for x in p1.annots()] == ["Highlight"]
        assert sum(len(list(p.annots())) for p in d) == 2


def test_compress_makes_smaller_valid_file(service, events, tmp_path):
    src_dir = tmp_path / "원본"
    src_dir.mkdir()
    src = src_dir / "scan.pdf"
    import random

    rnd = random.Random(7)
    doc = pymupdf.open()
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 1200, 1200), False)
    pix.set_rect(pix.irect, (255, 255, 255))
    for _ in range(4000):  # 잡음 사각형 — 잘 안 줄어드는 그림
        x, y = rnd.randrange(1180), rnd.randrange(1180)
        pix.set_rect(pymupdf.IRect(x, y, x + 20, y + 20), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    for i in range(3):
        p = doc.new_page(width=595, height=842)
        # 6인치에 1200px = 200DPI — 150DPI 로 줄이려면 정수 배가 아니어서 평균 축소로는 안 줄어든다(실제 회귀)
        p.insert_image(pymupdf.Rect(72, 72, 72 + 432, 72 + 432), pixmap=pix)
        p.insert_text((72, 500), f"MARK-S{i + 1}")
    doc.save(src)
    doc.close()
    before = snapshot(src)
    d = _open(service, events, src)
    out = tmp_path / "out" / "scan_압축.pdf"
    out.parent.mkdir()
    job_id = service.compress(d.doc_id, str(out), "submit")
    r = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == job_id, timeout=120).result
    assert r["status"] == ds.EXPORT_OK and r["after"] < r["before"] and r["pages"] == 3
    assert out.stat().st_size == r["after"]
    assert page_marks(out) == ["MARK-S1", "MARK-S2", "MARK-S3"]
    with pymupdf.open(out) as o:
        w = o[0].get_image_info()[0]["width"]
    assert 880 <= w <= 920  # 200DPI 1200px → 150DPI 900px
    assert snapshot(src) == before
    # 원본 경로로는 만들지 않는다
    job_id = service.compress(d.doc_id, str(src), "submit")
    r = events.wait(lambda e: isinstance(e, ds.JobDone) and e.job_id == job_id, timeout=60).result
    assert r["status"] == ds.EXPORT_REFUSED and snapshot(src) == before


def test_compress_not_smaller_leaves_no_file(tmp_path):
    src = tmp_path / "tiny.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "MARK-T1")
    doc.save(src, garbage=4, deflate=True, use_objstms=1)
    doc.close()
    out = tmp_path / "o" / "tiny_압축.pdf"
    out.parent.mkdir()
    r = engine_build.compress(str(src), None, None, str(out), "clean")
    assert r["status"] == ds.JOB_NOT_SMALLER and r["after"] >= r["before"]
    assert not out.exists() and not list(out.parent.iterdir())


def test_job_cancel_and_crash_remove_temp_files(tmp_path):
    from conftest import Events

    ev = Events()
    runner = jobs.JobRunner(ev)
    out = tmp_path / "x.pdf"
    job_id = runner.start("_sleep", ([str(out)],), [str(out)])
    ev.wait(lambda e: isinstance(e, jobs.JobProgress) and e.job_id == job_id)
    assert os.path.exists(str(out) + TMP_SUFFIX)
    runner.cancel(job_id)
    done = ev.wait(lambda e: isinstance(e, jobs.JobDone) and e.job_id == job_id)
    assert done.result["status"] == ds.JOB_CANCELLED
    assert not os.path.exists(str(out) + TMP_SUFFIX) and not out.exists()
    # 급사 → interrupted
    job_id = runner.start("_sleep", ([str(out)],), [str(out)])
    ev.wait(lambda e: isinstance(e, jobs.JobProgress) and e.job_id == job_id)
    runner._jobs[job_id]["proc"].kill()
    done = ev.wait(lambda e: isinstance(e, jobs.JobDone) and e.job_id == job_id)
    assert done.result["status"] == ds.EXPORT_INTERRUPTED and not os.path.exists(str(out) + TMP_SUFFIX)
