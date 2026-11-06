"""1-4 화면 (offscreen): 페이지 편집 격자(고르기·끌어 순서·회전·삭제·되돌리기·저장), 병합·분할·추출 대화상자 흐름,
저장 안 한 편집 경고, 원본 경로 거절, 인쇄(PDF 출력 쪽 수), 용량 압축본, 모든 작업 뒤 원본 불변."""
from __future__ import annotations

import pymupdf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtPrintSupport import QPrinter
from PySide6.QtTest import QTest

from bom_pdf.services import document as ds
from conftest import make_marked_pdf, page_marks, qt_wait, snapshot


def _settle(qapp, n=10):
    for _ in range(n):
        qapp.processEvents()


def _setup(qapp, window, tmp_path, pages=8):
    src_dir = tmp_path / "사건 폴더"
    src_dir.mkdir()
    out_dir = tmp_path / "산출물"
    out_dir.mkdir()
    a = make_marked_pdf(src_dir / "기록A.pdf", pages, "A")
    window.documents_dir = lambda: str(out_dir)
    window.open_paths([str(a)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    qt_wait(qapp, lambda: window.current_view().doc.sha256 is not None)
    return a, out_dir


def _editor(qapp, window):
    ed = window.open_page_editor()
    assert ed is not None and window.page_editor is ed
    ed.confirm_discard = lambda: True
    _settle(qapp)
    return ed


def _center(ed, row) -> QPoint:
    return ed.grid.visualRect(ed.model.index(row, 0)).center()


def _click(qapp, ed, row, mod=Qt.KeyboardModifier.NoModifier):
    QTest.mouseClick(ed.grid.viewport(), Qt.MouseButton.LeftButton, mod, _center(ed, row))
    _settle(qapp, 3)


def _wait_job(qapp, ed, timeout=60):
    qt_wait(qapp, lambda: ed.job_id is None and ed.last_result is not None, timeout)
    return ed.last_result


def test_grid_select_drag_rotate_delete_undo_save(qapp, window, tmp_path):
    a, out_dir = _setup(qapp, window, tmp_path)
    before = snapshot(a)
    ed = _editor(qapp, window)
    assert ed.model.rowCount() == 8
    # 썸네일이 EDIT_LANE 으로 온다
    qt_wait(qapp, lambda: len(ed.thumbs) >= 4)

    # 고르기: 클릭, Ctrl+클릭, Shift+클릭
    _click(qapp, ed, 1)
    assert ed.grid.selected_rows() == [1]
    _click(qapp, ed, 3, Qt.KeyboardModifier.ControlModifier)
    assert ed.grid.selected_rows() == [1, 3]
    _click(qapp, ed, 3)  # 고른 것 위를 그냥 누르면(끌지 않고) 그것만 남는다
    assert ed.grid.selected_rows() == [3]
    _click(qapp, ed, 5, Qt.KeyboardModifier.ShiftModifier)
    assert ed.grid.selected_rows() == [3, 4, 5]

    # 끌어서 순서: A4·A5·A6 을 맨 앞(0번 쪽 왼쪽 절반)으로
    vp = ed.grid.viewport()
    start = _center(ed, 4)
    r0 = ed.grid.visualRect(ed.model.index(0, 0))
    target = QPoint(r0.left() + 4, r0.center().y())
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, start)
    for k in range(1, 6):
        QTest.mouseMove(vp, start + (target - start) * k / 5)
        _settle(qapp, 2)
    assert ed.grid.dragging and ed.grid.drop_index == 0
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, target)
    _settle(qapp)
    assert [r.page for r in ed.ops.pages] == [3, 4, 5, 0, 1, 2, 6, 7]
    assert ed.grid.selected_rows() == [0, 1, 2]

    # 회전(단축키는 창 안에서만) · 삭제(Delete)
    ed.grid.select_rows([0])
    ed.act_rot_right.trigger()
    ed.grid.select_rows([1])
    ed.act_rot_left.trigger()
    assert [r.rotate for r in ed.ops.pages[:2]] == [90, 270]
    ed.grid.setFocus()
    ed.grid.select_rows([7], 7)
    QTest.keyClick(ed.grid, Qt.Key.Key_Delete)
    _settle(qapp)
    assert len(ed.ops) == 7 and ed.ops.dirty
    # 되돌리기·다시 하기
    ed.act_undo.trigger()
    assert len(ed.ops) == 8
    ed.act_redo.trigger()
    assert len(ed.ops) == 7
    # 모든 쪽 삭제는 막는다
    ed.grid.select_rows(range(7))
    assert not ed.act_delete.isEnabled()

    # 원본 경로로 저장 → 거절(작업을 시작하지 않음)
    ed.ask_save_path = lambda title, default: str(a)
    ed.save()
    assert ed.job_id is None and ed.last_result is None and "덮어쓸 수 없습니다" in ed.last_notice

    # 새 파일로 저장 — 기본 이름 §3-3, 기본 폴더는 원본 폴더가 아님
    asked = []
    ed.ask_save_path = lambda title, default: asked.append(default) or default
    ed.open_check.setChecked(True)
    ed.save()
    assert ed.job_id is not None and not ed.save_btn.isEnabled()
    what, r = _wait_job(qapp, ed)
    out = out_dir / "기록A_편집.pdf"
    assert what == "save" and r["status"] == ds.EXPORT_OK and asked == [str(out)]
    assert page_marks(out) == ["MARK-A4", "MARK-A5", "MARK-A6", "MARK-A1", "MARK-A2", "MARK-A3", "MARK-A7"]
    with pymupdf.open(out) as d:
        assert [p.rotation for p in d] == [90, 270, 0, 0, 0, 90, 0]
    assert not ed.ops.dirty
    # 저장한 파일을 새 탭으로 연다
    qt_wait(qapp, lambda: window._tab_for_path(str(out)) is not None)
    # 같은 이름이 있으면 (2)
    assert window.default_output_path(str(a), "_편집").name == "기록A_편집(2).pdf"
    ed.close()
    _settle(qapp)
    assert window.page_editor is None
    assert snapshot(a) == before


