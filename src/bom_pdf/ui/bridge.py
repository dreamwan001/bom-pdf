"""서비스 이벤트(백그라운드 스레드) → Qt 신호(주 스레드).

렌더된 픽셀은 받는 스레드에서 바로 QImage(RGB32)로 바꿔 넘긴다 — 주 스레드는 그리기만 한다.
신호 이름에 `event`를 쓰지 않는다(위키 P25).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtGui import QImage

from bom_pdf.services.document import PageImage


@dataclass
class RenderedPage:
    doc_id: int
    page: int
    scale: float
    seq: int
    image: QImage
    lane: int = 0  # THUMB_LANE 이면 썸네일
    tile: tuple | None = None  # (tx, ty, x, y) 확대 타일 — image.offset() 도 (x, y) 장치 px


class ServiceBridge(QObject):
    received = Signal(object)

    def __call__(self, ev) -> None:  # 서비스 콜백 — 백그라운드 스레드
        if isinstance(ev, PageImage):
            raw = QImage(ev.data, ev.width, ev.height, ev.stride, QImage.Format.Format_RGB888)
            img = raw.convertToFormat(QImage.Format.Format_RGB32)  # 새 버퍼 소유, 그리기 빠른 형식
            del raw
            if ev.tile is not None:
                img.setOffset(QPoint(ev.tile[2], ev.tile[3]))  # 쪽 전체 그림 안의 자리
            ev = RenderedPage(ev.doc_id, ev.page, ev.scale, ev.seq, img, ev.lane, ev.tile)
        self.received.emit(ev)
