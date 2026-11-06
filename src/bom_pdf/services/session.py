"""세션 기록 (1-6): 열린 탭·쪽 위치·확대를 데이터 폴더 `session.json` 에 원자적으로 저장한다.

- 경로·쪽 번호·확대만 적는다 — 문서 내용·주석·검색어는 넣지 않는다.
- 실행 중에는 `state = "running"`, 정상 종료 때 `"closed"`. 다음 실행에서 `"running"` 이 남아 있으면
  비정상 종료(급사·강제 종료)였다는 뜻 → 복원을 **묻는다**(조용히 열지 않는다).
- PySide6 를 import하지 않는다(시험·비서 연결용 업무 층).
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from bom_pdf import paths

SCHEMA_VERSION = 1
FILE_NAME = "session.json"
STATE_RUNNING = "running"
STATE_CLOSED = "closed"
MAX_TABS = 50


@dataclass
class TabState:
    path: str
    page: int = 0          # 0부터
    zoom: float = 1.0
    fit: str = "width"     # ui.page_view FIT_* 값 그대로(none·page·width)

    @staticmethod
    def from_dict(d) -> TabState | None:
        if not isinstance(d, dict) or not isinstance(d.get("path"), str) or not d["path"]:
            return None
        try:
            page = max(0, int(d.get("page", 0)))
            zoom = float(d.get("zoom", 1.0))
        except (TypeError, ValueError):
            page, zoom = 0, 1.0
        fit = d.get("fit") if isinstance(d.get("fit"), str) else "width"
        return TabState(d["path"], page, zoom if 0.01 < zoom < 100 else 1.0, fit)


@dataclass
class Session:
    state: str
    tabs: list[TabState]
    current: int = 0
    saved_at: float = 0.0

    @property
    def abnormal(self) -> bool:
        return self.state == STATE_RUNNING and bool(self.tabs)


class SessionStore:
    def __init__(self, data_dir: Path | None = None):
        self.path = (data_dir or paths.data_dir()) / FILE_NAME
        self._last: dict | None = None

    def load(self) -> Session | None:
        data = paths.read_json(self.path, None)  # 깨졌으면 .damaged 로 옮기고 None
        if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
            return None
        tabs = [t for t in (TabState.from_dict(d) for d in data.get("tabs", [])[:MAX_TABS]) if t is not None]
        cur = data.get("current", 0)
        return Session(str(data.get("state", "")), tabs, cur if isinstance(cur, int) else 0,
                       float(data.get("saved_at", 0) or 0))

    def save(self, tabs: list[TabState], current: int, state: str = STATE_RUNNING) -> bool:
        """바뀐 것이 있을 때만 쓴다. 썼으면 True."""
        body = {"schema_version": SCHEMA_VERSION, "state": state, "pid": os.getpid(),
                "tabs": [asdict(t) for t in tabs[:MAX_TABS]], "current": current}
        if body == self._last:
            return False
        paths.atomic_write_json(self.path, {**body, "saved_at": round(time.time(), 1)})
        self._last = body
        return True


def split_existing(tabs: list[TabState]) -> tuple[list[TabState], list[TabState]]:
    """(있는 파일, 없어진 파일)."""
    ok, missing = [], []
    for t in tabs:
        (ok if os.path.isfile(t.path) else missing).append(t)
    return ok, missing
