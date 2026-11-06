"""포장본(또는 소스) 첫 쪽 시간·3탭 메모리 측정 (1-6). 숫자만 출력한다 — 본문은 읽지도 출력하지도 않는다.

사용:
    .venv/Scripts/python.exe tools/measure_package.py first-page <pdf> [--runs 6] [--source]
    .venv/Scripts/python.exe tools/measure_package.py tabs <pdf1> <pdf2> <pdf3> [--settle 8] [--source]

- first-page: 실행(프로세스 시작) → 첫 쪽 렌더가 보기에 도착하고 그려진 시각(`BOM_PDF_FIRST_PAGE_MARK`)까지.
  실제 창(Windows 플랫폼)을 띄운다. 첫 회는 콜드에 가까움(직전 빌드·백신 검사 영향), 나머지는 웜.
  매 회 창을 정상 종료 경로로 닫는다(`BOM_PDF_EXIT_AFTER_FIRST_PAGE`).
- tabs: 세 파일을 한 번에 열고(탭 3개) 첫 쪽이 뜬 뒤 settle 초 동안 0.2초마다 프로세스 트리(UI + 렌더 작업자)
  메모리(RSS 합)를 재어 최고값·마지막 값을 낸다. 끝나면 이 측정이 띄운 프로세스 트리만 끝낸다.
- 데이터 폴더는 매번 임시 폴더(BOM_PDF_HOME) — 사용자 %APPDATA% 와 떠 있는 bom-pdf 와 섞이지 않는다.
결과 한 줄 JSON 을 출력하고 spike/out/results/measure_package.jsonl 에 덧붙인다(Git 무시).
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "bom-pdf" / "bom-pdf.exe"
OUT = ROOT / "spike" / "out" / "results" / "measure_package.jsonl"


def command(source: bool) -> list[str]:
    if source:
        return [str(ROOT / ".venv" / "Scripts" / "pythonw.exe"), "-m", "bom_pdf"]
    if not EXE.exists():
        raise SystemExit(f"missing {EXE} — run tools/build_exe.py first")
    return [str(EXE)]


def env_for(home: str, mark: str | None, exit_after: bool) -> dict:
    env = {**os.environ, "BOM_PDF_HOME": home, "PYTHONPATH": str(ROOT / "src")}
    env.pop("QT_QPA_PLATFORM", None)
    if mark:
        env["BOM_PDF_FIRST_PAGE_MARK"] = mark
    if exit_after:
        env["BOM_PDF_EXIT_AFTER_FIRST_PAGE"] = "1"
    return env


def tree_rss_mb(proc: psutil.Process) -> float:
    total = 0
    for p in [proc, *proc.children(recursive=True)]:
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total / 2**20


def first_page(pdf: str, runs: int, source: bool) -> dict:
    times = []
    for _ in range(runs):
        home = tempfile.mkdtemp(prefix="bom-pdf-measure-")
        mark = os.path.join(home, "first_page.txt")
        t0 = time.time()
        p = subprocess.Popen(command(source) + [pdf], env=env_for(home, mark, True))
        try:
            p.wait(timeout=90)
        except subprocess.TimeoutExpired:
            kill_tree(p.pid)
            times.append(None)
            continue
        times.append(round((float(Path(mark).read_text()) - t0) * 1000) if os.path.exists(mark) else None)
    ok = [t for t in times if t is not None]
    warm = ok[1:] if len(ok) > 1 else ok
    return {"runs_ms": times, "first_run_ms": times[0], "warm_median_ms": statistics.median(warm) if warm else None,
            "warm_max_ms": max(warm) if warm else None}


def kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    procs = [*root.children(recursive=True), root]
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=10)


def tabs(pdfs: list[str], settle: float, source: bool) -> dict:
    home = tempfile.mkdtemp(prefix="bom-pdf-measure-")
    mark = os.path.join(home, "first_page.txt")
    t0 = time.time()
    p = subprocess.Popen(command(source) + pdfs, env=env_for(home, mark, False))
    proc = psutil.Process(p.pid)
    try:
        end = time.time() + 60
        while not os.path.exists(mark) and time.time() < end:
            time.sleep(0.05)
        first_ms = round((float(Path(mark).read_text()) - t0) * 1000) if os.path.exists(mark) else None
        samples = []
        end = time.time() + settle
        while time.time() < end:
            samples.append(tree_rss_mb(proc))
            time.sleep(0.2)
        n_proc = 1 + len(proc.children(recursive=True))
    finally:
        kill_tree(p.pid)
    return {"files": len(pdfs), "first_page_ms": first_ms, "peak_tree_rss_mb": round(max(samples), 1),
            "last_tree_rss_mb": round(samples[-1], 1), "processes": n_proc}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["first-page", "tabs"])
    ap.add_argument("pdfs", nargs="+")
    ap.add_argument("--runs", type=int, default=6)
    ap.add_argument("--settle", type=float, default=8.0)
    ap.add_argument("--source", action="store_true", help="포장본 대신 소스 실행(pythonw -m bom_pdf)")
    a = ap.parse_args()
    pdfs = [os.path.abspath(x) for x in a.pdfs]
    sizes = [round(os.path.getsize(x) / 2**20, 1) for x in pdfs]
    pages_note = {"file_mb": sizes, "target": "source" if a.source else "package"}
    if a.mode == "first-page":
        res = {"mode": "first-page", **pages_note, **first_page(pdfs[0], a.runs, a.source)}
    else:
        if len(pdfs) != 3:
            raise SystemExit("tabs needs 3 files")
        res = {"mode": "tabs", **pages_note, **tabs(pdfs, a.settle, a.source)}
    line = json.dumps(res, ensure_ascii=False)
    print(line)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
