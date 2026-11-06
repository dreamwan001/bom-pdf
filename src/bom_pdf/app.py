"""앱 시작: 로그·예외 기록 → 렌더 작업자 → 주 창 → 명령줄 파일 열기."""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import os
import sys
import threading

from bom_pdf import APP_NAME, __version__, paths

log = logging.getLogger("bom_pdf")
_fault_file = None  # faulthandler 가 쓰는 파일은 프로세스가 끝날 때까지 열어 둔다


def setup_logging() -> None:
    """`logs\\bom-pdf.log` (1MB × 3 회전) + C 수준 급사는 `logs\\faulthandler.log`."""
    global _fault_file
    # 창 실행(pythonw/포장본)에서는 표준 입출력이 None 이다 (위키 P13)
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    logs = paths.logs_dir()
    handler = logging.handlers.RotatingFileHandler(
        logs / "bom-pdf.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    try:
        _fault_file = open(logs / "faulthandler.log", "a", encoding="utf-8")
        faulthandler.enable(_fault_file)
    except OSError:
        pass


def install_excepthooks() -> None:
    """처리 못 한 예외는 기록하고 창은 계속 산다."""
    showing = [False]

    def hook(exc_type, exc, tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("unhandled exception", exc_info=(exc_type, exc, tb))
        if showing[0] or threading.current_thread() is not threading.main_thread():
            return
        try:
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                showing[0] = True
                QMessageBox.warning(None, APP_NAME,
                                    f"예기치 않은 오류가 생겨 기록했습니다.\n기록 위치: {paths.logs_dir()}")
        except Exception:
            pass
        finally:
            showing[0] = False

    sys.excepthook = hook
    threading.excepthook = lambda a: log.critical(
        "unhandled thread exception in %s", a.thread.name if a.thread else "?",
        exc_info=(a.exc_type, a.exc_value, a.exc_traceback))


ENV_FIRST_PAGE_MARK = "BOM_PDF_FIRST_PAGE_MARK"  # 측정용: 첫 쪽이 그려지면 이 파일에 time.time() 을 적는다
ENV_EXIT_AFTER_FIRST_PAGE = "BOM_PDF_EXIT_AFTER_FIRST_PAGE"  # 측정용: 그 뒤 창을 닫는다(정상 종료 경로)


def _install_first_page_mark(app, bridge, win) -> None:
    """측정 전용(환경 변수가 있을 때만): 첫 렌더 결과가 보기에 전달되고 다음 이벤트 차례(그리기)가 지난 시각."""
    import time

    mark = os.environ.get(ENV_FIRST_PAGE_MARK)
    if not mark:
        return
    from PySide6.QtCore import QTimer

    from bom_pdf.ui.bridge import RenderedPage

    done = [False]

    def on_event(ev) -> None:
        if done[0] or not isinstance(ev, RenderedPage) or ev.lane < 0:  # 썸네일·편집·인쇄 통로 제외
            return
        done[0] = True

        def write() -> None:
            app.processEvents()  # 대기 중인 그리기
            with open(mark, "w", encoding="utf-8") as f:
                f.write(repr(time.time()))
            if os.environ.get(ENV_EXIT_AFTER_FIRST_PAGE):
                QTimer.singleShot(200, win.close)

        QTimer.singleShot(0, write)

    bridge.received.connect(on_event)


def _log_environment() -> None:
    """O1: 시작 때 환경 한 줄(Windows·RAM·CPU·화면·데이터 폴더 여유·설치 위치). 파일 이름·문서 내용 없음."""
    import json

    try:
        from bom_pdf import sysinfo
        from bom_pdf.ui.page_cache import real_screens

        log.info("env %s", json.dumps(sysinfo.environment(real_screens(), paths.data_dir()), ensure_ascii=False))
    except Exception:  # 환경 기록 실패로 시작이 막히면 안 된다
        log.exception("env log failed")


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--self-test"]:  # 포장본 점검(작업자·작업 프로세스·Pillow·OpenCV) — 창 없음
        from bom_pdf import selftest

        return selftest.main(argv[1:])
    setup_logging()
    install_excepthooks()
    log.info("start %s %s", APP_NAME, __version__)

    from PySide6.QtWidgets import QApplication

    from bom_pdf.ui.single_instance import SingleInstance, send_to_running

    app = QApplication([sys.argv[0], *[a for a in argv if a.startswith("-")]])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    from bom_pdf.ui.desktop import app_icon

    app.setWindowIcon(app_icon())  # 제목 표시줄·작업 표시줄·대화상자(포장본 EXE 아이콘은 spec)
    _log_environment()
    files = [os.path.abspath(a) for a in argv if not a.startswith("-")]

    # 1-6 단일 인스턴스: 무거운 import·작업자 기동 전에 가른다
    instance = SingleInstance(paths.data_dir())
    if not instance.acquire():
        if send_to_running(instance.name, files):
            log.info("handed %d path(s) to the running instance", len(files))
            return 0
        log.warning("running instance did not answer")
        from PySide6.QtWidgets import QMessageBox

        QMessageBox.warning(None, APP_NAME, f"이미 실행 중인 {APP_NAME}가 응답하지 않습니다.\n"
                            "잠시 뒤 다시 시도하거나, 작업 관리자에서 남아 있는 bom-pdf 를 확인해 주세요.")
        return 1

    from PySide6.QtCore import QTimer

    from bom_pdf.services.document import DocumentService
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.services.session import SessionStore
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow
    from bom_pdf.ui.session import SessionRecorder
    from bom_pdf.ui.single_instance import bring_to_front

    bridge = ServiceBridge()
    service = DocumentService(bridge)
    service.start()  # 작업자 프로세스를 창보다 먼저 띄워 첫 쪽을 앞당긴다
    win = MainWindow(service, bridge, RecentDocuments())
    _install_first_page_mark(app, bridge, win)

    def on_paths(received: list[str]) -> None:
        bring_to_front(win)
        if received:
            win.receive_paths(received)

    instance.pathsReceived.connect(on_paths)
    recorder = SessionRecorder(win, SessionStore())
    win.show()
    if files:
        QTimer.singleShot(0, lambda: win.receive_paths(files))
    recorder.start()
    code = app.exec()
    recorder.mark_closed()  # closeEvent 를 거치지 않은 종료(시스템 종료 등)도 정상 종료로
    service.shutdown()
    instance.release()
    log.info("exit %s", code)
    return code
