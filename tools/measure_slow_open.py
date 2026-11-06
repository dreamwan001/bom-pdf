"""느린 저장소(NAS·Dropbox·원격) 근사에서 열기 → 첫 쪽, sha256, 메모 포함 내보내기의 읽은 양·시간 (O4). 숫자만 출력한다.

느린 저장소는 `BOM_PDF_SIM_SLOW_READ=MB/s:지연ms` 로 **근사**한다(sysinfo.SlowReadMeter): 렌더 작업자는 열기·렌더마다
실제로 읽은 바이트·읽기 횟수만큼 잠들고, sha256 은 1MB 조각마다 잠든다. 대역폭 다툼·OS/SMB 캐시는 흉내 내지 않는다.
읽은 양은 작업자 프로세스 OS 읽기 카운터(psutil io_counters — 파이프로 받은 명령 몇 바이트 포함)와 sha256 이 읽은 바이트.

    .venv/Scripts/python.exe tools/measure_slow_open.py spike/out/fixtures/big_text_891.pdf --sim 10:2 --label after

--remote: 원격 파일로 본다(작은 파일은 작업자가 한 번에 순서대로 읽어 메모리에서 연다, O4).
한 번 실행에 두 차례 연다: cold(해시 캐시 없음) → warm(같은 데이터 폴더로 다시 — 캐시 적중이어야 함).
결과 한 줄 JSON 을 spike/out/results/measure_slow_open.jsonl 에 덧붙인다.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--sim", default="", help="MB/s:지연ms (빈 값 = 로컬 그대로)")
    ap.add_argument("--scale", type=float, default=1.5)
    ap.add_argument("--label", default="")
    ap.add_argument("--no-export", action="store_true")
    ap.add_argument("--remote", action="store_true", help="원격 파일로 본다(BOM_PDF_SIM_REMOTE=1 — 작은 파일은 메모리로 읽어 연다)")
    a = ap.parse_args()

    if a.sim:
        os.environ["BOM_PDF_SIM_SLOW_READ"] = a.sim
    if a.remote:
        os.environ["BOM_PDF_SIM_REMOTE"] = "1"
    home = tempfile.mkdtemp(prefix="bom-pdf-slow-")
    os.environ["BOM_PDF_HOME"] = home
    sys.path.insert(0, str(ROOT / "src"))
    import psutil

    from bom_pdf import storage
    from bom_pdf.services import document as ds

    src = os.path.abspath(a.pdf)
    out_dir = tempfile.mkdtemp(prefix="bom-pdf-slow-out-")
    rec: dict = {"label": a.label, "sim": a.sim or "local", "remote": a.remote, "bytes": os.path.getsize(src),
                 "name": os.path.basename(src) if "fixtures" in src else "(실물)"}
    try:
        for phase in ("cold", "warm"):
            q: queue.Queue = queue.Queue()
            service = ds.DocumentService(q.put)
            service.start()
            try:
                wp = psutil.Process(service.engine.pid)
                time.sleep(0.5)  # 작업자 import 끝나기를 기다린다(읽기 카운터에 넣지 않으려고)
                while True:  # 작업자가 준비됐는지(첫 명령 처리) 확인
                    try:
                        q.get_nowait()
                    except queue.Empty:
                        break
                io0 = wp.io_counters()
                h0 = storage.hash_bytes_read
                t0 = time.perf_counter()
                doc_id = service.open(src)
                r: dict = {}
                hashed_at = None
                deadline = t0 + 600
                while "first" not in r and time.perf_counter() < deadline:
                    ev = q.get(timeout=600)
                    now = time.perf_counter() - t0
                    if isinstance(ev, ds.DocumentOpened):
                        r["open_s"] = round(now, 3)
                        if ev.doc.sha256 is not None:
                            hashed_at = now
                        service.request_pages(doc_id, a.scale, [0], 0)
                    elif isinstance(ev, ds.DocumentOpenFailed):
                        r["failed"] = ev.kind
                        break
                    elif isinstance(ev, ds.DocumentHashed):
                        hashed_at = now
                    elif isinstance(ev, ds.PageImage) and ev.lane == 0 and ev.page == 0:
                        r["first_page_s"] = round(now, 3)
                        io1 = wp.io_counters()
                        r["worker_read_mb_first"] = round((io1.read_bytes - io0.read_bytes) / 2**20, 2)
                        r["worker_reads_first"] = io1.read_count - io0.read_count
                        r["hash_mb_before_first"] = round((storage.hash_bytes_read - h0) / 2**20, 2)
                        r["first"] = True
                while hashed_at is None and time.perf_counter() < deadline:
                    ev = q.get(timeout=600)
                    if isinstance(ev, ds.DocumentHashed):
                        hashed_at = time.perf_counter() - t0
                r.pop("first", None)
                r["hash_s"] = round(hashed_at, 3) if hashed_at is not None else None
                r["hash_mb"] = round((storage.hash_bytes_read - h0) / 2**20, 2)
                if not a.no_export and "failed" not in r:
                    item = {"id": "m1", "type": "box", "page": 0, "rect": [72, 72, 200, 140], "color": [1, 0, 0],
                            "note": "", "tags": [], "label": "box"}
                    out = os.path.join(out_dir, f"export_{phase}.pdf")
                    io2 = wp.io_counters()
                    t1 = time.perf_counter()
                    service.export_annotated(doc_id, out, [item])
                    while True:
                        ev = q.get(timeout=900)
                        if isinstance(ev, ds.ExportDone):
                            break
                    io3 = wp.io_counters()
                    r["export_s"] = round(time.perf_counter() - t1, 3)
                    r["export_status"] = ev.result.get("status")
                    r["export_read_mb"] = round((io3.read_bytes - io2.read_bytes) / 2**20, 2)
                rec[phase] = r
            finally:
                service.shutdown()
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(home, ignore_errors=True)
    line = json.dumps(rec, ensure_ascii=False)
    print(line)
    res = ROOT / "spike" / "out" / "results"
    res.mkdir(parents=True, exist_ok=True)
    with open(res / "measure_slow_open.jsonl", "a", encoding="utf-8", newline="\n") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
