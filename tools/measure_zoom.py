"""확대(400%·800%) 상태에서 이동할 때의 메모리·빈 화면·선명해지기까지 시간 측정 (O2·O3). 숫자만 출력한다.

PDF 를 열고 --zoom 으로 확대한 뒤 --page 쪽 가운데로 가서 선명해질 때까지 기다리고(처음),
그다음 화면의 40%씩 좌우·아래로 10번 옮기며(각 단계는 선명해지거나 2초까지) 마지막으로 4초 동안 부드럽게 아래로 스크롤한다.
- 메모리: 별도 스레드가 30ms 마다 UI + 렌더 작업자 RSS 를 재서 최고값
- 빈 화면: 보이는 쪽 면적 중 아무 그림(대체 그림 포함)도 없는 부분이 있던 연속 시간의 최대
- 선명: 보이는 부분이 모두 지금 배율 그림(쪽 전체 또는 타일)으로 덮일 때까지 시간
- 깜빡임 회귀: 같은 (쪽, 배율, 타일)을 두 번 이상 그린 수

사용(이 PC 실제 모니터, 최대화):
    .venv/Scripts/python.exe tools/measure_zoom.py spike/out/fixtures/big_text_891.pdf --zoom 8 --platform windows --maximized
목표 PC 흉내(FHD 100% / 125%, 2코어, RAM 8GB 등급):
    ... --platform offscreen --width 1920 --height 1080 --scale 1.0 --cores 2 --ram-gb 8
    ... --platform offscreen --width 1536 --height 864 --scale 1.25 --cores 2 --ram-gb 8
흉내는 근사다(offscreen 은 실제 화면에 그리지 않는다). 결과 한 줄 JSON 을 spike/out/results/measure_zoom.jsonl 에 덧붙인다.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--zoom", type=float, default=8.0)
    ap.add_argument("--page", type=int, default=10)
    ap.add_argument("--platform", default="offscreen")
    ap.add_argument("--maximized", action="store_true")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--scale", type=float, default=0.0, help="QT_SCALE_FACTOR (0 = 그대로)")
    ap.add_argument("--cores", type=int, default=0, help="UI·작업자를 앞 N개 논리 CPU 에 묶기(엔트리 CPU 근사)")
    ap.add_argument("--ram-gb", type=float, default=0.0, help="RAM 등급 흉내(BOM_PDF_SIM_RAM_GB)")
    ap.add_argument("--screen", default="", help="화면 흉내(BOM_PDF_SIM_SCREEN, 예: 1920x1080@1.25)")
    ap.add_argument("--label", default="")
    a = ap.parse_args()

    os.environ["QT_QPA_PLATFORM"] = a.platform
    if a.scale:
        os.environ["QT_SCALE_FACTOR"] = str(a.scale)
    if a.ram_gb:
        os.environ["BOM_PDF_SIM_RAM_GB"] = str(a.ram_gb)
    if a.screen:
        os.environ["BOM_PDF_SIM_SCREEN"] = a.screen
    os.environ.setdefault("BOM_PDF_HOME", tempfile.mkdtemp(prefix="bom-pdf-measure-"))
    sys.path.insert(0, str(ROOT / "src"))

    import psutil
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services.document import DocumentService, EngineFailed
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import RenderedPage, ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    me = psutil.Process()
    if a.cores:
        me.cpu_affinity(list(range(a.cores)))  # spawn 한 작업자도 물려받는다(아래에서 한 번 더 확인)
    src = os.path.abspath(a.pdf)
    app = QApplication([sys.argv[0]])
    bridge = ServiceBridge()
    service = DocumentService(bridge)
    service.start()
    win = MainWindow(service, bridge, RecentDocuments())
    if a.maximized:
        win.showMaximized()
    else:
        win.resize(a.width, a.height)
        win.show()
    win.open_paths([src])

    st = {"peak_ui": 0.0, "peak_worker": 0.0, "peak_total": 0.0, "stop": False, "phase": "open",
          "renders": Counter(), "steps": [], "blank_max_s": 0.0, "blank_since": None, "failed": False}

    def sampler() -> None:
        while not st["stop"]:
            try:
                ui = me.memory_info().rss / 2**20
                wk = 0.0
                if service.engine.pid:
                    wk = psutil.Process(service.engine.pid).memory_info().rss / 2**20
            except psutil.Error:
                time.sleep(0.03)
                continue
            st["peak_ui"] = max(st["peak_ui"], ui)
            st["peak_worker"] = max(st["peak_worker"], wk)
            st["peak_total"] = max(st["peak_total"], ui + wk)
            time.sleep(0.03)

    threading.Thread(target=sampler, daemon=True).start()

    def on_service(ev) -> None:
        if isinstance(ev, RenderedPage) and ev.lane >= 0:
            key = (ev.page, ev.scale, (ev.tile[0], ev.tile[1]) if getattr(ev, "tile", None) else None)
            st["renders"][key] += 1
            st.setdefault("render_phase", Counter())[(key, st["phase"])] += 1
        elif isinstance(ev, EngineFailed):
            st["failed"] = True
            QTimer.singleShot(0, app.quit)

    bridge.received.connect(on_service)

    def coverage(view) -> tuple[float, float]:
        """(선명한 몫, 빈 몫) — 보이는 쪽 면적 기준. 새 코드는 PageView.coverage() 를 쓴다."""
        fn = getattr(view, "coverage", None)
        if fn is not None:
            return fn()
        did, scale = view.doc.doc_id, view.render_scale()
        vis = [p for p in view.visible_pages() if p not in view._errors]
        if not vis:
            return 1.0, 0.0
        sharp = sum(view.cache.contains(did, p, scale) for p in vis) / len(vis)
        blank = sum(not view.cache.contains(did, p, scale) and view.cache.find_any(did, p) is None
                    for p in vis) / len(vis)
        return sharp, blank

    W = [(0.4, 0), (0.4, 0), (0, 0.4), (-0.4, 0), (-0.4, 0), (0, 0.4), (0.4, 0), (0, 0.4), (-0.4, 0), (0, 0.4)]
    ticks: list[float] = []

    def tick() -> None:
        now = time.perf_counter()
        view = win.current_view()
        if view is None:
            return
        ticks.append(now)
        sharp, blank = coverage(view)
        if st["phase"] not in ("open", "zoom"):
            if blank > 0:
                if st["blank_since"] is None:
                    st["blank_since"] = now
                st["blank_max_s"] = max(st["blank_max_s"], now - st["blank_since"])
            else:
                st["blank_since"] = None
        ph = st["phase"]
        if ph == "open":
            if view.cache.contains(view.doc.doc_id, 0, view.render_scale()) or len(view.cache):
                if a.cores and service.engine.pid:
                    try:
                        psutil.Process(service.engine.pid).cpu_affinity(list(range(a.cores)))
                    except psutil.Error:
                        pass
                view.set_zoom(a.zoom)
                view.go_to_page(a.page)
                hs = view.horizontalScrollBar()
                hs.setValue(hs.maximum() // 2)
                sb = view.verticalScrollBar()
                sb.setValue(sb.value() + view.viewport().height() // 3)
                st.update(phase="settle", t=now)
        elif ph == "settle":
            if sharp >= 1.0 or now - st["t"] > 15:
                st["first_sharp_s"] = round(now - st["t"], 2)
                st.update(phase="pan", step=0, t=now, moved=False)
        elif ph == "pan":
            if not st["moved"]:
                dx, dy = W[st["step"]]
                hs, vs = view.horizontalScrollBar(), view.verticalScrollBar()
                hs.setValue(hs.value() + int(dx * view.viewport().width()))
                vs.setValue(vs.value() + int(dy * view.viewport().height()))
                st.update(moved=True, t=now)
                return
            if sharp >= 1.0 or now - st["t"] > 2.0:
                st["steps"].append(round(now - st["t"], 3) if sharp >= 1.0 else None)
                st["step"] += 1
                st["moved"] = False
                if st["step"] >= len(W):
                    st.update(phase="scroll", t=now)
        elif ph == "scroll":
            vs = view.verticalScrollBar()
            vs.setValue(vs.value() + 20)
            if now - st["t"] > 4.0:
                st.update(phase="end", t=now)
        elif ph == "end":
            if sharp >= 1.0 or now - st["t"] > 2.0:
                st["end_sharp_s"] = round(now - st["t"], 2)
                st["cache_mb"] = round(view.cache.bytes_used / 2**20, 1)
                st["cache_max_mb"] = round(view.cache.max_bytes / 2**20, 1)
                st["viewport"] = [view.viewport().width(), view.viewport().height(), view.devicePixelRatioF()]
                st["render_scale"] = view.render_scale()
                timer.stop()
                QTimer.singleShot(200, app.quit)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(16)
    QTimer.singleShot(180_000, app.quit)
    app.exec()
    st["stop"] = True
    win.close()
    service.shutdown()
    g = sorted(b - x for x, b in zip(ticks, ticks[1:])) or [0.0]
    sharp_steps = [s for s in st["steps"] if s is not None]
    rep = [n for n in st["renders"].values() if n > 1]
    res = {"label": a.label, "file": os.path.basename(src), "zoom": a.zoom, "platform": a.platform,
           "maximized": a.maximized, "qt_scale": a.scale or None, "cores": a.cores or None, "ram_gb_sim": a.ram_gb or None,
           "viewport": st.get("viewport"), "render_scale": st.get("render_scale"),
           "first_sharp_s": st.get("first_sharp_s"), "pan_sharp_s_max": max(sharp_steps) if sharp_steps else None,
           "pan_steps_not_sharp_2s": sum(s is None for s in st["steps"]), "end_sharp_s": st.get("end_sharp_s"),
           "blank_max_s": round(st["blank_max_s"], 3), "renders": sum(st["renders"].values()),
           "rerendered_items": len(rep), "max_repeats": max(rep, default=1),
           "tick_gap_ms_max": round(g[-1] * 1000, 1), "ticks_over_50ms": sum(x > .05 for x in g),
           "peak_rss_mb_ui": round(st["peak_ui"], 1), "peak_rss_mb_worker": round(st["peak_worker"], 1),
           "peak_rss_mb_total": round(st["peak_total"], 1), "cache_mb_end": st.get("cache_mb"),
           "cache_max_mb": st.get("cache_max_mb"), "engine_failed": st["failed"],
           "ui_process_imported_pymupdf": "pymupdf" in sys.modules or "fitz" in sys.modules}
    if os.environ.get("MEASURE_ZOOM_DEBUG"):
        top = sorted(((n, k) for k, n in st["renders"].items() if n > 1), reverse=True)[:6]
        for n, k in top:
            print("repeat", n, k, {ph: c for (kk, ph), c in st["render_phase"].items() if kk == k}, file=sys.stderr)
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    out = ROOT / "spike" / "out" / "results"
    out.mkdir(parents=True, exist_ok=True)
    with (out / "measure_zoom.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
