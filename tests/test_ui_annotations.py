"""1-3 화면 (offscreen): 종류별 주석 만들기, 목록 패널, 편집·삭제·되돌리기, 사본 공유, 내보내기, 원본 불변."""
from __future__ import annotations

import os
import shutil

import pymupdf
from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest

from bom_pdf.services import annotations as an
from bom_pdf.ui.page_view import TOOL_BOX, TOOL_HIGHLIGHT, TOOL_NOTE, TOOL_SELECT, TOOL_UNDERLINE
from conftest import qt_wait, sha256

WORD = "임대차보증금반환청구"


def _settle(qapp, n=10):
    for _ in range(n):
        qapp.processEvents()


def _open_ready(qapp, window, path):
    window.open_paths([str(path)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    view = window.current_view()
    qt_wait(qapp, lambda: window.store_of(view) is not None)  # sha256 → 저장소
    view.set_zoom(1.0)
    view.go_to_page(1)
    _settle(qapp)
    return view


def _word_box(qapp, window, view, page, word):
    """글자 상자를 받아 낱말의 첫·끝 글자 가운데(pt)."""
    qt_wait(qapp, lambda: window.text_layer.get(view.doc.doc_id, page) is not None)
    chars = window.text_layer.get(view.doc.doc_id, page)
    text = "".join(c[0] for c in chars)
    i = text.index(word)
    a, b = chars[i], chars[i + len(word) - 1]
    return ((a[1] + a[3]) / 2, (a[2] + a[4]) / 2), ((b[1] + b[3]) / 2, (b[2] + b[4]) / 2)


def _drag(qapp, view, page, a, b):
    pa = view.page_point_to_view(page, *a).toPoint()
    pb = view.page_point_to_view(page, *b).toPoint()
    vp = view.viewport()
    QTest.mousePress(vp, Qt.MouseButton.LeftButton, pos=pa)
    mid = QPoint((pa.x() + pb.x()) // 2, (pa.y() + pb.y()) // 2)
    for p in (mid, pb):
        QTest.mouseMove(vp, p)
        _settle(qapp, 2)
    QTest.mouseRelease(vp, Qt.MouseButton.LeftButton, pos=pb)
    _settle(qapp)


def _click(qapp, view, page, pt, double=False):
    p = view.page_point_to_view(page, *pt).toPoint()
    if double:
        QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=p)
    else:
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=p)
    _settle(qapp)


def test_create_each_type_and_list_panel(qapp, window, tmp_path, korean_pdf):
    src = tmp_path / "doc.pdf"
    shutil.copy(korean_pdf, src)
    view = _open_ready(qapp, window, src)
    store = window.store_of(view)
    asked = []
    window.ask_annotation = lambda title, note, tags, color: (asked.append(title) or
                                                               {"note": "합성 메모", "tags": ["쟁점1"], "color": color})

    # 형광펜: 글자를 끌어서
    a, b = _word_box(qapp, window, view, 1, WORD)
    window.set_tool(TOOL_HIGHLIGHT)
    assert window.annot_dock.isVisible()
    _drag(qapp, view, 1, a, b)
    qt_wait(qapp, lambda: len(store.items) == 1)
    hl = store.items[0]
    assert hl["type"] == an.HIGHLIGHT and hl["page"] == 1 and hl["text"] == WORD and len(hl["quads"]) == 1
    assert hl["color"] == an.DEFAULT_COLOR

    # 밑줄: 색을 바꾸고 두 줄에 걸쳐
    window.set_tool(TOOL_UNDERLINE)
    window.color_combo.setCurrentIndex(window.color_combo.findData("blue"))
    window._on_color_chosen(0)
    view.set_selected(None)
    a2, _ = _word_box(qapp, window, view, 1, "원고는")
    _, b2 = _word_box(qapp, window, view, 1, "피고는")
    _drag(qapp, view, 1, a2, b2)
    qt_wait(qapp, lambda: len(store.items) == 2)
    ul = store.items[1]
    assert ul["type"] == an.UNDERLINE and ul["color"] == "blue" and len(ul["quads"]) == 2 and "\n" in ul["text"]

    # 메모: 누른 자리 + 대화상자
    window.set_tool(TOOL_NOTE)
    _click(qapp, view, 1, (450, 300))
    assert len(store.items) == 3 and asked == ["메모"]
    note = store.items[2]
    assert note["type"] == an.NOTE and note["note"] == "합성 메모" and note["tags"] == ["쟁점1"]
    assert note["rect"][0] == round(450, 2) or abs(note["rect"][0] - 450) < 1.5

    # 메모 대화상자를 취소하면 만들지 않는다
    window.ask_annotation = lambda *a: None
    _click(qapp, view, 1, (450, 500))
    assert len(store.items) == 3

    # 사각형: 끌어서
    window.set_tool(TOOL_BOX)
    _drag(qapp, view, 1, (100, 400), (300, 500))
    assert len(store.items) == 4
    box = store.items[3]
    assert box["type"] == an.BOX and abs(box["rect"][0] - 100) < 1.5 and abs(box["rect"][3] - 500) < 1.5
    _drag(qapp, view, 1, (100, 600), (101, 601))  # 너무 작으면 만들지 않는다
    assert len(store.items) == 4

    # 목록 패널: 2쪽 묶음 하나에 4개, 종류·태그 거르기
    panel = window.annot_panel
    assert panel.store is store
    assert panel.tree.topLevelItemCount() == 1 and panel.tree.topLevelItem(0).text(0) == "2쪽 (4)"
    panel.type_filter.setCurrentIndex(panel.type_filter.findData(an.NOTE))
    assert panel.count_shown() == 1
    panel.type_filter.setCurrentIndex(0)
    panel.tag_filter.setCurrentIndex(panel.tag_filter.findData("쟁점1"))
    assert panel.count_shown() == 1
    panel.tag_filter.setCurrentIndex(0)
    assert panel.count_shown() == 4

    # 다시 시작해도(새 저장소) 그대로 — 파일에 저장됨
    assert [it["type"] for it in an.AnnotationStore(store.sha256, store.folder).items] == \
           [an.HIGHLIGHT, an.UNDERLINE, an.NOTE, an.BOX]


def test_select_edit_delete_undo_and_navigate(qapp, window, tmp_path, korean_pdf):
    src = tmp_path / "doc.pdf"
    shutil.copy(korean_pdf, src)
    view = _open_ready(qapp, window, src)
    store = window.store_of(view)
    box = store.add({"type": an.BOX, "page": 3, "rect": [100, 100, 300, 200]})
    hl = store.add({"type": an.HIGHLIGHT, "page": 1, "quads": [[72, 70, 300, 84]]})
    _settle(qapp)
    panel = window.annot_panel
    assert panel.count_shown() == 2

    # 목록에서 누르면 그 쪽으로 가고 고른다
    node = panel.find_item(box["id"])
    panel.tree.itemClicked.emit(node, 0)
    _settle(qapp)
    assert 3 in view.visible_pages() and view.selected_id == box["id"]

    # 보기에서 형광펜을 누르면 고른다(선택 도구), 빈 곳을 누르면 해제
    window.set_tool(TOOL_SELECT)
    view.go_to_page(1)
    _settle(qapp)
    _click(qapp, view, 1, (150, 77))
    assert view.selected_id == hl["id"] and panel.selected_id() == hl["id"]
    _click(qapp, view, 1, (500, 700))
    assert view.selected_id is None

    # 두 번 누르면 편집
    window.ask_annotation = lambda title, note, tags, color: {"note": "편집함", "tags": ["가"], "color": "red"}
    _click(qapp, view, 1, (150, 77), double=True)
    assert store.get(hl["id"])["note"] == "편집함" and store.get(hl["id"])["color"] == "red"

    # Delete → 삭제, Ctrl+Z → 되살림, Ctrl+Y → 다시 삭제
    view.set_selected(hl["id"])
    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Delete)
    _settle(qapp)
    assert store.get(hl["id"]) is None and panel.count_shown() == 1
    window.act_undo.trigger()
    _settle(qapp)
    assert store.get(hl["id"])["note"] == "편집함" and panel.count_shown() == 2
    window.act_undo.trigger()  # 편집도 되돌린다
    assert store.get(hl["id"])["note"] == ""
    window.act_redo.trigger()
    window.act_redo.trigger()
    assert store.get(hl["id"]) is None
    # 사각형 안쪽은 글자 선택 자리 — 테두리를 눌러야 고른다
    view.go_to_page(3)
    _settle(qapp)
    _click(qapp, view, 3, (200, 150))
    assert view.selected_id is None
    _click(qapp, view, 3, (100.5, 150))
    assert view.selected_id == box["id"]


