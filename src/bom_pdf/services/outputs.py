"""산출물 이름·기본 폴더 규칙 (system-design §3-3, 결정 D4). PySide6 를 import하지 않는다.

- 기본 이름: `<원본 이름>_<접미>.pdf`, 같은 이름이 있으면 `<원본 이름>_<접미>(2).pdf`, `(3)` …
- 기본 폴더는 **원본 폴더가 아니다**: 마지막으로 내보낸 폴더(원본 폴더가 아닐 때) → 사용자 문서 폴더.
  원본 폴더는 사용자가 대화상자에서 직접 고를 때만 쓴다. 원본이 여럿(사진 → PDF)이면 그 모든 폴더를 피한다.
- 산출물 경로가 열린 원본(어느 문서든)과 같으면 거절한다 — 원본을 덮어쓰지 않는다.
- 마지막 내보낸 폴더는 데이터 폴더 `settings.json` 의 `lastExportDir`.
"""
from __future__ import annotations

import os
from pathlib import Path

from bom_pdf import paths

SUFFIX_NOTES = "_메모"
_SETTINGS = "settings.json"


def default_name(original: str, suffix: str) -> str:
    stem = Path(original).stem
    return f"{stem}{suffix}.pdf"


def unique_path(folder: str | os.PathLike, name: str) -> Path:
    folder = Path(folder)
    p = folder / name
    if not p.exists():
        return p
    stem, ext = os.path.splitext(name)
    n = 2
    while (folder / f"{stem}({n}){ext}").exists():
        n += 1
    return folder / f"{stem}({n}){ext}"


def same_path(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    """같은 파일인지 — 둘 다 있으면 samefile(대소문자·짧은 이름 무관), 아니면 정규화한 경로 비교."""
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def same_folder(a: str | os.PathLike, b: str | os.PathLike) -> bool:
    return same_path(a, b)


def default_export_dir(original: str, last_dir: str | None, documents_dir: str) -> str:
    """원본 폴더가 아닌 기본 폴더. last_dir 가 있고 원본 폴더가 아니면 그것, 아니면 문서 폴더."""
    orig_dir = os.path.dirname(os.path.abspath(original))
    if last_dir and os.path.isdir(last_dir) and not same_folder(last_dir, orig_dir):
        return last_dir
    # 문서 폴더가 OneDrive 로 옮겨진 PC 는 Qt 가 옮긴 곳(예: %USERPROFILE%\OneDrive\문서)을 준다(O4 확인).
    # OneDrive 를 끊어 그 폴더가 없으면 사용자 폴더로.
    if documents_dir and os.path.isdir(documents_dir) and not same_folder(documents_dir, orig_dir):
        return documents_dir
    return os.path.expanduser("~")  # 문서 폴더가 곧 원본 폴더이거나 없는 드문 경우


def default_export_dir_for(originals: list[str], last_dir: str | None, documents_dir: str) -> str:
    """원본이 여러 폴더에 있을 때(사진 → PDF) — 그 어느 폴더도 아닌 기본 폴더."""
    orig_dirs = {os.path.dirname(os.path.abspath(p)) for p in originals}

    def ok(folder: str | None) -> bool:
        return bool(folder) and os.path.isdir(folder) and not any(same_folder(folder, d) for d in orig_dirs)

    for cand in (last_dir, documents_dir, os.path.expanduser("~")):
        if ok(cand):
            return cand
    return os.path.expanduser("~")


def default_export_path(original: str, suffix: str, last_dir: str | None, documents_dir: str) -> Path:
    folder = default_export_dir(original, last_dir, documents_dir)
    return unique_path(folder, default_name(original, suffix))


def is_protected(target: str, protected: list[str]) -> bool:
    """target 이 열린 원본 중 하나와 같은 파일이면 True."""
    return any(same_path(target, p) for p in protected)


# --------------------------------------------------------------------------- settings.json


def _settings_path(data_dir: Path | None) -> Path:
    return (data_dir or paths.data_dir()) / _SETTINGS


def last_export_dir(data_dir: Path | None = None) -> str | None:
    s = paths.read_json(_settings_path(data_dir), {})
    v = s.get("lastExportDir") if isinstance(s, dict) else None
    return v if isinstance(v, str) else None


def set_last_export_dir(folder: str, data_dir: Path | None = None) -> None:
    p = _settings_path(data_dir)
    s = paths.read_json(p, {})
    if not isinstance(s, dict):
        s = {}
    s["lastExportDir"] = folder
    paths.atomic_write_json(p, s)
