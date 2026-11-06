"""1-5 화면 (offscreen): 사진 → PDF 창 — 추가(작업 프로세스가 정보 읽기)·자동 정렬·위로/아래로·돌리기·문서 사진 체크,
예상 용량 표시, 기본 저장 위치(사진 폴더 아님), 원본 경로 거절, 만들기 → 열기, 주 창 메뉴·끌어놓기, 원본 불변."""
from __future__ import annotations

import os

import pymupdf
from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent

from bom_pdf.services import document as ds
from conftest import make_marked_pdf, qt_wait, snapshot
from photo_fixtures import make_photo


def _setup(qapp, window, tmp_path):
    src = tmp_path / "카톡 사진"
    src.mkdir()
    out = tmp_path / "산출물"
    out.mkdir()
    window.documents_dir = lambda: str(out)
    files = [
        make_photo(src / "KakaoTalk_20241015_100000000_02.jpg", orientation=6),
        make_photo(src / "KakaoTalk_20241015_100000000_01.jpg"),
        make_photo(src / "KakaoTalk_20241015_100000000_03.png"),
    ]
    return src, out, files


def _wait_scan(qapp, dlg, n):
    qt_wait(qapp, lambda: dlg.scan_id is None and len(dlg.photos) == n, 60)


def test_dialog_flow_sort_reorder_rotate_make_open(qapp, window, tmp_path):
    src, out, files = _setup(qapp, window, tmp_path)
    snaps = {f: snapshot(f) for f in files}
    window.act_images.trigger()
    dlg = window.image_dialog
    assert dlg is not None and dlg.isVisible() and not dlg.make_btn.isEnabled()
    dlg.ask_image_files = lambda: [str(f) for f in files]
    dlg.btn_add.click()
    _wait_scan(qapp, dlg, 3)
    assert [p.name[-6:-4] for p in dlg.photos] == ["01", "02", "03"]  # 카톡 번호 순
    assert dlg.list.count() == 3 and dlg.list.item(0).text().startswith("1. KakaoTalk_20241015_100000000_01")
    assert all(p.thumb for p in dlg.photos) and dlg.bpp
    assert "3쪽" in dlg.estimate.text() and dlg.last_estimate > 0
    # 같은 사진은 다시 더하지 않는다
    assert dlg.add_paths([str(files[0])]) == 0

    # 쪽당 2장 → 2쪽, 예상 용량 갱신
    dlg.per_combo.setCurrentIndex(1)
    assert "2쪽" in dlg.estimate.text()
    # 3번째를 맨 위로, 돌리기, 문서 사진 체크
    dlg.select_rows([2])
    dlg.btn_up.click()
    dlg.btn_up.click()
    assert dlg.selected_rows() == [0] and dlg.photos[0].name.endswith("_03.png") and dlg.manual_order
    dlg.btn_right.click()
    assert dlg.photos[0].rotate == 90 and "↻90°" in dlg.list.item(0).text()
    dlg.list.item(1).setCheckState(Qt.CheckState.Checked)
    assert dlg.photos[1].doc_mode and not dlg.photos[0].doc_mode
    dlg.list.item(1).setCheckState(Qt.CheckState.Unchecked)
    # 자동 정렬로 되돌리면 카톡 순서
    dlg.auto_sort()
    assert [p.name[-6:-4] for p in dlg.photos] == ["01", "02", "03"] and not dlg.manual_order
    dlg.select_rows([2])
    dlg.btn_up.click()  # 01, 03, 02

    asked = {}

    def ask(title, default):
        asked["default"] = default
        return default

    window.ask_output_path = ask
    jid = dlg.make_pdf()
    assert jid is not None and not dlg.make_btn.isEnabled()
    qt_wait(qapp, lambda: dlg.job_id is None and dlg.last_result is not None, 60)
    r = dlg.last_result
    assert r["status"] == ds.EXPORT_OK and r["pages"] == 2 and r["images"] == 3
    # 기본 위치: 사진 폴더가 아니라 문서 폴더, 이름 `<사진 폴더>_사진.pdf`
    assert os.path.dirname(asked["default"]) == str(out)
    assert os.path.basename(asked["default"]) == "카톡 사진_사진.pdf"
    result = out / "카톡 사진_사진.pdf"
    with pymupdf.open(result) as d:
        assert d.page_count == 2
    # 만든 PDF 가 탭으로 열린다
    qt_wait(qapp, lambda: window.current_view() is not None and window.current_view().doc.path == str(result))
    # 원본 불변·사진 폴더에 아무것도 안 생김
    assert {f: snapshot(f) for f in files} == snaps
    assert sorted(os.listdir(src)) == sorted(f.name for f in files)
    # 다음 기본 이름은 (2)
    assert os.path.basename(dlg.default_output_path()) == "카톡 사진_사진(2).pdf"


def test_refuses_open_pdf_path_and_cancel(qapp, window, tmp_path):
    src, out, files = _setup(qapp, window, tmp_path)
    opened = make_marked_pdf(out / "열린 기록.pdf", 3, "A")
    before = snapshot(opened)
    window.open_paths([str(opened)])
    qt_wait(qapp, lambda: window.current_view() is not None)
    dlg = window.open_image_dialog([str(src)])  # 폴더째
    _wait_scan(qapp, dlg, 3)
    window.ask_output_path = lambda title, default: str(opened)
    assert dlg.make_pdf() is None and "덮어쓸 수 없습니다" in dlg.last_notice
    assert snapshot(opened) == before
    # 취소: 큰 사진 여러 장 → 첫 진행 뒤 취소 → 파일 없음
    big = make_photo(src / "big.jpg", (3000, 2000), noise=True)
    dlg.add_paths([str(big)])
    _wait_scan(qapp, dlg, 4)
    dlg.photos = dlg.photos + [dlg.photos[-1]] * 60
    dlg.q_quality.setChecked(True)
    target = out / "취소.pdf"
    window.ask_output_path = lambda title, default: str(target)
    jid = dlg.make_pdf()
    assert jid is not None
    qt_wait(qapp, lambda: dlg.progress.value() >= 1 or dlg.job_id is None, 60)
    dlg.cancel_btn.click()
    qt_wait(qapp, lambda: dlg.job_id is None, 60)
    assert dlg.last_result["status"] == ds.JOB_CANCELLED
    assert not target.exists() and not list(out.glob("*.tmp"))


def test_main_window_drop_images_opens_dialog(qapp, window, tmp_path):
    src, out, files = _setup(qapp, window, tmp_path)
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(str(f)) for f in files[:2]])
    ev = QDropEvent(QPointF(10, 10), Qt.DropAction.CopyAction, md, Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier)
    window.dropEvent(ev)
    qt_wait(qapp, lambda: window.image_dialog is not None)
    dlg = window.image_dialog
    _wait_scan(qapp, dlg, 2)
    assert window.tabs.count() == 0  # 사진은 PDF 탭으로 열지 않는다
    dlg.close()
    qt_wait(qapp, lambda: window.image_dialog is None)
