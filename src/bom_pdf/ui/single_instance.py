"""단일 인스턴스 (1-6): 이미 떠 있으면 파일 경로를 그 창에 넘기고 이 프로세스는 끝낸다.

- 누가 "주 인스턴스"인지는 데이터 폴더의 `instance.lock`(QLockFile)이 정한다. Windows 에서는
  `QLocalServer.listen` 이 같은 이름이 이미 쓰이고 있어도 성공하므로(같은 이름의 파이프 인스턴스가 하나 더 생김)
  listen 결과로는 가를 수 없다. QLockFile 은 PID 를 적어 두고, 그 프로세스가 죽었으면(급사) 낡은 잠금으로 보고 지운다.
- 파이프 이름은 사용자 이름 + 데이터 폴더 경로의 해시 → 사용자마다·`BOM_PDF_HOME`(시험)마다 따로.
  `UserAccessOption` 으로 같은 사용자만 접속.
- 전달 형식: UTF-8 JSON 한 줄 `{"v": 1, "paths": [절대 경로…]}` + `\\n`, 답은 `ok\\n`. 경로는 보내는 쪽에서
  절대 경로로 바꾼다(두 프로세스의 현재 폴더가 다르다). 본문·내용은 오가지 않는다.
- 창 앞으로 가져오기: Windows 는 포그라운드가 아닌 프로세스가 창을 앞으로 못 가져오므로, 보내는 쪽(사용자가 방금
  실행한 프로세스)이 `AllowSetForegroundWindow(ASFW_ANY)` 를 먼저 부른다.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_MESSAGE = 1 << 20  # 1MB — 경로 목록만 오가므로 넉넉하다
LOCK_NAME = "instance.lock"
SEND_DEADLINE_S = 8.0  # 주 인스턴스가 막 뜨는 중이면 listen 까지 기다린다


def server_name(data_dir: Path) -> str:
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME", "")
    key = f"{user}|{os.path.normcase(os.path.abspath(str(data_dir)))}"
    return "bom-pdf-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def encode_paths(paths: list[str]) -> bytes:
    return (json.dumps({"v": PROTOCOL_VERSION, "paths": [os.path.abspath(p) for p in paths]},
                       ensure_ascii=False) + "\n").encode("utf-8")


def decode_paths(data: bytes) -> list[str] | None:
    try:
        msg = json.loads(data.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(msg, dict) or not isinstance(msg.get("paths"), list):
        return None
    return [p for p in msg["paths"] if isinstance(p, str) and p]


def allow_foreground() -> None:
    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        except Exception:
            pass


def send_to_running(name: str, paths: list[str], deadline_s: float = SEND_DEADLINE_S) -> bool:
    """떠 있는 주 인스턴스에 경로를 보낸다. 답(ok)을 받으면 True."""
    payload = encode_paths(paths)
    allow_foreground()
    end = time.monotonic() + deadline_s
    while True:
        sock = QLocalSocket()
        sock.connectToServer(name)
        if sock.waitForConnected(500):
            sock.write(payload)
            sock.flush()
            sock.waitForBytesWritten(2000)
            got = b""
            while b"\n" not in got and sock.waitForReadyRead(3000):
                got += bytes(sock.readAll().data())
            sock.disconnectFromServer()
            if got.startswith(b"ok"):
                return True
            log.warning("single instance: no ack from running instance")
        if time.monotonic() > end:
            return False
        time.sleep(0.1)


class SingleInstance(QObject):
    """주 인스턴스 쪽 서버. `pathsReceived(list)` 는 GUI 스레드에서 나간다."""

    pathsReceived = Signal(list)

    def __init__(self, data_dir: Path, parent=None):
        super().__init__(parent)
        self.data_dir = Path(data_dir)
        self.name = server_name(self.data_dir)
        self._lock = QLockFile(str(self.data_dir / LOCK_NAME))
        self._lock.setStaleLockTime(0)  # 시간으로 낡음을 보지 않는다 — PID 가 죽었을 때만(오래 켜 둔 창 보호)
        self._server: QLocalServer | None = None
        self._buffers: dict[QLocalSocket, bytes] = {}

    def acquire(self) -> bool:
        """주 인스턴스가 되면 True(서버를 연다). 다른 인스턴스가 살아 있으면 False."""
        # tryLock 은 잠금 파일의 PID 가 죽었으면(또는 다른 프로그램이 그 PID 를 쓰면) 낡은 잠금으로 보고 지운 뒤 잡는다.
        # removeStaleLockFile() 은 살아 있는 잠금도 지워 버리므로 쓰지 않는다.
        if not self._lock.tryLock(0):
            if self._lock.error() == QLockFile.LockError.LockFailedError:
                return False
            # 권한 등으로 잠금 파일을 못 만드는 경우 — 단일 인스턴스 없이 그냥 뜬다
            log.warning("single instance lock error %s", self._lock.error())
            return True
        self._listen()
        return True

    def _listen(self) -> None:
        server = QLocalServer(self)
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not server.listen(self.name):
            QLocalServer.removeServer(self.name)  # 유닉스식 낡은 소켓 파일(Windows 에서는 할 일 없음)
            if not server.listen(self.name):
                log.warning("single instance listen failed: %s", server.errorString())
                return
        server.newConnection.connect(self._on_connection)
        self._server = server
        log.info("single instance server %s", self.name)

    def _on_connection(self) -> None:
        while self._server is not None and (sock := self._server.nextPendingConnection()) is not None:
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._drop(s))

    def _on_ready(self, sock: QLocalSocket) -> None:
        buf = self._buffers.get(sock, b"") + bytes(sock.readAll().data())
        if len(buf) > MAX_MESSAGE:
            log.warning("single instance: message too large, dropped")
            self._drop(sock)
            sock.abort()
            return
        if b"\n" not in buf:
            self._buffers[sock] = buf
            return
        line = buf.split(b"\n", 1)[0]
        self._buffers[sock] = b""
        paths = decode_paths(line)
        sock.write(b"ok\n" if paths is not None else b"bad\n")
        sock.flush()
        if paths is not None:
            log.info("single instance: %d path(s) from another launch", len(paths))
            self.pathsReceived.emit(paths)

    def _drop(self, sock: QLocalSocket) -> None:
        self._buffers.pop(sock, None)
        sock.deleteLater()

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._lock.isLocked():
            self._lock.unlock()


def bring_to_front(win) -> None:
    if win.isMinimized():
        win.showNormal()
    win.show()
    win.raise_()
    win.activateWindow()
