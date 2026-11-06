# 제3자 구성 요소 고지 (Third-party notices)

bom-pdf 는 GNU Affero General Public License v3 (`LICENSE`)로 배포합니다.
설치 파일(`bom-pdf-Setup-<판>.exe`)에는 아래 구성 요소가 함께 들어 있습니다. 각 구성 요소는 자기 라이선스를 따릅니다.
판 번호는 이 문서를 쓸 때(0.1.5) 빌드에 쓴 판입니다.

| 구성 요소 | 판 | 라이선스 | 소스·라이선스 원문 |
|---|---|---|---|
| PyMuPDF / MuPDF (Artifex) | 2.1 | GNU AGPL v3 (또는 Artifex 상용 라이선스) | https://github.com/pymupdf/PyMuPDF · https://mupdf.com |
| Qt for Python (PySide6-Essentials, shiboken6) / Qt | 6.12.0 | GNU LGPL v3 | https://code.qt.io/cgit/pyside/pyside-setup.git · https://www.qt.io/licensing |
| Pillow | 13.0.0 | MIT-CMU (HPND) | https://github.com/python-pillow/Pillow |
| pi-heif | 1.4.0 | BSD-3-Clause | https://github.com/bigcat88/pillow_heif |
| libheif, libde265 (pi-heif 에 포함) | — | GNU LGPL v3 | https://github.com/strukturag/libheif · https://github.com/strukturag/libde265 |
| OpenCV (opencv-python-headless) | 5.0.0.93 | Apache-2.0 (포함된 FFmpeg 등은 각자 라이선스, LGPL 포함) | https://github.com/opencv/opencv-python |
| NumPy (OpenBLAS 등 포함) | 2.5.4 | BSD-3-Clause 외 (0BSD, MIT, Zlib, CC0-1.0) | https://github.com/numpy/numpy |
| Python | 3.12.11 | PSF License | https://www.python.org/download/releases/3.12/license/ |
| PyInstaller 부트로더 | 6.22.3 | GPL v2 + 부트로더 예외 | https://github.com/pyinstaller/pyinstaller |

## LGPL 구성 요소 (Qt, libheif, libde265 등)

- 설치 폴더의 `_internal` 안에 별도 DLL·파이썬 확장 모듈로 들어 있습니다(정적 링크 아님). 사용자는 같은 판과 호환되는 다른 빌드로 바꿔 넣을 수 있습니다.
- 각 라이브러리의 소스는 위 주소에서 받을 수 있습니다.

## bom-pdf 자체

- 저작권: bom-pdf 기여자. 라이선스: GNU AGPL v3 (`LICENSE`)
- 소스 코드: https://github.com/dreamwan001/bom-pdf