def test_unsaved_warning_and_context_menu_actions(qapp, window, tmp_path):
    a, _out = _setup(qapp, window, tmp_path)
    ed = _editor(qapp, window)
    asked = []
    ed.confirm_discard = lambda: asked.append(1) or False
    ed.close()
    _settle(qapp)
    assert not asked and window.page_editor is None  # 바뀐 것 없으면 묻지 않음

    ed = _editor(qapp, window)
    ed.confirm_discard = lambda: asked.append(1) or False
    ed.grid.select_rows([2])
    ed.act_rot_right.trigger()
    assert ed.grid.menu_actions[0] is ed.act_rot_left and ed.act_extract in ed.grid.menu_actions
    ed.close()
    _settle(qapp)
    assert asked == [1] and window.page_editor is ed and ed.isVisible()  # 취소 → 남아 있다
    # 주 창을 닫으려 해도 묻는다
    asked.clear()
    window.close()
    _settle(qapp)
    assert asked == [1] and window.isVisible()
    ed.confirm_discard = lambda: True
    ed.close()
    _settle(qapp)
    assert window.page_editor is None


def test_merge_split_extract_flows_and_original_unchanged(qapp, window, tmp_path):
    a, out_dir = _setup(qapp, window, tmp_path, pages=6)
    b = make_marked_pdf(tmp_path / "사건 폴더" / "기록B.pdf", 3, "B", toc=False, links=False)
    snaps = {p: snapshot(p) for p in (a, b)}
    ed = _editor(qapp, window)
    # 병합: 열려 있지 않은 B 를 앞에 — 탭 없이 작업자에 열었다가 창을 닫을 때 닫는다
    ed.ask_merge = lambda: [("file", str(b)), ("current",)]
    ed.merge()
    qt_wait(qapp, lambda: len(ed.ops) == 9)
    assert [ed.ops.sources[r.src].name for r in ed.ops.pages[:4]] == ["기록B.pdf"] * 3 + ["기록A.pdf"]
    aux = next(iter(ed.aux_docs))
    assert window._tab_for_path(str(b)) is None and window.service.document(aux) is not None
    assert ed.model.data(ed.model.index(0, 0)) == "1  (B1)"  # 여러 문서면 원본 글자·쪽

    # 추출: 고른 쪽만, 편집 내용은 그대로
    ed.open_check.setChecked(False)
    ed.grid.select_rows([3, 0])
    ed.ask_save_path = lambda title, default: default
    ed.extract_selected()
    what, r = _wait_job(qapp, ed)
    assert what == "extract" and r["status"] == ds.EXPORT_OK
    assert page_marks(out_dir / "기록A_추출.pdf") == ["MARK-B1", "MARK-A1"]
    assert len(ed.ops) == 9

    # 분할: 잘못된 범위 → 안내만
    ed.last_result = None
    ed.ask_split = lambda: ("ranges", "1-3, 9-20")
    ed.ask_folder = lambda default: str(out_dir)
    ed.split()
    assert ed.job_id is None and "밖" in ed.last_notice
    # N쪽마다
    ed.ask_split = lambda: ("every", 4)
    ed.split()
    what, r = _wait_job(qapp, ed)
    assert what == "split" and r["status"] == ds.EXPORT_OK and len(r["outputs"]) == 3
    assert [len(page_marks(out_dir / f"기록A_{i}.pdf")) for i in (1, 2, 3)] == [4, 4, 1]
    # 책갈피: A 의 최상위(1·3·6쪽) = 가상 문서 4·6·9번째. 그 앞의 B 세 쪽은 첫 묶음
    ed.last_result = None
    ed.ask_split = lambda: ("bookmarks", None)
    assert ed.has_bookmarks()
    ed.split()
    what, r = _wait_job(qapp, ed)
    assert r["status"] == ds.EXPORT_OK and len(r["outputs"]) == 4
    names = ["기록A_1(2).pdf", "기록A_2(2).pdf", "기록A_3(2).pdf", "기록A_4.pdf"]  # 이미 있는 이름은 (2)
    assert [len(page_marks(out_dir / n)) for n in names] == [3, 2, 3, 1]
    assert page_marks(out_dir / names[0]) == ["MARK-B1", "MARK-B2", "MARK-B3"]

    ed.ops.mark_saved()
    ed.close()
    _settle(qapp)
    assert window.service.document(aux) is None  # 병합용으로 연 문서는 닫힘
    for p, s in snaps.items():
        assert snapshot(p) == s
    assert sorted(x.name for x in (tmp_path / "사건 폴더").iterdir()) == ["기록A.pdf", "기록B.pdf"]


