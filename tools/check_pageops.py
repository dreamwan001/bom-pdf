"""1-4 실물 확인(숫자만 출력): 페이지 편집 저장·용량 압축본·인쇄(PDF 출력)의 시간·크기, 원본 불변.

    .venv/Scripts/python.exe tools/check_pageops.py <pdf> [--presets submit,small,clean] [--print-pages N]

- 데이터 폴더·산출물은 임시 폴더에 만들고 끝나면 지운다(실물 내용이 든 파일을 남기지 않음). 쪽 글자는 출력하지 않는다.
- 저장: 모든 쪽을 거꾸로 + 홀수 쪽 90° 회전 + 마지막 쪽 삭제 → 새 파일(작업 프로세스). 쪽 수·회전 확인.
- 인쇄: offscreen 주 창에서 앞 N쪽을 300DPI 로 QPrinter PDF 출력.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _sha(p: str) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while b := f.read(1 << 20):
            h.update(b)
    return h.hexdigest()


def _snap(p: str) -> tuple:
    return _sha(p), os.stat(p).st_mtime_ns, sorted(os.listdir(os.path.dirname(os.path.abspath(p))))


def _wait(q: queue.Queue, pred, timeout: float):
    end = time.monotonic() + timeout
    while True:
        ev = q.get(timeout=max(0.1, end - time.monotonic()))
        if pred(ev):
            return ev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--presets", default="submit")
    ap.add_argument("--print-pages", type=int, default=0)
    ap.add_argument("--skip-save", action="store_true")
    args = ap.parse_args()
    src = os.path.abspath(args.pdf)
    work = tempfile.mkdtemp(prefix="bom-pdf-check14-")
    os.environ["BOM_PDF_HOME"] = os.path.join(work, "home")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    out_dir = os.path.join(work, "out")
    os.makedirs(out_dir)
    before = _snap(src)
    print(f"size_bytes={os.path.getsize(src)}")
    try:
        from bom_pdf.services import document as ds
        from bom_pdf.services import pageops as po

        q: queue.Queue = queue.Queue()
        service = ds.DocumentService(q.put, data_dir=Path(work) / "home")
        service.start()
        try:
            doc_id = service.open(src)
            doc = _wait(q, lambda e: isinstance(e, ds.DocumentOpened) and e.doc.doc_id == doc_id, 120).doc
            t = time.perf_counter()
            _wait(q, lambda e: isinstance(e, ds.DocumentHashed) and e.doc_id == doc_id, 300)
            print(f"pages={doc.page_count} sha256_s={time.perf_counter() - t:.2f}")
            if not args.skip_save:
                ops = po.PageOps(po.Source(doc.path, list(doc.page_sizes), None, doc.sha256, doc.doc_id))
                n = len(ops)
                ops.move(list(range(n - 1, -1, -1)), 0)  # 거꾸로 (고른 순서 유지)
                ops.rotate(range(0, n, 2), 90)
                if n > 1:
                    ops.delete([n - 1])
                out = os.path.join(out_dir, "save.pdf")
                t = time.perf_counter()
                job = service.build_pages(ops.plan([(out, list(range(len(ops))))]))
                r = _wait(q, lambda e: isinstance(e, ds.JobDone) and e.job_id == job, 1800).result
                wall = time.perf_counter() - t
                print(f"save status={r['status']} wall_s={wall:.2f} worker_s={r.get('seconds')} "
                      f"pages={r.get('outputs', [{}])[0].get('pages')} out_bytes={r.get('outputs', [{}])[0].get('bytes')}")
                os.unlink(out)
            for preset in [p for p in args.presets.split(",") if p]:
                out = os.path.join(out_dir, f"compress_{preset}.pdf")
                t = time.perf_counter()
                job = service.compress(doc_id, out, preset)
                r = _wait(q, lambda e: isinstance(e, ds.JobDone) and e.job_id == job, 3600).result
                wall = time.perf_counter() - t
                pct = round(100 * (1 - r["after"] / r["before"]), 1) if r.get("before") else None
                print(f"compress preset={preset} status={r['status']} before={r.get('before')} after={r.get('after')} "
                      f"reduced_pct={pct} worker_s={r.get('seconds')} wall_s={wall:.2f}")
                if os.path.exists(out):
                    os.unlink(out)
        finally:
            service.shutdown()

        if args.print_pages:
            _print(src, out_dir, args.print_pages)
    finally:
        after = _snap(src)
        print(f"original_unchanged={after == before}")
        shutil.rmtree(work, ignore_errors=True)


def _print(src: str, out_dir: str, pages: int) -> None:
    from PySide6.QtPrintSupport import QPrinter
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services import document as ds
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    home = Path(os.environ["BOM_PDF_HOME"])
    bridge = ServiceBridge()
    service = ds.DocumentService(bridge, data_dir=home)
    service.start()
    win = MainWindow(service, bridge, RecentDocuments(home))
    win.show()
    target = os.path.join(out_dir, "print.pdf")
    try:
        win.open_paths([src])
        end = time.monotonic() + 120
        while win.current_view() is None and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.005)

        def fake(printer: QPrinter, n: int) -> bool:
            printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
            printer.setOutputFileName(target)
            printer.setPrintRange(QPrinter.PrintRange.PageRange)
            printer.setFromTo(1, min(n, pages))
            return True

        win.ask_print = fake
        t = time.perf_counter()
        job = win.print_document()
        worst = 0.0
        last = time.perf_counter()
        while win.last_print is None and time.perf_counter() - t < 1800:
            app.processEvents()
            now = time.perf_counter()
            worst = max(worst, now - last)
            last = now
            time.sleep(0.002)
        wall = time.perf_counter() - t
        import pymupdf

        with pymupdf.open(target) as d:
            n_out = d.page_count
        print(f"print status={win.last_print} dpi={job.dpi} pages={n_out} wall_s={wall:.2f} "
              f"per_page_s={wall / max(1, n_out):.2f} longest_ui_gap_ms={worst * 1000:.0f} "
              f"out_bytes={os.path.getsize(target)}")
    finally:
        win.close()
        service.shutdown()


if __name__ == "__main__":
    main()
