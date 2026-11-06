"""공통 픽스처. 실제 사건 자료를 쓰지 않는다 — 합성 PDF를 임시 폴더에 만든다.

- 데이터 폴더는 `BOM_PDF_HOME`으로 임시 폴더에 돌린다(실제 %APPDATA% 를 건드리지 않음).
- Qt 는 offscreen 플랫폼.
"""
from __future__ import annotations

import hashlib
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

os.environ["BOM_PDF_HOME"] = tempfile.mkdtemp(prefix="bom-pdf-test-home-")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf  # noqa: E402  (픽스처 생성용 — 앱 UI 프로세스 코드는 쓰지 않는다)
import pytest  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "spike"))
import make_fixtures  # noqa: E402

SECRET_WORD = "synthetic-needle-7731"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_text_pdf(path: Path, pages: int = 12) -> Path:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page(width=595, height=842)
        page.insert_text((72, 100), f"Synthetic page {i + 1}", fontsize=20)
        if i == 3:
            page.insert_text((72, 140), SECRET_WORD, fontsize=12)
    # 다른 크기·회전 쪽도 하나
    p = doc.new_page(width=842, height=595)
    p.insert_text((72, 100), "Landscape page", fontsize=20)
    p = doc.new_page(width=595, height=842)
    p.set_rotation(90)
    doc.save(path)
    doc.close()
    return path


KOREAN_FONT = Path(r"C:\Windows\Fonts\malgun.ttf")
# 합성 한국어 문장 — 실제 사건 자료 아님. 3쪽에 "임대차보증금반환청구"를 줄바꿈·띄어쓰기로 쪼개 넣는다.
KO_LINES = [
    "원고는 피고에게 합성 계약서 사본을 보냈다.",
    "피고는 잔금 지급일을 다시 정하자고 하였다.",
]


def make_korean_pdf(path: Path) -> Path:
    """한국어 검색 시험용 5쪽: 줄바꿈이 낀 낱말, 띄어 쓴 낱말, 영문 대소문자, 회전 쪽, 책갈피."""
    if not KOREAN_FONT.exists():
        pytest.skip("맑은 고딕 글꼴이 없어 한국어 합성 PDF를 만들 수 없음")
    doc = pymupdf.open()

    def page(lines, rotate=0):
        p = doc.new_page(width=595, height=842)
        p.insert_font(fontname="malgun", fontfile=str(KOREAN_FONT))
        y = 80
        for ln in lines:
            p.insert_text((72, y), ln, fontname="malgun", fontsize=12)
            y += 20
        if rotate:
            p.set_rotation(rotate)
        return p

    page(["1쪽 머리말", *KO_LINES])                                       # 0: 없음
    page(["사건 이름: 임대차보증금반환청구", *KO_LINES])                   # 1: 붙여 씀 1건
    page(["청구 취지는 임대차보증", "금반환청구 이다", *KO_LINES])        # 2: 줄바꿈으로 쪼갬
    page(["임대차 보증금 반환 청구 사건", "Needle needle NEEDLE"])         # 3: 띄어 씀 + 영문 3건
    page(["회전된 쪽의 임대차보증금반환청구"], rotate=90)                  # 4: 회전 쪽 1건
    doc.set_toc([[1, "처음", 1], [2, "청구 취지", 3], [1, "회전 쪽", 5]])
    doc.save(path)
    doc.close()
    return path


def make_long_pdf(path: Path, pages: int = 600) -> Path:
    """검색 취소·흐름 시험용: 쪽마다 'line' 30번 (600쪽이면 18,000건 > 결과 상한)."""
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((50, 60), "\n".join(f"Synthetic line {j} of page {i + 1}" for j in range(30)), fontsize=9)
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def korean_pdf(tmp_path_factory) -> Path:
    return make_korean_pdf(tmp_path_factory.mktemp("ko") / "korean.pdf")


@pytest.fixture(scope="session")
def long_pdf(tmp_path_factory) -> Path:
    return make_long_pdf(tmp_path_factory.mktemp("long") / "long.pdf")


