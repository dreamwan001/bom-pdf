# -*- mode: python ; coding: utf-8 -*-
# bom-pdf PyInstaller onedir 정본 spec (D12). 직접 부르지 말고 tools/build_exe.py 로 빌드한다
# (PATH 제한·이전 산출물 정리·README·라이선스 고지·포장본 점검을 함께 한다). GNU AGPL v3.
from pathlib import Path

ROOT = Path(SPECPATH).parent
SRC = ROOT / "src"
APP = "bom-pdf"
ICON = ROOT / "assets" / "bom-pdf.ico"            # tools/make_icon.py 산출(Git 포함)
VERSION_FILE = ROOT / "build" / "version_info.txt"  # tools/build_exe.py 가 __version__ 으로 만든다

a = Analysis(
    [str(ROOT / "launcher.py")],
    pathex=[str(SRC)],
    binaries=[],
    datas=[(str(ICON), "assets")],  # 창·작업 표시줄 아이콘(bom_pdf.ui.desktop.icon_path)
    # 함수 안에서 불러오는 것(작업자·작업 프로세스 전용 모듈)도 정적 분석이 따라가지만 이름으로 한 번 더 고정한다(위키 P01)
    hiddenimports=[
        "bom_pdf.engine.pdf", "bom_pdf.engine.build", "bom_pdf.engine.images", "bom_pdf.engine.worker",
        "bom_pdf.engine.jobs", "bom_pdf.selftest", "pi_heif", "cv2",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 개발·시험 전용, 쓰지 않는 큰 패키지(위키 P03)
    excludes=["tkinter", "pytest", "psutil", "IPython", "matplotlib", "scipy", "pandas", "PyInstaller",
              "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtOpenGL", "PySide6.QtSql", "PySide6.QtTest",
              # O7 기능 그대로 빼기: HEIC 는 읽기 전용 pi_heif 로(pillow_heif 는 인코더 libx265 22MB 를 끌고 온다),
              # Pillow AVIF 플러그인(7.5MB — AVIF 는 받지 않음, 없으면 Pillow 가 조용히 끈다), 쓰지 않는 ssl·yaml
              "pillow_heif", "PIL._avif", "PIL.AvifImagePlugin", "ssl", "_ssl", "yaml"],
    noarchive=False,
    optimize=0,
)

# 빌드 PC PATH 의 다른 앱 런타임 DLL 이 섞이는 것 방지(위키 P04), 소프트웨어 OpenGL(Qt 위젯 래스터 그리기에는 불필요),
# OpenCV 동영상 입출력용 ffmpeg(문서 사진 모드는 정지 그림만 — cv2 가 동영상을 열 때만 찾는 DLL, 29MB)
_DROP_PREFIX = ("api-ms-win-", "ext-ms-win-", "opencv_videoio_ffmpeg")
_DROP_NAMES = {"ucrtbase.dll", "icuuc.dll", "icuin.dll", "opengl32sw.dll"}
# O7: Qt 플러그인 중 쓰지 않는 것 — 창은 qwindows(+ 점검용 qoffscreen), 그림은 쪽 화소(원시 RGB)·아이콘(ico/png)·
# 사진 미리보기(JPEG)만. TLS(인터넷 연결 안 함)·네트워크 정보·터치 입력·Direct2D 실험 창 플러그인은 뺀다.
_DROP_QT_PLUGINS = {
    "platforms/qdirect2d.dll", "platforms/qminimal.dll",
    "imageformats/qgif.dll", "imageformats/qicns.dll", "imageformats/qtga.dll", "imageformats/qtiff.dll",
    "imageformats/qwbmp.dll", "imageformats/qwebp.dll",
    "tls/qcertonlybackend.dll", "tls/qopensslbackend.dll", "tls/qschannelbackend.dll",
    "networkinformation/qnetworklistmanager.dll", "generic/qtuiotouchplugin.dll",
}


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/").lower()
    name = Path(dest).name
    if name.startswith(_DROP_PREFIX) or name in _DROP_NAMES:
        return False
    return not any(dest.endswith("plugins/" + p) for p in _DROP_QT_PLUGINS)


a.binaries = [b for b in a.binaries if _keep(b)]
# Qt 번역 파일은 한국어만 남긴다(메뉴·대화상자 문구는 앱이 한국어로 직접 쓴다)
a.datas = [d for d in a.datas
           if not ("translations" in d[0].replace("\\", "/") and d[0].endswith(".qm") and "_ko" not in d[0])]

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    icon=str(ICON),
    version=str(VERSION_FILE) if VERSION_FILE.exists() else None,
)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, upx_exclude=[], name=APP)
