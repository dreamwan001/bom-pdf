"""이 PC 환경 정보 (O1) 와 메모리 등급 (O3). Qt·PyMuPDF 를 import하지 않는다.

- 환경 한 줄: Windows 빌드, RAM(전체·사용 가능), CPU 수·이름, 화면(호출자가 Qt 에서 넘김), 데이터 폴더 여유 공간,
  설치·실행 위치(사용자 폴더는 %USERPROFILE% 로 줄임). 파일 이름·문서 내용은 넣지 않는다.
- 시험용 흉내(환경 변수, 값이 있을 때만):
  `BOM_PDF_SIM_RAM_GB`   총 RAM 을 이 값으로 본다(RAM 등급 흉내, 예: 8)
  `BOM_PDF_SIM_AVAIL_MB` 사용 가능 메모리를 이 값으로 본다(메모리 부족 흉내)
  `BOM_PDF_SIM_SCREEN`   화면 목록 `1920x1080@1.25[,2560x1440@1.5]` (논리 폭x높이@배율) — 캐시 상한 계산용
  `BOM_PDF_SIM_SLOW_READ` 느린 저장소 근사 `MB/s:지연ms` (예: `10:2` NAS, `2:20` 느린 원격). 렌더 작업자는 열기·렌더
                          한 번마다 그동안 실제로 읽은 바이트·횟수만큼 잠들고, sha256 계산은 조각마다 잠든다(O4).
                          대역폭 다툼·OS 캐시 효과는 흉내 내지 않는 **근사**다.
"""
from __future__ import annotations

import ctypes
import os
import platform
import shutil
import sys
import time

ENV_SIM_RAM_GB = "BOM_PDF_SIM_RAM_GB"
ENV_SIM_AVAIL_MB = "BOM_PDF_SIM_AVAIL_MB"
ENV_SIM_SCREEN = "BOM_PDF_SIM_SCREEN"
ENV_SIM_SLOW_READ = "BOM_PDF_SIM_SLOW_READ"
LOW_AVAIL_MB = 768  # 사용 가능 메모리가 이보다 적으면 미리 그리기를 멈추고 보이는 것만 그린다
_LOW_CHECK_S = 2.0


