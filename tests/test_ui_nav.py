"""1-2 화면 (offscreen): 썸네일·책갈피 이동, 검색 결과 이동·강조, 분할 보기, 단축키."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from conftest import qt_wait

WORD = "임대차보증금반환청구"


def _open(qapp, window, *paths):
    window.open_paths([str(p) for p in paths])
    qt_wait(qapp, lambda: all(t.view is not None for t in window.tabs_by_doc.values()))
    for _ in range(5):
        qapp.processEvents()
    return window.current_view()


def _settle(qapp, n=10):
    for _ in range(n):
        qapp.processEvents()


def test_thumbnail_click_navigates_and_current_page_is_highlighted(qapp, window, long_pdf):
    view = _open(qapp, window, long_pdf)
    did = view.doc.doc_id
    tv = window.sidebar.thumbnails(did)
    assert tv is not None and tv.isVisible()
    qt_wait(qapp, lambda: all(window.sidebar.cache.contains(did, p, tv.scale()) for p in tv.visible_pages()))
    # 보이는 썸네일만 요청한다 — 600쪽 전부가 아님
    assert len(window.sidebar.cache) < 60

    x, y, w, h = tv._rects[2]
    QTest.mouseClick(tv.viewport(), Qt.MouseButton.LeftButton, pos=QPoint(x + w // 2, y + h // 2))
    _settle(qapp)
    assert view.visible_pages()[0] == 2
    assert tv._current == view.current_page()

    view.go_to_page(450)  # 보기를 옮기면 썸네일도 따라 스크롤·강조
    _settle(qapp)
    assert tv._current == view.current_page() >= 449
    assert tv._current in tv.visible_pages()


def test_outline_click_navigates(qapp, window, korean_pdf):
    view = _open(qapp, window, korean_pdf)
    sb = window.sidebar
    qt_wait(qapp, lambda: sb.has_outline(view.doc.doc_id))
    sb.setCurrentIndex(1)
    assert sb.tree.topLevelItemCount() == 2
    child = sb.tree.topLevelItem(0).child(0)
    assert child.text(0) == "청구 취지"
    sb._on_outline_clicked(child)
    _settle(qapp)
    assert 2 in view.visible_pages()
    view.go_to_page(4)
    _settle(qapp)
    assert sb.tree.currentItem().text(0) == "회전 쪽"  # 현재 쪽의 책갈피가 선택된다


def test_search_results_navigate_and_highlight(qapp, window, korean_pdf):
    view = _open(qapp, window, korean_pdf)
    view.set_zoom(1.0)
    window.show_search()
    panel = window.search_panel
    assert window.search_dock.isVisible() and panel.query.hasFocus()
    panel.query.setText(WORD)
    panel.ignore_space.setChecked(True)
    QTest.keyClick(panel.query, Qt.Key.Key_Return)  # Enter = 검색
    qt_wait(qapp, lambda: not panel.running)
    assert [h.page for h in panel.model.hits] == [1, 2, 3, 4]
    assert "4건" in panel.status.text()
    assert view._hits is panel.hits_by_page  # 강조 자료를 참조로 받음

    panel.go_to_row(1)  # 줄바꿈으로 쪼개진 2쪽 결과
    _settle(qapp)
    assert 2 in view.visible_pages() and view._cur_hit.page == 2
    window.act_find_next.trigger()  # F3
    _settle(qapp)
    assert panel.current_row == 2 and 3 in view.visible_pages()
    window.act_find_prev.trigger()  # Shift+F3
    assert panel.current_row == 1
    QTest.keyClick(panel.query, Qt.Key.Key_Return)  # 같은 검색어 Enter = 다음 결과
    assert panel.current_row == 2
    view.grab()  # 강조를 실제로 그려 본다 (예외 없이)

    panel.ignore_space.setChecked(False)  # 옵션을 바꾸면 Enter 는 새 검색
    QTest.keyClick(panel.query, Qt.Key.Key_Return)
    qt_wait(qapp, lambda: not panel.running)
    assert [h.page for h in panel.model.hits] == [1, 4]


def test_new_search_replaces_running_one_in_panel(qapp, window, long_pdf):
    _open(qapp, window, long_pdf)
    panel = window.search_panel
    panel.query.setText("line")
    panel.start_search()
    first = panel.search_id
    panel.query.setText("page 59")
    panel.start_search()
    assert panel.search_id != first
    qt_wait(qapp, lambda: not panel.running)
    assert panel.model.hits and all(h.match == "page 59" for h in panel.model.hits)


def test_search_stop_button(qapp, window, long_pdf):
    _open(qapp, window, long_pdf)
    panel = window.search_panel
    panel.query.setText("page")
    panel.start_search()
    panel.cancel()
    qt_wait(qapp, lambda: not panel.running)
    assert "중지" in panel.status.text() or "처음" in panel.status.text()


def test_split_same_document_independent_scroll_and_zoom(qapp, window, long_pdf):
    left = _open(qapp, window, long_pdf)
    tab = window.tabs.currentWidget()
    window.act_split.trigger()
    _settle(qapp)
    right = tab.split_view
    assert right is not None and right.doc is left.doc and right.lane != left.lane
    right.setFocus()
    _settle(qapp)
    assert window.current_view() is right  # 포커스 받은 칸이 활성 보기
    window.act_zoom_in.trigger()
    assert right.zoom != left.zoom
    right.go_to_page(300)
    left.go_to_page(10)
    did = left.doc.doc_id
    qt_wait(qapp, lambda: all(left.cache.contains(did, p, left.render_scale()) for p in left.visible_pages())
            and all(right.cache.contains(did, p, right.render_scale()) for p in right.visible_pages()))
    assert 10 in left.visible_pages() and 300 in right.visible_pages()
    assert window.sidebar.thumbnails(did)._current == right.current_page()
    window.act_split.trigger()  # 다시 누르면 닫힘
    assert tab.split_view is None and window.current_view() is left


def test_split_other_document_and_closing_it(qapp, window, long_pdf, korean_pdf):
    _open(qapp, window, long_pdf, korean_pdf)
    window.tabs.setCurrentIndex(0)
    tab = window.tabs.currentWidget()
    window.toggle_split()
    combo = tab.split_combo
    assert combo.count() == 2
    combo.setCurrentIndex(1)
    combo.activated.emit(1)
    _settle(qapp)
    right = tab.split_view
    ko_id = window.tabs.widget(1).doc_id
    assert right.doc.doc_id == ko_id and window.current_view() is right
    assert window.sidebar.document.doc_id == ko_id  # 사이드바가 활성 칸 문서를 따라감
    qt_wait(qapp, lambda: right.cache.contains(ko_id, 0, right.render_scale()))
    window.close_tab(1)  # 오른쪽 칸 문서의 탭을 닫으면 분할도 닫힌다
    _settle(qapp)
    assert tab.split_view is None and window.current_view() is tab.view


def test_keyboard_shortcuts(qapp, window, long_pdf, sample_pdf):
    view = _open(qapp, window, long_pdf, sample_pdf)
    window.tabs.setCurrentIndex(0)
    window.activateWindow()
    view = window.current_view()
    view.set_fit("page")
    view.setFocus()
    _settle(qapp)
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    assert view.visible_pages()[0] == 1
    QTest.keyClick(view, Qt.Key.Key_PageDown)
    QTest.keyClick(view, Qt.Key.Key_PageUp)
    assert view.visible_pages()[0] == 1
    QTest.keyClick(view, Qt.Key.Key_End)
    _settle(qapp)
    assert view.doc.page_count - 1 in view.visible_pages()
    QTest.keyClick(view, Qt.Key.Key_Home)
    assert view.visible_pages()[0] == 0

    QTest.keyClick(view, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    assert window.page_spin.hasFocus()
    QTest.keyClicks(window.page_spin, "123")
    QTest.keyClick(window.page_spin, Qt.Key.Key_Return)
    _settle(qapp)
    assert 122 in view.visible_pages() and view.hasFocus()  # 이동 후 보기로 포커스

    z = view.zoom
    QTest.keyClick(view, Qt.Key.Key_Equal, Qt.KeyboardModifier.ControlModifier)
    assert view.zoom > z

    QTest.keyClick(view, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert window.search_dock.isVisible() and window.search_panel.query.hasFocus()

    view.setFocus()
    QTest.keyClick(view, Qt.Key.Key_Backslash, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentWidget().split_view is not None

    QTest.keyClick(view, Qt.Key.Key_PageDown, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentIndex() == 1
    QTest.keyClick(window.current_view() or window, Qt.Key.Key_PageUp, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.currentIndex() == 0
    QTest.keyClick(window.current_view(), Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    assert window.tabs.count() == 1


def test_f3_without_results_opens_search(qapp, window, sample_pdf):
    _open(qapp, window, sample_pdf)
    window.act_find_next.trigger()
    assert window.search_dock.isVisible()
