"""1-6 단일 인스턴스·세션 복원.

- 세션 기록은 데이터 폴더(임시 BOM_PDF_HOME)에만, 경로·쪽·확대만. 원본·원본 폴더는 그대로.
- 다른 프로세스 시험: 실제 `python -m bom_pdf` 를 offscreen 으로 띄워 두 번째 실행이 경로를 넘기고 끝나는지,
  강제 종료 뒤 기록이 "running" 으로 남고 잠금이 낡은 것으로 풀리는지 본다.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from bom_pdf.services import session as ss
from conftest import make_text_pdf, qt_wait, snapshot

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- 저장소


def test_session_store_roundtrip_and_skips_unchanged(tmp_path):
    store = ss.SessionStore(tmp_path)
    assert store.load() is None
    tabs = [ss.TabState(str(tmp_path / "a.pdf"), 3, 1.25, "none"), ss.TabState(str(tmp_path / "b.pdf"))]
    assert store.save(tabs, 1) is True
    mtime = store.path.stat().st_mtime_ns
    assert store.save(tabs, 1) is False  # 바뀐 것 없음 → 쓰지 않음
    assert store.path.stat().st_mtime_ns == mtime
    s = ss.SessionStore(tmp_path).load()
    assert s.state == ss.STATE_RUNNING and s.abnormal and s.current == 1
    assert s.tabs == tabs
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(data) == {"schema_version", "state", "pid", "tabs", "current", "saved_at"}  # 경로·위치만
    store.save(tabs, 1, ss.STATE_CLOSED)
    assert not ss.SessionStore(tmp_path).load().abnormal


def test_session_damaged_or_bad_entries(tmp_path):
    store = ss.SessionStore(tmp_path)
    store.path.write_text("{broken", encoding="utf-8")
    assert store.load() is None
    assert (tmp_path / "session.json.damaged").exists()  # 손상본 보존
    store.path.write_text(json.dumps({"schema_version": 1, "state": "running", "tabs": [
        {"path": "x.pdf", "page": "7", "zoom": 1e9}, {"page": 2}, "junk", {"path": ""}]}), encoding="utf-8")
    s = store.load()
    assert s.tabs == [ss.TabState("x.pdf", 7, 1.0, "width")]
    store.path.write_text(json.dumps({"schema_version": 99, "state": "running", "tabs": []}), encoding="utf-8")
    assert store.load() is None  # 모르는 판은 무시


# --------------------------------------------------------------------------- 주 창 + 기록기


def _recorder(win, home):
    from bom_pdf.ui.session import SessionRecorder

    return SessionRecorder(win, ss.SessionStore(home))


def _wait_view(qapp, win, path):
    qt_wait(qapp, lambda: (t := win._tab_for_path(os.path.abspath(str(path)))) is not None and t.view is not None)
    return win._tab_for_path(os.path.abspath(str(path))).view


def test_recorder_saves_positions_debounced_and_marks_clean_close(qapp, window, tmp_path):
    (tmp_path / "docs").mkdir()
    a = make_text_pdf(tmp_path / "docs" / "a.pdf", pages=20)
    b = make_text_pdf(tmp_path / "docs" / "b.pdf", pages=5)
    before = [snapshot(a), snapshot(b)]
    home = tmp_path / "home"
    rec = _recorder(window, home)
    rec.start()
    assert ss.SessionStore(home).load().tabs == []  # 비정상 기록이 없으면 바로 기록 시작
    window.open_paths([str(a), str(b)])
    va = _wait_view(qapp, window, a)
    _wait_view(qapp, window, b)
    window.tabs.setCurrentIndex(0)
    va.set_zoom(1.5)
    va.go_to_page(9)
    qt_wait(qapp, lambda: (s := ss.SessionStore(home).load()) is not None and len(s.tabs) == 2
            and s.tabs[0].page == 9, timeout=10)  # 디바운스(1.5초) 뒤 저장
    s = ss.SessionStore(home).load()
    assert s.state == ss.STATE_RUNNING and s.current == 0
    assert s.tabs[0] == ss.TabState(os.path.abspath(str(a)), 9, 1.5, "none")
    assert s.tabs[1].path == os.path.abspath(str(b))
    window.close()  # 정상 종료 → closed
    s = ss.SessionStore(home).load()
    assert s.state == ss.STATE_CLOSED and not s.abnormal and len(s.tabs) == 2
    assert [snapshot(a), snapshot(b)] == before


def test_abnormal_session_offers_restore_and_skips_missing(qapp, window, tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    a = make_text_pdf(docs / "a.pdf", pages=20)
    gone = docs / "gone.pdf"
    before = snapshot(a)
    home = tmp_path / "home"
    ss.SessionStore(home).save([ss.TabState(str(gone), 2), ss.TabState(str(a), 12, 1.75, "none")], 1)
    asked, missing = [], []
    window.ask_restore = lambda s: asked.append(s) or True
    window.report_missing = lambda m: missing.extend(m)
    rec = _recorder(window, home)
    assert rec.pending_offer
    rec.save_now()  # 묻기 전에는 저장하지 않는다(지난 기록 보존)
    assert len(ss.SessionStore(home).load().tabs) == 2
    rec.start()
    va = _wait_view(qapp, window, a)
    qt_wait(qapp, lambda: va.current_page() == 12 and abs(va.zoom - 1.75) < 1e-3, timeout=10)
    assert len(asked) == 1 and len(asked[0].tabs) == 2
    assert [t.path for t in missing] == [str(gone)]
    assert window.tabs.count() == 1
    qt_wait(qapp, lambda: [t.path for t in ss.SessionStore(home).load().tabs] == [os.path.abspath(str(a))],
            timeout=10)
    assert snapshot(a) == before and not gone.exists()


def test_abnormal_session_declined_opens_nothing(qapp, window, tmp_path):
    a = make_text_pdf(tmp_path / "a.pdf", pages=3)
    home = tmp_path / "home"
    ss.SessionStore(home).save([ss.TabState(str(a), 1)], 0)
    window.ask_restore = lambda s: False
    rec = _recorder(window, home)
    rec.start()
    qt_wait(qapp, lambda: not rec.pending_offer)
    assert window.tabs.count() == 0
    assert ss.SessionStore(home).load().tabs == []  # 거절 → 새 기록


def test_clean_session_is_not_offered(qapp, window, tmp_path):
    a = make_text_pdf(tmp_path / "a.pdf", pages=3)
    home = tmp_path / "home"
    ss.SessionStore(home).save([ss.TabState(str(a), 1)], 0, ss.STATE_CLOSED)
    window.ask_restore = lambda s: pytest.fail("clean exit must not ask")
    rec = _recorder(window, home)
    assert not rec.pending_offer
    rec.start()
    qapp.processEvents()
    assert window.tabs.count() == 0


# --------------------------------------------------------------------------- 단일 인스턴스


def test_protocol_roundtrip_and_name_is_per_data_dir(tmp_path):
    from bom_pdf.ui import single_instance as si

    msg = si.encode_paths(["rel/x.pdf", "C:/한글 폴더/y.pdf"])
    assert msg.endswith(b"\n")
    got = si.decode_paths(msg.strip())
    assert got == [os.path.abspath("rel/x.pdf"), os.path.abspath("C:/한글 폴더/y.pdf")]
    assert si.decode_paths(b"{bad") is None and si.decode_paths(b'{"paths": 3}') is None
    assert si.server_name(tmp_path / "a") != si.server_name(tmp_path / "b")
    assert si.server_name(tmp_path / "a") == si.server_name(tmp_path / "a")


def _env(home: Path) -> dict:
    env = {**os.environ, "BOM_PDF_HOME": str(home), "QT_QPA_PLATFORM": "offscreen",
           "PYTHONPATH": str(ROOT / "src")}
    env.pop("BOM_PDF_FIRST_PAGE_MARK", None)
    return env


def _kill_tree(proc: subprocess.Popen) -> None:
    """강제 종료(급사 흉내). venv 의 python.exe 는 진짜 인터프리터를 자식으로 띄우는 실행기라 그 자식까지
    (그리고 렌더 작업자까지) 끝내고 다 죽을 때까지 기다린다 — 실행기만 죽이면 잠금을 쥔 자식이 잠깐 살아 있다."""
    import psutil

    try:
        root = psutil.Process(proc.pid)
        procs = [root, *root.children(recursive=True)]
    except psutil.Error:
        procs = []
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=20)
    proc.wait(timeout=20)


def _wait_file(pred, timeout=40.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return
        except (OSError, ValueError, AttributeError):
            pass
        time.sleep(0.1)
    raise TimeoutError


def test_second_launch_hands_paths_to_running_instance_then_crash_leaves_restorable_session(qapp, tmp_path):
    from bom_pdf.ui.single_instance import SingleInstance

    docs = tmp_path / "사건 폴더"
    docs.mkdir()
    a = make_text_pdf(docs / "a.pdf", pages=4)
    b = make_text_pdf(docs / "b.pdf", pages=4)
    before = [snapshot(a), snapshot(b)]
    home = tmp_path / "home"
    store = ss.SessionStore(home)
    first = subprocess.Popen([sys.executable, "-m", "bom_pdf", str(a)], env=_env(home), cwd=str(ROOT))
    try:
        _wait_file(lambda: [t.path for t in store.load().tabs] == [str(a)])
        # 두 번째 실행: 다른 현재 폴더에서 상대 경로로 → 주 인스턴스가 새 탭으로 열고 이 프로세스는 바로 끝난다
        t0 = time.monotonic()
        second = subprocess.run([sys.executable, "-m", "bom_pdf", "b.pdf"], env=_env(home), cwd=str(docs),
                                timeout=60)
        assert second.returncode == 0
        assert time.monotonic() - t0 < 30
        _wait_file(lambda: [t.path for t in store.load().tabs] == [str(a), str(b)])
        assert first.poll() is None  # 주 인스턴스는 그대로
        # 셋째: 파일 없이 실행해도 넘기고 끝난다(창만 앞으로)
        assert subprocess.run([sys.executable, "-m", "bom_pdf"], env=_env(home), cwd=str(ROOT),
                              timeout=60).returncode == 0
    finally:
        _kill_tree(first)
    s = store.load()
    assert s.abnormal and [t.path for t in s.tabs] == [str(a), str(b)]  # 급사 → 다음 실행에서 복원 제안 대상
    # 주인이 죽은 잠금은 낡은 것으로 풀린다
    inst = SingleInstance(home)
    assert inst.acquire() is True
    inst.release()
    assert [snapshot(a), snapshot(b)] == before  # 원본·원본 폴더(목록) 그대로


def test_lock_held_by_live_instance_is_not_taken(qapp, tmp_path):
    from bom_pdf.ui.single_instance import SingleInstance

    holder = subprocess.Popen(
        [sys.executable, "-c", "import sys, time; from PySide6.QtCore import QLockFile; "
         "l = QLockFile(sys.argv[1]); assert l.tryLock(0); print('locked', flush=True); time.sleep(60)",
         str(tmp_path / "instance.lock")], stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        assert SingleInstance(tmp_path).acquire() is False
    finally:
        _kill_tree(holder)
    inst = SingleInstance(tmp_path)
    assert inst.acquire() is True  # 죽은 뒤에는 잡힌다
    inst.release()
