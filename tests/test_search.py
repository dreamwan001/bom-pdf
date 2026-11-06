"""검색·썸네일·책갈피: 쪽 단위 정규화 검색(직접 호출)과 작업자 프로토콜(흐름·취소·대체·우선순위)."""
from __future__ import annotations

import time

import pymupdf
import pytest

from bom_pdf.engine import pdf
from bom_pdf.engine import worker as eng
from bom_pdf.engine.textnorm import normalize_query

WORD = "임대차보증금반환청구"


# --------------------------------------------------------------------------- 쪽 단위 (작업자 없이)


def _hits(doc, query, ignore_space):
    n = normalize_query(query, ignore_space)
    return {i: pdf.search_page(doc, i, n, ignore_space, 100) for i in range(doc.page_count)}


def test_plain_search_does_not_cross_line_breaks_or_spaces(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    hits = _hits(doc, WORD, False)
    assert {i for i, h in hits.items() if h} == {1, 4}  # 붙여 쓴 것만


def test_ignore_space_matches_across_line_break_and_spaces(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    hits = _hits(doc, WORD, True)
    assert {i for i, h in hits.items() if h} == {1, 2, 3, 4}
    rects, before, match, after = hits[2][0]
    assert len(rects) == 2  # 두 줄에 걸친 일치 → 줄마다 사각형 하나
    assert rects[0][1] < rects[1][1]  # 윗줄이 먼저
    assert match.replace(" ", "") == WORD and before == "청구 취지는 "
    rects3 = hits[3][0][0]
    assert len(rects3) == 1  # 한 줄 안의 띄어 쓴 낱말은 사각형 하나


def test_query_spaces_are_ignored_too_when_option_is_on(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    assert _hits(doc, "임대차 보증금\n반환", True)[1]


def test_case_insensitive_and_context(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    h = _hits(doc, "NEEDLE", False)[3]
    assert [m for _, _, m, _ in h] == ["Needle", "needle", "NEEDLE"]
    rects, before, match, after = h[1]
    assert before.endswith("Needle ") and after == " NEEDLE"
    assert len(before) <= pdf.CONTEXT_CHARS + 1


def test_hit_rects_are_in_rotated_page_coordinates(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    page = doc[4]
    assert page.rotation == 90
    (rects, *_), = _hits(doc, WORD, False)[4]
    x0, y0, x1, y1 = rects[0]
    w, h = page.rect.width, page.rect.height  # 회전 반영 (842 x 595)
    assert 0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h
    assert (y1 - y0) > (x1 - x0)  # 90도 회전 → 가로 글줄이 세로로 선다


def test_outline_levels_and_pages(korean_pdf):
    doc = pymupdf.open(korean_pdf)
    assert pdf.outline(doc) == [(1, "처음", 0), (2, "청구 취지", 2), (1, "회전 쪽", 4)]


# --------------------------------------------------------------------------- 작업자 프로토콜


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


def _collect_search(events, sid, timeout=30.0):
    results = []
    while True:
        ev = events.wait(lambda e: isinstance(e, (eng.SearchResults, eng.SearchDone)) and e.search_id == sid,
                         timeout)
        if isinstance(ev, eng.SearchDone):
            return results, ev
        results.append(ev)


def test_worker_search_streams_and_finishes(client, events, korean_pdf):
    _open(client, events, 1, korean_pdf)
    sid = client.search(1, WORD, ignore_space=True)
    results, done = _collect_search(events, sid)
    hits = [h for r in results for h in r.hits]
    assert done.status == eng.SEARCH_DONE and done.hit_count == len(hits) == 4
    assert [h.page for h in hits] == [1, 2, 3, 4]
    assert results[-1].pages_done == results[-1].page_count == 5
    assert len(hits[1].rects) == 2


def test_worker_search_results_arrive_incrementally(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    sid = client.search(1, "page 5")  # "page 5", "page 50".."page 599" 쪽마다 30건 이하
    results, done = _collect_search(events, sid)
    assert len(results) >= 3  # 한 번에 다 오지 않고 조각으로
    done_pages = [r.pages_done for r in results]
    assert done_pages == sorted(done_pages) and done_pages[-1] == 600
    assert done.status == eng.SEARCH_DONE and done.hit_count == sum(len(r.hits) for r in results) > 0


def test_worker_search_truncates_at_limit(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    sid = client.search(1, "line")  # 600쪽 × 30 = 18,000 > 상한
    results, done = _collect_search(events, sid)
    assert done.status == eng.SEARCH_TRUNCATED and done.hit_count == eng.MAX_SEARCH_HITS
    assert sum(len(r.hits) for r in results) == eng.MAX_SEARCH_HITS


def test_worker_search_cancel(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    sid = client.search(1, "page")
    client.cancel_search(sid)
    results, done = _collect_search(events, sid)
    assert done.status == eng.SEARCH_CANCELLED
    assert not results or results[-1].pages_done < 600


def test_new_search_supersedes_old(client, events, long_pdf, korean_pdf):
    _open(client, events, 1, long_pdf)
    _open(client, events, 2, korean_pdf)
    old = client.search(1, "line")
    new = client.search(2, WORD)
    results, done = _collect_search(events, new)
    assert done.status == eng.SEARCH_DONE and done.hit_count == 2
    rest = events.drain(0.5)
    assert not [e for e in rest if isinstance(e, eng.SearchDone) and e.search_id == old]
    late = [e for e in rest if isinstance(e, eng.SearchResults) and e.search_id == old]
    assert len(late) <= 1  # 대체 명령이 닿기 전 한 조각까지만 — 그 뒤로는 진행하지 않는다


def test_render_is_not_blocked_by_running_search(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    sid = client.search(1, "page")  # 상한까지 수백 ms
    client.want(1, 1.5, [300], 0)
    order = []
    while True:
        ev = events.wait(lambda e: isinstance(e, eng.PageImage) or (isinstance(e, eng.SearchDone)
                                                                    and e.search_id == sid))
        order.append(type(ev).__name__)
        if isinstance(ev, eng.SearchDone):
            break
    assert order[0] == "PageImage"


def test_thumbnails_do_not_starve_visible_pages(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    client.want_thumbs(1, 0.2, list(range(200)), 0)
    client.want(1, 1.5, [10, 11, 12], 0)
    seen = []
    t0 = time.monotonic()
    while len([s for s in seen if s == "view"]) < 3:
        ev = events.wait(lambda e: isinstance(e, eng.PageImage))
        seen.append("thumb" if ev.lane == eng.THUMB_LANE else "view")
    assert seen.count("thumb") <= 2, seen  # 이미 그리던 썸네일 1~2장 뒤에 보이는 쪽이 먼저
    assert time.monotonic() - t0 < 10
    # 보이는 쪽이 다 그려진 뒤에는 썸네일이 계속 나온다
    ev = events.wait(lambda e: isinstance(e, eng.PageImage) and e.lane == eng.THUMB_LANE)
    assert ev.scale == 0.2


def test_two_view_lanes_of_same_document_keep_their_requests(client, events, sample_pdf):
    _open(client, events, 1, sample_pdf)
    client.want(1, 1.0, [0], 0, lane=1)
    client.want(1, 1.0, [5], 0, lane=2)  # 예전에는 문서 대기열이 하나라 [0] 요청을 지웠다
    got = {events.wait(lambda e: isinstance(e, eng.PageImage)).page for _ in range(2)}
    assert got == {0, 5}


def test_outline_command(client, events, korean_pdf, sample_pdf):
    _open(client, events, 1, korean_pdf)
    _open(client, events, 2, sample_pdf)
    client.request_outline(1)
    client.request_outline(2)
    a = events.wait(lambda e: isinstance(e, eng.Outline) and e.doc_id == 1)
    b = events.wait(lambda e: isinstance(e, eng.Outline) and e.doc_id == 2)
    assert [p for _, _, p in a.items] == [0, 2, 4] and b.items == []


def test_search_interrupted_when_worker_dies(client, events, long_pdf):
    _open(client, events, 1, long_pdf)
    sid = client.search(1, "page")
    client._debug_crash(1)
    done = events.wait(lambda e: isinstance(e, eng.SearchDone) and e.search_id == sid)
    assert done.status in (eng.SEARCH_INTERRUPTED, eng.SEARCH_TRUNCATED)
    events.wait(lambda e: isinstance(e, eng.WorkerRestarted))
