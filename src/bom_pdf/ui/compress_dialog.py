"""용량 압축본 방식 고르기 (1-4). 결과 프리셋 키(engine.build.COMPRESS_PRESETS 와 같은 이름)."""
from __future__ import annotations

from PySide6.QtWidgets import QButtonGroup, QDialog, QDialogButtonBox, QLabel, QRadioButton, QVBoxLayout

PRESETS = [  # (키, 이름, 설명) — 키는 engine.build.COMPRESS_PRESETS
    ("submit", "제출용 (권장)", "사진·스캔 이미지를 150DPI·JPEG 품질 75로 줄이고 안 쓰는 데이터를 정리합니다."),
    ("small", "최소 용량", "100DPI·품질 60. 화면으로 읽을 정도 — 작은 글씨·도장은 흐려질 수 있습니다."),
    ("clean", "화질 그대로", "이미지는 그대로 두고 안 쓰는 데이터 정리·압축·글꼴 부분 집합만 합니다."),
]


def human_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / 1024 / 1024:.1f}MB"
    return f"{max(1, round(n / 1024))}KB"


class CompressDialog(QDialog):
    def __init__(self, name: str, size: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("용량 압축본 만들기")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"{name} ({human_size(size)}) — 원본은 그대로 두고 <원본>_압축.pdf 를 새로 만듭니다."))
        self.group = QButtonGroup(self)
        self.buttons = {}
        for key, label, desc in PRESETS:
            b = QRadioButton(label)
            b.setToolTip(desc)
            self.group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
            d = QLabel(desc)
            d.setWordWrap(True)
            d.setStyleSheet("color:#555; margin-left:22px;")
            lay.addWidget(d)
        self.buttons["submit"].setChecked(True)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("만들기…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def preset(self) -> str:
        return next(k for k, b in self.buttons.items() if b.isChecked())
