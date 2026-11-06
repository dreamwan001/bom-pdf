"""페이지 편집 창 (1-4, system-design §4-4): 썸네일 격자 + 회전·삭제·순서·추출·병합·분할 → 새 파일로 저장.

- 편집은 services.pageops.PageOps(가상 문서)만 바꾼다. 파일은 "새 파일로 저장"(또는 추출·분할)을 눌러야
  작업 프로세스가 한 번에 만든다. 원본은 읽기만 한다(D1·D4).
- 격자: Ctrl/Shift 다중 선택, 끌어서 순서 바꾸기(자체 끌기 — Qt 끌어놓기는 모델 행을 지우는 경로가 있어 쓰지 않음),
  오른쪽 메뉴(왼쪽·오른쪽 회전, 삭제, 추출). 썸네일은 렌더 작업자의 EDIT_LANE 으로 보이는 쪽만 요청한다.
- 병합할 파일 중 열려 있지 않은 것은 탭 없이 작업자에 열어 두고(쪽 수·썸네일용) 창을 닫을 때 닫는다.
- 주석: 새 파일은 sha256 이 달라 사이드카 메모가 따라가지 않는다. 대신 "메모 넣기"를 켜면 원본들의 사이드카 주석을
  1-3 내보내기 코드로 표준 주석으로 넣는다(쪽이 옮겨지면 함께, 지운 쪽 것은 빠짐).
- 시험에서는 ask_* 함수를 바꿔 끼운다.
"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import QAbstractListModel, QModelIndex, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QKeySequence, QPainter, QPen, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QSpinBox, QToolBar, QVBoxLayout,
)

from bom_pdf.services import diskspace
from bom_pdf.services import document as ds
from bom_pdf.services import outputs
from bom_pdf.services import pageops as po
from bom_pdf.ui.page_cache import PageCache

log = logging.getLogger(__name__)

GRID_BOX = 120  # 썸네일 칸 (논리 px, 정사각형 안에 맞춤)
GRID_CACHE_BYTES = 48 * 1024 * 1024
PLACEHOLDER = QColor(0xf4, 0xf4, 0xf4)
DROP_PEN = QColor(0x1a, 0x73, 0xe8)
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

JOB_MESSAGES = {
    ds.EXPORT_CHANGED: "원본 파일이 연 뒤에 바뀌어 만들지 않았습니다. 탭을 닫고 다시 열어 주세요.",
    ds.EXPORT_REFUSED: "원본 파일에는 덮어쓸 수 없습니다. 다른 이름이나 폴더를 고르세요.",
    ds.EXPORT_INTERRUPTED: "만드는 중 작업이 멈췄습니다. 원본은 바뀌지 않았습니다. 다시 시도해 주세요.",
    ds.JOB_CANCELLED: "취소했습니다. 만들던 파일은 지웠습니다.",
    ds.EXPORT_ERROR: "새 파일을 만들지 못했습니다. 저장 위치의 권한·공간을 확인해 주세요.",
}


# --------------------------------------------------------------------------- 모델


class PageGridModel(QAbstractListModel):
    def __init__(self, editor: "PageEditorDialog"):
        super().__init__()
        self.editor = editor
        self._pix: dict[tuple, QPixmap] = {}

    @property
    def ops(self) -> po.PageOps:
        return self.editor.ops

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.ops.pages)

    def reset(self) -> None:
        self.beginResetModel()
        self.endResetModel()

    def flags(self, index):
        return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid() or index.row() >= len(self.ops.pages):
            return None
        i = index.row()
        ref = self.ops.pages[i]
        src = self.ops.sources[ref.src]
        multi = len({r.src for r in self.ops.pages}) > 1
        if role == Qt.ItemDataRole.DisplayRole:
            label = str(i + 1)
            if multi:
                label += f"  ({LETTERS[ref.src % 26]}{ref.page + 1})"
            if ref.rotate:
                label += f"  ↻{ref.rotate}°"
            return label
        if role == Qt.ItemDataRole.ToolTipRole:
            tip = f"{src.name} {ref.page + 1}쪽"
            return tip + (f", {ref.rotate}° 더 돌림" if ref.rotate else "")
        if role == Qt.ItemDataRole.DecorationRole:
            return self._decoration(i)
        return None

    def _decoration(self, i: int) -> QPixmap:
        ref = self.ops.pages[i]
        src = self.ops.sources[ref.src]
        scale = self.editor.thumb_scale(ref.src)
        img = self.editor.thumbs.get(src.doc_id, ref.page, scale) if src.doc_id is not None else None
        key = (src.doc_id, ref.page, ref.rotate, scale, img is not None)
        pm = self._pix.get(key)
        if pm is not None:
            return pm
        w, h = self.ops.page_size(i)
        k = GRID_BOX / max(w, h)
        size = QSize(max(1, round(w * k)), max(1, round(h * k)))
        if img is None:
            pm = QPixmap(size)
            pm.fill(PLACEHOLDER)
        else:
            im = img.transformed(QTransform().rotate(ref.rotate)) if ref.rotate else img
            pm = QPixmap.fromImage(im.scaled(size, Qt.AspectRatioMode.KeepAspectRatio,
                                             Qt.TransformationMode.SmoothTransformation))
        if len(self._pix) > 400:  # 칸 하나 ≈ 80KB
            self._pix.clear()
        self._pix[key] = pm
        return pm


# --------------------------------------------------------------------------- 격자


class PageGrid(QListView):
    """썸네일 격자. 고른 쪽을 끌면 moveRequested(쪽 번호들, 들어갈 자리)."""

    moveRequested = Signal(list, int)
    visibleChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setUniformItemSizes(True)
        self.setIconSize(QSize(GRID_BOX, GRID_BOX))
        self.setGridSize(QSize(GRID_BOX + 24, GRID_BOX + 34))
        self.setSpacing(4)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(False)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self.menu_actions: list = []
        self._press: QPoint | None = None
        self._drag_ok = False
        self.dragging = False
        self.drop_index: int | None = None
        self._vis_timer = QTimer(self)
        self._vis_timer.setSingleShot(True)
        self._vis_timer.setInterval(30)
        self._vis_timer.timeout.connect(self.visibleChanged)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._vis_timer.start())

    def selected_rows(self) -> list[int]:
        return sorted(ix.row() for ix in self.selectionModel().selectedIndexes())

    def select_rows(self, rows, current: int | None = None) -> None:
        sm = self.selectionModel()
        sm.clearSelection()
        m = self.model()
        for r in rows:
            if 0 <= r < m.rowCount():
                sm.select(m.index(r, 0), sm.SelectionFlag.Select)
        if current is not None and 0 <= current < m.rowCount():
            sm.setCurrentIndex(m.index(current, 0), sm.SelectionFlag.NoUpdate)
            self.scrollTo(m.index(current, 0))

    def visible_rows(self) -> list[int]:
        vr = self.viewport().rect()
        m = self.model()
        out = []
        for r in range(m.rowCount()):
            if self.visualRect(m.index(r, 0)).intersects(vr):
                out.append(r)
            elif out:
                break  # 줄 단위로 이어지므로 보이는 구간이 끝남
        return out

    def drop_index_at(self, pos: QPoint) -> int:
        """끌어 놓을 자리(그 쪽 앞). 쪽 오른쪽 절반이면 그 뒤."""
        m = self.model()
        n = m.rowCount()
        best, best_d = n, None
        for r in self.visible_rows() or range(n):
            rect = self.visualRect(m.index(r, 0))
            c = rect.center()
            d = (abs(c.y() - pos.y()) * 4) + abs(c.x() - pos.x())  # 같은 줄 우선
            if best_d is None or d < best_d:
                best_d = d
                best = r + (1 if pos.x() > c.x() else 0)
        return best

    # ---- 마우스
    def mousePressEvent(self, ev) -> None:  # noqa: N802
        self._press = None
        self._drag_ok = False
        super().mousePressEvent(ev)
        if ev.button() == Qt.MouseButton.LeftButton and ev.modifiers() == Qt.KeyboardModifier.NoModifier:
            ix = self.indexAt(ev.position().toPoint())
            if ix.isValid() and self.selectionModel().isSelected(ix):
                self._press = ev.position().toPoint()
                self._drag_ok = True

    def mouseMoveEvent(self, ev) -> None:  # noqa: N802
        if self._drag_ok and self._press is not None and ev.buttons() & Qt.MouseButton.LeftButton:
            pos = ev.position().toPoint()
            if self.dragging or (pos - self._press).manhattanLength() >= 6:
                self.dragging = True
                self.drop_index = self.drop_index_at(pos)
                h = self.viewport().height()
                vs = self.verticalScrollBar()
                if pos.y() < 24:
                    vs.setValue(vs.value() - 20)
                elif pos.y() > h - 24:
                    vs.setValue(vs.value() + 20)
                self.viewport().update()
                return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        if self.dragging:
            rows = self.selected_rows()
            dest = self.drop_index_at(ev.position().toPoint())
            self.dragging = False
            self.drop_index = None
            self._drag_ok = False
            self.setState(QAbstractItemView.State.NoState)
            self.viewport().update()
            if rows:
                self.moveRequested.emit(rows, dest)
            return
        self._drag_ok = False
        super().mouseReleaseEvent(ev)

    def contextMenuEvent(self, ev) -> None:  # noqa: N802
        ix = self.indexAt(ev.pos())
        if ix.isValid() and not self.selectionModel().isSelected(ix):
            self.select_rows([ix.row()], ix.row())
        if not self.selected_rows():
            return
        menu = QMenu(self)
        for a in self.menu_actions:
            if a is None:
                menu.addSeparator()
            else:
                menu.addAction(a)
        menu.exec(ev.globalPos())

    # ---- 그리기
    def paintEvent(self, ev) -> None:  # noqa: N802
        super().paintEvent(ev)
        if self.dragging and self.drop_index is not None:
            m = self.model()
            n = m.rowCount()
            if n:
                r = min(self.drop_index, n - 1)
                rect = self.visualRect(m.index(r, 0))
                x = rect.left() - 2 if self.drop_index < n else rect.right() + 2
                qp = QPainter(self.viewport())
                qp.setPen(QPen(DROP_PEN, 3))
                qp.drawLine(x, rect.top(), x, rect.bottom())
                qp.end()
        self._vis_timer.start()

    def resizeEvent(self, ev) -> None:  # noqa: N802
        super().resizeEvent(ev)
        self._vis_timer.start()


# --------------------------------------------------------------------------- 병합·분할 대화상자


class MergeDialog(QDialog):
    """합칠 문서와 차례. 결과 [("current",) | ("doc", doc_id) | ("file", 경로)]."""

    def __init__(self, current_label: str, open_docs: list[tuple[int, str]], parent=None):
        super().__init__(parent)
        self.setWindowTitle("문서 합치기")
        self.resize(520, 380)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("위에서부터 차례로 이어 붙입니다. 순서는 위로·아래로 버튼으로 바꿉니다."))
        row = QHBoxLayout()
        self.list = QListWidget()
        row.addWidget(self.list, 1)
        side = QVBoxLayout()
        self.open_menu = QMenu(self)
        for doc_id, path in open_docs:
            a = self.open_menu.addAction(os.path.basename(path))
            a.triggered.connect(lambda _=False, d=doc_id, p=path: self._add(("doc", d), os.path.basename(p)))
        b_tab = QPushButton("열린 문서 추가 ▾")
        b_tab.setMenu(self.open_menu)
        b_tab.setEnabled(bool(open_docs))
        b_file = QPushButton("파일 추가…")
        b_file.clicked.connect(self._add_files)
        b_up, b_down, b_del = QPushButton("위로"), QPushButton("아래로"), QPushButton("빼기")
        b_up.clicked.connect(lambda: self._shift(-1))
        b_down.clicked.connect(lambda: self._shift(1))
        b_del.clicked.connect(self._remove)
        for b in (b_tab, b_file, b_up, b_down, b_del):
            side.addWidget(b)
        side.addStretch(1)
        row.addLayout(side)
        lay.addLayout(row, 1)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("합치기")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._add(("current",), current_label)

    def _add(self, entry: tuple, label: str) -> None:
        it = QListWidgetItem(label)
        it.setData(Qt.ItemDataRole.UserRole, entry)
        self.list.addItem(it)
        self.list.setCurrentItem(it)

    def _add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "합칠 PDF", "", "PDF 파일 (*.pdf)")
        for f in files:
            self._add(("file", os.path.abspath(f)), os.path.basename(f))

    def _shift(self, step: int) -> None:
        r = self.list.currentRow()
        t = r + step
        if r < 0 or not 0 <= t < self.list.count():
            return
        it = self.list.takeItem(r)
        self.list.insertItem(t, it)
        self.list.setCurrentRow(t)

    def _remove(self) -> None:
        it = self.list.currentItem()
        if it is not None and it.data(Qt.ItemDataRole.UserRole) != ("current",):
            self.list.takeItem(self.list.row(it))

    def entries(self) -> list[tuple]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]


class SplitDialog(QDialog):
    """나누기 기준. 결과 ("ranges", "1-3, 4-") | ("every", N) | ("bookmarks", None)."""

    def __init__(self, page_count: int, has_bookmarks: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("나누기")
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(f"지금 편집 중인 {page_count}쪽을 여러 파일로 나눕니다. 파일 이름은 <원본>_1, _2 …"))
        self.group = QButtonGroup(self)
        self.r_ranges = QRadioButton("쪽 범위로 (쉼표로 구분, 범위마다 파일 하나)")
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("예: 1-3, 4-10, 11-")
        self.r_every = QRadioButton("N쪽마다")
        self.every = QSpinBox()
        self.every.setRange(1, max(1, page_count))
        self.every.setValue(min(10, max(1, page_count)))
        self.r_marks = QRadioButton("최상위 책갈피마다")
        self.r_marks.setEnabled(has_bookmarks)
        if not has_bookmarks:
            self.r_marks.setToolTip("책갈피가 없습니다")
        for b in (self.r_ranges, self.r_every, self.r_marks):
            self.group.addButton(b)
        self.r_ranges.setChecked(True)
        lay.addWidget(self.r_ranges)
        lay.addWidget(self.ranges)
        h = QHBoxLayout()
        h.addWidget(self.r_every)
        h.addWidget(self.every)
        h.addStretch(1)
        lay.addLayout(h)
        lay.addWidget(self.r_marks)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("나누기…")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def value(self) -> tuple:
        if self.r_every.isChecked():
            return ("every", self.every.value())
        if self.r_marks.isChecked():
            return ("bookmarks", None)
        return ("ranges", self.ranges.text())


# --------------------------------------------------------------------------- 편집 창


class PageEditorDialog(QDialog):
    closed = Signal()

    def __init__(self, host, doc: ds.Document, parent=None):
        super().__init__(parent)
        self.host = host
        self.service: ds.DocumentService = host.service
        self.ops = po.PageOps(po.Source(doc.path, list(doc.page_sizes), doc.password, doc.sha256, doc.doc_id))
        self.thumbs = PageCache(GRID_CACHE_BYTES)
        self._acks: dict[int, int] = {}
        self._last_req: dict[int, tuple] = {}
        self.aux_docs: set[int] = set()  # 이 창이 탭 없이 연 문서
        self.outlines: dict[int, list] = {}  # doc_id -> 책갈피
        self._pending: dict[int, str] = {}  # 여는 중인 병합 파일 doc_id -> 경로
        self._merge_entries: list | None = None
        self._merge_failed: list[str] = []
        self.job_id: int | None = None
        self._job_done = None
        self.last_result: tuple[str, dict] | None = None  # (무엇, 결과) — 시험·확인용
        self.last_notice: str | None = None
        self._closing = False
        self._annots: dict[str, list | None] = {}

        self.setWindowTitle(f"페이지 편집 — {doc.name}")
        self.resize(1000, 720)
        self.setModal(False)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        tb = QToolBar()
        lay.addWidget(tb)
        self.model = PageGridModel(self)
        self.grid = PageGrid()
        self.grid.setModel(self.model)
        self.grid.moveRequested.connect(self.move_pages)
        self.grid.visibleChanged.connect(self._request_thumbs)
        self.grid.selectionModel().selectionChanged.connect(lambda *_: self._update_actions())
        lay.addWidget(self.grid, 1)

        def act(text, slot, key=None, tip=None):
            a = QAction(text, self)
            a.triggered.connect(lambda _=False: slot())
            if key is not None:
                a.setShortcuts(key if isinstance(key, list) else [key])
                a.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            if tip:
                a.setToolTip(tip)
            self.addAction(a)
            return a

        K = QKeySequence
        self.act_rot_left = act("왼쪽으로 회전", lambda: self.rotate_selected(-90), K("Ctrl+L"))
        self.act_rot_right = act("오른쪽으로 회전", lambda: self.rotate_selected(90), K("Ctrl+R"))
        self.act_delete = act("삭제", self.delete_selected, K(Qt.Key.Key_Delete))
        self.act_extract = act("고른 쪽 추출…", self.extract_selected, tip="고른 쪽만 새 파일로 (편집 내용은 그대로)")
        self.act_merge = act("문서 합치기…", self.merge)
        self.act_split = act("나누기…", self.split)
        self.act_undo = act("되돌리기", self.undo, K.StandardKey.Undo)
        self.act_redo = act("다시 하기", self.redo, [K("Ctrl+Y"), K("Ctrl+Shift+Z")])
        for a in (self.act_rot_left, self.act_rot_right, self.act_delete, self.act_extract, None,
                  self.act_merge, self.act_split, None, self.act_undo, self.act_redo):
            if a is None:
                tb.addSeparator()
            else:
                tb.addAction(a)
        self.grid.menu_actions = [self.act_rot_left, self.act_rot_right, None, self.act_delete, None,
                                  self.act_extract]

        foot = QHBoxLayout()
        self.status = QLabel()
        foot.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(180)
        self.progress.hide()
        foot.addWidget(self.progress)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_job)
        self.cancel_btn.hide()
        foot.addWidget(self.cancel_btn)
        lay.addLayout(foot)
        foot2 = QHBoxLayout()
        self.annot_check = QCheckBox("메모(주석)를 표준 주석으로 넣기")
        self.annot_check.setToolTip("새 파일은 원본과 다른 파일이라 사이드카 메모가 따라가지 않습니다. "
                                    "켜면 원본의 메모를 다른 뷰어에서도 보이는 주석으로 넣습니다.")
        foot2.addWidget(self.annot_check)
        self.open_check = QCheckBox("저장한 파일 열기")
        self.open_check.setChecked(True)
        foot2.addWidget(self.open_check)
        foot2.addStretch(1)
        self.save_btn = QPushButton("새 파일로 저장…")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self.save)
        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.close)
        foot2.addWidget(self.save_btn)
        foot2.addWidget(self.close_btn)
        lay.addLayout(foot2)

        out = host.sidebar.outline(doc.doc_id)
        if out is not None:
            self.outlines[doc.doc_id] = out
        else:
            self.service.request_outline(doc.doc_id)
        self._refresh(select=[])

    # ------------------------------------------------------------------ 썸네일
    def thumb_scale(self, si: int) -> float:
        src = self.ops.sources[si]
        big = max(max(w, h) for w, h in src.page_sizes)
        return round(GRID_BOX / big * self.devicePixelRatioF(), 4)

    def _request_thumbs(self) -> None:
        want: dict[int, list[int]] = {}
        scales: dict[int, float] = {}
        for r in self.grid.visible_rows():
            ref = self.ops.pages[r]
            src = self.ops.sources[ref.src]
            if src.doc_id is None:
                continue
            s = scales.setdefault(src.doc_id, self.thumb_scale(ref.src))
            if not self.thumbs.contains(src.doc_id, ref.page, s):
                lst = want.setdefault(src.doc_id, [])
                if ref.page not in lst:
                    lst.append(ref.page)
        for doc_id in set(want) | set(self._last_req):
            pages = want.get(doc_id, [])
            scale = scales.get(doc_id) or self._last_req[doc_id][0]
            key = (scale, tuple(pages))
            if self._last_req.get(doc_id) == key:
                continue
            self._last_req[doc_id] = key
            self.service.request_pages(doc_id, scale, pages, self._acks.get(doc_id, 0), ds.EDIT_LANE)
            if not pages:
                del self._last_req[doc_id]

    def on_thumb(self, ev) -> None:
        self._acks[ev.doc_id] = max(self._acks.get(ev.doc_id, 0), ev.seq)
        if ev.image is not None:
            self.thumbs.put(ev.doc_id, ev.page, ev.scale, ev.image)
            self.grid.viewport().update()
        self._last_req.pop(ev.doc_id, None)
        self._request_thumbs()

    def on_thumb_error(self, ev) -> None:
        self._acks[ev.doc_id] = max(self._acks.get(ev.doc_id, 0), ev.seq)

    # ------------------------------------------------------------------ 서비스 이벤트 (주 창이 넘겨 준다)
    def on_service_event(self, ev) -> None:
        if isinstance(ev, ds.DocumentOpened) and ev.doc.doc_id in self._pending:
            self._pending.pop(ev.doc.doc_id)
            self.aux_docs.add(ev.doc.doc_id)
            self.service.request_outline(ev.doc.doc_id)
            self._maybe_finish_merge()
        elif isinstance(ev, ds.DocumentOpenFailed) and ev.doc_id in self._pending:
            path = self._pending.pop(ev.doc_id)
            self._merge_failed.append(os.path.basename(path))
            self.service.close(ev.doc_id)
            self._maybe_finish_merge()
        elif isinstance(ev, ds.DocumentHashed):
            for s in self.ops.sources:
                if s.doc_id == ev.doc_id and s.sha256 is None:
                    s.sha256 = ev.sha256
            self._update_actions()
        elif isinstance(ev, ds.Outline):
            self.outlines[ev.doc_id] = ev.items
            self._update_actions()
        elif isinstance(ev, ds.EngineRestarted):
            self._acks.clear()
            self._last_req.clear()
            self._request_thumbs()

    # ------------------------------------------------------------------ 편집
    def _refresh(self, select=None, current=None) -> None:
        keep = self.grid.selected_rows() if select is None else select
        self.model.reset()
        n = len(self.ops)
        self.grid.select_rows([r for r in keep if r < n], current)
        self._update_actions()
        self._request_thumbs()

    def _update_actions(self) -> None:
        busy = self.job_id is not None or bool(self._pending)
        sel = self.grid.selected_rows()
        for a in (self.act_rot_left, self.act_rot_right, self.act_extract):
            a.setEnabled(bool(sel) and not busy)
        self.act_delete.setEnabled(bool(sel) and len(sel) < len(self.ops) and not busy)
        self.act_merge.setEnabled(not busy)
        self.act_split.setEnabled(len(self.ops) > 1 and not busy)
        self.act_undo.setEnabled(self.ops.can_undo() and not busy)
        self.act_redo.setEnabled(self.ops.can_redo() and not busy)
        self.save_btn.setEnabled(not busy)
        n_annots = sum(len(x) for x in self._annotation_lists() if x)
        self.annot_check.setEnabled(n_annots > 0)
        self.annot_check.setText(f"메모(주석 {n_annots}개)를 표준 주석으로 넣기" if n_annots
                                 else "메모(주석)를 표준 주석으로 넣기 — 원본에 메모 없음")
        if n_annots == 0:
            self.annot_check.setChecked(False)
        srcs = len({r.src for r in self.ops.pages})
        text = f"{len(self.ops)}쪽" + (f" · 문서 {srcs}개" if srcs > 1 else "")
        if sel:
            text += f" · {len(sel)}쪽 고름"
        if self.ops.dirty:
            text += " · 바뀜(아직 저장 안 함)"
        if self._pending:
            text += " · 합칠 문서를 여는 중…"
        if self.job_id is None:
            self.status.setText(text)
        self.setWindowTitle(f"페이지 편집 — {self.ops.primary.name}" + (" *" if self.ops.dirty else ""))

    def rotate_selected(self, delta: int) -> None:
        sel = self.grid.selected_rows()
        if sel and self.ops.rotate(sel, delta):
            self._refresh(select=sel)

    def delete_selected(self) -> None:
        sel = self.grid.selected_rows()
        if not sel:
            return
        if len(sel) >= len(self.ops):
            self._notify("모든 쪽을 지울 수는 없습니다.", error=True)
            return
        if self.ops.delete(sel):
            nxt = min(sel[0], len(self.ops) - 1)
            self._refresh(select=[nxt], current=nxt)

    def move_pages(self, rows: list, dest: int) -> None:
        new = self.ops.move(rows, dest)
        if new:
            self._refresh(select=new, current=new[0])

    def undo(self) -> None:
        if self.ops.undo() is not None:
            self._refresh(select=[])

    def redo(self) -> None:
        if self.ops.redo() is not None:
            self._refresh(select=[])

    # ---- 병합
    def ask_merge(self) -> list | None:
        """합칠 문서·차례 — 시험에서는 바꿔 끼운다."""
        docs = [(d.doc_id, d.path) for d in self.host.open_documents()
                if d.doc_id != self.ops.primary.doc_id]
        dlg = MergeDialog(f"편집 중인 문서 ({len(self.ops)}쪽)", docs, self)
        return dlg.entries() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def merge(self) -> None:
        entries = self.ask_merge()
        if not entries or len(entries) < 2:
            return
        self._merge_entries = []
        self._merge_failed = []
        for e in entries:
            if e[0] == "file":
                doc = self.host.document_for_path(e[1])
                if doc is None:
                    doc_id = self.service.open(e[1])
                    self._pending[doc_id] = e[1]
                    self._merge_entries.append(("doc", doc_id))
                    continue
                e = ("doc", doc.doc_id)
            self._merge_entries.append(e)
        self._update_actions()
        self._maybe_finish_merge()

    def _maybe_finish_merge(self) -> None:
        if self._pending or self._merge_entries is None:
            self._update_actions()
            return
        order = []
        for e in self._merge_entries:
            if e[0] == "current":
                order.append(None)
                continue
            doc = self.service.document(e[1])
            if doc is None:
                continue
            order.append(self.ops.add_source(po.Source(doc.path, list(doc.page_sizes), doc.password, doc.sha256,
                                                       doc.doc_id)))
            if doc.doc_id not in self.outlines:
                out = self.host.sidebar.outline(doc.doc_id)
                if out is not None:
                    self.outlines[doc.doc_id] = out
                else:
                    self.service.request_outline(doc.doc_id)
        self._merge_entries = None
        if self._merge_failed:
            self._notify("열 수 없어 빼고 합친 파일: " + ", ".join(self._merge_failed), error=True)
        if self.ops.merge(order):
            self._refresh(select=[])
        else:
            self._update_actions()

    # ---- 분할
    def _outlines_by_source(self) -> dict[int, list]:
        return {i: self.outlines.get(s.doc_id) or [] for i, s in enumerate(self.ops.sources)}

    def has_bookmarks(self) -> bool:
        try:
            po.split_by_bookmarks(self.ops, self._outlines_by_source())
            return True
        except ValueError:
            return False

    def ask_split(self) -> tuple | None:
        dlg = SplitDialog(len(self.ops), self.has_bookmarks(), self)
        return dlg.value() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def ask_folder(self, default: str) -> str | None:
        d = QFileDialog.getExistingDirectory(self, "나눈 파일을 둘 폴더", default)
        return d or None

    def split(self) -> None:
        spec = self.ask_split()
        if spec is None:
            return
        mode, value = spec
        n = len(self.ops)
        try:
            if mode == "every":
                groups = po.split_every(n, int(value))
            elif mode == "bookmarks":
                groups = po.split_by_bookmarks(self.ops, self._outlines_by_source())
            else:
                groups = po.parse_ranges(str(value), n)
        except ValueError as e:
            self._notify(str(e), error=True)
            return
        if len(groups) < 2 and mode != "ranges":
            self._notify("나누면 파일이 하나뿐입니다. 기준을 바꿔 주세요.", error=True)
            return
        folder = self.ask_folder(self.host.default_output_dir(self.ops.primary.path))
        if not folder:
            return
        paths = []
        for name in po.split_names(self.ops.primary.path, len(groups)):
            paths.append(str(outputs.unique_path(folder, name)))
        self._run([(p, g) for p, g in zip(paths, groups)], "split")

    # ---- 저장·추출
    def ask_save_path(self, title: str, default: str) -> str | None:
        path, _ = QFileDialog.getSaveFileName(self, title, default, "PDF 파일 (*.pdf)")
        return path or None

    def save(self) -> None:
        default = self.host.default_output_path(self.ops.primary.path, po.SUFFIX_EDIT)
        path = self.ask_save_path("새 파일로 저장", str(default))
        if path:
            self._run([(path, list(range(len(self.ops))))], "save")

    def extract_selected(self) -> None:
        sel = self.grid.selected_rows()
        if not sel:
            return
        default = self.host.default_output_path(self.ops.primary.path, po.SUFFIX_EXTRACT)
        path = self.ask_save_path(f"고른 {len(sel)}쪽 추출", str(default))
        if path:
            self._run([(path, sel)], "extract")

    def _annotation_lists(self) -> list:
        """원본마다 사이드카 주석. 창이 열린 동안(창 모달) 주석은 바뀌지 않으므로 sha256 별로 한 번만 읽는다."""
        out = []
        for s in self.ops.sources:
            if s.sha256 and s.sha256 not in self._annots:
                self._annots[s.sha256] = self.host.annotation_items(s.sha256)
            out.append(self._annots.get(s.sha256) if s.sha256 else None)
        return out

    def _run(self, outs: list[tuple[str, list[int]]], what: str) -> None:
        if self.job_id is not None:
            return
        fixed = []
        for p, idx in outs:
            if not p.lower().endswith(".pdf"):
                p += ".pdf"
            fixed.append((os.path.abspath(p), idx))
        protected = self.host.protected_paths() + [s.path for s in self.ops.sources]
        if any(outputs.is_protected(p, protected) for p, _ in fixed):
            self._notify(JOB_MESSAGES[ds.EXPORT_REFUSED], error=True)
            return
        self.host.remember_output_dir(os.path.dirname(fixed[0][0]))
        annots = self._annotation_lists() if self.annot_check.isChecked() else None
        job = self.ops.plan(fixed, annots)
        est = diskspace.estimate_build(job, [src.page_count for src in self.ops.sources])
        if not self.host.space_ok([p for p, _ in fixed], est, self):
            return
        self.job_id = self.service.build_pages(job)
        self._job_done = (what, [p for p, _ in fixed], tuple(self.ops.pages))
        self.status.setText({"save": "새 파일을 만드는 중…", "extract": "추출하는 중…",
                             "split": f"{len(fixed)}개 파일로 나누는 중…"}[what])
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_btn.show()
        self.host.watch_job(self.job_id, self)
        self._update_actions()

    def cancel_job(self) -> None:
        if self.job_id is not None:
            self.service.cancel_job(self.job_id)

    def on_job_progress(self, ev) -> None:
        if ev.job_id == self.job_id and ev.total:
            self.progress.setRange(0, ev.total)
            self.progress.setValue(ev.done)

    def on_job_done(self, ev) -> None:
        if ev.job_id != self.job_id:
            return
        what, paths, pages = self._job_done
        self.job_id = None
        self._job_done = None
        self.progress.hide()
        self.cancel_btn.hide()
        r = ev.result
        self.last_result = (what, r)
        if r.get("status") == ds.EXPORT_OK:
            outs = r.get("outputs", [])
            if what == "save" and tuple(self.ops.pages) == pages:
                self.ops.mark_saved()
            names = ", ".join(os.path.basename(p) for p in paths[:3]) + (" …" if len(paths) > 3 else "")
            msg = {"save": "새 파일로 저장했습니다", "extract": "추출했습니다",
                   "split": f"{len(paths)}개 파일로 나눴습니다"}[what] + f": {names}"
            msg += f"\n{sum(o['pages'] for o in outs)}쪽"
            kept_toc = sum(o.get("toc", 0) for o in outs)
            if kept_toc:
                msg += f", 책갈피 {kept_toc}개"
            if sum(o.get("annots", 0) for o in outs):
                msg += f", 넣은 메모 {sum(o['annots'] for o in outs)}개"
            self._notify(msg)
            if self.open_check.isChecked() and what != "split":
                self.host.open_paths(paths)
        else:
            self._notify(JOB_MESSAGES.get(r.get("status"), JOB_MESSAGES[ds.EXPORT_ERROR]), error=True)
        self._update_actions()

    # ------------------------------------------------------------------ 알림·닫기
    def _notify(self, text: str, error: bool = False) -> None:
        self.last_notice = text
        self.status.setText(text.split("\n")[0])
        box = QMessageBox(QMessageBox.Icon.Warning if error else QMessageBox.Icon.Information, "페이지 편집", text,
                          parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def confirm_discard(self) -> bool:
        """저장하지 않은 편집을 버릴지 — 시험에서는 바꿔 끼운다."""
        r = QMessageBox.question(self, "페이지 편집", "저장하지 않은 편집이 있습니다. 버리고 닫을까요?\n"
                                 "(원본 파일은 처음부터 바뀌지 않았습니다)",
                                 QMessageBox.StandardButton.Discard | QMessageBox.StandardButton.Cancel,
                                 QMessageBox.StandardButton.Cancel)
        return r == QMessageBox.StandardButton.Discard

    def can_close(self) -> bool:
        if self.job_id is not None:
            self._notify("파일을 만드는 중입니다. 끝나거나 취소한 뒤에 닫을 수 있습니다.", error=True)
            return False
        return not self.ops.dirty or self.confirm_discard()

    def closeEvent(self, ev) -> None:  # noqa: N802
        if not self._closing and not self.can_close():
            ev.ignore()
            return
        self._finish()
        super().closeEvent(ev)

    def reject(self) -> None:
        if self._closing or self.can_close():
            self._finish()
            super().reject()

    def force_close(self) -> None:
        """주 창이 이미 확인했다 — 묻지 않고 닫는다."""
        self._closing = True
        self.close()

    def _finish(self) -> None:
        if getattr(self, "_finished", False):
            return
        self._finished = True
        self._closing = True
        for doc_id in list(self._last_req):
            self.service.request_pages(doc_id, self._last_req[doc_id][0], [], self._acks.get(doc_id, 0),
                                       ds.EDIT_LANE)
        for doc_id in self.aux_docs | set(self._pending):
            self.service.close(doc_id)
        self.aux_docs.clear()
        self._pending.clear()
        self.closed.emit()

