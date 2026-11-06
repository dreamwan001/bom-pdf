"""저장 전 여유 공간 확인 (O4). PySide6·PyMuPDF 를 import하지 않는다.

새 파일은 늘 산출물 옆 임시 파일에 다 쓴 뒤 바꾸므로(원자 저장) 도중에 공간이 모자라면 실패할 뿐 원본·기존 파일은
안전하다. 다만 큰 작업을 몇십 초 돌린 뒤 실패하면 헛수고라, **시작 전에** 예상 크기 × 1.2 + 여유를 확인해 모자라면
시작하지 않고 알린다. 예상은 대략값이다(압축본은 원본 크기를 최댓값으로, 쪽 편집은 쪽 수 비례).
여유 공간을 못 읽으면(권한·일부 네트워크 드라이브) 막지 않는다 — 실패하면 임시 파일을 지우는 기존 보호가 있다.
"""
from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass

from bom_pdf.paths import long_path

log = logging.getLogger(__name__)

SAFETY = 1.2
TEMP_MARGIN = 32 * 2**20  # 저장 중 잠깐 더 쓰는 몫·파일 시스템 여유


@dataclass
class SpaceCheck:
    ok: bool
    need: int        # 바이트
    free: int | None  # 바이트, 모르면 None
    folder: str


def free_bytes(folder: str) -> int | None:
    """folder(없으면 가장 가까운 있는 상위 폴더)가 있는 드라이브의 여유 바이트. 모르면 None."""
    f = os.path.abspath(folder)
    while f and not os.path.isdir(long_path(f)):
        parent = os.path.dirname(f)
        if parent == f:
            return None
        f = parent
    try:
        return shutil.disk_usage(long_path(f)).free
    except OSError:
        log.warning("disk_usage failed")
        return None


def required(estimate: int) -> int:
    return int(max(0, estimate) * SAFETY) + TEMP_MARGIN


def check(folder: str, estimate: int) -> SpaceCheck:
    need = required(estimate)
    free = free_bytes(folder)
    return SpaceCheck(free is None or free >= need, need, free, folder)


def check_outputs(out_paths: list[str], estimate: int) -> SpaceCheck:
    """산출물이 여러 폴더(드라이브)에 걸치면 가장 모자란 곳 기준. estimate 는 전체 합."""
    folders = list(dict.fromkeys(os.path.dirname(os.path.abspath(p)) for p in out_paths))
    worst = None
    for f in folders:
        c = check(f, estimate)
        if worst is None or (not c.ok and worst.ok):
            worst = c
    return worst or SpaceCheck(True, required(estimate), None, "")


def file_size(path: str) -> int:
    try:
        return os.path.getsize(long_path(path))
    except OSError:
        return 0


def estimate_build(job: dict, page_counts: list[int]) -> int:
    """페이지 편집 결과(저장·추출·분할·병합): 원본 크기를 쪽 수로 나눠 쓴 쪽만큼 더한다(대략)."""
    per_page = []
    for s, n in zip(job["sources"], page_counts):
        per_page.append(file_size(s["path"]) / max(1, n))
    total = 0.0
    for o in job["outputs"]:
        for si, _p, _r in o["pages"]:
            total += per_page[si] if si < len(per_page) else 0
    return int(total)


def human(n: int) -> str:
    if n >= 2**30:
        return f"{n / 2**30:.1f}GB"
    if n >= 2**20:
        return f"{n / 2**20:.0f}MB"
    return f"{max(1, round(n / 1024))}KB"


def message(c: SpaceCheck) -> str:
    free = human(c.free) if c.free is not None else "알 수 없음"
    return ("저장할 곳의 여유 공간이 부족해 시작하지 않았습니다.\n"
            f"필요한 공간 약 {human(c.need)}, 남은 공간 {free}.\n"
            "다른 폴더(드라이브)를 고르거나 공간을 비운 뒤 다시 시도해 주세요. 원본과 기존 파일은 그대로입니다.")