def test_text_selection_copy(qapp, window, tmp_path, korean_pdf):
    src = tmp_path / "doc.pdf"
    shutil.copy(korean_pdf, src)
    view = _open_ready(qapp, window, src)
    a, b = _word_box(qapp, window, view, 1, WORD)
    window.set_tool(TOOL_SELECT)
    _drag(qapp, view, 1, a, b)
    assert view.selected_text() == WORD
    assert window.store_of(view).items == []  # 선택 도구는 주석을 만들지 않는다
    window.act_copy.trigger()
    assert QGuiApplication.clipboard().text() == WORD
    assert view.create_from_selection(an.HIGHLIGHT)  # 오른쪽 메뉴의 형광펜과 같은 길
    _settle(qapp)
    assert window.store_of(view).items[0]["text"] == WORD


def test_copies_share_annotations_in_window(qapp, window, tmp_path, sample_pdf):
    a = tmp_path / "a" / "x.pdf"
    b = tmp_path / "b" / "x 사본.pdf"
    for p in (a, b):
        p.parent.mkdir()
        shutil.copy(sample_pdf, p)
    window.open_paths([str(a), str(b)])
    qt_wait(qapp, lambda: len(window.stores) == 2)
    sa, sb = (window.stores[t.doc_id] for t in window.tabs_by_doc.values())
    assert sa is sb
    sa.add({"type": an.NOTE, "page": 0, "rect": [10, 10, 30, 30], "note": "공유"})
    _settle(qapp)
    tab_b = list(window.tabs_by_doc.values())[1]
    window.tabs.setCurrentWidget(tab_b)
    _settle(qapp)
    assert window.annot_panel.count_shown() == 1
    window.close_tab(0)
    assert window.library.get(sa.sha256) is sb  # 다른 탭이 아직 쓴다
    window.close_tab(0)
    assert window.library.get(sa.sha256) is None


