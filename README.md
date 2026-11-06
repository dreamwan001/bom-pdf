# bom-pdf

법률문서(소송기록·서증 등)를 다루기 위한 가벼운 Windows용 PDF 리더/편집기입니다.
원본 PDF·사진은 고치지 않고, 주석은 따로 저장하며, 편집 결과는 늘 새 파일로 만듭니다.

## 기능

- **보기**: 빠른 첫 쪽 표시, 큰 문서(수천 쪽) 이어 보기, 폭·쪽 맞춤, 확대 시 부분 그리기, 탭, 좌우 분할 보기
- **탐색·검색**: 썸네일·책갈피 사이드바, 문서 안 검색(공백·줄바꿈 무시 옵션)
- **주석**: 형광펜·밑줄·메모·사각형, 메모·태그·색, 되돌리기/다시 하기. 원본을 건드리지 않고 `%APPDATA%\bom-pdf\annotations\<sha256>.json` 에 저장하며, "메모 포함 PDF"로 내보내면 표준 PDF 주석이 든 새 파일을 만듭니다
- **쪽 편집**: 회전·삭제·추출·순서 바꾸기·합치기·나누기, 인쇄, 용량 줄이기
- **사진 → PDF**: JPG·PNG·WebP·HEIC, 촬영 시각·파일명 순 정렬, 위치정보(GPS) 빼기, 문서 사진 테두리·원근 보정
- **그 밖에**: 이미 실행 중이면 새 탭으로 열기, 비정상 종료 뒤 탭 복원, 기본 PDF 프로그램 등록, 환경 점검·자가 점검

## 설치

[Releases](https://github.com/dreamwan001/bom-pdf/releases)에서 `bom-pdf-Setup-<판>.exe` 를 받아 실행합니다.

- Windows 11 전용입니다. 관리자 권한 없이 `%LOCALAPPDATA%\Programs\bom-pdf` 에 설치됩니다
- 코드 서명이 없어 처음 실행 때 SmartScreen 경고가 나올 수 있습니다(추가 정보 → 실행)
- 설정·주석·기록(`%APPDATA%\bom-pdf`)은 업데이트·제거 때 지우지 않습니다

## 소스에서 실행·개발

Python 3.12 와 [uv](https://docs.astral.sh/uv/)를 씁니다.

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv/Scripts/python.exe -e ".[dev]" pyinstaller
```

```bash
.venv/Scripts/python.exe -m bom_pdf
```

```bash
.venv/Scripts/python.exe -m pytest
```

- 시험은 합성 PDF·사진만 씁니다. Qt 는 offscreen, 데이터 폴더는 임시 폴더(`BOM_PDF_HOME`)로 돌립니다
- 설치 파일 만들기: `.venv/Scripts/python.exe tools/build_exe.py --installer` ([NSIS 3](https://nsis.sourceforge.io) 필요) → `release/`

### 구조

```
src/bom_pdf/
  engine/     PyMuPDF 를 쓰는 렌더 작업자·작업 프로세스 (화면 프로세스는 PyMuPDF 를 불러오지 않음)
  services/   문서·주석·쪽 편집·사진 PDF·출력 경로 등 (Qt 없음)
  ui/         PySide6 화면
tests/        pytest (offscreen)
tools/        빌드·설치 프로그램·측정 도구
```

## 라이선스

bom-pdf 는 [GNU Affero General Public License v3](LICENSE)로 배포합니다. PDF 엔진인 PyMuPDF·MuPDF 가 AGPL 이기 때문입니다.
함께 들어 있는 구성 요소의 라이선스는 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 보십시오.

이 프로그램은 보증 없이 제공됩니다.
