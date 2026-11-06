"""사진 → PDF 대화상자 (1-5, system-design §4-5).

흐름: 사진·폴더 추가(버튼·끌어놓기) → 작업 프로세스가 정보·미리보기를 읽음 → 자동 정렬(카톡 번호 → 촬영시각 → 파일 이름)
→ 순서 바꾸기(끌기·위로/아래로)·돌리기·사진마다 문서 사진 모드(체크) → 쪽 설정·캡션·화질, 예상 용량 → "PDF 만들기…"
→ 작업 프로세스가 새 PDF 를 만든다(진행·취소).

이 창은 Pillow·OpenCV·PyMuPDF 를 import하지 않는다(미리보기는 작업 프로세스가 만든 작은 JPEG 를 Qt 로 그린다).
산출물 기본 폴더는 사진 폴더가 아니다(D4). 원본 사진·열린 PDF 경로로는 저장하지 않는다.
"""
from __future__ import annotations

import os

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QImage, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton,
    QRadioButton, QVBoxLayout, QWidget,
)

from bom_pdf.services import document as ds
from bom_pdf.services import imagetopdf as itp
from bom_pdf.services import outputs
from bom_pdf.ui.compress_dialog import human_size

THUMB = 72
FILTER = "사진 (*.jpg *.jpeg *.png *.webp *.heic *.heif)"
JOB_MESSAGES = {
    ds.EXPORT_REFUSED: "원본 사진이나 열린 PDF 에는 덮어쓸 수 없습니다. 다른 이름이나 폴더를 고르세요.",
    ds.EXPORT_INTERRUPTED: "만드는 중 작업이 멈췄습니다. 원본 사진은 바뀌지 않았습니다. 다시 시도해 주세요.",
    ds.JOB_CANCELLED: "취소했습니다. 만들던 파일은 지웠습니다.",
    ds.EXPORT_ERROR: "PDF를 만들지 못했습니다. 사진 파일과 저장 위치의 권한·공간을 확인해 주세요.",
}