def test_original_unchanged_after_annotate_and_export(qapp, window, tmp_path, korean_pdf):
    case = tmp_path / "case"
    case.mkdir()
    src = case / "원본.pdf"
    shutil.copy(korean_pdf, src)
    before = (sha256(src), src.stat().st_mtime_ns, sorted(os.listdir(case)))
    view = _open_ready(qapp, window, src)
    store = window.store_of(view)
    a, b = _word_box(qapp, window, view, 1, WORD)
    window.set_tool(TOOL_HIGHLIGHT)
    _drag(qapp, view, 1, a, b)
    window.ask_annotation = lambda *x: {"note": "합성", "tags": [], "color": "pink"}
    window.set_tool(TOOL_NOTE)
    _click(qapp, view, 1, (400, 400))
    window.set_tool(TOOL_BOX)
    _drag(qapp, view, 1, (80, 300), (200, 360))
    window.set_tool(TOOL_UNDERLINE)
    _drag(qapp, view, 1, a, b)
    assert len(store.items) == 4

    out_dir = tmp_path / "보낸곳"
    out_dir.mkdir()
    window.documents_dir = lambda: str(out_dir)
    defaults = []

    def ask(default):
        defaults.append(default)
        return default

    window.ask_export_path = ask
    window.act_export.trigger()
    qt_wait(qapp, lambda: window.last_export is not None, timeout=60)
    path, r = window.last_export
    assert defaults == [str(out_dir / "원본_메모.pdf")] and path == defaults[0]
    assert r["status"] == "ok" and r["added"] == 4
    assert r["by_type"] == {"Highlight": 1, "Text": 1, "Square": 1, "Underline": 1}
    chk = pymupdf.open(path)
    assert sum(1 for p in chk for _ in p.annots()) == 4
    chk.close()

    # 두 번째 내보내기: 같은 이름이 있으면 (2), 기본 폴더는 마지막 내보낸 폴더
    window.last_export = None
    window.act_export.trigger()
    qt_wait(qapp, lambda: window.last_export is not None, timeout=60)
    assert defaults[1] == str(out_dir / "원본_메모(2).pdf")

    # 원본 경로를 고르면 거절
    window.last_export = None
    window.ask_export_path = lambda default: str(src)
    window.act_export.trigger()
    _settle(qapp)
    assert window.last_export is None and "덮어쓸 수 없습니다" in window.last_notice

    assert (sha256(src), src.stat().st_mtime_ns, sorted(os.listdir(case))) == before
    assert view.doc.sha256 == before[0]


