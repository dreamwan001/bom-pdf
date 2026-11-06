"""오른쪽 검색 패널: 검색어·옵션·결과 목록(쪽·앞뒤 문맥)·이전/다음.

- 검색은 작업자 프로세스에서 한다. 결과는 조각마다 도착하며 목록 끝에 붙인다(beginInsertRows).
- 새 검색은 앞 검색을 대체한다. 앞 검색의 늦게 온 결과는 search_id 로 걸러 버린다.
- 강조용 {쪽: [SearchHit]} 를 들고 있고, 주 창이 그 문서의 보기들에 참조로 넘긴다.
- 검색어·문맥은 기록(log)하지 않는다.
"""
from __future__ import annotations

from PySide6.QtCore import QAbstractListModel, QModelIndex, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPalette
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QLineEdit, QListView, QPushButton, QStyle, QStyledItemDelegate,
    QVBoxLayout, QWidget,
)

from bom_pdf.services import document as ds

MATCH_BG = QColor(255, 214, 0, 140)


class HitModel(QAbstractListModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.hits: list[ds.SearchHit] = []

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.hits)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        h = self.hits[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{h.page + 1}쪽  {h.before}{h.match}{h.after}"
        if role == Qt.ItemDataRole.UserRole:
            return h
        return None

    def clear(self) -> None:
        self.beginResetModel()
        self.hits = []
        self.endResetModel()

    def extend(self, hits: list) -> None:
        if not hits:
            return
        n = len(self.hits)
        self.beginInsertRows(QModelIndex(), n, n + len(hits) - 1)
        self.hits.extend(hits)
        self.endInsertRows()


class HitDelegate(QStyledItemDelegate):
    """'12쪽  …앞 [일치] 뒤…' — 일치 부분은 굵게·노란 바탕. 넘치면 앞 문맥부터 줄인다."""

    def paint(self, qp, option, index) -> None:
        h = index.data(Qt.ItemDataRole.UserRole)
        if h is None:
            return super().paint(qp, option, index)
        qp.save()
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if selected:
            qp.fillRect(option.rect, option.palette.highlight())
        r = option.rect.adjusted(6, 0, -6, 0)
        fm = QFontMetrics(option.font)
        bold = QFont(option.font)
        bold.setBold(True)
        bfm = QFontMetrics(bold)
        text_color = option.palette.color(QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text)
        dim = QColor(text_color)
        dim.setAlpha(150)
        label = f"{h.page + 1}쪽"
        label_w = max(fm.horizontalAdvance("0000쪽"), fm.horizontalAdvance(label)) + 8
        qp.setPen(dim)
        qp.drawText(QRect(r.x(), r.y(), label_w, r.height()), Qt.AlignmentFlag.AlignVCenter, label)
        x = r.x() + label_w
        avail = r.right() - x
        match = bfm.elidedText(h.match, Qt.TextElideMode.ElideRight, max(20, avail * 2 // 3))
        mw = bfm.horizontalAdvance(match)
        rest = max(0, avail - mw)
        before = fm.elidedText(h.before, Qt.TextElideMode.ElideLeft, rest // 2) if h.before else ""
        bw = fm.horizontalAdvance(before)
        after = fm.elidedText(h.after, Qt.TextElideMode.ElideRight, rest - bw) if h.after else ""
        qp.setPen(text_color)
        qp.drawText(QRect(x, r.y(), bw + 2, r.height()), Qt.AlignmentFlag.AlignVCenter, before)
        x += bw
        qp.fillRect(QRect(x, r.y() + 3, mw, r.height() - 6), MATCH_BG)
        qp.setFont(bold)
        qp.drawText(QRect(x, r.y(), mw + 2, r.height()), Qt.AlignmentFlag.AlignVCenter, match)
        x += mw
        qp.setFont(option.font)
        qp.drawText(QRect(x, r.y(), r.right() - x, r.height()), Qt.AlignmentFlag.AlignVCenter, after)
        qp.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(200, QFontMetrics(option.font).height() + 8)


class SearchPanel(QWidget):
    """검색은 서비스를 직접 부른다. 주 창은 대상 문서 제공(doc_provider)·결과 이동·강조만 맡는다."""

    hitActivated = Signal(object)  # SearchHit — 이 결과로 이동
    hitsChanged = Signal(int)  # doc_id — 강조를 다시 그려야 함

    def __init__(self, service: ds.DocumentService, parent=None):
        super().__init__(parent)
        self.service = service
        self.doc_id: int | None = None
        self.search_id: int | None = None
        self.running = False
        self.hits_by_page: dict[int, list] = {}
        self.current_row = -1
        self._key = None  # (doc_id, 검색어, 옵션) — 같으면 Enter 는 다음 결과
        self.doc_provider = lambda: None  # 주 창이 바꿔 끼운다: 활성 보기의 Document

        self.query = QLineEdit()
        self.query.setPlaceholderText("문서 안 검색 (Ctrl+F)")
        self.query.setClearButtonEnabled(True)
        self.query.returnPressed.connect(self.search_or_next)
        self.ignore_space = QCheckBox("공백·줄바꿈 무시")
        self.ignore_space.setToolTip("스캔·OCR 문서에서 단어 사이에 낀 줄바꿈·띄어쓰기를 무시하고 찾습니다")
        self.btn_find = QPushButton("찾기")
        self.btn_find.clicked.connect(self.start_search)
        self.btn_prev = QPushButton("◀ 이전")
        self.btn_prev.setToolTip("이전 결과 (Shift+F3)")
        self.btn_prev.clicked.connect(self.prev_hit)
        self.btn_next = QPushButton("다음 ▶")
        self.btn_next.setToolTip("다음 결과 (F3)")
        self.btn_next.clicked.connect(self.next_hit)
        self.btn_stop = QPushButton("중지")
        self.btn_stop.clicked.connect(self.cancel)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.model = HitModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setItemDelegate(HitDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setEditTriggers(QListView.EditTrigger.NoEditTriggers)
        self.list.clicked.connect(lambda ix: self.go_to_row(ix.row()))
        self.list.activated.connect(lambda ix: self.go_to_row(ix.row()))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.btn_find)
        lay.addLayout(row)
        lay.addWidget(self.ignore_space)
        row2 = QHBoxLayout()
        row2.addWidget(self.btn_prev)
        row2.addWidget(self.btn_next)
        row2.addStretch(1)
        row2.addWidget(self.btn_stop)
        lay.addLayout(row2)
        lay.addWidget(self.status)
        lay.addWidget(self.list, 1)
        self._update_buttons()

    # ---- 검색
    def focus_query(self) -> None:
        self.query.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.query.selectAll()

    def search_or_next(self) -> None:
        doc = self.doc_provider()
        key = (doc.doc_id if doc else None, self.query.text(), self.ignore_space.isChecked())
        if key == self._key and self.model.hits:
            self.next_hit()
        else:
            self.start_search()

    def start_search(self) -> None:
        doc = self.doc_provider()
        text = self.query.text()
        if doc is None or not text.strip():
            return
        if self.running and self.search_id is not None:
            self.service.cancel_search(self.search_id)
        self._reset()
        self.doc_id = doc.doc_id
        self._key = (doc.doc_id, text, self.ignore_space.isChecked())
        self.running = True
        self.search_id = self.service.search(doc.doc_id, text, self.ignore_space.isChecked())
        self.status.setText(f"검색 중… 0 / {doc.page_count}쪽")
        self._update_buttons()
        self.hitsChanged.emit(doc.doc_id)

    def cancel(self) -> None:
        if self.running and self.search_id is not None:
            self.service.cancel_search(self.search_id)

    def clear_for_doc(self, doc_id: int) -> None:
        """그 문서가 닫혔다 — 결과를 비운다."""
        if self.doc_id == doc_id:
            self.cancel()
            self._reset()
            self.status.setText("")
            self._key = None
            self.doc_id = None
            self._update_buttons()

    def _reset(self) -> None:
        old = self.doc_id
        self.model.clear()
        self.hits_by_page = {}
        self.current_row = -1
        self.running = False
        self.search_id = None
        if old is not None:
            self.hitsChanged.emit(old)

    # ---- 결과
    def on_results(self, ev: ds.SearchResults) -> None:
        if ev.search_id != self.search_id:
            return
        for h in ev.hits:
            self.hits_by_page.setdefault(h.page, []).append(h)
        self.model.extend(ev.hits)
        if self.running:
            self.status.setText(f"검색 중… {ev.pages_done} / {ev.page_count}쪽 · {len(self.model.hits)}건")
        if ev.hits:
            self._update_buttons()
            self.hitsChanged.emit(ev.doc_id)

    def on_done(self, ev: ds.SearchDone) -> None:
        if ev.search_id != self.search_id:
            return
        self.running = False
        n = len(self.model.hits)
        if ev.status == ds.SEARCH_DONE:
            self.status.setText(f"{n}건" if n else "찾는 말이 없습니다")
        elif ev.status == ds.SEARCH_TRUNCATED:
            self.status.setText(f"결과가 너무 많아 처음 {n}건만 보여 줍니다")
        elif ev.status == ds.SEARCH_CANCELLED:
            self.status.setText(f"중지했습니다 — 그때까지 {n}건")
        else:
            self.status.setText(f"검색이 중단되었습니다(PDF 엔진 재시작) — 그때까지 {n}건. 다시 찾으세요")
            self._key = None
        self._update_buttons()

    def _update_buttons(self) -> None:
        has = bool(self.model.hits)
        self.btn_prev.setEnabled(has)
        self.btn_next.setEnabled(has)
        self.btn_stop.setEnabled(self.running)

    # ---- 이동
    def current_hit(self):
        if 0 <= self.current_row < len(self.model.hits):
            return self.model.hits[self.current_row]
        return None

    def go_to_row(self, row: int) -> None:
        if not 0 <= row < len(self.model.hits):
            return
        self.current_row = row
        ix = self.model.index(row)
        self.list.setCurrentIndex(ix)
        self.list.scrollTo(ix)
        self.hitActivated.emit(self.model.hits[row])

    def next_hit(self) -> None:
        if self.model.hits:
            self.go_to_row((self.current_row + 1) % len(self.model.hits))

    def prev_hit(self) -> None:
        if self.model.hits:
            self.go_to_row((self.current_row - 1) % len(self.model.hits) if self.current_row >= 0
                           else len(self.model.hits) - 1)

    def keyPressEvent(self, ev) -> None:  # noqa: N802
        if ev.key() == Qt.Key.Key_Escape and self.running:
            self.cancel()
            return
        super().keyPressEvent(ev)
