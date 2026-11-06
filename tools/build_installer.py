"""설치 프로그램 빌드: `dist/bom-pdf/` → `release/bom-pdf-Setup-<판>.exe` (Git 무시).

사용: .venv/Scripts/python.exe tools/build_installer.py [--makensis <makensis.exe>]
      (보통은 `tools/build_exe.py --installer` 로 빌드·포장본 점검에 이어 부른다)

- NSIS 3 으로 만든다. 정본 스크립트 `tools/installer.nsi`
  (UTF-8 BOM 필수 — 없으면 이 스크립트가 멈춘다, 위키 P18)
- `dist/bom-pdf/bom-pdf.exe` 와 마지막 빌드 보고(`build/build_report.json`)의 포장본 점검이 ok 여야 한다
- 크기·SHA-256 을 출력하고 `release/bom-pdf-Setup-<판>.sha256` 에 적는다
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NSI = ROOT / "tools" / "installer.nsi"
APP_DIR = ROOT / "dist" / "bom-pdf"
RELEASE = ROOT / "release"
REPORT = ROOT / "build" / "build_report.json"
MAKENSIS_DEFAULT = Path(r"C:\Program Files (x86)\NSIS\makensis.exe")


def app_version() -> str:
    sys.path.insert(0, str(ROOT / "src"))
    from bom_pdf import __version__

    return __version__


def find_makensis(given: str | None) -> Path:
    for cand in (given, shutil.which("makensis"), str(MAKENSIS_DEFAULT)):
        if cand and Path(cand).is_file():
            return Path(cand)
    raise SystemExit("makensis.exe 가 없습니다 — `winget install -e --id NSIS.NSIS` 로 설치하거나 --makensis 로 지정")


def installer_path(version: str | None = None) -> Path:
    return RELEASE / f"bom-pdf-Setup-{version or app_version()}.exe"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--makensis")
    a = ap.parse_args(argv)
    if not NSI.read_bytes().startswith(b"\xef\xbb\xbf"):
        raise SystemExit(f"{NSI.name} 가 UTF-8 BOM 이 아닙니다 — 한글이 깨집니다(위키 P18)")
    if not (APP_DIR / "bom-pdf.exe").is_file():
        raise SystemExit("dist/bom-pdf/bom-pdf.exe 가 없습니다 — 먼저 tools/build_exe.py")
    if REPORT.exists():
        sel = json.loads(REPORT.read_text(encoding="utf-8")).get("selftest")
        if sel is not None and not sel.get("ok"):
            raise SystemExit("마지막 빌드의 포장본 점검이 실패했습니다 — 설치 프로그램을 만들지 않습니다")
    version = app_version()
    out = installer_path(version)
    RELEASE.mkdir(exist_ok=True)
    from bom_pdf import SOURCE_URL  # app_version() 이 src 를 경로에 넣었다

    cmd = [str(find_makensis(a.makensis)), "/V2", f"/DVERSION={version}", f"/DSOURCE={APP_DIR}",
           f"/DSOURCE_URL={SOURCE_URL}", f"/DOUTFILE={out}", str(NSI)]
    t0 = time.perf_counter()
    code = subprocess.run(cmd, cwd=str(ROOT), timeout=900).returncode
    if code != 0 or not out.is_file():
        raise SystemExit(f"makensis 실패(종료 코드 {code})")
    digest = hashlib.sha256(out.read_bytes()).hexdigest()
    (out.with_suffix(".sha256")).write_text(f"{digest}  {out.name}\n", encoding="utf-8")
    print(json.dumps({"installer": str(out), "size_mb": round(out.stat().st_size / 2**20, 1), "sha256": digest,
                      "build_s": round(time.perf_counter() - t0, 1)}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