class _MemoryStatus(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _real_memory_mb() -> tuple[float, float]:
    """(전체, 사용 가능) MB. 못 읽으면 (0, 0)."""
    if sys.platform != "win32":
        return 0.0, 0.0
    st = _MemoryStatus()
    st.dwLength = ctypes.sizeof(_MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
        return 0.0, 0.0
    return st.ullTotalPhys / 2**20, st.ullAvailPhys / 2**20


def _env_float(name: str) -> float | None:
    try:
        v = float(os.environ.get(name, ""))
    except ValueError:
        return None
    return v if v > 0 else None


def total_ram_gb() -> float:
    sim = _env_float(ENV_SIM_RAM_GB)
    if sim is not None:
        return sim
    total, _ = _real_memory_mb()
    return total / 1024 if total else 8.0  # 모르면 낮은 등급으로


def available_mb() -> float:
    sim = _env_float(ENV_SIM_AVAIL_MB)
    if sim is not None:
        return sim
    total, avail = _real_memory_mb()
    return avail if total else float(LOW_AVAIL_MB * 4)


_low = {"t": -1e9, "v": False}


def memory_low() -> bool:
    """사용 가능 메모리가 LOW_AVAIL_MB 미만인가 — 2초에 한 번만 실제로 묻는다."""
    now = time.monotonic()
    if now - _low["t"] >= _LOW_CHECK_S:
        _low["t"] = now
        _low["v"] = available_mb() < LOW_AVAIL_MB
    return _low["v"]


def simulated_screens() -> list[tuple[float, float, float]] | None:
    raw = os.environ.get(ENV_SIM_SCREEN, "").strip()
    if not raw:
        return None
    out = []
    try:
        for part in raw.split(","):
            size, _, dpr = part.strip().partition("@")
            w, _, h = size.partition("x")
            out.append((float(w), float(h), float(dpr or 1)))
    except ValueError:
        return None
    return out or None


def slow_read_spec() -> tuple[float, float] | None:
    """`BOM_PDF_SIM_SLOW_READ=MB/s:지연ms` → (바이트/초, 읽기 한 번 지연 초). 없거나 틀리면 None."""
    raw = os.environ.get(ENV_SIM_SLOW_READ, "").strip()
    if not raw:
        return None
    mbps, _, lat = raw.partition(":")
    try:
        bps = float(mbps) * 2**20
        lat_s = float(lat or 0) / 1000
    except ValueError:
        return None
    return (bps, lat_s) if bps > 0 else None


def slow_read_delay(nbytes: int, nreads: int, spec: tuple[float, float]) -> float:
    return nbytes / spec[0] + nreads * spec[1]


class SlowReadMeter:
    """(시험·측정 전용) 이 프로세스가 `begin()` 이후 읽은 바이트·횟수만큼 `settle()` 에서 잠든다 — 느린 저장소 근사.

    파이프 읽기도 OS 읽기 횟수에 들어가므로 명령을 받은 **뒤에** begin, 작업 직후 settle 한다.
    """

    def __init__(self, spec: tuple[float, float]):
        import psutil

        self.spec = spec
        self._p = psutil.Process()
        self._mark = (0, 0)
        self.total_bytes = 0
        self.total_reads = 0

    def _now(self) -> tuple[int, int]:
        c = self._p.io_counters()
        return c.read_bytes, c.read_count

    def begin(self) -> None:
        self._mark = self._now()

    def settle(self) -> None:
        b, n = self._now()
        db, dn = b - self._mark[0], n - self._mark[1]
        self.total_bytes += db
        self.total_reads += dn
        if db > 0 or dn > 0:
            time.sleep(slow_read_delay(db, dn, self.spec))
        self._mark = self._now()


def windows_version() -> str:
    if sys.platform != "win32":
        return platform.platform()
    v = sys.getwindowsversion()
    name = "Windows 11" if v.build >= 22000 else "Windows 10" if v.major == 10 else "Windows"
    edition = ""
    try:
        edition = platform.win32_edition() or ""
    except Exception:
        pass
    return f"{name} {edition} {v.major}.{v.minor}.{v.build}".replace("  ", " ")


def cpu_name() -> str:
    if sys.platform == "win32":
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return str(winreg.QueryValueEx(k, "ProcessorNameString")[0]).strip()
        except OSError:
            pass
    return platform.processor() or "?"


def short_path(p: str) -> str:
    home = os.path.expanduser("~")
    if home and os.path.normcase(p).startswith(os.path.normcase(home)):
        return "%USERPROFILE%" + p[len(home):]
    return p


def install_location() -> str:
    if getattr(sys, "frozen", False):
        return "포장본 " + short_path(os.path.dirname(sys.executable))
    return "소스 실행"


def environment(screens: list[tuple[float, float, float]], data_dir=None) -> dict:
    """환경 요약 dict(로그 한 줄·환경 점검 창 공용). screens: [(논리 폭, 높이, 배율)]."""
    total, avail = _real_memory_mb()
    free_gb = None
    if data_dir is not None:
        try:
            free_gb = round(shutil.disk_usage(data_dir).free / 2**30, 1)
        except OSError:
            pass
    env = {"os": windows_version(), "ram_total_gb": round(total / 1024, 1), "ram_avail_gb": round(avail / 1024, 1),
           "cpus": os.cpu_count(), "cpu": cpu_name(),
           "screens": [f"{int(w)}x{int(h)}@{dpr:g}" for w, h, dpr in screens],
           "data_free_gb": free_gb, "install": install_location()}
    sims = {k: os.environ[k] for k in (ENV_SIM_RAM_GB, ENV_SIM_AVAIL_MB, ENV_SIM_SCREEN, ENV_SIM_SLOW_READ)
            if os.environ.get(k)}
    if sims:
        env["simulated"] = sims
    return env
