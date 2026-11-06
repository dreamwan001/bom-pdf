"""아이콘·설치 프로그램 정적 점검 (설치 자체는 tools/check_installer.py — 이 PC 레지스트리를 건드리므로 pytest 밖).

- 아이콘 ICO 가 16–256 크기를 다 갖고 Qt 가 읽는다
- 설치 스크립트: UTF-8 BOM(위키 P18), 앱의 RegisteredApplications 이름과 같음, UserChoice 를 쓰지 않음,
  사용자 데이터 폴더를 지우지 않음, 설치 때 쓰는 HKCU 키·값을 제거 때 모두 지움
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from bom_pdf.ui import desktop

ROOT = Path(__file__).resolve().parents[1]
NSI = ROOT / "tools" / "installer.nsi"


def test_icon_has_all_sizes_and_loads(qapp):
    p = desktop.icon_path()
    assert p == ROOT / "assets" / "bom-pdf.ico" and p.is_file()
    with Image.open(p) as im:
        assert {16, 24, 32, 48, 256} <= {w for w, h in im.info["sizes"]}
    icon = desktop.app_icon()
    assert not icon.isNull()
    assert 16 in {s.width() for s in icon.availableSizes()}


def _nsi() -> str:
    raw = NSI.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM 없으면 makensis 가 한글을 깨뜨린다
    return raw[3:].decode("utf-8")


def test_installer_registration_names_match_app():
    text = _nsi()
    assert f'!define REG_APP "{desktop.REGISTERED_APP}"' in text
    assert f'!define DEFAULT_APPS_URI "{desktop.DEFAULT_APPS_URI}"' in text
    assert "UserChoice" not in text.replace("(UserChoice)은 쓰지 않는다", "").replace(
        "(UserChoice)은 건드리지 않는다", "")
    assert 'InstallDir "$LOCALAPPDATA\\Programs\\${APP}"' in text and "RequestExecutionLevel user" in text
    assert "$APPDATA" not in text  # 사용자 데이터(%APPDATA%\bom-pdf)는 건드리지 않는다
    for line in text.splitlines():
        assert not re.match(r"\s*(WriteReg\w+|DeleteReg\w+)\s+HK(LM|CR|U\b)", line), line  # HKCU 만


def _expand(s: str, defines: dict) -> str:
    return re.sub(r"\$\{(\w+)\}", lambda m: defines.get(m.group(1), m.group(0)), s)


def test_uninstall_removes_everything_install_writes():
    text = _nsi()
    defines = dict(re.findall(r'^!define (\w+) "([^"]*)"', text, re.M))
    install, uninstall = text.split('Section "Uninstall"', 1)
    written = {(_expand(k, defines), _expand(v, defines))
               for k, v in re.findall(r'WriteReg(?:Str|DWORD) HKCU "([^"]+)" "([^"]*)"', install)}
    deleted_keys = {_expand(k, defines) for k in re.findall(r'DeleteRegKey(?: /ifempty)? HKCU "([^"]+)"', uninstall)}
    deleted_values = {(_expand(k, defines), _expand(v, defines))
                      for k, v in re.findall(r'DeleteRegValue HKCU "([^"]+)" "([^"]*)"', uninstall)}
    assert written
    for key, value in written:
        covered = (key, value) in deleted_values or any(
            key == d or key.startswith(d + "\\") for d in deleted_keys)
        assert covered, (key, value)
    # .pdf 키 자체·다른 앱의 OpenWithProgids 는 지우지 않는다(값 하나만)
    assert not any(d.endswith(".pdf") or d.endswith("OpenWithProgids") for d in deleted_keys)
