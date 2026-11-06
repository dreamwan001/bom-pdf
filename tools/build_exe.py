"""PyInstaller onedir 빌드 (D12) → `dist/bom-pdf/` (Git 무시). `--installer` 면 이어서 설치 프로그램
(`tools/build_installer.py` → `release/bom-pdf-Setup-<판>.exe`)까지 만든다.

사용: .venv/Scripts/python.exe tools/build_exe.py [--skip-selftest] [--installer] [--timeout 1200]

1. 이전 산출물(`build/pyi`, `dist/bom-pdf`)을 지운다 — 고친 소스가 빠지는 일 방지(위키 P07).
   EXE 버전 정보 `build/version_info.txt` 를 `__version__` 으로 만든다(아이콘은 spec 이 `assets/bom-pdf.ico`)
2. PATH 를 venv Scripts·System32 로 줄여 PyInstaller 를 돌린다(위키 P04). 제한 시간을 넘기면 멈춘다
3. `dist/bom-pdf/README.txt`(판·커밋·소스 위치) + `LICENSE`·`THIRD_PARTY_NOTICES.md` 복사 — AGPL 고지
4. 포장본 점검: 합성 PDF·합성 사진(실제 사건 자료 아님)으로 `bom-pdf.exe --self-test` — 렌더 작업자·작업 프로세스
   spawn, Pillow·pi-heif(HEIC 읽기)·OpenCV, UI 프로세스에 무거운 모듈 없음. 데이터 폴더는 임시 폴더
5. EXE 매니페스트에 `longPathAware`(260자 넘는 경로, O4)가 있는지 확인한다(PyInstaller 기본 매니페스트)
6. 크기·파일 수·빌드 시간을 `build/build_report.json` 에 적고 출력한다(숫자만)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "tools" / "bom-pdf.spec"
DIST = ROOT / "dist"
APP_DIR = DIST / "bom-pdf"
WORK = ROOT / "build" / "pyi"
REPORT = ROOT / "build" / "build_report.json"

VERSION_INFO = ROOT / "build" / "version_info.txt"

VERSION_TEMPLATE = """# UTF-8 — tools/build_exe.py 가 만든다
VSVersionInfo(
  ffi=FixedFileInfo(filevers={tup}, prodvers={tup}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041204B0', [
      StringStruct('CompanyName', 'bom-pdf'),
      StringStruct('FileDescription', 'bom-pdf PDF 리더/편집기'),
      StringStruct('FileVersion', '{ver}'),
      StringStruct('InternalName', 'bom-pdf'),
      StringStruct('LegalCopyright', 'GNU AGPL v3 — https://github.com/dreamwan001/bom-pdf'),
      StringStruct('OriginalFilename', 'bom-pdf.exe'),
      StringStruct('ProductName', 'bom-pdf'),
      StringStruct('ProductVersion', '{ver}')])]),
    VarFileInfo([VarStruct('Translation', [0x0412, 1200])])
  ]
)
"""

README = """bom-pdf {version} (빌드 {date}, 커밋 {commit})

법률문서용 PDF 리더/편집기입니다.

라이선스: GNU Affero General Public License v3 (LICENSE). 보증 없이 제공됩니다.
소스 코드: {source}
포함한 다른 구성 요소의 라이선스: THIRD_PARTY_NOTICES.md