class ImagePdfDialog(QDialog):
    closed = Signal()

    def __init__(self, host, service: ds.DocumentService, parent=None):
        super().__init__(parent)
        self.host = host
        self.service = service
        self.setWindowTitle("사진 → PDF")
        self.setAcceptDrops(True)
        self.resize(820, 640)
        self.photos: list[itp.Photo] = []
        self.bpp: dict = {}
        self.manual_order = False
        self.scan_id: int | None = None
        self._scan_queue: list[str] = []
        self.job_id: int | None = None
        self._job_out: str | None = None
        self.last_result: dict | None = None
        self.last_notice: str | None = None
        self.last_skipped: list[str] = []
        self._syncing = False
        self._finished = False
        self._build()
        self._refresh()

    # ------------------------------------------------------------------ 화면
    def _build(self) -> None:
        lay = QVBoxLayout(self)
        tip = QLabel("사진이나 폴더를 이 창에 끌어 놓거나 [사진 추가]로 고르세요. 원본 사진은 바뀌지 않고, "
                     "위치정보(GPS) 등 사진 속 정보는 PDF 에 넣지 않습니다. 체크한 사진은 문서 사진 모드"
                     "(종이 테두리를 찾아 반듯하게 — 못 찾으면 원본 그대로)로 넣습니다.")
        tip.setWordWrap(True)
        tip.setStyleSheet("color:#444;")
        lay.addWidget(tip)

        bar = QHBoxLayout()
        self.btn_add = QPushButton("사진 추가…")
        self.btn_add.clicked.connect(self.add_files_dialog)
        self.btn_folder = QPushButton("폴더 추가…")
        self.btn_folder.clicked.connect(self.add_folder_dialog)
        self.btn_remove = QPushButton("빼기")
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_up = QPushButton("위로")
        self.btn_up.clicked.connect(lambda: self.move_selected(-1))
        self.btn_down = QPushButton("아래로")
        self.btn_down.clicked.connect(lambda: self.move_selected(1))
        self.btn_left = QPushButton("⟲ 왼쪽")
        self.btn_left.clicked.connect(lambda: self.rotate_selected(-90))
        self.btn_right = QPushButton("⟳ 오른쪽")
        self.btn_right.clicked.connect(lambda: self.rotate_selected(90))
        self.btn_sort = QPushButton("자동 정렬")
        self.btn_sort.setToolTip("카톡 파일 번호 → 촬영시각(EXIF) → 파일 이름 순")
        self.btn_sort.clicked.connect(self.auto_sort)
        for b in (self.btn_add, self.btn_folder, self.btn_remove, self.btn_up, self.btn_down, self.btn_left,
                  self.btn_right, self.btn_sort):
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)

        self.list = QListWidget()
        self.list.setIconSize(QSize(THUMB, THUMB))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.itemSelectionChanged.connect(self._update_actions)
        lay.addWidget(self.list, 1)

        opts = QWidget()
        ol = QHBoxLayout(opts)
        ol.setContentsMargins(0, 0, 0, 0)
        g1 = QGroupBox("쪽")
        f1 = QFormLayout(g1)
        self.page_combo = QComboBox()
        self.page_combo.addItem("A4 맞춤", itp.PAGE_A4)
        self.page_combo.addItem("원본 비율 (쪽당 1장)", itp.PAGE_ORIGINAL)
        self.per_combo = QComboBox()
        self.per_combo.addItem("쪽당 1장", 1)
        self.per_combo.addItem("쪽당 2장 (위·아래)", 2)
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 40)
        self.margin.setDecimals(0)
        self.margin.setSuffix(" mm")
        self.margin.setValue(10)
        f1.addRow("크기", self.page_combo)
        f1.addRow("배치", self.per_combo)
        f1.addRow("여백", self.margin)
        ol.addWidget(g1)

        g2 = QGroupBox("캡션 (사진 아래)")
        v2 = QVBoxLayout(g2)
        self.cap_number = QCheckBox("번호 (사진 1, 사진 2 …)")
        self.cap_name = QCheckBox("파일 이름")
        self.cap_date = QCheckBox("촬영일시 (사진 정보에 있을 때)")
        for c in (self.cap_number, self.cap_name, self.cap_date):
            v2.addWidget(c)
        ol.addWidget(g2)

        g3 = QGroupBox("화질")
        v3 = QVBoxLayout(g3)
        self.q_group = QButtonGroup(self)
        self.q_submit = QRadioButton("제출용 (권장) — 150DPI·JPEG 75")
        self.q_quality = QRadioButton("화질 우선 — 300DPI·JPEG 90")
        self.q_submit.setChecked(True)
        for b in (self.q_submit, self.q_quality):
            self.q_group.addButton(b)
            v3.addWidget(b)
        self.estimate = QLabel()
        self.estimate.setStyleSheet("font-weight:bold;")
        v3.addWidget(self.estimate)
        ol.addWidget(g3)
        lay.addWidget(opts)

        for w in (self.page_combo, self.per_combo):
            w.currentIndexChanged.connect(self._refresh)
        self.margin.valueChanged.connect(self._refresh)
        for c in (self.cap_number, self.cap_name, self.cap_date, self.q_submit, self.q_quality):
            c.toggled.connect(self._refresh)

        bottom = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        bottom.addWidget(self.status, 1)
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(200)
        self.progress.hide()
        bottom.addWidget(self.progress)
        self.cancel_btn = QPushButton("취소")
        self.cancel_btn.clicked.connect(self.cancel_job)
        self.cancel_btn.hide()
        bottom.addWidget(self.cancel_btn)
        self.open_check = QCheckBox("만든 PDF 열기")
        self.open_check.setChecked(True)
        bottom.addWidget(self.open_check)
        self.make_btn = QPushButton("PDF 만들기…")
        self.make_btn.setDefault(True)
        self.make_btn.clicked.connect(self.make_pdf)
        bottom.addWidget(self.make_btn)
        close = QPushButton("닫기")
        close.clicked.connect(self.close)
        bottom.addWidget(close)
        lay.addLayout(bottom)

    # ------------------------------------------------------------------ 설정
    def options(self) -> itp.Options:
        cap = tuple(k for k, c in ((itp.CAPTION_NUMBER, self.cap_number), (itp.CAPTION_NAME, self.cap_name),
                                   (itp.CAPTION_DATE, self.cap_date)) if c.isChecked())
        return itp.Options(page=self.page_combo.currentData(), per_page=self.per_combo.currentData(),
                           margin_mm=self.margin.value(), caption=cap,
                           preset="quality" if self.q_quality.isChecked() else "submit")

    def _refresh(self, *_a) -> None:
        self.per_combo.setEnabled(self.page_combo.currentData() != itp.PAGE_ORIGINAL)
        opts = self.options()
        n = len(self.photos)
        self.last_estimate = itp.estimate_size(self.photos, opts, self.bpp) if n else 0
        if n:
            self.estimate.setText(f"사진 {n}장 → {itp.page_count(n, opts)}쪽, "
                                  f"예상 약 {human_size(self.last_estimate)}")
        else:
            self.estimate.setText("사진 없음")
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self.job_id is not None
        has_sel = bool(self.list.selectedItems())
        for b in (self.btn_remove, self.btn_up, self.btn_down, self.btn_left, self.btn_right):
            b.setEnabled(has_sel and not busy)
        for b in (self.btn_add, self.btn_folder):
            b.setEnabled(not busy)
        self.btn_sort.setEnabled(len(self.photos) > 1 and not busy)
        self.make_btn.setEnabled(bool(self.photos) and not busy and self.scan_id is None)
        self.list.setEnabled(not busy)

    # ------------------------------------------------------------------ 목록
    def _rebuild_list(self, select: list[int] | None = None) -> None:
        self._syncing = True
        try:
            self.list.clear()
            for i, p in enumerate(self.photos):
                it = QListWidgetItem(self._icon(p), self._label(i, p))
                it.setData(Qt.ItemDataRole.UserRole, i)
                it.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDragEnabled
                            | Qt.ItemFlag.ItemIsUserCheckable)
                it.setCheckState(Qt.CheckState.Checked if p.doc_mode else Qt.CheckState.Unchecked)
                it.setToolTip(p.path)
                self.list.addItem(it)
            for r in select or []:
                if 0 <= r < self.list.count():
                    self.list.item(r).setSelected(True)
        finally:
            self._syncing = False
        self._refresh()

    @staticmethod
    def _label(i: int, p: itp.Photo) -> str:
        extra = [p.taken[:16] if p.taken else "촬영시각 없음", f"{p.shown_size[0]}×{p.shown_size[1]}"]
        if p.rotate:
            extra.append(f"↻{p.rotate}°")
        if p.doc_mode:
            extra.append("문서 사진")
        return f"{i + 1}. {p.name}\n    " + " · ".join(extra)

    @staticmethod
    def _icon(p: itp.Photo) -> QIcon:
        img = QImage.fromData(p.thumb) if p.thumb else QImage()
        if img.isNull():
            pm = QPixmap(THUMB, THUMB)
            pm.fill(Qt.GlobalColor.lightGray)
            return QIcon(pm)
        if p.rotate:
            img = img.transformed(QTransform().rotate(p.rotate))
        return QIcon(QPixmap.fromImage(img))

    def selected_rows(self) -> list[int]:
        return sorted(self.list.row(it) for it in self.list.selectedItems())

    def select_rows(self, rows: list[int]) -> None:
        self.list.clearSelection()
        for r in rows:
            self.list.item(r).setSelected(True)

    def _on_rows_moved(self, *_a) -> None:
        """끌어서 순서를 바꿨다 — 목록 순서대로 사진 목록을 다시 만든다."""
        if self._syncing:
            return
        order = [self.list.item(r).data(Qt.ItemDataRole.UserRole) for r in range(self.list.count())]
        if sorted(order) != list(range(len(self.photos))):
            return
        sel = self.selected_rows()
        self.photos = [self.photos[i] for i in order]
        self.manual_order = True
        self._rebuild_list(sel)

    def _on_item_changed(self, it: QListWidgetItem) -> None:
        if self._syncing:
            return
        r = self.list.row(it)
        if 0 <= r < len(self.photos):
            on = it.checkState() == Qt.CheckState.Checked
            if self.photos[r].doc_mode != on:
                self.photos[r].doc_mode = on
                sel = self.selected_rows()
                self._rebuild_list(sel)

    def set_doc_mode(self, rows: list[int], on: bool) -> None:
        for r in rows:
            self.photos[r].doc_mode = on
        self._rebuild_list(rows)

    def move_selected(self, step: int) -> None:
        sel = self.selected_rows()
        if not sel:
            return
        n = len(self.photos)
        if (step < 0 and sel[0] == 0) or (step > 0 and sel[-1] == n - 1):
            return
        order = list(range(n))
        for r in (sel if step < 0 else reversed(sel)):
            order[r], order[r + step] = order[r + step], order[r]
        self.photos = [self.photos[i] for i in order]
        self.manual_order = True
        self._rebuild_list([r + step for r in sel])

    def rotate_selected(self, deg: int) -> None:
        sel = self.selected_rows()
        for r in sel:
            self.photos[r].rotate = (self.photos[r].rotate + deg) % 360
        self._rebuild_list(sel)

    def remove_selected(self) -> None:
        sel = set(self.selected_rows())
        self.photos = [p for i, p in enumerate(self.photos) if i not in sel]
        self._rebuild_list()

    def auto_sort(self) -> None:
        self.photos = itp.sort_photos(self.photos)
        self.manual_order = False
        self._rebuild_list()

    # ------------------------------------------------------------------ 추가(작업 프로세스가 정보를 읽는다)
    def ask_image_files(self) -> list[str]:
        """사진 고르기 — 시험에서는 바꿔 끼운다."""
        files, _ = QFileDialog.getOpenFileNames(self, "사진 추가", "", FILTER)
        return files

    def add_files_dialog(self) -> None:
        files = self.ask_image_files()
        if files:
            self.add_paths(files)

    def add_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "사진 폴더 추가")
        if folder:
            self.add_paths([folder])

    def add_paths(self, paths: list[str]) -> int:
        """파일·폴더 → 아직 없는 사진만 정보 읽기를 시작. 새로 읽을 장 수를 돌려준다."""
        have = {os.path.normcase(p.path) for p in self.photos} | {os.path.normcase(p) for p in self._scan_queue}
        new = [p for p in itp.collect_inputs(paths) if os.path.normcase(p) not in have]
        if not new:
            if paths:
                self.status.setText("더할 새 사진이 없습니다 (jpg·png·webp·heic)")
            return 0
        self._scan_queue.extend(new)
        if self.scan_id is None:
            self._start_scan()
        return len(new)

    def _start_scan(self) -> None:
        paths, self._scan_queue = self._scan_queue, []
        self._scanning = paths
        self.scan_id = self.service.scan_images(paths)
        self.host.watch_job(self.scan_id, self)
        self.status.setText(f"사진 {len(paths)}장의 정보를 읽는 중…")
        self.progress.setRange(0, len(paths))
        self.progress.setValue(0)
        self.progress.show()
        self._update_actions()

    def _on_scan_done(self, r: dict) -> None:
        self.scan_id = None
        self.progress.hide()
        new = []
        for d in r.get("items", []) if r.get("status") == ds.EXPORT_OK else []:
            new.append(itp.Photo(path=d["path"], width=d["width"], height=d["height"], taken=d.get("taken"),
                                 thumb=d.get("thumb")))
        if r.get("bpp"):
            self.bpp = _merge_bpp(self.bpp, r["bpp"], len(self.photos), len(new))
        if self.manual_order:
            self.photos += itp.sort_photos(new)
        else:
            self.photos = itp.sort_photos(self.photos + new)
        failed = r.get("failed", []) if r.get("status") == ds.EXPORT_OK else list(self._scanning)
        self.last_skipped = list(failed)
        self._rebuild_list()
        if failed:
            self._notify(skipped_message(failed, r.get("skipped", [])), error=True)
        else:
            self.status.setText(f"사진 {len(self.photos)}장")
        if self._scan_queue:
            self._start_scan()

    # ------------------------------------------------------------------ 만들기
    def default_output_path(self) -> str:
        folder = outputs.default_export_dir_for([p.path for p in self.photos],
                                                outputs.last_export_dir(self.service.data_dir),
                                                self.host.documents_dir())
        return str(outputs.unique_path(folder, itp.default_name(self.photos)))

    def make_pdf(self) -> int | None:
        if not self.photos or self.job_id is not None or self.scan_id is not None:
            return None
        path = self.host.ask_output_path("사진 → PDF 저장", self.default_output_path())
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        path = os.path.abspath(path)
        protected = self.host.protected_paths() + [p.path for p in self.photos]
        if outputs.is_protected(path, protected):
            self._notify(JOB_MESSAGES[ds.EXPORT_REFUSED], error=True)
            return None
        opts = self.options()
        if not self.host.space_ok([path], itp.estimate_size(self.photos, opts, self.bpp), self):
            return None
        self.host.remember_output_dir(os.path.dirname(path))
        font = itp.find_font() if opts.caption else None
        job = itp.plan(self.photos, opts, path, self.host.protected_paths(), font)
        self.job_id = self.service.images_to_pdf(job)
        self._job_out = path
        self.host.watch_job(self.job_id, self)
        self.status.setText("PDF를 만드는 중…")
        self.progress.setRange(0, len(self.photos))
        self.progress.setValue(0)
        self.progress.show()
        self.cancel_btn.show()
        self._update_actions()
        return self.job_id

    def cancel_job(self) -> None:
        if self.job_id is not None:
            self.service.cancel_job(self.job_id)

    def on_job_progress(self, ev) -> None:
        if ev.job_id in (self.job_id, self.scan_id) and ev.total:
            self.progress.setRange(0, ev.total)
            self.progress.setValue(ev.done)

    def on_job_done(self, ev) -> None:
        if ev.job_id == self.scan_id:
            self._on_scan_done(ev.result)
            return
        if ev.job_id != self.job_id:
            return
        path = self._job_out
        self.job_id = None
        self._job_out = None
        self.progress.hide()
        self.cancel_btn.hide()
        r = ev.result
        self.last_result = r
        if r.get("status") == ds.EXPORT_OK:
            msg = (f"PDF를 만들었습니다: {os.path.basename(path)}\n"
                   f"사진 {r['images']}장, {r['pages']}쪽, {human_size(r['size'])} ({r['seconds']:.1f}초)")
            if r.get("doc_fixed") or r.get("doc_kept"):
                msg += f"\n문서 사진 보정 {r['doc_fixed']}장"
                if r.get("doc_kept"):
                    msg += f", 테두리를 못 찾아 원본 그대로 {r['doc_kept']}장"
            if r.get("too_large"):
                msg += f"\n너무 커서 뺀 사진 {r['too_large']}장"
            if r.get("failed", 0) - r.get("too_large", 0) > 0:
                msg += f"\n읽지 못해 뺀 사진 {r['failed'] - r.get('too_large', 0)}장"
            if r.get("caption_skipped"):
                msg += f"\n한국어 글꼴이 없어 캡션을 넣지 못한 사진 {r['caption_skipped']}장"
            self._notify(msg)
            if self.open_check.isChecked():
                self.host.open_paths([path])
        else:
            self._notify(JOB_MESSAGES.get(r.get("status"), JOB_MESSAGES[ds.EXPORT_ERROR]), error=True)
        self._update_actions()

    # ------------------------------------------------------------------ 끌어놓기·알림·닫기
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls() and self.job_id is None:
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls() and self.job_id is None:
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if files:
            ev.acceptProposedAction()
            self.add_paths(files)

    def _notify(self, text: str, error: bool = False) -> None:
        self.last_notice = text
        self.status.setText(text.split("\n")[0])
        box = QMessageBox(QMessageBox.Icon.Warning if error else QMessageBox.Icon.Information, "사진 → PDF", text,
                          parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def can_close(self) -> bool:
        if self.job_id is not None:
            self._notify("PDF를 만드는 중입니다. 끝나거나 취소한 뒤에 닫을 수 있습니다.", error=True)
            return False
        return True

    def closeEvent(self, ev) -> None:  # noqa: N802
        if not self._finished and not self.can_close():
            ev.ignore()
            return
        self._finish()
        super().closeEvent(ev)

    def reject(self) -> None:
        if self._finished or self.can_close():
            self._finish()
            super().reject()

    def force_close(self) -> None:
        """주 창이 끝나는 중 — 만들던 작업은 취소하고 닫는다."""
        self.cancel_job()
        self._finish()
        self.close()

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self.scan_id is not None:
            self.service.cancel_job(self.scan_id)
        self.closed.emit()


def skipped_message(failed: list[str], skipped: list[dict]) -> str:
    """뺀 사진 알림 — 너무 큰 사진(O5)과 읽지 못한 사진을 나눠 이유를 적는다."""
    big = [d for d in skipped if d.get("reason") == "too_large"]
    big_paths = {d["path"] for d in big}
    other = [p for p in failed if p not in big_paths]
    lines = []
    if big:
        names = ", ".join(f"{os.path.basename(d['path'])}(약 {d.get('mp', 0):,}백만 화소)" for d in big[:3])
        lines.append(f"너무 큰 사진 {len(big)}장은 뺐습니다: {names}" + (" …" if len(big) > 3 else ""))
        lines.append("  JPEG 가 아닌 사진(PNG·HEIC 등)은 1억 화소까지, JPEG 는 훨씬 큰 것도 넣을 수 있습니다. "
                     "그림 프로그램에서 크기를 줄여 저장한 뒤 다시 넣어 주세요.")
    if other:
        names = ", ".join(os.path.basename(p) for p in other[:5]) + (" …" if len(other) > 5 else "")
        lines.append(f"읽지 못한 사진 {len(other)}장은 뺐습니다: {names}")
    return "\n".join(lines)


def _merge_bpp(old: dict, new: dict, n_old: int, n_new: int) -> dict:
    """사진 수로 가중 평균."""
    out = dict(old)
    for k, v in new.items():
        out[k] = (old[k] * n_old + v * n_new) / (n_old + n_new) if k in old and n_old else v
    return out
