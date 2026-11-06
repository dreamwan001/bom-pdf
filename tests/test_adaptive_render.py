"""O1–O3: 확대 타일 렌더(화질·위치·깜빡임), 화면·RAM 에 맞춘 캐시 상한, 메모리 부족 때 보이는 것만, 환경 점검."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pymupdf
import pytest

from bom_pdf import selftest, sysinfo
from bom_pdf.engine import pdf
from bom_pdf.engine import worker as eng
from bom_pdf.ui import page_cache, page_view
from conftest import qt_wait

MB = 1024 * 1024


# --------------------------------------------------------------------------- 엔진: 타일 = 쪽 전체의 같은 자리


def test_tiles_are_pixel_identical_to_full_render_including_rotated_page(tmp_path):
    path = tmp_path / "t.pdf"
    d = pymupdf.open()
    for rot in (0, 90):
        p = d.new_page(width=595, height=842)
        p.insert_text((60, 120), "Tile seam check 0123456789", fontsize=28)
        p.draw_rect(pymupdf.Rect(100, 300, 400, 500), color=(1, 0, 0), width=3)
        p.set_rotation(rot)
    d.save(path)
    d.close()
    doc = pdf.open_pdf(str(path)).doc
    scale, t = 3.0, 256
    for page in (0, 1):
        full = pdf.render_page(doc, page, scale)
        dl = pdf.display_list(doc, page)
        n = 0
        for ty in range(-(-full.height // t)):
            for tx in range(-(-full.width // t)):
                tile = pdf.render_tile(dl, scale, tx, ty, t)
                assert (tile.x, tile.y) == (tx * t, ty * t)
                w, h = min(tile.width, full.width - tile.x), min(tile.height, full.height - tile.y)
                fs, ts = full.samples_mv, tile.samples_mv
                for y in range(h):
                    a = (tile.y + y) * full.stride + tile.x * 3
                    b = y * tile.stride
                    assert bytes(fs[a:a + w * 3]) == bytes(ts[b:b + w * 3]), (page, tx, ty, y)
                n += 1
        assert n > 20
    doc.close()


def test_worker_renders_tile_and_preview_items(events, sample_pdf):
    c = eng.EngineClient(events)
    c.start()
    try:
        c.open(1, str(sample_pdf))
        assert events.wait(lambda e: isinstance(e, eng.Opened)).status == "ok"
        c.want(1, 3.0, [(0, 1, 1), (0, 0.5)], 0)
        got = [events.wait(lambda e: isinstance(e, eng.PageImage)) for _ in range(2)]
        tile = next(e for e in got if e.tile is not None)
        prev = next(e for e in got if e.tile is None)
        assert tile.scale == 3.0 and tile.tile == (1, 1, eng.TILE_PX, eng.TILE_PX)
        assert tile.width == round(595 * 3.0) - eng.TILE_PX and tile.height == eng.TILE_PX
        assert prev.scale == 0.5 and (prev.width, prev.height) == (round(595 * .5), 421)
    finally:
        c.shutdown()


# --------------------------------------------------------------------------- 보기: 타일 그림 = 쪽 전체 그림


def _open_zoomed(qapp, window, sample_pdf, zoom=2.0, page=3):
    window.open_paths([str(sample_pdf)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    view = window.current_view()
    view.set_zoom(zoom)
    view.go_to_page(page)
    hs = view.horizontalScrollBar()
    hs.setValue(hs.maximum() // 3)
    qapp.processEvents()
    return view


def _wait_sharp(qapp, view):
    qt_wait(qapp, lambda: (view.viewport().update(), view.coverage()[0] >= 1.0)[1])
    for _ in range(5):
        view.viewport().repaint()
        qapp.processEvents()


def test_tiled_view_paints_same_pixels_and_overlays_as_full_page(qapp, window, sample_pdf, monkeypatch):
    monkeypatch.setattr(page_view, "TILE_MIN_PX", 1_000_000)  # 200% 한 쪽(약 360만 화소)도 타일로
    view = _open_zoomed(qapp, window, sample_pdf)
    vis = list(view.visible_pages())
    assert vis and all(view.is_tiled(p) for p in vis)
    # 검색 강조(가짜 결과)와 선택이 타일 위에도 같은 자리에 그려지는지 함께 본다
    hit = SimpleNamespace(page=vis[0], rects=[(72, 85, 300, 104)])
    view.set_search_hits({vis[0]: [hit]}, hit)
    _wait_sharp(qapp, view)
    did, scale = view.doc.doc_id, view.render_scale()
    assert not view.cache.contains(did, vis[0], scale), "타일 모드에서 쪽 전체를 그리면 안 됨"
    assert any(k[3] is not None for k in view.cache._d)
    tiled = view.viewport().grab().toImage()

    monkeypatch.setattr(page_view, "TILE_MIN_PX", 10**12)  # 같은 자리를 쪽 전체로
    view._update_requests()
    qt_wait(qapp, lambda: all(view.cache.contains(did, p, scale) for p in view.visible_pages()))
    _wait_sharp(qapp, view)
    full = view.viewport().grab().toImage()
    assert tiled.size() == full.size()
    assert tiled == full, "타일로 그린 화면이 쪽 전체 그림과 다름"


def test_tiled_visible_tiles_never_rerequested_when_cache_is_small(qapp, window, sample_pdf, monkeypatch):
    """깜빡임 회귀(0.1.1)와 같은 조건을 타일에: 상한이 작아도 보이는 타일은 밀려나거나 다시 그려지지 않는다."""
    monkeypatch.setattr(page_view, "TILE_MIN_PX", 1_000_000)
    view = _open_zoomed(qapp, window, sample_pdf)
    _wait_sharp(qapp, view)
    view.cache.max_bytes = 8 * MB  # 보이는 타일·대체 그림보다 작게
    view.viewport().update()
    qapp.processEvents()
    _wait_sharp(qapp, view)
    did, scale = view.doc.doc_id, view.render_scale()
    vis_tiles = {(p, t) for p in view.visible_pages() for t in view._tiles(p)}
    assert len(vis_tiles) >= 2
    sent = []
    orig = view.service.request_pages
    view.service.request_pages = lambda *a, **k: (sent.append(a[2]), orig(*a, **k))
    try:
        end = time.monotonic() + 1.5
        while time.monotonic() < end:
            view.viewport().update()
            qapp.processEvents()
            time.sleep(0.005)
            assert all(view.cache.contains(did, p, scale, t) for p, t in vis_tiles), "보이는 타일이 밀려남"
    finally:
        view.service.request_pages = orig
    again = {(it[0], (it[1], it[2])) for s in sent for it in s if isinstance(it, tuple) and len(it) == 3}
    assert not again & vis_tiles, "보이는 타일을 다시 그리게 함(깜빡임)"


def test_zoom_shows_lower_resolution_fallback_until_tiles_arrive(qapp, window, sample_pdf, monkeypatch):
    monkeypatch.setattr(page_view, "TILE_MIN_PX", 1_000_000)
    view = _open_zoomed(qapp, window, sample_pdf, zoom=1.0)
    _wait_sharp(qapp, view)  # 100% 는 쪽 전체(타일 아님)
    view.set_zoom(4.0)
    qapp.processEvents()
    assert view.coverage()[1] == 0.0, "확대 직후 빈 칸 — 그 전 배율 그림을 늘려 보여야 함"
    _wait_sharp(qapp, view)


# --------------------------------------------------------------------------- O3 캐시 상한·메모리 부족


def test_budget_from_screen_and_ram_tier():
    fhd100, fhd125, here = (1920, 1080, 1.0), (1536, 864, 1.25), (2560, 1440, 1.5)
    assert page_cache.adaptive_budget([fhd100], 8) == 64 * MB
    assert page_cache.adaptive_budget([fhd125], 16) == 64 * MB
    assert page_cache.adaptive_budget([here], 8) == 128 * MB  # 8GB 등급 상한
    big = page_cache.adaptive_budget([here], 31.9)
    assert 128 * MB < big <= 384 * MB
    assert page_cache.adaptive_budget([fhd100, here], 31.9) == big  # 가장 큰 화면 기준
    # 폭 맞춤 한 쪽 기준으로 보이는 두 쪽 + 미리 그리기 1쪽 이상이 들어간다
    for scr, ram in ((fhd100, 8), (fhd125, 8), (here, 31.9)):
        one = page_cache.fit_width_page_bytes(scr[0], scr[2])
        assert page_cache.adaptive_budget([scr], ram) // one - 2 >= 1, scr


def test_simulation_env_vars(monkeypatch):
    monkeypatch.setenv(sysinfo.ENV_SIM_RAM_GB, "8")
    monkeypatch.setenv(sysinfo.ENV_SIM_SCREEN, "1920x1080@1,1536x864@1.25")
    assert sysinfo.total_ram_gb() == 8
    assert sysinfo.simulated_screens() == [(1920, 1080, 1.0), (1536, 864, 1.25)]
    assert page_cache.default_budget() == 64 * MB


def test_low_memory_requests_only_visible_pages(qapp, window, sample_pdf, monkeypatch):
    window.open_paths([str(sample_pdf)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    view = window.current_view()
    view.cache.max_bytes = 512 * MB
    view.go_to_page(4)
    qapp.processEvents()
    monkeypatch.setitem(sysinfo._low, "t", -1e9)
    assert len(view._wanted()) > len(view.visible_pages())  # 평소: 미리 그리기 포함
    monkeypatch.setenv(sysinfo.ENV_SIM_AVAIL_MB, "200")
    monkeypatch.setitem(sysinfo._low, "t", -1e9)
    assert view._wanted() == list(view.visible_pages())


# --------------------------------------------------------------------------- O1 환경 기록·점검


def test_environment_summary_has_no_document_names(tmp_path):
    env = sysinfo.environment([(1920, 1080, 1.25)], tmp_path)
    for key in ("os", "ram_total_gb", "ram_avail_gb", "cpus", "cpu", "screens", "data_free_gb", "install"):
        assert key in env
    assert env["screens"] == ["1920x1080@1.25"]
    assert env["ram_total_gb"] > 0 and env["data_free_gb"] is not None
    json.dumps(env, ensure_ascii=False)


def test_builtin_selftest_inputs_are_valid():
    with pymupdf.open(stream=selftest.tiny_pdf(), filetype="pdf") as d:
        assert d.page_count == 1
    from PIL import Image
    import io
    assert Image.open(io.BytesIO(selftest.tiny_png())).size == (160, 120)


def test_env_dialog_runs_selftest_and_copies(qapp):
    from PySide6.QtGui import QGuiApplication

    from bom_pdf.ui.env_dialog import EnvDialog

    dlg = EnvDialog(cache_mb=64)
    dlg.show()
    qt_wait(qapp, lambda: dlg.selftest is not None, timeout=90)
    rep = dlg.selftest
    # 시험 프로세스는 픽스처 때문에 PyMuPDF·Pillow 를 이미 불러왔다 — 그 항목만 빼고 본다(앱에서는 비어 있어야 함)
    assert (rep["render"], rep["scan"], rep["image_pdf"], rep["image_failed"]) == ("ok", "ok", "ok", 0), rep
    dlg.copy()
    text = QGuiApplication.clipboard().text()
    assert "자가 점검:" in text and "PDF 렌더 작업자: 열기 ok" in text and "Windows" in text and "64MB" in text
    dlg.close()


@pytest.mark.parametrize("menu_text", ["환경 점검…"])
def test_help_menu_has_env_check(window, menu_text):
    assert window.act_env_check.text() == menu_text
