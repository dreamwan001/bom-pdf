"""Windows 데스크톱 연동: 앱 아이콘, 기본 앱 설정 열기.

- 아이콘 `assets/bom-pdf.ico` 는 `tools/make_icon.py` 가 만든다. 포장본은 `_internal/assets/`(spec datas).
- 파일 연결 등록(ProgID `BomPdf.Document`·`RegisteredApplications` 의 `bom-pdf`)은 설치 프로그램
  (`tools/installer.nsi`)이 한다. 기본 앱 지정(UserChoice)은 Windows 정책상 사용자가 설정 앱에서 직접 고른다.
"""
from __future__ import annotations

import sys
from pathlib import Path

ICON_NAME = "bom-pdf.ico"
REGISTERED_APP = "bom-pdf"  # 설치 프로그램의 RegisteredApplications 값 이름과 같아야 한다
DEFAULT_APPS_URI = f"ms-settings:defaultapps?registeredAppUser={REGISTERED_APP}"


def icon_path() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    root = Path(base) if base else Path(__file__).resolve().parents[3]  # src/bom_pdf/ui → 프로젝트 루트
    return root / "assets" / ICON_NAME


def app_icon():
    from PySide6.QtGui import QIcon

    p = icon_path()
    return QIcon(str(p)) if p.is_file() else QIcon()


def open_default_apps_settings() -> bool:
    """Windows 설정 → 기본 앱 → bom-pdf 쪽을 연다(설치본에서만 bom-pdf 가 목록에 있다)."""
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices

    return QDesktopServices.openUrl(QUrl(DEFAULT_APPS_URI))
