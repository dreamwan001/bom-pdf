"""도움말 › 환경 점검 (O1) — 이 PC 환경 요약 + 포장본 자가 점검 결과, 복사 버튼.

다른 PC 에서 문제가 났을 때 사용자가 이 창의 내용을 복사해 보내면 환경을 알 수 있게 한다.
파일 이름·문서 내용은 넣지 않는다(합성 자료로만 점검). 자가 점검은 별도 스레드에서 돌고(약 1–3초),
렌더 작업자·작업 프로세스를 하나씩 더 띄웠다가 끝낸다.
"""
from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from bom_pdf import APP_NAME, __version__, paths, sysinfo
from bom_pdf.ui import page_cache


def env_lines(env: dict, cache_mb: int | None = None) -> list[str]:
    lines = [f"{APP_NAME} {__version__}",
             f"Windows: {env['os']}",
             f"RAM: 전체 {env['ram_total_gb']}GB · 사용 가능 {env['ram_avail_gb']}GB",
             f"CPU: 논리 {env['cpus']}개 · {env['cpu']}",
             f"화면(논리 해상도@배율): {', '.join(env['screens']) or '?'}",
             f"데이터 폴더 여유 공간: {env['data_free_gb']}GB",
             f"설치: {env['install']}"]
    if cache_mb is not None:
        lines.append(f"쪽 그림 캐시 상한: {cache_mb}MB")
    if env.get("simulated"):
        lines.append(f"시험용 흉내: {env['simulated']}")
    return lines


def selftest_lines(rep: dict) -> list[str]:
    if rep.get("ok"):
        head = "자가 점검: 정상"
    else:
        head = "자가 점검: 문제 있음" + (f" ({rep['error']})" if rep.get("error") else "")
    return [head,
            f"  PDF 렌더 작업자: 열기 {rep.get('open', '-')} · 렌더 {rep.get('render', '-')} · "
            f"첫 쪽 {rep.get('first_page_ms', '-')}ms",
            f"  사진 작업 프로세스: 읽기 {rep.get('scan', '-')} · 사진→PDF {rep.get('image_pdf', '-')} · "
            f"{rep.get('jobs_ms', '-')}ms",
            f"  화면 프로세스의 무거운 모듈: {', '.join(rep.get('ui_process_heavy_modules') or []) or '없음'}"]


class _Relay(QObject):
    done = Signal(object)


class EnvDialog(QDialog):
    def __init__(self, parent=None, cache_mb: int | None = None, run_selftest: bool = True):
        super().__init__(parent)
        self.setWindowTitle("환경 점검")
        self.resize(640, 380)
        env = sysinfo.environment(page_cache.real_screens(), paths.data_dir())
        self._env_text = "\n".join(env_lines(env, cache_mb))
        self.selftest: dict | None = None
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("문제를 알릴 때 [복사] 를 눌러 이 내용을 함께 보내 주세요. 파일 이름·문서 내용은 들어 있지 않습니다."))
        self.text = QPlainTextEdit(readOnly=True)
        lay.addWidget(self.text)
        buttons = QDialogButtonBox()
        self.copy_button = QPushButton("복사")
        self.copy_button.clicked.connect(self.copy)
        buttons.addButton(self.copy_button, QDialogButtonBox.ButtonRole.ActionRole)
        buttons.addButton(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._relay = _Relay(self)
        self._relay.done.connect(self._on_selftest)
        self._refresh("자가 점검: 하는 중…" if run_selftest else "")
        if run_selftest:
            threading.Thread(target=self._run, daemon=True, name="bom-pdf-selftest").start()

    def _run(self) -> None:
        from bom_pdf import selftest

        try:
            rep = selftest.run_builtin()
        except Exception as e:  # 점검 실패도 보여 준다
            rep = {"ok": False, "error": type(e).__name__}
        try:
            self._relay.done.emit(rep)
        except RuntimeError:
            pass  # 창이 이미 닫힘

    def _on_selftest(self, rep: dict) -> None:
        self.selftest = rep
        self._refresh("\n".join(selftest_lines(rep)))

    def _refresh(self, tail: str) -> None:
        self.text.setPlainText(self._env_text + ("\n\n" + tail if tail else ""))

    def copy(self) -> None:
        QGuiApplication.clipboard().setText(self.text.toPlainText())
