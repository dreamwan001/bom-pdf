"""오른쪽 주석 목록 패널과 메모·태그 편집 대화상자.

- 쪽별로 묶어 보여 주고(쪽 순서, 쪽 안에서는 위에서 아래), 종류·태그로 거른다.
- 항목을 누르면 그 위치로 가고 주석을 고른다. 두 번 누르면 메모·태그 편집, Delete 는 삭제.
- 메모·선택 원문은 화면에만 보이고 기록(log)하지 않는다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from bom_pdf.services import annotations as an

ID_ROLE = Qt.ItemDataRole.UserRole
PAGE_ROLE = Qt.ItemDataRole.UserRole + 1
SNIPPET = 60


def color_icon(name: str) -> QIcon:
    r, g, b = an.color_rgb(name)
    pm = QPixmap(12, 12)
    pm.fill(QColor(round(r * 255), round(g * 255), round(b * 255)))
    return QIcon(pm)


def item_label(it: dict) -> str:
    body = it["note"] or it["text"]
    body = " ".join(body.split())
    if len(body) > SNIPPET:
        body = body[:SNIPPET - 1] + "…"
    tags = "  " + " ".join("#" + t for t in it["tags"]) if it["tags"] else ""
    return f"[{an.TYPE_LABELS[it['type']]}] {body}{tags}".rstrip()


class AnnotationPanel(QWidget):
    itemActivated = Signal(int, str)  # 쪽, 주석 id
    editRequested = Signal(str)
    deleteRequested = Signal(str)
    exportRequested = Signal()

    ALL = ""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.store: an.AnnotationStore | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        self.status = QLabel()
        self.status.setWordWrap(True)
        lay.addWidget(self.status)
        row = QHBoxLayout()
        self.type_filter = QComboBox()
        self.type_filter.addItem("모든 종류", self.ALL)
        for t in an.TYPES:
            self.type_filter.addItem(an.TYPE_LABELS[t], t)
        self.type_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.type_filter)
        self.tag_filter = QComboBox()
        self.tag_filter.addItem("모든 태그", self.ALL)
        self.tag_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.tag_filter, 1)
        lay.addLayout(row)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setRootIsDecorated(True)
        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.itemActivated.connect(self._on_clicked)  # Enter
        self.tree.itemDoubleClicked.connect(self._on_double)
        self.tree.installEventFilter(self)
        lay.addWidget(self.tree, 1)
        btns = QHBoxLayout()
        self.edit_btn = QPushButton("편집…")
        self.edit_btn.clicked.connect(lambda: self._emit_selected(self.editRequested))
        self.delete_btn = QPushButton("삭제")
        self.delete_btn.clicked.connect(lambda: self._emit_selected(self.deleteRequested))
        self.export_btn = QPushButton("메모 포함 PDF…")
        self.export_btn.setToolTip("주석을 표준 PDF 주석으로 넣은 새 파일을 만듭니다(원본은 그대로)")
        self.export_btn.clicked.connect(self.exportRequested)
        btns.addWidget(self.edit_btn)
        btns.addWidget(self.delete_btn)
        btns.addStretch(1)
        btns.addWidget(self.export_btn)
        lay.addLayout(btns)
        self.tree.currentItemChanged.connect(lambda *_: self._update_buttons())
        self.set_store(None, "")

    # ---- 자료
    def set_store(self, store: an.AnnotationStore | None, status: str = "") -> None:
        self.store = store
        self.status.setText(status)
        self.status.setVisible(bool(status))
        self.refresh()

    def refresh(self) -> None:
        keep = self.selected_id()
        tags = self.store.all_tags() if self.store else []
        cur_tag = self.tag_filter.currentData()
        self.tag_filter.blockSignals(True)
        self.tag_filter.clear()
        self.tag_filter.addItem("모든 태그", self.ALL)
        for t in sorted(tags):
            self.tag_filter.addItem("#" + t, t)
        self.tag_filter.setCurrentIndex(max(0, self.tag_filter.findData(cur_tag)))
        self.tag_filter.blockSignals(False)

        self.tree.clear()
        if self.store is None:
            self._update_buttons()
            return
        want_type, want_tag = self.type_filter.currentData(), self.tag_filter.currentData()
        items = [it for it in self.store.items
                 if (not want_type or it["type"] == want_type) and (not want_tag or want_tag in it["tags"])]
        items.sort(key=lambda it: (it["page"], an.item_bounds(it)[1], an.item_bounds(it)[0]))
        groups: dict[int, QTreeWidgetItem] = {}
        for it in items:
            g = groups.get(it["page"])
            if g is None:
                g = QTreeWidgetItem([""])
                g.setData(0, PAGE_ROLE, it["page"])
                g.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.tree.addTopLevelItem(g)
                groups[it["page"]] = g
            child = QTreeWidgetItem([item_label(it)])
            child.setIcon(0, color_icon(it["color"]))
            child.setData(0, ID_ROLE, it["id"])
            child.setData(0, PAGE_ROLE, it["page"])
            child.setToolTip(0, it["note"] or it["text"])
            g.addChild(child)
        for page, g in groups.items():
            g.setText(0, f"{page + 1}쪽 ({g.childCount()})")
            g.setExpanded(True)
        if keep:
            self.select_item(keep)
        self._update_buttons()

    def count_shown(self) -> int:
        return sum(self.tree.topLevelItem(i).childCount() for i in range(self.tree.topLevelItemCount()))

    def find_item(self, item_id: str) -> QTreeWidgetItem | None:
        for i in range(self.tree.topLevelItemCount()):
            g = self.tree.topLevelItem(i)
            for j in range(g.childCount()):
                if g.child(j).data(0, ID_ROLE) == item_id:
                    return g.child(j)
        return None

    def select_item(self, item_id: str | None) -> None:
        node = self.find_item(item_id) if item_id else None
        self.tree.blockSignals(True)
        if node is not None:
            self.tree.setCurrentItem(node)
        else:
            self.tree.setCurrentItem(None)
            self.tree.clearSelection()
        self.tree.blockSignals(False)
        self._update_buttons()

    def selected_id(self) -> str | None:
        node = self.tree.currentItem()
        return node.data(0, ID_ROLE) if node is not None else None

    # ---- 신호
    def _update_buttons(self) -> None:
        writable = self.store is not None and not self.store.read_only
        has = self.selected_id() is not None
        self.edit_btn.setEnabled(writable and has)
        self.delete_btn.setEnabled(writable and has)
        self.export_btn.setEnabled(self.store is not None and bool(self.store.items))

    def _on_clicked(self, node: QTreeWidgetItem, _col: int = 0) -> None:
        item_id = node.data(0, ID_ROLE)
        if item_id:
            self.itemActivated.emit(node.data(0, PAGE_ROLE), item_id)

    def _on_double(self, node: QTreeWidgetItem, _col: int = 0) -> None:
        item_id = node.data(0, ID_ROLE)
        if item_id:
            self.editRequested.emit(item_id)

    def _emit_selected(self, sig) -> None:
        item_id = self.selected_id()
        if item_id:
            sig.emit(item_id)

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        from PySide6.QtCore import QEvent

        if obj is self.tree and ev.type() == QEvent.Type.KeyPress and ev.key() == Qt.Key.Key_Delete:
            if self.delete_btn.isEnabled():
                self._emit_selected(self.deleteRequested)
            return True
        return super().eventFilter(obj, ev)


class AnnotationDialog(QDialog):
    """메모·태그·색 편집. 결과는 values()."""

    def __init__(self, title: str, note: str = "", tags=(), color: str = an.DEFAULT_COLOR, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(420, 260)
        form = QFormLayout(self)
        self.note = QPlainTextEdit(note)
        self.note.setPlaceholderText("메모")
        form.addRow("메모", self.note)
        self.tags = QLineEdit(", ".join(tags))
        self.tags.setPlaceholderText("쉼표로 구분 — 예: 쟁점2, 반박")
        form.addRow("태그", self.tags)
        self.color = QComboBox()
        for key, (label, _rgb) in an.COLORS.items():
            self.color.addItem(color_icon(key), label, key)
        self.color.setCurrentIndex(max(0, self.color.findData(color)))
        form.addRow("색", self.color)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.note.setFocus()

    def values(self) -> dict:
        return {"note": self.note.toPlainText().strip(), "tags": an.clean_tags(self.tags.text()),
                "color": self.color.currentData()}