def test_edit_existing_while_drawing_tool_is_on(qapp, window, tmp_path, korean_pdf):
    """만들기 도구(형광펜·밑줄·메모·사각형)를 켠 채로도 기존 주석을 누르면 고르고, 두 번 누르면 편집한다."""
    src = tmp_path / "doc.pdf"
    shutil.copy(korean_pdf, src)
    view = _open_ready(qapp, window, src)
    store = window.store_of(view)
    hl = store.add({"type": an.HIGHLIGHT, "page": 1, "quads": [[72, 70, 300, 84]]})
    note = store.add({"type": an.NOTE, "page": 1, "rect": [400, 400, 420, 420], "note": "처음"})
    box = store.add({"type": an.BOX, "page": 1, "rect": [100, 500, 300, 600]})
    _settle(qapp)
    asked = []
    window.ask_annotation = lambda title, n, tags, color: (asked.append(title) or
                                                            {"note": f"편집{len(asked)}", "tags": [], "color": "green"})
    for tool in (TOOL_HIGHLIGHT, TOOL_UNDERLINE, TOOL_NOTE, TOOL_BOX):
        window.set_tool(tool)
        view.set_selected(None)
        _click(qapp, view, 1, (150, 77))  # 누르기 → 고르기
        assert view.selected_id == hl["id"], tool
    assert len(store.items) == 3 and asked == []  # 누르기만으로는 새 주석이 생기지 않음

    window.set_tool(TOOL_HIGHLIGHT)
    _click(qapp, view, 1, (150, 77), double=True)
    assert store.get(hl["id"])["note"] == "편집1" and store.get(hl["id"])["color"] == "green"

    window.set_tool(TOOL_NOTE)  # 메모 도구로 기존 메모를 두 번 눌러도 새 메모를 겹쳐 만들지 않는다
    _click(qapp, view, 1, (410, 410), double=True)
    assert len(store.items) == 3 and store.get(note["id"])["note"] == "편집2"
    assert asked == ["형광펜 편집", "메모 편집"]

    window.set_tool(TOOL_BOX)
    _click(qapp, view, 1, (100.5, 550), double=True)
    assert len(store.items) == 3 and store.get(box["id"])["note"] == "편집3"

    # 고르기를 풀면 색 상자는 새 주석 색으로 돌아온다
    window.set_tool(TOOL_SELECT)
    _click(qapp, view, 1, (500, 700))
    assert view.selected_id is None and window.color_combo.currentData() == window.current_color


