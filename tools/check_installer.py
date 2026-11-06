"""설치 프로그램 점검(숫자·키 이름만 출력, 합성 PDF 만 사용).

사용: .venv/Scripts/python.exe tools/check_installer.py [--setup release\\bom-pdf-Setup-0.1.0.exe] [--real]

기본(점검 모드, 이 PC 설정 무변경): 임시 폴더에 `/S /TEST /D=` 설치(바로 가기·프로그램 목록·파일 연결 없음)
  → 설치된 EXE `--self-test` → 실행 중 업그레이드는 거절되고 파일이 그대로인지 → 닫은 뒤 업그레이드 성공
  → `uninstall.exe /S _?=` → 남은 파일 0, 관련 레지스트리 전후 동일.

--real: 이 PC 의 기본 위치(%LOCALAPPDATA%\\Programs\\bom-pdf)에 실제로 `/S` 설치 → 파일 연결 키·바로 가기 확인
  → ProgID `BomPdf.Document` 로 합성 PDF 3개를 탐색기처럼 파일마다 따로 열어(ShellExecuteEx, lpClass) 한 창의
  탭 3개로 모이는지(데이터 폴더는 임시 BOM_PDF_HOME, 사용자의 %APPDATA%\\bom-pdf 무관) → 제거 → 전후 레지스트리·
  파일 차이를 출력. 기본 앱(UserChoice)은 읽기만 하고 바꾸지 않는다. 이미 설치돼 있으면 하지 않는다.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "spike"))

EXE = "bom-pdf.exe"
PROGID = "BomPdf.Document"
UNINSTALL = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\bom-pdf"
WATCH_KEYS = [
    r"Software\Classes\.pdf",
    r"Software\Classes\BomPdf.Document",
    r"Software\Classes\Applications\bom-pdf.exe",
    r"Software\bom-pdf",
    r"Software\RegisteredApplications",
    UNINSTALL,
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.pdf",
]


# ------------------------------------------------------------------ 레지스트리


def read_tree(path: str) -> dict | None:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
    except OSError:
        return None
    out: dict = {"values": {}, "keys": {}}
    with key:
        i = 0
        while True:
            try:
                name, data, typ = winreg.EnumValue(key, i)
            except OSError:
                break
            out["values"][name] = repr(data) if not isinstance(data, (str, int)) else data
            i += 1
        i = 0
        subs = []
        while True:
            try:
                subs.append(winreg.EnumKey(key, i))
            except OSError:
                break
            i += 1
    for s in subs:
        out["keys"][s] = read_tree(path + "\\" + s)
    return out


def snapshot() -> dict:
    return {k: read_tree(k) for k in WATCH_KEYS}


def flatten(tree: dict | None, prefix: str, acc: dict) -> dict:
    if tree is None:
        return acc
    acc[prefix] = "<key>"
    for n, v in tree["values"].items():
        acc[f"{prefix} :: {n or '(기본값)'}"] = v
    for n, sub in tree["keys"].items():
        flatten(sub, f"{prefix}\\{n}", acc)
    return acc


def diff(before: dict, after: dict) -> dict:
    a = {}
    b = {}
    for k in WATCH_KEYS:
        flatten(before[k], "HKCU\\" + k, a)
        flatten(after[k], "HKCU\\" + k, b)
    return {"added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "changed": sorted(k for k in set(a) & set(b) if a[k] != b[k])}


def user_choice(snap: dict) -> object:
    t = snap[WATCH_KEYS[-1]]
    if not t or "UserChoice" not in t["keys"] or t["keys"]["UserChoice"] is None:
        return None
    return t["keys"]["UserChoice"]["values"].get("ProgId")


# ------------------------------------------------------------------ 실행


def run(exe: Path | str, *args: str, timeout: int = 600) -> int:
    return subprocess.run([str(exe), *args], timeout=timeout).returncode


def procs_in(folder: Path):
    import psutil

    folder = os.path.normcase(str(folder.resolve()))
    for p in psutil.process_iter(["exe", "pid", "ppid"]):
        exe = p.info.get("exe")
        if exe and os.path.normcase(exe).startswith(folder):
            yield p


def close_app(folder: Path) -> None:
    """이 점검이 띄운(설치 폴더의) bom-pdf 만 닫는다 — 창에 WM_CLOSE(taskkill, /F 없이), 남으면 강제."""
    import psutil

    mains = [p for p in procs_in(folder) if not any(q.pid == p.ppid() for q in procs_in(folder))]
    for p in mains:
        subprocess.run(["taskkill", "/PID", str(p.pid)], capture_output=True)
    gone, alive = psutil.wait_procs(list(procs_in(folder)), timeout=15)
    for p in alive:
        p.kill()
    psutil.wait_procs(alive, timeout=10)


class SHELLEXECUTEINFOW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]


def shell_open_with_progid(path: Path) -> bool:
    """탐색기가 파일 하나를 여는 것과 같은 경로(ProgID 의 open 명령)로 연다 — 기본 앱 설정과 무관."""
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x1 | 0x400  # SEE_MASK_CLASSNAME | SEE_MASK_FLAG_NO_UI
    info.lpVerb = "open"
    info.lpFile = str(path)
    info.lpClass = PROGID
    info.nShow = 1
    return bool(ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)))


def files_under(folder: Path) -> list[Path]:
    return [f for f in folder.rglob("*") if f.is_file()] if folder.exists() else []


# ------------------------------------------------------------------ 점검 모드


def check_test_mode(setup: Path) -> dict:
    import conftest

    rep: dict = {"mode": "test"}
    before = snapshot()
    work = Path(tempfile.mkdtemp(prefix="bom-pdf-install-check-"))
    target = work / "app"
    home = work / "home"
    os.environ["BOM_PDF_HOME"] = str(home)
    try:
        # O6: Windows 11 이 아니면(빌드 22000 미만인 척) 아무것도 쓰지 않고 멈춰야 한다(종료 코드 3)
        rep["win10_refused_exit"] = run(setup, "/S", "/TEST", "/WINBUILD=19045", f"/D={target}")
        rep["win10_refused_files"] = len(files_under(target)) if target.exists() else 0
        rep["install_exit"] = run(setup, "/S", "/TEST", f"/D={target}")
        files = files_under(target)
        rep["installed_files"] = len(files)
        rep["installed_mb"] = round(sum(f.stat().st_size for f in files) / 2**20, 1)
        exe = target / EXE
        assert exe.is_file(), "설치된 EXE 없음"
        pdf = conftest.make_text_pdf(work / "selftest.pdf")
        report = work / "selftest.json"
        rep["selftest_exit"] = run(exe, "--self-test", str(report), str(pdf))
        rep["selftest_ok"] = json.loads(report.read_text(encoding="utf-8")).get("ok") if report.exists() else False

        # 실행 중 업그레이드: 조용한 설치는 거절(닫아 달라는 물음 → 취소)하고 파일을 건드리지 않아야 한다
        proc = subprocess.Popen([str(exe), str(pdf)])
        time.sleep(4)
        mtime = exe.stat().st_mtime_ns
        rep["upgrade_while_running_exit"] = run(setup, "/S", "/TEST", f"/D={target}")
        rep["upgrade_while_running_files_kept"] = exe.is_file() and exe.stat().st_mtime_ns == mtime and \
            (target / "_internal").is_dir()
        close_app(target)
        proc.wait(timeout=10)
        rep["upgrade_after_close_exit"] = run(setup, "/S", "/TEST", f"/D={target}")
        rep["upgrade_after_close_files"] = len(files_under(target))

        rep["uninstall_exit"] = run(target / "uninstall.exe", "/S", f"_?={target}")
        left = [f for f in files_under(target) if f.name != "uninstall.exe"]
        rep["left_files_after_uninstall"] = len(left)
        rep["registry_diff"] = diff(before, snapshot())
    finally:
        close_app(target) if target.exists() else None
        shutil.rmtree(work, ignore_errors=True)
    rep["ok"] = (rep.get("win10_refused_exit") == 3 and rep.get("win10_refused_files") == 0
                 and rep.get("install_exit") == 0 and rep.get("selftest_ok") is True
                 and rep.get("upgrade_while_running_exit") != 0 and rep.get("upgrade_while_running_files_kept")
                 and rep.get("upgrade_after_close_exit") == 0 and rep.get("uninstall_exit") == 0
                 and rep.get("left_files_after_uninstall") == 0
                 and not any(rep["registry_diff"].values()))
    return rep


# ------------------------------------------------------------------ 실제 설치


def check_real(setup: Path) -> dict:
    import conftest

    os.environ.pop("QT_QPA_PLATFORM", None)  # conftest 의 offscreen 을 빼고 실제 창으로 띄운다(아이콘·작업 표시줄 확인)

    target = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "bom-pdf"
    if target.exists() or read_tree(UNINSTALL) is not None:
        raise SystemExit(f"이미 설치돼 있습니다({target}) — 실제 설치 점검은 하지 않습니다")
    rep: dict = {"mode": "real", "target": str(target)}
    before = snapshot()
    rep["user_choice_before"] = user_choice(before)
    start_menu = Path(os.environ["APPDATA"]) / r"Microsoft\Windows\Start Menu\Programs" / "bom-pdf.lnk"
    desktop = Path(os.environ["USERPROFILE"]) / "Desktop" / "bom-pdf.lnk"
    work = Path(tempfile.mkdtemp(prefix="bom-pdf-real-check-"))
    home = work / "home"
    os.environ["BOM_PDF_HOME"] = str(home)  # 설치본 시험 실행의 데이터 폴더(사용자 %APPDATA%\bom-pdf 아님)
    appdata_user = Path(os.environ["APPDATA"]) / "bom-pdf"
    appdata_before = sorted(str(p.relative_to(appdata_user)) for p in files_under(appdata_user))
    try:
        rep["install_exit"] = run(setup, "/S")
        mid = snapshot()
        rep["installed_files"] = len(files_under(target))
        rep["shortcuts"] = {"start_menu": start_menu.exists(), "desktop": desktop.exists()}
        rep["registry_added_by_install"] = diff(before, mid)
        cmd = (read_tree(r"Software\Classes\BomPdf.Document\shell\open\command") or {}).get("values", {}).get("")
        rep["open_command_ok"] = cmd == f'"{target / EXE}" "%1"'
        rep["registered_app"] = (read_tree(r"Software\RegisteredApplications") or {}).get("values", {}).get("bom-pdf")
        rep["user_choice_after_install"] = user_choice(mid)

        # 탐색기에서 여러 파일 고르고 Enter = 파일마다 ProgID open 명령이 따로 실행된다(MultiSelectModel=Document)
        pdfs = [conftest.make_text_pdf(work / f"multi-{i}.pdf") for i in range(1, 4)]
        t0 = time.perf_counter()
        rep["shell_open_calls_ok"] = all(shell_open_with_progid(p) for p in pdfs)
        session = home / "session.json"
        tabs = []
        while time.perf_counter() - t0 < 40:
            try:
                tabs = [t["path"] for t in json.loads(session.read_text(encoding="utf-8")).get("tabs", [])]
            except (OSError, ValueError):
                tabs = []
            if len(tabs) >= 3:
                break
            time.sleep(0.5)
        time.sleep(2)
        rep["multi_open_tabs"] = len(tabs)
        rep["multi_open_all_files"] = sorted(os.path.normcase(t) for t in tabs) == \
            sorted(os.path.normcase(str(p)) for p in pdfs)
        rep["multi_open_main_processes"] = sum(
            1 for p in procs_in(target) if not any(q.pid == p.ppid() for q in procs_in(target)))
        rep["multi_open_s"] = round(time.perf_counter() - t0, 1)
        close_app(target)

        rep["uninstall_exit"] = run(target / "uninstall.exe", "/S", f"_?={target}")
        (target / "uninstall.exe").unlink(missing_ok=True)  # _?= 로 그 자리 실행하면 자기 자신은 남는다
        if target.exists() and not any(target.iterdir()):
            target.rmdir()
        after = snapshot()
        rep["left_files"] = [str(p) for p in files_under(target)]
        rep["install_dir_exists"] = target.exists()
        rep["shortcuts_after_uninstall"] = {"start_menu": start_menu.exists(), "desktop": desktop.exists()}
        rep["registry_diff_before_vs_after_uninstall"] = diff(before, after)
        rep["user_choice_after_uninstall"] = user_choice(after)
        rep["user_appdata_unchanged"] = appdata_before == sorted(
            str(p.relative_to(appdata_user)) for p in files_under(appdata_user))
    finally:
        if target.exists():
            close_app(target)
        shutil.rmtree(work, ignore_errors=True)
    rep["ok"] = (rep.get("install_exit") == 0 and rep.get("open_command_ok") and rep.get("multi_open_tabs") == 3
                 and rep.get("multi_open_all_files") and rep.get("multi_open_main_processes") == 1
                 and rep.get("uninstall_exit") == 0 and not rep.get("left_files")
                 and rep["user_choice_before"] == rep["user_choice_after_uninstall"])
    return rep


def main(argv: list[str] | None = None) -> int:
    import build_installer
    import conftest  # 픽스처 생성기 — import 때 만드는 임시 데이터 폴더는 끝나면 지운다

    conftest_home = os.environ["BOM_PDF_HOME"]

    ap = argparse.ArgumentParser()
    ap.add_argument("--setup")
    ap.add_argument("--real", action="store_true")
    a = ap.parse_args(argv)
    setup = Path(a.setup) if a.setup else build_installer.installer_path()
    if not setup.is_file():
        raise SystemExit(f"설치 프로그램 없음: {setup}")
    try:
        rep = check_real(setup) if a.real else check_test_mode(setup)
    finally:
        shutil.rmtree(conftest_home, ignore_errors=True)
    print(json.dumps(rep, ensure_ascii=False, indent=1))
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