def test_print_to_pdf_page_range_and_copies(qapp, window, tmp_path):
    a, out_dir = _setup(qapp, window, tmp_path)
    before = snapshot(a)
    target = out_dir / "printed.pdf"

    def fake_dialog(printer: QPrinter, n: int) -> bool:
        assert n == 8
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(target))
        printer.setPrintRange(QPrinter.PrintRange.PageRange)
        printer.setFromTo(2, 4)
        printer.setCopyCount(2)
        printer.setCollateCopies(True)
        return True

    window.ask_print = fake_dialog
    job = window.print_document()
    assert job is not None and job.dpi == 300 and not window.act_print.isEnabled()
    qt_wait(qapp, lambda: window.last_print is not None, 60)
    assert window.last_print == "ok" and window.print_job is None
    copies = 1 if job.printer.supportsMultipleCopies() else 2
    assert job.sequence == [1, 2, 3] * copies
    with pymupdf.open(target) as d:
        assert d.page_count == 3 * copies
        # 3쪽(원래 90° 회전 — 가로)은 세로 용지에 맞춰 돌려 찍는다: 모든 쪽이 세로
        assert all(p.rect.height > p.rect.width for p in d)
    assert snapshot(a) == before

    # 취소
    window.last_print = None
    target2 = out_dir / "cancel.pdf"

    def all_pages(printer: QPrinter, n: int) -> bool:
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(target2))
        return True

    window.ask_print = all_pages
    job = window.print_document()
    job.cancel()
    qt_wait(qapp, lambda: window.last_print is not None)
    assert window.last_print == "cancelled" and window.act_print.isEnabled()
    assert snapshot(a) == before


def test_compress_from_menu(qapp, window, tmp_path):
    a, out_dir = _setup(qapp, window, tmp_path)
    before = snapshot(a)
    window.ask_compress = lambda doc: "clean"
    window.ask_output_path = lambda title, default: str(a)  # 원본 → 거절
    assert window.compress_document() is None and "덮어쓸 수 없습니다" in window.last_notice
    asked = []
    window.ask_output_path = lambda title, default: asked.append(default) or default
    job = window.compress_document()
    assert job is not None
    qt_wait(qapp, lambda: window.last_compress is not None, 60)
    path, r = window.last_compress
    assert asked == [str(out_dir / "기록A_압축.pdf")]
    assert r["status"] in (ds.EXPORT_OK, ds.JOB_NOT_SMALLER)
    if r["status"] == ds.EXPORT_OK:
        assert r["after"] < r["before"] and page_marks(path) == [f"MARK-A{i}" for i in range(1, 9)]
        assert "줄임" in window.last_notice
    else:
        assert "줄지 않아" in window.last_notice and not (out_dir / "기록A_압축.pdf").exists()
    assert snapshot(a) == before
