"""원본 파일 읽기 쪽 공용 도구 (O4) — sha256·해시 캐시·느린 저장소 판정. Qt·PyMuPDF 를 import하지 않는다.

UI 프로세스·렌더 작업자·작업 프로세스가 함께 쓴다.

- 해시 캐시: `(경로, 크기, mtime_ns) → sha256`, 데이터 폴더 `hash_cache.json`(원자적 저장). 같은 파일을 다시 열거나
  저장·내보내기 전에 "원본이 연 뒤에 바뀌었나"를 확인할 때 파일 전체를 다시 읽지 않는다. 크기·수정 시각이 그대로면
  내용도 그대로라고 본다(주석 사이드카 식별과 같은 가정, 1-3).
- 원격 판정: 네트워크 드라이브(UNC·연결한 드라이브 문자)·클라우드 "온라인 전용" 파일(Dropbox·OneDrive 자리 표시자).
  이런 파일은 MuPDF 가 잘게(4KB 안팎) 여러 번 읽으면 왕복 지연이 쌓이므로, 작은 것은 작업자가 한 번에 순서대로 읽어
  메모리에서 연다(engine.worker). 시험·측정은 `BOM_PDF_SIM_REMOTE=1`(모든 파일을 원격으로 본다).
- 읽기는 원본을 읽기 전용으로 연다. 쓰는 것은 데이터 폴더의 캐시 하나뿐이다.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import sys
import threading
import time
from pathlib import Path

from bom_pdf import paths, sysinfo

log = logging.getLogger(__name__)

ENV_SIM_REMOTE = "BOM_PDF_SIM_REMOTE"
CHUNK = 1 << 20
MEM_OPEN_MAX = 32 * 2**20  # 원격 파일을 한 번에 순서대로 읽어 메모리에서 열 최대 크기
_FILE_ATTRIBUTE_OFFLINE = 0x1000
_FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x40000
_FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x400000
_DRIVE_REMOTE = 4

hash_bytes_read = 0  # 이 프로세스에서 sha256 계산으로 읽은 바이트 합(측정·시험용)


def file_sha256(path: str | os.PathLike, chunk: int = CHUNK, cancel: threading.Event | None = None) -> str | None:
    """파일 전체 sha256. cancel 이 서면 None. 느린 저장소 흉내(BOM_PDF_SIM_SLOW_READ)면 조각마다 잠든다."""
    global hash_bytes_read
    spec = sysinfo.slow_read_spec()
    h = hashlib.sha256()
    with open(paths.long_path(path), "rb") as f:
        while b := f.read(chunk):
            h.update(b)
            hash_bytes_read += len(b)
            if spec is not None:  # 시험·측정 전용 느린 저장소 근사
                time.sleep(sysinfo.slow_read_delay(len(b), 1, spec))
            if cancel is not None and cancel.is_set():
                return None
    return h.hexdigest()


def stat_key(path: str, size: int, mtime_ns: int) -> str:
    return f"{os.path.normcase(os.path.abspath(paths.plain_path(path)))}|{size}|{mtime_ns}"


class HashCache:
    """`(경로, 크기, mtime_ns) → sha256`. 데이터 폴더 JSON, 원자적 저장."""

    MAX_ENTRIES = 2000

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None

    @staticmethod
    def key(path: str) -> str | None:
        try:
            st = os.stat(paths.long_path(path))
        except OSError:
            return None
        return stat_key(path, st.st_size, st.st_mtime_ns)

    def _load(self) -> dict:
        if self._data is None:
            data = paths.read_json(self.path, {})
            self._data = data if isinstance(data, dict) else {}
        return self._data

    def get(self, key: str) -> str | None:
        with self._lock:
            e = self._load().get(key)
            return e.get("sha256") if isinstance(e, dict) else None

    def cached(self, path: str) -> str | None:
        """지금 (경로, 크기, 수정 시각)으로 캐시에 있으면 sha256, 없으면 None — 파일 내용은 읽지 않는다."""
        key = self.key(path)
        return self.get(key) if key is not None else None

    def put(self, key: str, sha256: str) -> None:
        with self._lock:
            data = self._load()
            data[key] = {"sha256": sha256, "t": time.time()}
            if len(data) > self.MAX_ENTRIES:
                for k in sorted(data, key=lambda k: data[k].get("t", 0))[: len(data) - self.MAX_ENTRIES]:
                    del data[k]
            try:
                paths.atomic_write_json(self.path, data)
            except OSError:
                log.exception("hash cache save failed")

    def sha256_of(self, path: str, cancel: threading.Event | None = None) -> str | None:
        key = self.key(path)
        if key is not None and (hit := self.get(key)):
            return hit
        digest = file_sha256(path, cancel=cancel)
        if digest is None:
            return None
        key_after = self.key(path)
        if key_after is not None and key_after == key:  # 계산 중 파일이 바뀌지 않았을 때만 저장
            self.put(key, digest)
        return digest


def source_unchanged(path: str, expect_sha256: str | None) -> bool:
    """저장·내보내기 직전 확인(작업자·작업 프로세스): 원본이 연 뒤에 바뀌지 않았나.

    해시 캐시에 지금 (경로, 크기, 수정 시각)의 sha256 이 있으면 그것으로(파일을 다시 읽지 않음), 없으면 전체를 읽어 비교.
    """
    if not expect_sha256:
        return True
    try:
        hit = HashCache(paths.data_dir() / "hash_cache.json").cached(path)
    except OSError:
        hit = None
    if hit is not None:
        return hit == expect_sha256
    return file_sha256(path) == expect_sha256


# --------------------------------------------------------------------------- 느린 저장소 판정


def _attributes(path: str) -> int:
    try:
        return int(getattr(os.stat(paths.long_path(path)), "st_file_attributes", 0))
    except OSError:
        return 0


def is_online_only(path: str) -> bool:
    """클라우드 자리 표시자(Dropbox·OneDrive "온라인 전용") — 읽으면 그때 내려받는다."""
    return bool(_attributes(path) & (_FILE_ATTRIBUTE_OFFLINE | _FILE_ATTRIBUTE_RECALL_ON_OPEN
                                     | _FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS))


def is_network(path: str) -> bool:
    p = os.path.abspath(paths.plain_path(path))
    if p.startswith("\\\\"):
        return True
    if sys.platform != "win32":
        return False
    drive = os.path.splitdrive(p)[0]
    if not drive:
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == _DRIVE_REMOTE
    except Exception:
        return False


def is_remote(path: str) -> bool:
    """네트워크·클라우드 온라인 전용 파일인가. 시험·측정은 BOM_PDF_SIM_REMOTE=1 이면 모두 원격."""
    if os.environ.get(ENV_SIM_REMOTE) == "1":
        return True
    return is_network(path) or is_online_only(path)


def read_small_remote(path: str) -> bytes | None:
    """원격이고 MEM_OPEN_MAX 이하이면 1MB 씩 순서대로 한 번 읽은 내용, 아니면 None(경로로 열면 된다).

    저장·내보내기·압축이 원본을 다시 열 때 쓴다 — MuPDF 의 잘게 나눈 읽기가 네트워크 왕복을 쌓지 않게.
    """
    if not is_remote(path):
        return None
    lp = paths.long_path(path)
    try:
        size = os.path.getsize(lp)
        if size <= 0 or size > MEM_OPEN_MAX:
            return None
        parts = []
        with open(lp, "rb") as f:
            while b := f.read(CHUNK):
                parts.append(b)
    except OSError:
        return None
    data = b"".join(parts)
    return data if len(data) == size else None


def background_io() -> None:
    """지금 스레드를 낮은 CPU·디스크 입출력 우선순위로(Windows THREAD_MODE_BACKGROUND_BEGIN) — 화면 렌더가 먼저.

    스레드가 끝나면 함께 풀린다. 실패해도 그대로 진행한다.
    """
    if sys.platform != "win32":
        return
    try:
        k = ctypes.windll.kernel32
        k.SetThreadPriority(k.GetCurrentThread(), 0x00010000)
    except Exception:
        pass