- 설치: bom-pdf-Setup-<판>.exe 로 설치하면 %LOCALAPPDATA%\\Programs\\bom-pdf 에 들어갑니다(관리자 권한 불필요)
- 실행: bom-pdf.exe (폴더째 쓸 때는 _internal 폴더가 함께 있어야 합니다)
- 사용자 데이터·기록: %APPDATA%\\bom-pdf\\ (설정·최근 문서·주석·세션·logs)
- 원본 PDF·사진은 고치지 않습니다. 편집 결과는 늘 새 파일입니다.
- 포함 구성: Python {py}, PySide6(Qt, LGPL), PyMuPDF(AGPL), Pillow, pi-heif(libheif·libde265, LGPL), OpenCV(Apache-2.0)
"""


def app_version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from bom_pdf import __version__

    return __version__


def write_version_info() -> None:
    ver = app_version()
    nums = [int(x) for x in ver.split(".")[:4]]
    tup = tuple(nums + [0] * (4 - len(nums)))
    VERSION_INFO.parent.mkdir(parents=True, exist_ok=True)
    VERSION_INFO.write_text(VERSION_TEMPLATE.format(tup=tup, ver=ver), encoding="utf-8")


def run_pyinstaller(timeout: int) -> float:
    env = dict(os.environ)
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), os.path.join(sysroot, "System32"), sysroot])
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC), "--noconfirm", "--clean",
           "--distpath", str(DIST), "--workpath", str(WORK), "--log-level", "WARN"]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env)
    try:
        code = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()  # 이 빌드가 띄운 프로세스만
        raise SystemExit(f"PyInstaller timed out after {timeout}s — stopped (no unrelated processes touched)")
    if code != 0:
        raise SystemExit(f"PyInstaller failed with exit code {code}")
    return time.perf_counter() - t0


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(ROOT), capture_output=True,
                             text=True, timeout=10)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True,
                               text=True, timeout=10).stdout.strip()
        return out.stdout.strip() + ("+수정본" if dirty else "")
    except Exception:
        return "?"


def write_readme() -> None:
    from bom_pdf import SOURCE_URL

    text = README.format(version=app_version(), date=dt.date.today().isoformat(), commit=git_commit(),
                         py=".".join(map(str, sys.version_info[:3])), source=SOURCE_URL)
    (APP_DIR / "README.txt").write_text(text, encoding="utf-8-sig", newline="\r\n")
    for name in ("LICENSE", "THIRD_PARTY_NOTICES.md"):  # AGPL·LGPL 고지 — 설치 폴더에 함께
        shutil.copyfile(ROOT / name, APP_DIR / name)


def make_selftest_inputs(folder: Path) -> tuple[Path, list[Path]]:
    """합성 자료(시험 픽스처 생성기 재사용) — 실제 사건 자료 아님."""
    sys.path.insert(0, str(ROOT / "tests"))
    sys.path.insert(0, str(ROOT / "spike"))
    os.environ.setdefault("BOM_PDF_HOME", str(folder / "home"))
    import conftest  # noqa: E402  (픽스처 함수만 쓴다)
    import photo_fixtures as pf  # noqa: E402

    pdf = conftest.make_text_pdf(folder / "selftest.pdf")
    photos = [pf.make_photo(folder / "doc.jpg", orientation=6, taken="2024:05:06 07:08:09",
                            image=pf.document_photo()),
              pf.make_photo(folder / "photo.heic", size=(800, 600)),
              pf.make_photo(folder / "photo.png", size=(640, 480))]
    return pdf, photos


def selftest() -> dict:
    exe = APP_DIR / "bom-pdf.exe"
    folder = Path(tempfile.mkdtemp(prefix="bom-pdf-build-selftest-"))
    try:
        pdf, photos = make_selftest_inputs(folder)
        report = folder / "report.json"
        env = {**os.environ, "BOM_PDF_HOME": str(folder / "home")}
        t0 = time.perf_counter()
        code = subprocess.run([str(exe), "--self-test", str(report), str(pdf), *map(str, photos)],
                              env=env, timeout=300).returncode
        rep = json.loads(report.read_text(encoding="utf-8")) if report.exists() else {"ok": False}
        rep["exit_code"] = code
        rep["wall_ms"] = round((time.perf_counter() - t0) * 1000)
        return rep
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def long_path_aware(exe: Path) -> bool:
    """EXE 에 들어간 매니페스트에 longPathAware=true 가 있는가(레지스트리 LongPathsEnabled 가 켜진 PC 에서 효과 —
    꺼진 PC 는 앱이 긴 경로를 \\\\?\\ 형식으로 바꿔 쓴다, paths.long_path)."""
    data = exe.read_bytes()
    i = data.find(b"<longPathAware")
    return i >= 0 and b">true<" in data[i:i + 200]


def dir_stats(p: Path) -> dict:
    files = [f for f in p.rglob("*") if f.is_file()]
    biggest = sorted(files, key=lambda f: f.stat().st_size, reverse=True)[:12]
    return {"size_mb": round(sum(f.stat().st_size for f in files) / 2**20, 1), "files": len(files),
            "largest_mb": {str(f.relative_to(p)): round(f.stat().st_size / 2**20, 1) for f in biggest}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-selftest", action="store_true")
    ap.add_argument("--installer", action="store_true", help="빌드·점검 뒤 설치 프로그램도 만든다(release/)")
    ap.add_argument("--timeout", type=int, default=1200, help="PyInstaller 제한 시간(초), 기본 20분")
    a = ap.parse_args()
    if Path(sys.executable).resolve().parent.parent != (ROOT / ".venv").resolve():
        print("warning: not running the project .venv python", file=sys.stderr)
    for d in (WORK, APP_DIR):
        if d.exists():
            shutil.rmtree(d)
    write_version_info()
    secs = run_pyinstaller(a.timeout)
    write_readme()
    report = {"build_s": round(secs, 1), "long_path_aware": long_path_aware(APP_DIR / "bom-pdf.exe"),
              **dir_stats(APP_DIR)}
    if not a.skip_selftest:
        report["selftest"] = selftest()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=1))
    ok = report["long_path_aware"] and (a.skip_selftest or report["selftest"].get("ok"))
    if not ok:
        return 1
    if a.installer:
        sys.path.insert(0, str(ROOT / "tools"))
        import build_installer

        return build_installer.main([])
    return 0


if __name__ == "__main__":
    sys.exit(main())
