"""렌더 작업자 프로토콜: 최신 요청만 유효, 보낸 쪽 중복 방지, 급사 후 재기동."""
from __future__ import annotations

import time

import pytest

from bom_pdf.engine import worker as eng
from conftest import Events


@pytest.fixture
def client(events):
    c = eng.EngineClient(events)
    c.start()
    yield c
    c.shutdown()


def _open(client, events, doc_id, path):
    client.open(doc_id, str(path))
    ev = events.wait(lambda e: isinstance(e, eng.Opened) and e.doc_id == doc_id)
    assert ev.status == "ok"
    return ev


def test_render_returns_rgb_pixels(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    client.want(1, 1.0, [0], 0)
    ev = events.wait(lambda e: isinstance(e, eng.PageImage))
    assert (ev.page, ev.width, ev.height) == (0, 595, 842)
    assert len(ev.data) == ev.stride * ev.height and ev.stride >= ev.width * 3


def test_newer_request_replaces_older_queue(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    # 무거운 배율로 앞 요청 12쪽, 곧바로 새 요청 1쪽 → 앞 요청은 많아야 1~2쪽만 그려진다
    client.want(1, 4.0, list(range(12)), 0)
    client.want(1, 4.0, [13], 0)
    events.wait(lambda e: isinstance(e, eng.PageImage) and e.page == 13)
    rest = [e for e in events.drain(1.0) if isinstance(e, eng.PageImage)]
    stale = [e.page for e in rest if e.page != 13]
    assert len(stale) <= 2, stale


def test_in_flight_page_not_rendered_twice_but_rerendered_after_ack(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    client.want(1, 1.0, [0], 0)
    first = events.wait(lambda e: isinstance(e, eng.PageImage))
    client.want(1, 1.0, [0], 0)  # ack 0 < seq → 이미 보낸 것(도착 예정)으로 보고 건너뜀
    assert not [e for e in events.drain(0.6) if isinstance(e, eng.PageImage)]
    client.want(1, 1.0, [0], first.seq)  # UI 가 받아 처리했고(ack) 그래도 원하면 다시 그린다(캐시에서 밀려남)
    again = events.wait(lambda e: isinstance(e, eng.PageImage))
    assert again.page == 0 and again.seq > first.seq


def test_other_scale_is_a_different_request(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    client.want(1, 1.0, [0], 0)
    events.wait(lambda e: isinstance(e, eng.PageImage) and e.scale == 1.0)
    client.want(1, 2.0, [0], 0)
    ev = events.wait(lambda e: isinstance(e, eng.PageImage) and e.scale == 2.0)
    assert ev.width == 1190


def test_many_renders_trigger_store_trim_without_dying(client, events, sample_pdf):
    from bom_pdf.engine import pdf

    _open(client, events, 1, sample_pdf)
    pid = client.pid
    n = 0
    for scale in (0.5, 0.6):  # 14쪽 × 2 배율 = 28회 > TRIM_EVERY
        client.want(1, scale, list(range(14)), 0)
        for _ in range(14):
            events.wait(lambda e: isinstance(e, eng.PageImage))
            n += 1
    assert n > pdf.TRIM_EVERY
    assert client.pid == pid and client.is_alive()
    assert not [e for e in events.drain(0.3) if isinstance(e, eng.WorkerRestarted)]


def test_bad_page_reports_error_and_worker_survives(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    client.want(1, 1.0, [500], 0)
    assert events.wait(lambda e: isinstance(e, eng.PageError)).page == 500
    client.want(1, 1.0, [1], 0)
    assert events.wait(lambda e: isinstance(e, eng.PageImage)).page == 1


def test_killed_worker_restarts_and_reopens_documents(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    old_pid = client.pid
    client._proc.kill()
    ev = events.wait(lambda e: isinstance(e, eng.WorkerRestarted))
    assert ev.culprit_doc is None and ev.pid != old_pid
    re = events.wait(lambda e: isinstance(e, eng.Opened))
    assert re.reopen and re.doc_id == 1 and re.status == "ok"
    client.want(1, 1.0, [2], 0)
    assert events.wait(lambda e: isinstance(e, eng.PageImage)).page == 2


def test_crash_blames_the_document_and_keeps_others(client, events, sample_pdf, fixtures_dir):
    _open(client, events, 1, sample_pdf)
    _open(client, events, 2, fixtures_dir / "corrupt_no_xref.pdf")
    client._debug_crash(2)
    ev = events.wait(lambda e: isinstance(e, eng.WorkerRestarted))
    assert ev.culprit_doc == 2
    re = events.wait(lambda e: isinstance(e, eng.Opened))
    assert re.doc_id == 1 and re.reopen
    assert not [e for e in events.drain(0.5) if isinstance(e, eng.Opened) and e.doc_id == 2]
    client.want(1, 1.0, [0], 0)
    assert events.wait(lambda e: isinstance(e, eng.PageImage)).doc_id == 1


def test_gives_up_after_repeated_crashes(sample_pdf):
    events = Events()
    c = eng.EngineClient(events)
    c.start()
    try:
        for i in range(c.RESTART_LIMIT):
            c._debug_crash(0)
            events.wait(lambda e: isinstance(e, eng.WorkerRestarted))
        c._debug_crash(0)
        ev = events.wait(lambda e: isinstance(e, eng.WorkerFailed))
        assert "다시 실행" in ev.message
        c.want(1, 1.0, [0], 0)  # 포기한 뒤 명령은 조용히 버려진다
        time.sleep(0.2)
    finally:
        c.shutdown()


def test_shutdown_stops_process(sample_pdf):
    events = Events()
    c = eng.EngineClient(events)
    c.start()
    proc = c._proc
    c.shutdown()
    assert not proc.is_alive() and proc.exitcode == 0
    assert not [e for e in events.drain(0.3) if isinstance(e, eng.WorkerRestarted)]