def test_embedded_pdf_annotations_are_explained(qapp, window, tmp_path, korean_pdf, monkeypatch):
    """원본 PDF 에 이미 든 주석(다른 프로그램·메모 포함 PDF)은 편집할 수 없다는 것을 목록 패널에 알린다."""
    src = tmp_path / "with_annots.pdf"
    doc = pymupdf.open(korean_pdf)
    doc[0].add_highlight_annot(pymupdf.Rect(72, 70, 300, 84))
    doc[1].add_text_annot(pymupdf.Point(100, 100), "합성 메모")  # 팝업은 따로 세지 않는다
    doc.save(src)
    doc.close()
    view = _open_ready(qapp, window, src)
    assert view.doc.embedded_annots == 2
    assert "2개" in window.annot_panel.status.text() and not window.annot_panel.status.isHidden()
    assert window.store_of(view).items == []

    # 메모 글이 든 것은 목록에 [원본 메모]로 나오고(읽기만), 쪽 위 그 자리에서 찾을 수 있다
    notes = view.doc.embedded_notes
    assert [(n["page"], n["text"]) for n in notes] == [(1, "합성 메모")]
    panel = window.annot_panel
    assert panel.count_shown() == 1
    node = panel.tree.topLevelItem(0).child(0)
    assert node.text(0) == "[원본 메모] 합성 메모" and panel.selected_id() is None
    panel.tree.setCurrentItem(node)
    assert not panel.edit_btn.isEnabled() and not panel.delete_btn.isEnabled()
    panel._on_clicked(node)
    qapp.processEvents()
    assert 1 in view.visible_pages()
    r = notes[0]["rect"]
    pos = view.page_point_to_view(1, (r[0] + r[2]) / 2, (r[1] + r[3]) / 2).toPoint()
    assert view.embedded_note_at(pos) is notes[0]
    assert view.embedded_note_at(view.page_point_to_view(1, 400, 600).toPoint()) is None
    from PySide6.QtCore import QEvent
    from PySide6.QtGui import QHelpEvent
    from PySide6.QtWidgets import QApplication, QToolTip

    tip = QHelpEvent(QEvent.Type.ToolTip, pos, view.viewport().mapToGlobal(pos))  # 마우스를 올리면 메모 글
    assert QApplication.sendEvent(view.viewport(), tip) and "합성 메모" in QToolTip.text()
    QToolTip.hideText()  # 누르지 않고 지나가기만 해도 바로 뜨고, 벗어나면 닫힌다
    away = view.page_point_to_view(1, 400, 600).toPoint()
    QTest.mouseMove(view.viewport(), away)
    QTest.mouseMove(view.viewport(), pos)
    assert view._hover_note is notes[0] and "합성 메모" in QToolTip.text() and "width='420'" in QToolTip.text()
    calls = []  # 같은 메모 위에서 움직이는 동안에는 다시 띄우지 않는다(나타나는 중에 다시 띄우면 깜빡임)
    monkeypatch.setattr(QToolTip, "showText", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(QToolTip, "isVisible", lambda: False)  # Windows: 서서히 나타나는 동안의 답
    for dx in range(1, 6):
        QTest.mouseMove(view.viewport(), pos + QPoint(dx, 0))
    assert calls == []
    view._tip_shown_at -= 1.0  # 풍선말이 닫힌 뒤(휠·누름) 시간이 지나면 조금만 움직여도 다시 뜬다
    QTest.mouseMove(view.viewport(), pos + QPoint(6, 0))
    QTest.mouseMove(view.viewport(), pos + QPoint(7, 0))
    assert len(calls) == 1
    monkeypatch.undo()
    QTest.mouseMove(view.viewport(), away)
    assert view._hover_note is None
    got = []  # 두 번 누르면 전체 글 창(시험에서는 창 대신 기록)
    monkeypatch.setattr("bom_pdf.ui.main_window.show_embedded_note", lambda _parent, note: got.append(note))
    QTest.mouseDClick(view.viewport(), Qt.MouseButton.LeftButton, pos=pos)
    assert got == [notes[0]]
    panel._on_double(node)
    assert got == [notes[0]] * 2

    plain = tmp_path / "plain.pdf"
    shutil.copy(korean_pdf, plain)
    view2 = _open_ready(qapp, window, plain)
    assert view2.doc.embedded_annots == 0 and window.annot_panel.status.isHidden()
    assert view2.doc.embedded_notes == [] and window.annot_panel.count_shown() == 0