@pytest.fixture(scope="session")
def fixtures_dir(tmp_path_factory) -> Path:
    """합성 정상본 + spike/make_fixtures.py 의 손상본 생성기로 만든 손상 8종."""
    d = tmp_path_factory.mktemp("fixtures")
    src = make_text_pdf(d / "sample.pdf")
    old = make_fixtures.OUT
    make_fixtures.OUT = d
    try:
        make_fixtures.make_corrupt(src)
    finally:
        make_fixtures.OUT = old
    return d


@pytest.fixture(scope="session")
def sample_pdf(fixtures_dir) -> Path:
    return fixtures_dir / "sample.pdf"


class Events:
    """서비스·엔진 이벤트를 모아 두고 조건이 맞을 때까지 기다린다."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.seen: list = []

    def __call__(self, ev) -> None:
        self.q.put(ev)

    def wait(self, pred, timeout: float = 20.0):
        for ev in self.seen:
            if pred(ev):
                self.seen.remove(ev)
                return ev
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError("event not received")
            try:
                ev = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if pred(ev):
                return ev
            self.seen.append(ev)

    def drain(self, settle: float = 0.5) -> list:
        out = list(self.seen)
        self.seen.clear()
        end = time.monotonic() + settle
        while (left := end - time.monotonic()) > 0:
            try:
                out.append(self.q.get(timeout=left))
            except queue.Empty:
                break
        return out


@pytest.fixture
def events() -> Events:
    return Events()


@pytest.fixture
def service(events, tmp_path):
    from bom_pdf.services.document import DocumentService

    s = DocumentService(events, data_dir=tmp_path)
    s.start()
    yield s
    s.shutdown()


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])
    yield app


@pytest.fixture
def window(qapp, tmp_path):
    from bom_pdf.services import document as ds
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow

    bridge = ServiceBridge()
    service = ds.DocumentService(bridge, data_dir=tmp_path / "home")
    service.start()
    win = MainWindow(service, bridge, RecentDocuments(tmp_path / "home"))
    win.ask_password = lambda path, wrong: None  # 대화상자 대신
    win.resize(1200, 800)
    win.show()
    yield win
    win.close()
    service.shutdown()


def qt_wait(app, cond, timeout: float = 20.0) -> None:
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise TimeoutError("condition not met")
        app.processEvents()
        time.sleep(0.005)


def make_marked_pdf(path: Path, pages: int = 8, tag: str = "A", toc: bool = True, links: bool = True) -> Path:
    """1-4 쪽 편집 시험용: 쪽마다 'MARK-<tag><번호>' 글자(순서 확인), 3쪽은 원래 90° 회전, 3단 책갈피, 링크.

    링크: 2쪽 → 5쪽(문서 안), 2쪽 → https://example.invalid (바깥). 책갈피: 1·3·6쪽 최상위, 2·4·5쪽 하위.
    """
    doc = pymupdf.open()
    for i in range(pages):
        p = doc.new_page(width=595, height=842)
        p.insert_text((72, 100), f"MARK-{tag}{i + 1}", fontsize=24)
    if pages >= 3:
        doc[2].set_rotation(90)
    if links and pages >= 5:
        doc[1].insert_link({"kind": pymupdf.LINK_GOTO, "from": pymupdf.Rect(72, 200, 300, 230), "page": 4,
                            "to": pymupdf.Point(72, 72)})
        doc[1].insert_link({"kind": pymupdf.LINK_URI, "from": pymupdf.Rect(72, 260, 300, 290),
                            "uri": "https://example.invalid/"})
    if toc and pages >= 6:
        doc.set_toc([[1, f"{tag} 하나", 1], [2, f"{tag} 하나-1", 2], [1, f"{tag} 둘", 3], [2, f"{tag} 둘-1", 4],
                     [3, f"{tag} 둘-1-가", 5], [1, f"{tag} 셋", 6]])
    doc.save(path)
    doc.close()
    return path


def page_marks(path) -> list[str]:
    """각 쪽의 MARK 글자."""
    import re

    with pymupdf.open(path) as d:
        return [(re.search(r"MARK-\w+", pg.get_text()) or [None])[0] for pg in d]


def snapshot(path: Path) -> tuple:
    """원본 불변 확인: (sha256, mtime_ns, 원본 폴더 파일 목록)."""
    path = Path(path)
    return sha256(path), path.stat().st_mtime_ns, sorted(os.listdir(path.parent))
