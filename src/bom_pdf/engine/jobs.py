"""긴 파일 작업(페이지 편집 저장·분할·추출·용량 압축본, 1-5 사진 → PDF)을 한 번 쓰고 끝나는 작업 프로세스에서 돌린다 (1-4).

왜 렌더 작업자와 따로인가: 891쪽 스캔본 압축은 수십 초 걸린다. 렌더 작업자 하나에서 하면 그동안 보기 렌더가
멈춘다. 작업마다 프로세스를 하나 띄우면(spawn, 0.5초 안팎) 보기는 그대로이고, 취소는 프로세스를 끝내는 것으로 된다.
PyMuPDF·Pillow·OpenCV 는 작업 프로세스에서만 불러온다(D3) — 이 모듈의 클라이언트 쪽은 PyMuPDF·PySide6 를 import하지 않는다.

통신(작업 → 클라이언트): ("progress", 한 수, 전체) …, ("done", 결과 dict). "done" 없이 끝나면
취소(JOB_CANCELLED)거나 급사(EXPORT_INTERRUPTED). 그때 남은 임시 파일(`<산출물>` + TMP_SUFFIX)은 클라이언트가 지운다.
"""
from __future__ import annotations

import itertools
import logging
import multiprocessing
import os
import threading
from dataclasses import dataclass
from typing import Callable

from bom_pdf.engine.exportstatus import EXPORT_ERROR, EXPORT_INTERRUPTED, JOB_CANCELLED, TMP_SUFFIX
from bom_pdf.paths import long_path

log = logging.getLogger(__name__)

KIND_BUILD = "build"
KIND_COMPRESS = "compress"
KIND_IMAGE_SCAN = "image_scan"  # 1-5 사진 정보·미리보기(산출물 없음)
KIND_IMAGE_PDF = "image_pdf"    # 1-5 사진 → PDF


def job_main(kind: str, args: tuple, conn) -> None:
    """작업 프로세스 본체."""
    from bom_pdf.engine import build  # 여기서만 PyMuPDF

    def progress(done: int, total: int) -> None:
        conn.send(("progress", done, total))

    try:
        if kind == KIND_BUILD:
            result = build.build(args[0], progress)
        elif kind == KIND_COMPRESS:
            result = build.compress(*args, progress=progress)
        elif kind == KIND_IMAGE_SCAN:
            from bom_pdf.engine import images  # Pillow·pillow-heif — 작업 프로세스에서만
            result = images.scan(args[0], progress)
        elif kind == KIND_IMAGE_PDF:
            from bom_pdf.engine import images  # + OpenCV(문서 사진 모드)
            result = images.build_pdf(args[0], progress)
        elif kind == "_sleep":  # 시험용: 취소·급사 흉내
            import time

            for p in args[0]:
                with open(p + TMP_SUFFIX, "wb") as f:
                    f.write(b"partial")
            conn.send(("progress", 0, 1))
            time.sleep(60)
            result = {"status": "ok"}
        else:
            result = {"status": EXPORT_ERROR, "reason": "kind"}
    except Exception as e:  # 경로·본문이 든 메시지는 보내지 않는다
        result = {"status": EXPORT_ERROR, "reason": type(e).__name__}
    try:
        conn.send(("done", result))
    finally:
        conn.close()


@dataclass
class JobProgress:
    job_id: int
    done: int
    total: int


@dataclass
class JobDone:
    job_id: int
    kind: str
    result: dict  # status: ok / changed / refused / error / not_smaller / cancelled / interrupted


class JobRunner:
    """클라이언트(UI 프로세스) 쪽. 이벤트 콜백은 수신 스레드에서 불린다."""

    def __init__(self, on_event: Callable[[object], None]):
        self._on_event = on_event
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._jobs: dict[int, dict] = {}  # job_id -> {"proc", "cancelled", "tmps"}

    def start(self, kind: str, args: tuple, out_paths: list[str]) -> int:
        """작업 시작. out_paths 는 남은 임시 파일을 지울 산출물 경로들."""
        ctx = multiprocessing.get_context("spawn")
        r, w = ctx.Pipe(duplex=False)
        proc = ctx.Process(target=job_main, args=(kind, args, w), daemon=True, name=f"bom-pdf-job-{kind}")
        proc.start()
        w.close()
        job_id = next(self._ids)
        with self._lock:
            self._jobs[job_id] = {"proc": proc, "cancelled": False,
                                  "tmps": [long_path(p + TMP_SUFFIX) for p in out_paths]}
        threading.Thread(target=self._read, args=(job_id, kind, r, proc), daemon=True,
                         name=f"bom-pdf-job-reader-{job_id}").start()
        log.info("job %s started kind=%s outputs=%d pid=%s", job_id, kind, len(out_paths), proc.pid)
        return job_id

    def cancel(self, job_id: int) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job["cancelled"] = True
        job["proc"].terminate()

    def running(self) -> list[int]:
        with self._lock:
            return list(self._jobs)

    def shutdown(self, timeout: float = 3.0) -> None:
        """프로그램 종료 — 남은 작업을 끝내고 임시 파일을 바로 지운다(수신 스레드를 기다리지 않음)."""
        with self._lock:
            jobs = list(self._jobs.values())
        for job in jobs:
            job["cancelled"] = True
            job["proc"].terminate()
        for job in jobs:
            job["proc"].join(timeout)
            for t in job["tmps"]:
                try:
                    os.unlink(t)
                except OSError:
                    pass

    def _read(self, job_id: int, kind: str, conn, proc) -> None:
        result = None
        try:
            while True:
                msg = conn.recv()
                if msg[0] == "progress":
                    self._emit(JobProgress(job_id, msg[1], msg[2]))
                elif msg[0] == "done":
                    result = msg[1]
                    break
        except (EOFError, OSError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
        proc.join(5.0)
        with self._lock:
            job = self._jobs.pop(job_id, {})
        if result is None:
            result = {"status": JOB_CANCELLED if job.get("cancelled") else EXPORT_INTERRUPTED}
            for t in job.get("tmps", ()):
                if os.path.exists(t):
                    try:
                        os.unlink(t)
                    except OSError:
                        log.warning("job %s tmp cleanup failed", job_id)
        log.info("job %s done status=%s", job_id, result.get("status"))
        self._emit(JobDone(job_id, kind, result))

    def _emit(self, ev) -> None:
        try:
            self._on_event(ev)
        except Exception:
            log.exception("job event handler failed")
