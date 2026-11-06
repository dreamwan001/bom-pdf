"""입력 한도 측정 (O5) — 합성 자료만, 숫자만 출력한다. 결과는 spike/out/results/measure_inputs.jsonl 에 덧붙인다.

    .venv/Scripts/python.exe tools/measure_inputs.py photos --n 500 [--doc-mode 0.1] [--label after]
        합성 12MP JPEG n장(50장을 만들어 이름만 바꿔 복사)을 사진 정보 읽기(scan) → PDF 만들기(작업 프로세스).
        작업 프로세스 RSS 최고값·50장마다 RSS(일정한지)·시간·결과 크기.
    .venv/Scripts/python.exe tools/measure_inputs.py huge [--label after]
        2억 화소 JPEG·1.5억 화소 PNG·4천만 화소 PNG 를 scan → PDF. 급사 없이 처리/건너뜀 이유·RSS 최고값.
    .venv/Scripts/python.exe tools/measure_inputs.py bigpdf [--pages 10000]
        합성 1만 쪽 텍스트 PDF 를 만들어 spike/out/fixtures/big_text_10000.pdf 에 둔다(이미 있으면 그대로).
        --measure: 실제 주 창(offscreen)으로 열기·첫 쪽·쪽 이동·스크롤·전체 검색 2번 동안 UI+작업자 RSS 최고값.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "spike" / "out" / "results" / "measure_inputs.jsonl"


def _record(rec: dict) -> None:
    line = json.dumps(rec, ensure_ascii=False)
    print(line)
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    with open(RESULTS, "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")


def _photo(path: Path, w: int, h: int, seed: int, fmt: str = "JPEG") -> None:
    from PIL import Image, ImageDraw

    small = Image.effect_noise((max(1, w // 8), max(1, h // 8)), 40).convert("RGB")
    im = small.resize((w, h), Image.Resampling.BILINEAR)
    d = ImageDraw.Draw(im)
    for k in range(12):
        x = (seed * 97 + k * 331) % max(1, w - w // 5)
        y = (seed * 53 + k * 211) % max(1, h - h // 5)
        d.rectangle([x, y, x + w // 6, y + h // 8], fill=((seed * 40 + k * 20) % 255, (k * 70) % 255, 120))
    if fmt == "JPEG":
        im.save(path, "JPEG", quality=88)
    else:
        im.save(path, fmt, compress_level=1)


class _Peak(threading.Thread):
    """작업 프로세스 RSS 를 30ms 마다 재서 최고값과 시계열을 둔다."""

    def __init__(self, pid: int):
        super().__init__(daemon=True)
        import psutil

        self.p = psutil.Process(pid)
        self.peak = 0
        self.samples: list[tuple[float, int]] = []
        self.stop = False
        self.t0 = time.perf_counter()

    def run(self) -> None:
        while not self.stop:
            try:
                rss = self.p.memory_info().rss
            except Exception:
                break
            self.peak = max(self.peak, rss)
            self.samples.append((time.perf_counter() - self.t0, rss))
            time.sleep(0.03)


def _run_job(service, start, progress_marks: list | None = None) -> tuple[dict, _Peak, float]:
    from bom_pdf.services import document as ds

    q = service._q  # type: ignore[attr-defined]
    t0 = time.perf_counter()
    job_id = start()
    proc = service._jobs._jobs[job_id]["proc"]  # noqa: SLF001 — 측정 전용
    peak = _Peak(proc.pid)
    peak.start()
    while True:
        ev = q.get(timeout=3600)
        if isinstance(ev, ds.JobProgress) and ev.job_id == job_id and progress_marks is not None:
            progress_marks.append((ev.done, peak.peak))
        if isinstance(ev, ds.JobDone) and ev.job_id == job_id:
            break
    peak.stop = True
    return ev.result, peak, time.perf_counter() - t0


def _service():
    from bom_pdf.services import document as ds

    q: queue.Queue = queue.Queue()
    s = ds.DocumentService(q.put)
    s._q = q  # type: ignore[attr-defined]
    return s


def cmd_photos(a) -> None:
    from bom_pdf.services import imagetopdf as itp

    work = Path(tempfile.mkdtemp(prefix="bom-pdf-photos-"))
    try:
        uniq = min(50, a.n)
        t = time.perf_counter()
        for i in range(uniq):
            _photo(work / f"u{i:03d}.jpg", 4032, 3024, i)
        paths = []
        for i in range(a.n):
            dst = work / f"사진_{i + 1:04d}.jpg"
            shutil.copyfile(work / f"u{i % uniq:03d}.jpg", dst)
            paths.append(str(dst))
        for i in range(uniq):
            (work / f"u{i:03d}.jpg").unlink()
        gen_s = time.perf_counter() - t
        s = _service()
        r_scan, pk_scan, t_scan = _run_job(s, lambda: s.scan_images(paths))
        photos = [itp.Photo(path=d["path"], width=d["width"], height=d["height"], taken=d.get("taken"))
                  for d in r_scan.get("items", [])]
        every = max(1, int(1 / a.doc_mode)) if a.doc_mode else 0
        for k, p in enumerate(photos):
            p.doc_mode = bool(every) and k % every == 0
        opts = itp.Options()
        out = work / "out.pdf"
        marks: list = []
        job = itp.plan(photos, opts, str(out), [], None)
        r_pdf, pk_pdf, t_pdf = _run_job(s, lambda: s.images_to_pdf(job), marks)
        s.shutdown()
        step = max(1, a.n // 10)
        curve = [round(m[1] / 2**20) for m in marks if m[0] % step == 0]
        _record({"what": "photos", "label": a.label, "n": a.n, "gen_s": round(gen_s, 1),
                 "scan_s": round(t_scan, 1), "scan_peak_mb": round(pk_scan.peak / 2**20),
                 "scan_items": len(photos), "scan_failed": len(r_scan.get("failed", [])),
                 "pdf_status": r_pdf.get("status"), "pdf_s": round(t_pdf, 1), "pdf_peak_mb": round(pk_pdf.peak / 2**20),
                 "pdf_rss_curve_mb": curve, "pdf_pages": r_pdf.get("pages"),
                 "pdf_mb": round(r_pdf.get("size", 0) / 2**20, 1), "doc_mode_every": every,
                 "doc_fixed": r_pdf.get("doc_fixed"), "estimate_mb": round(itp.estimate_size(photos, opts) / 2**20, 1)})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_huge(a) -> None:
    from PIL import Image

    from bom_pdf.services import imagetopdf as itp

    Image.MAX_IMAGE_PIXELS = None  # 생성기 쪽(측정 대상 아님)
    work = Path(tempfile.mkdtemp(prefix="bom-pdf-huge-"))
    try:
        specs = [("jpeg_201mp.jpg", 16384, 12288, "JPEG"), ("png_150mp.png", 14142, 10607, "PNG"),
                 ("png_40mp.png", 7300, 5480, "PNG")]
        paths = []
        for name, w, h, fmt in specs:
            p = work / name
            _photo(p, w, h, 7, fmt)
            paths.append(str(p))
        s = _service()
        recs = {}
        for p in paths:
            r_scan, pk_scan, t_scan = _run_job(s, lambda p=p: s.scan_images([p]))
            rec = {"scan_status": r_scan.get("status"), "scan_s": round(t_scan, 1),
                   "scan_peak_mb": round(pk_scan.peak / 2**20), "items": len(r_scan.get("items", [])),
                   "skipped": [x.get("reason") for x in r_scan.get("skipped", [])],
                   "failed": len(r_scan.get("failed", []))}
            items = r_scan.get("items", [])
            if items:
                for doc_mode in (False, True):
                    ph = [itp.Photo(path=d["path"], width=d["width"], height=d["height"], doc_mode=doc_mode)
                          for d in items]
                    out = work / f"out_{os.path.basename(p)}_{int(doc_mode)}.pdf"
                    job = itp.plan(ph, itp.Options(), str(out), [], None)
                    r_pdf, pk_pdf, t_pdf = _run_job(s, lambda j=job: s.images_to_pdf(j))
                    rec[f"pdf{'_doc' if doc_mode else ''}"] = {
                        "status": r_pdf.get("status"), "reason": r_pdf.get("reason"), "s": round(t_pdf, 1),
                        "peak_mb": round(pk_pdf.peak / 2**20), "images": r_pdf.get("images"),
                        "failed": r_pdf.get("failed"), "skipped": r_pdf.get("skipped")}
            recs[os.path.basename(p)] = rec
        s.shutdown()
        _record({"what": "huge", "label": a.label, **recs})
    finally:
        shutil.rmtree(work, ignore_errors=True)


def cmd_bigpdf(a) -> None:
    import pymupdf

    out = ROOT / "spike" / "out" / "fixtures" / f"big_text_{a.pages}.pdf"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        t = time.perf_counter()
        block = pymupdf.open()  # 100쪽 묶음을 만들어 되풀이해 붙인다(쪽마다 insert_text 1만 번은 매우 느림)
        for i in range(100):
            page = block.new_page(width=595, height=842)
            page.insert_text((72, 80), f"Synthetic block page {i + 1}", fontsize=16)
            y = 120
            for k in range(30):
                page.insert_text((72, y), f"line {k + 1}: lorem ipsum synthetic text block {i * 31 + k} needle{k % 7}",
                                 fontsize=10)
                y += 22
        doc = pymupdf.open()
        for _ in range(a.pages // 100):
            doc.insert_pdf(block)
        block.close()
        doc.save(out, garbage=1, deflate=True)
        doc.close()
        print(json.dumps({"made": out.name, "seconds": round(time.perf_counter() - t, 1),
                          "mb": round(out.stat().st_size / 2**20, 1)}))
    if not a.measure:
        print("측정: 같은 명령에 --measure")
        return
    _measure_bigpdf(out, a)


def _measure_bigpdf(pdf: Path, a) -> None:
    """실제 주 창(offscreen, FHD 창)으로 열기 → 첫 쪽 → 0·25·50·75·100% 쪽으로 이동(선명해질 때까지) → 300틱 스크롤
    → 전체 검색 2번(일반·띄어쓰기 무시). UI + 렌더 작업자 RSS 최고값을 30ms 마다 잰다."""
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    if a.ram_gb:
        os.environ["BOM_PDF_SIM_RAM_GB"] = str(a.ram_gb)
    import psutil
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services import document as ds
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    app = QApplication([sys.argv[0]])
    bridge = ServiceBridge()
    service = ds.DocumentService(bridge)
    service.start()
    win = MainWindow(service, bridge, RecentDocuments())
    win.resize(1920, 1040)
    win.show()
    me = psutil.Process()
    st = {"peak": 0.0, "peak_ui": 0.0, "peak_wk": 0.0, "stop": False}

    def sampler() -> None:
        while not st["stop"]:
            try:
                ui = me.memory_info().rss / 2**20
                wk = psutil.Process(service.engine.pid).memory_info().rss / 2**20 if service.engine.pid else 0
            except psutil.Error:
                ui = wk = 0
            st["peak_ui"], st["peak_wk"] = max(st["peak_ui"], ui), max(st["peak_wk"], wk)
            st["peak"] = max(st["peak"], ui + wk)
            time.sleep(0.03)

    threading.Thread(target=sampler, daemon=True).start()

    def wait(cond, timeout=600.0) -> float:
        t = time.perf_counter()
        while not cond():
            app.processEvents()
            time.sleep(0.002)
            if time.perf_counter() - t > timeout:
                raise TimeoutError
        return time.perf_counter() - t

    rec: dict = {"what": "bigpdf", "label": a.label, "mb": round(pdf.stat().st_size / 2**20, 1)}
    t0 = time.perf_counter()
    win.open_paths([str(pdf)])
    wait(lambda: win.current_view() is not None)
    rec["open_s"] = round(time.perf_counter() - t0, 2)
    view = win.current_view()
    rec["pages"] = view.doc.page_count
    wait(lambda: view.coverage()[0] >= 0.999)
    rec["first_page_s"] = round(time.perf_counter() - t0, 2)
    rec["after_open_mb"] = round(st["peak"])
    jumps = []
    for frac in (0.25, 0.5, 0.75, 1.0, 0.0):
        view.go_to_page(min(view.doc.page_count - 1, int(frac * view.doc.page_count)))
        jumps.append(round(wait(lambda: view.coverage()[0] >= 0.999), 3))
    rec["jump_sharp_s"] = jumps
    sb = view.verticalScrollBar()
    t1 = time.perf_counter()
    for _ in range(300):
        sb.setValue(sb.value() + 120)
        app.processEvents()
        time.sleep(0.016)
    rec["scroll_300_ticks_s"] = round(time.perf_counter() - t1, 2)
    panel = win.search_panel
    for q, ignore in (("needle3", False), ("lorem ipsum", True)):
        win.show_search()
        panel.query.setText(q)
        if hasattr(panel, "ignore_space"):
            panel.ignore_space.setChecked(ignore)
        t2 = time.perf_counter()
        panel.start_search()
        wait(lambda: not panel.running, timeout=900)
        rec[f"search_{'ignore_space' if ignore else 'plain'}_s"] = round(time.perf_counter() - t2, 2)
        rec[f"search_{'ignore_space' if ignore else 'plain'}_hits"] = panel.model.rowCount()
    st["stop"] = True
    rec.update(peak_rss_mb_total=round(st["peak"]), peak_rss_mb_ui=round(st["peak_ui"]),
               peak_rss_mb_worker=round(st["peak_wk"]), cache_max_mb=win.cache.max_bytes // 2**20,
               ram_tier_sim=a.ram_gb or None)
    win.close()
    service.shutdown()
    _record(rec)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("photos")
    p.add_argument("--n", type=int, default=500)
    p.add_argument("--doc-mode", type=float, default=0.0, help="문서 사진 모드 비율(0.1 = 10장에 1장)")
    p.add_argument("--label", default="")
    p = sub.add_parser("huge")
    p.add_argument("--label", default="")
    p = sub.add_parser("bigpdf")
    p.add_argument("--pages", type=int, default=10000)
    p.add_argument("--measure", action="store_true", help="주 창으로 열기·이동·스크롤·검색 메모리·시간")
    p.add_argument("--ram-gb", type=float, default=0.0, help="RAM 등급 흉내(BOM_PDF_SIM_RAM_GB)")
    p.add_argument("--label", default="")
    a = ap.parse_args()
    os.environ.setdefault("BOM_PDF_HOME", tempfile.mkdtemp(prefix="bom-pdf-inputs-"))
    sys.path.insert(0, os.environ.get("BOM_PDF_MEASURE_SRC") or str(ROOT / "src"))  # 전 판 비교: HEAD 의 src 를 풀어 둔 곳
    {"photos": cmd_photos, "huge": cmd_huge, "bigpdf": cmd_bigpdf}[a.cmd](a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
