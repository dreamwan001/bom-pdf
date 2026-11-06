"""사용자 데이터 위치와 JSON 원자적 저장.

데이터 폴더: `%APPDATA%\\bom-pdf\\` (시험·격리 실행은 환경 변수 `BOM_PDF_HOME`).
원본 PDF 폴더에는 아무것도 쓰지 않는다 — 이 모듈의 함수는 데이터 폴더 안에서만 쓴다.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from bom_pdf import APP_NAME

ENV_HOME = "BOM_PDF_HOME"


LONG_PATH_AT = 240  # 이 길이 이상이면 \\?\ 형식으로 (MAX_PATH 260, 폴더 만들기는 248)
_EXT = "\\\\?\\"
_EXT_UNC = "\\\\?\\UNC\\"


def long_path(p: str | os.PathLike) -> str:
    """Windows 에서 긴 경로(240자 이상)를 `\\\\?\\` 형식으로 바꾼다 — 레지스트리 LongPathsEnabled 가 꺼진 PC 에서도
    열기·쓰기·바꾸기가 되게(O4). 짧은 경로·다른 OS 는 그대로. UNC(`\\\\서버\\공유`)는 `\\\\?\\UNC\\서버\\공유`.
    """
    s = os.fspath(p)
    if sys.platform != "win32" or s.startswith(_EXT) or len(s) < LONG_PATH_AT:
        return s
    s = os.path.abspath(s).replace("/", "\\")
    if len(s) < LONG_PATH_AT:
        return s
    if s.startswith("\\\\"):
        return _EXT_UNC + s[2:]
    return _EXT + s


def plain_path(p: str) -> str:
    """long_path 의 반대 — 화면·기록에 보일 보통 경로."""
    if p.startswith(_EXT_UNC):
        return "\\\\" + p[len(_EXT_UNC):]
    if p.startswith(_EXT):
        return p[len(_EXT):]
    return p


def data_dir() -> Path:
    override = os.environ.get(ENV_HOME)
    if override:
        base = Path(override)
    else:
        appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        base = Path(appdata) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base


def logs_dir() -> Path:
    p = data_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


# Windows: 대상 파일을 백신·색인·동기화 프로그램이 잠깐 열고 있으면(삭제 공유 없이) os.replace 가
# PermissionError(WinError 5/32)로 실패한다. 잠깐 기다렸다 다시 하면 대개 된다. 합 약 0.8초.
REPLACE_RETRY_DELAYS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4)
READ_RETRY_DELAYS = (0.02, 0.05, 0.1, 0.2)


def replace_with_retry(src: str | os.PathLike, dst: str | os.PathLike) -> None:
    for delay in (*REPLACE_RETRY_DELAYS, None):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if delay is None:
                raise
            time.sleep(delay)


def atomic_write_json(path: Path, obj: Any) -> None:
    """같은 폴더 임시 파일 → fsync → replace(잠깐 잠겨 있으면 다시 시도). 중간에 죽어도 기존 파일은 온전하다.

    죽은 시점에 따라 `<이름>.<임의>.tmp` 가 남을 수 있다 — 읽는 쪽은 무시한다(주석 저장소는 오래된 것을 지운다).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        replace_with_retry(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_json_strict(path: Path) -> Any:
    """읽기만 한다 — 없으면 FileNotFoundError, 깨졌으면 ValueError(OSError). 손상 처리는 호출자가 한다."""
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def read_json(path: Path, default: Any) -> Any:
    """없으면 default. 깨졌으면(내용 오류) `.damaged`로 옮겨 보존하고 default.

    읽기 자체가 실패(잠김·권한 등 OSError)하면 파일은 멀쩡할 수 있으므로 옮기지 않고 default.
    """
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except OSError:
        return default
    except ValueError:
        try:
            os.replace(path, path.with_name(path.name + ".damaged"))
        except OSError:
            pass
        return default
