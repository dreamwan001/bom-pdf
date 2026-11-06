"""세션 기록기 (1-6): 주 창의 `sessionChanged` 를 모아(1.5초 디바운스) 저장하고, 30초마다 한 번 더 확인한다.

- 시작 때 지난 기록이 "running"(비정상 종료)이고 탭이 있으면, 창이 뜬 뒤 복원을 묻는다. 묻기 전·답하기 전에는
  저장하지 않는다(답하기 전에 또 죽어도 지난 탭 목록이 남도록).
- 정상 종료(`closing`)는 "closed" 로 적는다. 바뀐 것이 없으면 디스크에 쓰지 않는다(services.session).
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer

from bom_pdf.services import session as ss

log = logging.getLogger(__name__)

DEBOUNCE_MS = 1500
PERIOD_MS = 30_000


class SessionRecorder(QObject):
    def __init__(self, win, store: ss.SessionStore, parent=None):
        super().__init__(parent or win)
        self.win, self.store = win, store
        self.previous = store.load()
        self.pending_offer = self.previous is not None and self.previous.abnormal
        self.closed = False
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(DEBOUNCE_MS)
        self._debounce.timeout.connect(self.save_now)
        self._period = QTimer(self)
        self._period.setInterval(PERIOD_MS)
        self._period.timeout.connect(self.save_now)
        win.sessionChanged.connect(self._changed)
        win.closing.connect(self.mark_closed)

    def start(self) -> None:
        """창을 띄운 뒤 부른다: 필요하면 복원을 묻고, 기록을 시작한다."""
        self._period.start()
        if self.pending_offer:
            QTimer.singleShot(0, self.offer_restore)
        else:
            self.save_now()

    def offer_restore(self) -> None:
        prev = self.previous
        self.pending_offer = False
        if prev is not None and prev.abnormal and not self.closed:
            log.info("abnormal exit detected: offering restore of %d tab(s)", len(prev.tabs))
            if self.win.ask_restore(prev):
                missing = self.win.restore_session(prev.tabs, prev.current)
                log.info("session restored, %d missing", len(missing))
                if missing:
                    self.win.report_missing(missing)
        self.save_now()

    def _changed(self) -> None:
        if not self.pending_offer and not self.closed:
            self._debounce.start()

    def save_now(self) -> None:
        if self.pending_offer or self.closed:
            return
        tabs, cur = self.win.session_tabs()
        try:
            self.store.save(tabs, cur, ss.STATE_RUNNING)
        except OSError:
            log.exception("session save failed")

    def mark_closed(self) -> None:
        if self.closed:
            return
        self._debounce.stop()
        self._period.stop()
        if self.pending_offer:  # 묻기 전에 닫음 — 지난 기록을 그대로 둔다(다음에 다시 묻는다)
            self.closed = True
            return
        tabs, cur = self.win.session_tabs()
        self.closed = True
        try:
            self.store.save(tabs, cur, ss.STATE_CLOSED)
        except OSError:
            log.exception("session close mark failed")
