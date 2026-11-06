"""실제 PageView 자동 스크롤 측정 (spike/scroll_test.py 를 앱 구조로 옮긴 것).

주 창을 띄워 PDF를 열고, 첫 쪽이 그려지면 16ms 타이머로 끝까지 스크롤한다.
UI 멈춤 = 타이머 틱 간격. 숫자만 출력한다(본문은 읽지도 출력하지도 않는다).

사용:
    .venv/Scripts/python.exe tools/measure_scroll.py <pdf> [--dpi 96|144] [--step 120] [--platform offscreen]
  --dpi   : 화면 배율. 96 = 100%, 144 = 150% (QT_SCALE_FACTOR 로 devicePixelRatio 를 바꾼다, 확대율 100%)
  --step  : 틱당 스크롤 양(장치 픽셀). 120 → 7,500px/s, 400 → 25,000px/s (Phase 0 와 같은 조건)
  --zoom  : 확대율(기본 1.0 = 100%). 첫 쪽은 기본 폭 맞춤으로 열린 뒤 이 값으로 바뀐다
  --search QUERY : 첫 쪽이 그려지면 검색 패널로 문서 전체 검색을 시작한다(스크롤과 동시에).
                   --search-loop 이면 스크롤이 끝날 때까지 검색을 되풀이한다. 검색어·결과 본문은 출력하지
                   않고 건수·시간만 기록한다. --ignore-space 는 공백·줄바꿈 무시 옵션
결과 한 줄 JSON 을 출력하고 spike/out/results/measure_scroll.jsonl 에 덧붙인다(Git 무시).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def sha256(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--dpi", type=int, default=96)
    ap.add_argument("--step", type=int, default=120)
    ap.add_argument("--platform", default="offscreen")
    ap.add_argument("--zoom", type=float, default=1.0, help="0 이면 기본(폭 맞춤) 그대로")
    ap.add_argument("--width", type=int, default=900)
    ap.add_argument("--height", type=int, default=1000)
    ap.add_argument("--search", default="", help="스크롤하면서 문서 전체 검색 (검색어는 출력하지 않음)")
    ap.add_argument("--ignore-space", action="store_true")
    ap.add_argument("--search-loop", action="store_true", help="스크롤이 끝날 때까지 검색 반복")
    ap.add_argument("--diagnose", action="store_true", help="35ms 넘는 멈춤 순간의 주 스레드 스택·GC 시간을 stderr 로")
    ap.add_argument("--cores", type=int, default=0, help="UI·작업자를 앞 N개 논리 CPU 에 묶기(엔트리 CPU 근사)")
    a = ap.parse_args()

    os.environ["QT_QPA_PLATFORM"] = a.platform
    os.environ["QT_SCALE_FACTOR"] = str(a.dpi / 96)
    os.environ.setdefault("BOM_PDF_HOME", tempfile.mkdtemp(prefix="bom-pdf-measure-"))
    sys.path.insert(0, str(ROOT / "src"))

    import psutil
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services.document import DocumentService
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    src = os.path.abspath(a.pdf)
    before = sha256(src)
    app = QApplication([sys.argv[0]])
    bridge = ServiceBridge()
    service = DocumentService(bridge)
    t_start = time.perf_counter()
    if a.cores:
        psutil.Process().cpu_affinity(list(range(a.cores)))  # spawn 하는 작업자도 물려받는다
    service.start()
    win = MainWindow(service, bridge, RecentDocuments())
    win.resize(a.width, a.height)
    win.show()
    win.open_paths([src])

    st = {"ticks": [], "frames": 0, "blank": 0, "t_first": None, "t0": None, "peak_ui": 0.0,
          "peak_worker": 0.0, "peak_total": 0.0, "done": False,
          "search_t": None, "search_times": [], "search_hits": [], "scroll_done": False}
    panel = win.search_panel

    def search_step():
        """검색 시작·완료 기록·반복. 틱마다 부른다."""
        if not a.search:
            return
        if st["search_t"] is not None and not panel.running:
            st["search_times"].append(time.perf_counter() - st["search_t"])
            st["search_hits"].append(len(panel.model.hits))
            st["search_t"] = None
        if st["search_t"] is None and (not st["search_times"] or (a.search_loop and not st["scroll_done"])):
            panel.query.setText(a.search)
            panel.ignore_space.setChecked(a.ignore_space)
            panel.start_search()
            st["search_t"] = time.perf_counter()
    ui_proc = psutil.Process()

    def sample_mem():
        ui = ui_proc.memory_info().rss / 2**20
        wk = 0.0
        if service.engine.pid:
            try:
                wk = psutil.Process(service.engine.pid).memory_info().rss / 2**20
            except psutil.Error:
                pass
        st["peak_ui"] = max(st["peak_ui"], ui)
        st["peak_worker"] = max(st["peak_worker"], wk)
        st["peak_total"] = max(st["peak_total"], ui + wk)

    from bom_pdf.services.document import EngineFailed, EngineRestarted
    st["restarts"] = 0

    def on_service(ev):
        if isinstance(ev, EngineRestarted):
            st["restarts"] += 1
        elif isinstance(ev, EngineFailed):
            st["engine_failed"] = True
            QTimer.singleShot(0, app.quit)  # 측정 중단 — 멈춘 채 기다리지 않는다
    bridge.received.connect(on_service)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)

    def tick():
        now = time.perf_counter()
        view = win.current_view()
        if view is None:
            return
        if not st.get("zoom_set"):
            st["zoom_set"] = True
            if a.zoom > 0:
                view.set_zoom(a.zoom)  # 확대율 고정 → render_scale = dpi / 72
        did, scale = view.doc.doc_id, view.render_scale()
        if st["t_first"] is None:
            if view.cache.contains(did, 0, scale):
                st["t_first"] = now - t_start
                st["t0"] = now
            return
        st["ticks"].append(now)
        search_step()
        sb = view.verticalScrollBar()
        step = max(1, round(a.step / view.devicePixelRatioF()))
        sb.setValue(min(sb.maximum(), sb.value() + step))
        st["frames"] += 1
        vis = view.visible_pages()
        if any(not view.cache.contains(did, p, scale) and p not in view._errors for p in vis):
            st["blank"] += 1
        if len(st["ticks"]) % 60 == 0:
            sample_mem()
        last = view.doc.page_count - 1
        if sb.value() >= sb.maximum() and (view.cache.contains(did, last, scale) or last in view._errors):
            st["scroll_done"] = True
            if not a.search or st["search_times"]:
                finish(view)

    def finish(view):
        timer.stop()
        sample_mem()
        ticks = st["ticks"]
        gaps = sorted(b - a_ for a_, b in zip(ticks, ticks[1:]))

        def q(f):
            return round(gaps[min(len(gaps) - 1, int(len(gaps) * f))] * 1000, 1)

        res = {
            "file": os.path.basename(src), "pages": view.doc.page_count, "dpi": a.dpi,
            "dpr": view.devicePixelRatioF(), "render_scale": view.render_scale(),
            "platform": a.platform, "step_device_px": a.step, "scroll_px_per_s": a.step * 62.5,
            "first_page_s": round(st["t_first"], 3), "scroll_s": round(time.perf_counter() - st["t0"], 1),
            "frames": st["frames"], "tick_gap_ms_p50": q(.5), "tick_gap_ms_p99": q(.99),
            "tick_gap_ms_max": round(gaps[-1] * 1000, 1), "ticks_over_50ms": sum(g > .05 for g in gaps),
            "ticks_over_33ms": sum(g > .033 for g in gaps),
            "frames_with_blank_page_pct": round(100 * st["blank"] / max(1, st["frames"]), 1),
            "peak_rss_mb_ui": round(st["peak_ui"], 1), "peak_rss_mb_worker": round(st["peak_worker"], 1),
            "peak_rss_mb_total": round(st["peak_total"], 1),
            "cache_mb": round(view.cache.bytes_used / 2**20, 1),
            "ui_process_imported_pymupdf": "pymupdf" in sys.modules or "fitz" in sys.modules,
            "worker_restarts": st["restarts"], "cores": a.cores or None,
            "cache_max_mb": round(view.cache.max_bytes / 2**20, 1),
        }
        if a.search:
            if panel.running:
                panel.cancel()
            ts = st["search_times"]
            res.update({"search_query_len": len(a.search), "search_ignore_space": a.ignore_space,
                        "searches_completed": len(ts), "search_s_first": round(ts[0], 2),
                        "search_s_max": round(max(ts), 2), "search_hits": st["search_hits"][0],
                        "search_hits_stable": len(set(st["search_hits"])) == 1})
        st["result"] = res
        st["done"] = True
        QTimer.singleShot(0, app.quit)

    stalls: list = []
    if a.diagnose:  # 주 스레드가 35ms 넘게 틱을 못 하면 그 순간 스택을 채집
        import gc
        import threading
        import traceback
        main_id = threading.get_ident()
        gc_log: list = []
        gc_t = [0.0]

        def gc_cb(phase, info):
            if phase == "start":
                gc_t[0] = time.perf_counter()
            else:
                d = time.perf_counter() - gc_t[0]
                if d > 0.005:
                    gc_log.append((round(gc_t[0] - (st["t0"] or 0), 2), info["generation"], round(d * 1000, 1)))
        gc.callbacks.append(gc_cb)
        st["gc_log"] = gc_log

        def sampler():
            while not st["done"]:
                time.sleep(0.005)
                ticks = st["ticks"]
                if not ticks:
                    continue
                late = time.perf_counter() - ticks[-1]
                if late > 0.035 and (not stalls or stalls[-1][0] != len(ticks)):
                    frame = sys._current_frames().get(main_id)
                    stack = "".join(traceback.format_stack(frame, limit=8)) if frame else "?"
                    stalls.append((len(ticks), round(ticks[-1] - st["t0"], 2), stack))
        threading.Thread(target=sampler, daemon=True).start()

    timer.timeout.connect(tick)
    timer.start(16)
    QTimer.singleShot(30 * 60 * 1000, app.quit)  # 안전장치
    app.exec()
    win.close()
    service.shutdown()
    if not st["done"]:
        print(json.dumps({"error": "engine failed" if st.get("engine_failed") else "did not finish",
                          "worker_restarts": st["restarts"]}))
        return 1
    res = st["result"]
    res["source_unchanged"] = sha256(src) == before
    if a.diagnose:
        ticks = st["ticks"]
        big = [(round(ticks[i] - st["t0"], 2), round((ticks[i + 1] - ticks[i]) * 1000, 1))
               for i in range(len(ticks) - 1) if ticks[i + 1] - ticks[i] > 0.033]
        print("long gaps (t_s, ms):", big, file=sys.stderr)
        print("gc >5ms (t_s, gen, ms):", st.get("gc_log"), file=sys.stderr)
        for n, t, stack in stalls:
            print(f"--- stall after tick {n} t={t}s\n{stack}", file=sys.stderr)
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    out = ROOT / "spike" / "out" / "results"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "measure_scroll.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
