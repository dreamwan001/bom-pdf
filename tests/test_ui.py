"""주 창 (offscreen): 원본 불변, 손상 픽스처에서 앱 생존, 엔진 급사 시 그 문서만 오류."""
from __future__ import annotations

import os
import shutil

import pytest

from bom_pdf.services import document as ds
from conftest import qt_wait, sha256


def _settled(win) -> bool:
    for tab in win.tabs_by_doc.values():
        if tab.view is None and tab.status_label.text() == "여는 중…":
            return False
    return True


def _scroll_to_end(qapp, view) -> None:
    sb = view.verticalScrollBar()
    while sb.value() < sb.maximum():
        sb.setValue(sb.value() + max(1, view.viewport().height() // 2))
        qapp.processEvents()
    last = view.doc.page_count - 1
    qt_wait(qapp, lambda: view.cache.contains(view.doc.doc_id, last, view.render_scale())
            or last in view._errors)


def test_original_unchanged_after_open_render_scroll_zoom(qapp, window, tmp_path, sample_pdf):
    folder = tmp_path / "case"
    folder.mkdir()
    src = folder / "원본.pdf"
    shutil.copy(sample_pdf, src)
    before_hash, before_list, before_mtime = sha256(src), sorted(os.listdir(folder)), src.stat().st_mtime_ns

    window.open_paths([str(src)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    view = window.current_view()
    _scroll_to_end(qapp, view)
    view.zoom_in()
    view.set_fit("page")
    view.go_to_page(3)
    window.service.request_text(view.doc.doc_id, 3)
    # 1-2: 썸네일·책갈피·검색·분할도 원본을 읽기만 한다
    qt_wait(qapp, lambda: window.sidebar.has_outline(view.doc.doc_id))
    thumbs = window.sidebar.thumbnails(view.doc.doc_id)
    qt_wait(qapp, lambda: window.sidebar.cache.contains(view.doc.doc_id, 0, thumbs.scale()))
    window.search_panel.query.setText("synthetic")
    window.search_panel.start_search()
    qt_wait(qapp, lambda: not window.search_panel.running)
    assert window.search_panel.model.hits
    window.toggle_split()
    qt_wait(qapp, lambda: window.current_view() is not None and window.tabs.currentWidget().split_view is not None)
    qt_wait(qapp, lambda: view.doc.sha256 is not None)
    for _ in range(20):
        qapp.processEvents()

    assert sha256(src) == before_hash == view.doc.sha256
    assert src.stat().st_mtime_ns == before_mtime
    assert sorted(os.listdir(folder)) == before_list  # 원본 폴더에 아무것도 생기지 않음
    assert window.recent.list()[0] == str(src)


def test_app_survives_every_corrupt_fixture(qapp, window, fixtures_dir):
    files = sorted(str(p) for p in fixtures_dir.glob("corrupt_*.pdf"))
    window.open_paths(files)
    assert window.tabs.count() == len(files)
    qt_wait(qapp, lambda: _settled(window))
    opened = 0
    for tab in list(window.tabs_by_doc.values()):
        window.tabs.setCurrentWidget(tab)
        qapp.processEvents()
        if tab.view is not None:
            opened += 1
            _scroll_to_end(qapp, tab.view)
        else:
            assert tab.status_label.text() != "여는 중…"
    assert opened >= 2  # no_xref(복구)·byteflip 은 열린다 (작은 합성본에서는 truncated_tail 은 실패)
    enc = next(t for t in window.tabs_by_doc.values() if t.path.endswith("corrupt_encrypted.pdf"))
    assert ds.MSG_NEEDS_PASSWORD in enc.status_label.text()
    repaired = next(t for t in window.tabs_by_doc.values() if t.path.endswith("corrupt_no_xref.pdf"))
    assert repaired.banner.isVisibleTo(repaired) and repaired.banner.text() == ds.MSG_REPAIRED
    assert window.service.engine.is_alive()
    while window.tabs.count():
        window.close_tab(0)
    assert window.central.currentWidget() is window.empty


def test_engine_crash_marks_only_that_document(qapp, window, sample_pdf, fixtures_dir):
    window.open_paths([str(sample_pdf), str(fixtures_dir / "corrupt_no_xref.pdf")])
    qt_wait(qapp, lambda: all(t.view is not None for t in window.tabs_by_doc.values()))
    good, bad = list(window.tabs_by_doc.values())
    window.service.engine._debug_crash(bad.doc_id)
    qt_wait(qapp, lambda: bad.banner.text() == ds.MSG_ENGINE_CRASH)  # 복구 안내 → 엔진 오류로 바뀜
    assert bad.view._dead
    window.tabs.setCurrentWidget(good)
    good.view.set_zoom(2.0)  # 새 배율 → 재기동된 작업자가 그려야 한다
    qt_wait(qapp, lambda: good.view.cache.contains(good.doc_id, good.view.visible_pages()[0],
                                                   good.view.render_scale()))
    assert not good.banner.isVisibleTo(good)


def test_same_file_opens_once_and_zoom_controls(qapp, window, sample_pdf):
    window.open_paths([str(sample_pdf)])
    window.open_paths([str(sample_pdf)])
    assert window.tabs.count() == 1
    qt_wait(qapp, lambda: window.current_view() is not None)
    v = window.current_view()
    v.set_zoom(1.0)
    v.zoom_in()
    assert v.zoom == pytest.approx(1.1)
    v.zoom_out()
    v.zoom_out()
    assert v.zoom == pytest.approx(0.9)
    v.set_fit("width")
    assert v.fit_mode == "width"
    v.go_to_page(5)
    qapp.processEvents()
    assert 5 in v.visible_pages()


def test_visible_pages_never_evicted_when_cache_is_small(qapp, window, sample_pdf):
    """큰 화면(4K·배율 150%)에서는 보이는 두 쪽이 캐시 상한을 넘는다 — 서로 밀어내며 번갈아 깜빡이면 안 된다."""
    import time

    window.open_paths([str(sample_pdf)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    view = window.current_view()
    view.set_fit("page")
    view.go_to_page(1)
    qapp.processEvents()
    vis = list(view.visible_pages())
    scale = view.render_scale()
    qt_wait(qapp, lambda: all(view.cache.contains(view.doc.doc_id, p, scale) for p in vis))
    # 상한을 한 쪽 반 크기로 줄여 큰 화면 상황을 만든다 (두 쪽 이상 보이게 아래로 반 쪽 스크롤)
    one = view.cache.get(view.doc.doc_id, vis[0], scale).sizeInBytes()
    view.cache.max_bytes = int(one * 1.5)
    sb = view.verticalScrollBar()
    sb.setValue(sb.value() + view.viewport().height() // 2)
    qapp.processEvents()
    vis = list(view.visible_pages())
    assert len(vis) >= 2
    qt_wait(qapp, lambda: all(view.cache.contains(view.doc.doc_id, p, scale) for p in vis))

    sent = []
    orig = view.service.request_pages
    view.service.request_pages = lambda *a, **k: (sent.append(a[2]), orig(*a, **k))
    end = time.monotonic() + 1.5
    while time.monotonic() < end:
        view.viewport().update()
        qapp.processEvents()
        time.sleep(0.005)
        assert all(view.cache.contains(view.doc.doc_id, p, scale) for p in vis), "보이는 쪽이 캐시에서 밀려남"
    assert not any(set(s) & set(vis) for s in sent), "보이는 쪽을 다시 그리게 함(깜빡임)"
    view.service.request_pages = orig


def test_fit_modes_show_one_page_without_side_bands(qapp, window, long_pdf):
    """폭·쪽 맞춤: 양옆 회색 띠 없이 맞추고, 보던 쪽의 맨 위로 가서 위아래 쪽이 걸쳐 보이지 않는다."""
    window.open_paths([str(long_pdf)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    v = window.current_view()
    v.set_zoom(1.0)
    v.go_to_page(4)
    sb = v.verticalScrollBar()
    sb.setValue(sb.value() + v._rects[4][3] * 2 // 3)  # 4·5쪽 사이에 걸침
    qapp.processEvents()

    v.set_fit("page")
    qapp.processEvents()
    assert list(v.visible_pages()) == [4]
    x, y, w, h = v._rects[4]
    assert y == sb.value()  # 쪽 맨 위가 화면 맨 위(위 공간 0)
    vp = v.viewport()
    assert abs(h - vp.height()) <= 1 or abs(w - vp.width()) <= 1  # 화면 높이(또는 폭)를 꽉 채움

    window.resize(1000, 700)  # 창 크기가 바뀌어도 한 쪽만
    qapp.processEvents()
    assert list(v.visible_pages()) == [4]

    v.set_fit("width")
    qapp.processEvents()
    x, y, w, h = v._rects[4]
    assert x == 0 and abs(w - v.viewport().width()) <= 1  # 옆 띠 없음
    assert v.visible_pages()[0] == 4 and sb.value() == v._tops[4]  # 그 쪽 맨 위부터(위 공간 0)
    assert v._tops[5] - (v._tops[4] + v._rects[4][3]) == 4  # 쪽 사이 4px

    v.set_zoom(0.5)  # 배율을 직접 정하면 전처럼 여백
    qapp.processEvents()
    assert v._rects[4][0] >= 12 and v._tops[0] == 10 and v._tops[5] - (v._tops[4] + v._rects[4][3]) == 10
