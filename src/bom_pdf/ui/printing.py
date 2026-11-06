"""인쇄 (1-4): Qt 인쇄 대화상자 → 작업자가 프린터 해상도(최대 PRINT_DPI_MAX)로 쪽을 그림 → 차례로 프린터에 칠한다.

- 렌더는 렌더 작업자의 PRINT_LANE(썸네일 우선순위)으로 한 번에 두 쪽씩만 요청한다 — 메모리는 쪽 두 장 분량,
  UI 는 쪽 하나를 칠하는 동안만 바쁘다(진행 표시·취소 가능).
- 쪽이 용지와 방향이 다르면(가로 쪽·세로 용지) 90° 돌려 맞추고, 비율을 지켜 인쇄 가능 영역 가운데에 둔다.
- 부수: 드라이버가 여러 부를 못 하면(PDF 출력 등) 직접 반복한다(한 부씩 모아 찍기 / 쪽마다 여러 장).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QPainter, QTransform
from PySide6.QtPrintSupport import QPrinter

from bom_pdf.services import document as ds

log = logging.getLogger(__name__)

PRINT_DPI_MAX = 300
PRINT_OK = "ok"
PRINT_CANCELLED = "cancelled"
PRINT_ERROR = "error"


def print_sequence(printer: QPrinter, page_count: int, current: int) -> list[int]:
    """인쇄 대화상자 설정 → 칠할 쪽 차례(0부터, 부수 반복 포함)."""
    rng = printer.printRange()
    if rng == QPrinter.PrintRange.CurrentPage:
        pages = [current]
    elif rng == QPrinter.PrintRange.PageRange and printer.fromPage() > 0:
        a = max(1, printer.fromPage())
        b = min(page_count, printer.toPage() or page_count)
        pages = list(range(a - 1, b))
    else:
        pages = list(range(page_count))
    copies = 1 if printer.supportsMultipleCopies() else max(1, printer.copyCount())
    if copies == 1:
        return pages
    if printer.collateCopies():
        return pages * copies
    return [p for p in pages for _ in range(copies)]


class PrintJob(QObject):
    progress = Signal(int, int)
    finished = Signal(str)

    def __init__(self, service: ds.DocumentService, doc: ds.Document, printer: QPrinter, sequence: list[int]):
        super().__init__()
        self.service, self.doc, self.printer = service, doc, printer
        self.sequence = sequence
        self.dpi = min(printer.resolution(), PRINT_DPI_MAX)
        self.scale = round(self.dpi / 72.0, 4)
        self.pos = 0
        self._ack = 0
        self._images: dict[int, object] = {}
        self._painter: QPainter | None = None
        self.status: str | None = None

    # ---- 진행
    def start(self) -> None:
        if not self.sequence:
            self._finish(PRINT_ERROR)
            return
        self._painter = QPainter()
        if not self._painter.begin(self.printer):
            self._painter = None
            self._finish(PRINT_ERROR)
            return
        log.info("print start doc=%s pages=%d dpi=%d", self.doc.doc_id, len(self.sequence), self.dpi)
        self.progress.emit(0, len(self.sequence))
        self._request()

    def _request(self) -> None:
        want = []
        for p in self.sequence[self.pos:self.pos + 2]:
            if p not in self._images and p not in want:
                want.append(p)
        self.service.request_pages(self.doc.doc_id, self.scale, want, self._ack, ds.PRINT_LANE)

    def on_page(self, ev) -> None:
        self._ack = max(self._ack, ev.seq)
        if self.status is not None or ev.scale != self.scale:
            return
        self._images[ev.page] = ev.image
        self._pump()

    def on_page_error(self, ev) -> None:
        self._ack = max(self._ack, ev.seq)
        if self.status is None:
            self._abort(PRINT_ERROR)

    def on_engine_lost(self) -> None:
        """작업자가 죽었거나 문서가 닫힘 — 인쇄를 멈춘다."""
        if self.status is None:
            self._abort(PRINT_ERROR)

    def _pump(self) -> None:
        while self.status is None and self.pos < len(self.sequence) and self.sequence[self.pos] in self._images:
            p = self.sequence[self.pos]
            if self.pos > 0 and not self.printer.newPage():
                self._abort(PRINT_ERROR)
                return
            self._paint(self._images[p])
            self.pos += 1
            if p not in self.sequence[self.pos:self.pos + 2]:
                del self._images[p]
            self.progress.emit(self.pos, len(self.sequence))
        if self.status is not None:
            return
        if self.pos >= len(self.sequence):
            self._painter.end()
            self._painter = None
            self._finish(PRINT_OK)
        else:
            self._request()

    def _paint(self, img) -> None:
        qp = self._painter
        area = qp.viewport()  # 인쇄 가능 영역(장치 픽셀)
        if (img.width() > img.height()) != (area.width() > area.height()):
            img = img.transformed(QTransform().rotate(90))
        k = min(area.width() / img.width(), area.height() / img.height())
        w, h = img.width() * k, img.height() * k
        qp.save()
        qp.setWindow(area)  # 논리 좌표 = 장치 픽셀
        qp.drawImage(QRectF((area.width() - w) / 2, (area.height() - h) / 2, w, h), img)
        qp.restore()

    def cancel(self) -> None:
        if self.status is None:
            self._abort(PRINT_CANCELLED)

    def _abort(self, status: str) -> None:
        self.service.request_pages(self.doc.doc_id, self.scale, [], self._ack, ds.PRINT_LANE)
        if self._painter is not None:
            self.printer.abort()
            self._painter.end()
            self._painter = None
        self._finish(status)

    def _finish(self, status: str) -> None:
        self.status = status
        self._images.clear()
        log.info("print done doc=%s status=%s pages=%d", self.doc.doc_id, status, self.pos)
        self.finished.emit(status)
