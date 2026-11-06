"""3탭 부하 메모리 측정 (1-6, 소스 실행·offscreen). 숫자만 출력한다.

세 파일을 탭 3개로 열고, 탭마다 차례로 끝까지 자동 스크롤(16ms 틱, 틱당 --step 장치 px)하면서 그 탭 문서 전체 검색을
되풀이한다(--search 가 있을 때, 검색어·본문은 출력하지 않음). 0.2초마다 UI + 렌더 작업자 메모리(RSS)를 재서 최고값을 낸다.
틱 간격(UI 멈춤)도 함께 낸다 — tools/timer_baseline.py 대조군과 비교.

사용: .venv/Scripts/python.exe tools/measure_tabs.py <pdf1> <pdf2> <pdf3> [--search 원고] [--step 400]
결과 한 줄 JSON 을 출력하고 spike/out/results/measure_tabs.jsonl 에 덧붙인다(Git 무시).
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
    ap.add_argument("pdfs", nargs=3)
    ap.add_argument("--search", default="")
    ap.add_argument("--step", type=int, default=400)
    a = ap.parse_args()
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    os.environ.setdefault("BOM_PDF_HOME", tempfile.mkdtemp(prefix="bom-pdf-measure-"))
    sys.path.insert(0, str(ROOT / "src"))

    import psutil
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services.document import DocumentService
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    pdfs = [os.path.abspath(p) for p in a.pdfs]
    before = [sha256(p) for p in pdfs]
    app = QApplication([sys.argv[0]])
    bridge = ServiceBridge()
    service = DocumentService(bridge)
    service.start()
    win = MainWindow(service, bridge, RecentDocuments())
    win.resize(900, 1000)
    win.show()
    win.open_paths(pdfs)
    me = psutil.Process()
    st = {"tab": -1, "ticks": [], "peak": 0.0, "peak_ui": 0.0, "peak_worker": 0.0, "searches": 0,
          "search_on": False, "t0": time.perf_counter(), "per_tab_s": []}
    panel = win.search_panel

    def sample() -> None:
        ui = me.memory_info().rss / 2**20
        wk = 0.0
        for c in me.children(recursive=True):
            try:
                wk += c.memory_info().rss / 2**20
            except psutil.Error:
                pass
        st["peak_ui"] = max(st["peak_ui"], ui)
        st["peak_worker"] = max(st["peak_worker"], wk)
        st["peak"] = max(st["peak"], ui + wk)

    mem = QTimer()
    mem.timeout.connect(sample)
    mem.start(200)

    def next_tab() -> bool:
        st["tab"] += 1
        if st["tab"] >= 3:
            return False
        win.tabs.setCurrentIndex(st["tab"])
        st["tab_t"] = time.perf_counter()
        return True

    def tick() -> None:
        st["ticks"].append(time.perf_counter())
        view = win.current_view()
        if st["tab"] < 0:
            if all(t.view is not None for t in win.tabs_by_doc.values()) and len(win.tabs_by_doc) == 3:
                next_tab()
                view = win.current_view()
                view.set_zoom(1.0)
            return
        if view is None:
            return
        if a.search:
            if st["search_on"] and not panel.running:
                st["searches"] += 1
                st["search_on"] = False
            if not st["search_on"]:
                panel.query.setText(a.search)
                panel.start_search()
                st["search_on"] = True
        sb = view.verticalScrollBar()
        sb.setValue(min(sb.maximum(), sb.value() + max(1, round(a.step / view.devicePixelRatioF()))))
        did, scale, last = view.doc.doc_id, view.render_scale(), view.doc.page_count - 1
        if sb.value() >= sb.maximum() and (view.cache.contains(did, last, scale) or last in view._errors):
            if panel.running:
                panel.cancel()
            st["search_on"] = False
            st["per_tab_s"].append(round(time.perf_counter() - st["tab_t"], 1))
            if not next_tab():
                timer.stop()
                QTimer.singleShot(300, app.quit)
                return
            win.current_view().set_zoom(1.0)

    timer = QTimer()
    timer.setTimerType(Qt.TimerType.PreciseTimer)
    timer.timeout.connect(tick)
    timer.start(16)
    app.exec()
    sample()
    service.shutdown()
    g = sorted(b - x for x, b in zip(st["ticks"], st["ticks"][1:]))
    res = {"files_mb": [round(os.path.getsize(p) / 2**20, 1) for p in pdfs], "search": bool(a.search),
           "searches_completed": st["searches"], "per_tab_scroll_s": st["per_tab_s"],
           "tick_gap_ms_p99": round(g[int(len(g) * .99)] * 1000, 1), "tick_gap_ms_max": round(g[-1] * 1000, 1),
           "ticks_over_50ms": sum(x > .05 for x in g), "ticks_over_33ms": sum(x > .033 for x in g),
           "peak_rss_mb_ui": round(st["peak_ui"], 1), "peak_rss_mb_worker": round(st["peak_worker"], 1),
           "peak_rss_mb_total": round(st["peak"], 1),
           "originals_unchanged": [sha256(p) for p in pdfs] == before,
           "ui_process_imported_pymupdf": "pymupdf" in sys.modules or "fitz" in sys.modules}
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    out = ROOT / "spike" / "out" / "results" / "measure_tabs.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
