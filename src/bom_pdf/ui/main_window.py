"""주 창: 탭(문서 여러 개)·좌우 분할·사이드바(썸네일·책갈피)·검색 패널·메뉴·단축키·최근 문서·끌어놓기.

- 탭 하나 = 문서 하나. 탭 안을 좌우로 나누면 오른쪽 칸에 같은 문서나 다른 열린 문서를 띄운다.
  두 칸은 스크롤·확대가 따로이고 렌더 캐시·작업자는 함께 쓴다.
- "활성 보기" = 현재 탭에서 마지막으로 포커스를 받은 칸. 확대·쪽 이동·사이드바·검색 대상은 활성 보기를 따른다.
- 렌더 결과는 그 문서를 보여 주는 모든 보기에 보낸다(PageView 의 ack·캐시 공유 전제).
- 주석(1-3): sha256 을 안 뒤에야 사이드카 저장소를 붙이고 주석 도구를 켠다(그 전에는 도구가 꺼져 있다 —
  기존 주석도 sha256 이 있어야 읽을 수 있으므로 같은 시점에 함께 나타난다). 같은 sha256 의 사본은 저장소 하나를 쓴다.
  만들기·편집·삭제·되돌리기는 여기서 저장소에 하고, 저장소가 바뀌면 그 저장소를 보는 보기·목록을 다시 그린다.
- 메모 포함 PDF 내보내기: 기본 폴더는 원본 폴더가 아니고(D4), 열린 원본과 같은 경로는 거절한다. 작업자가 만든다.
- 1-4: 페이지 편집 창(ui.page_editor — 창 모달), 인쇄(ui.printing — 렌더 작업자 PRINT_LANE), 용량 압축본(작업 프로세스).
  산출물 이름·폴더는 1-3 과 같은 services.outputs 규칙. 작업 프로세스 결과(JobProgress·JobDone)는 watch_job 으로
  등록한 쪽(편집 창 또는 주 창)에 넘긴다.
- 1-5: 사진 → PDF 창(ui.image_pdf_dialog — 창 모달 아님, 하나만). 사진 파일을 주 창에 끌어 놓아도 그 창으로 간다.
- 1-6: 탭·쪽·확대가 바뀌면 `sessionChanged` 를 내보낸다(ui.session 이 모아서 저장). 복원은 `restore_session` —
  쪽·확대는 그 문서가 열린 뒤(`_on_opened`) 맞춘다. 다른 실행이 넘긴 경로는 `receive_paths`.
"""
from __future__ import annotations

import logging
import os
import time

from PySide6.QtCore import QStandardPaths, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence
from PySide6.QtPrintSupport import QAbstractPrintDialog, QPrintDialog, QPrinter
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDockWidget, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QProgressDialog, QPushButton, QSpinBox, QSplitter, QStackedWidget, QTabWidget, QToolBar,
    QToolButton, QVBoxLayout, QWidget,
)

from bom_pdf import APP_NAME, sysinfo
from bom_pdf.services import annotations as an
from bom_pdf.services import diskspace
from bom_pdf.services import document as ds
from bom_pdf.services import imagetopdf as itp
from bom_pdf.services import outputs
from bom_pdf.services import pageops as po
from bom_pdf.services import session as ss
from bom_pdf.services.recent import RecentDocuments
from bom_pdf.ui.annotation_panel import AnnotationDialog, AnnotationPanel, color_icon
from bom_pdf.ui.bridge import RenderedPage, ServiceBridge
from bom_pdf.ui.compress_dialog import CompressDialog, human_size
from bom_pdf.ui.image_pdf_dialog import ImagePdfDialog
from bom_pdf.ui.page_editor import JOB_MESSAGES, PageEditorDialog
from bom_pdf.ui import page_cache
from bom_pdf.ui.page_cache import PageCache
from bom_pdf.ui.page_view import (
    FIT_NONE, FIT_PAGE, FIT_WIDTH, TOOL_BOX, TOOL_HIGHLIGHT, TOOL_NOTE, TOOL_SELECT, TOOL_UNDERLINE, PageView,
)
from bom_pdf.ui.printing import PRINT_CANCELLED, PRINT_OK, PrintJob, print_sequence
from bom_pdf.ui.search_panel import SearchPanel
from bom_pdf.ui.sidebar import Sidebar
from bom_pdf.ui.text_layer import TextLayer

log = logging.getLogger(__name__)

BANNER_INFO = "background:#fff4ce; color:#3b2f00; padding:6px 10px; border-bottom:1px solid #e0cf8a;"
BANNER_ERROR = "background:#fde7e9; color:#5c0b12; padding:6px 10px; border-bottom:1px solid #e8a0a8;"
SPLIT_HEADER = "background:#e4e6e9; border-left:1px solid #c8cbd0;"
MSG_HASHING = "문서 식별값(sha256)을 계산하는 중입니다. 끝나면 주석을 보고 쓸 수 있습니다."
MSG_EMBEDDED = ("이 PDF 파일 안에 원래 들어 있는 주석 {n}개는 화면에 보이기만 하고 여기서 고치거나 지울 수 없습니다"
                "(목록에도 나오지 않음). 메모 포함 PDF 로 내보낸 파일을 다시 연 경우도 같습니다 — "
                "고치려면 처음 원본 PDF 를 여세요.")
SLOW_OPEN_S = 1.0  # (O4) 이보다 오래 여는 중이면 "불러오는 중"과 취소 단추를 보인다
MSG_SLOW_OPEN = "네트워크 드라이브·Dropbox 등의 \"온라인 전용\" 파일은 처음 열 때 내려받느라 오래 걸릴 수 있습니다."
EXPORT_MESSAGES = {
    ds.EXPORT_CHANGED: "원본 파일이 연 뒤에 바뀌어 내보내지 않았습니다. 탭을 닫고 다시 열어 주세요.",
    ds.EXPORT_REFUSED: "원본 파일에는 덮어쓸 수 없습니다. 다른 이름이나 폴더를 고르세요.",
    ds.EXPORT_INTERRUPTED: "내보내는 중 PDF 엔진이 멈췄습니다. 원본은 바뀌지 않았습니다. 다시 시도해 주세요.",
    ds.EXPORT_ERROR: "메모 포함 PDF를 만들지 못했습니다. 저장 위치의 권한·공간을 확인해 주세요.",
}


class DocumentTab(QWidget):
    """탭 하나 = 문서 하나. 여는 중 / 보기(+오른쪽 분할 칸) / 열기 실패 상태."""

    def __init__(self, doc_id: int, path: str, parent=None):
        super().__init__(parent)
        self.doc_id, self.path = doc_id, path
        self.view: PageView | None = None  # 이 탭 문서의 왼쪽 칸
        self.split_view: PageView | None = None  # 오른쪽 칸 (같은 문서 또는 다른 열린 문서)
        self.active: PageView | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.banner = QLabel()
        self.banner.setWordWrap(True)
        self.banner.hide()
        lay.addWidget(self.banner)
        self.stack = QStackedWidget()
        self.status_page = QWidget()
        sl = QVBoxLayout(self.status_page)
        sl.addStretch(1)
        self.status_label = QLabel("여는 중…")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        sl.addWidget(self.status_label)
        self.cancel_open = QPushButton("열기 취소")  # (O4) 느린 저장소 — 오래 걸리면 보인다. 누르면 탭을 닫는다
        self.cancel_open.hide()
        sl.addWidget(self.cancel_open, 0, Qt.AlignmentFlag.AlignHCenter)
        sl.addStretch(1)
        self.stack.addWidget(self.status_page)
        self.opening_since: float | None = time.monotonic()
        self.loading: tuple[int, int] | None = None  # (읽은 바이트, 전체) — 원격 파일을 메모리로 읽는 중
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.stack.addWidget(self.splitter)
        lay.addWidget(self.stack, 1)

        # 오른쪽 칸: 머리띠(문서 고르기·닫기) + 보기
        self.right = QWidget()
        rlay = QVBoxLayout(self.right)
        rlay.setContentsMargins(0, 0, 0, 0)
        rlay.setSpacing(0)
        head = QWidget()
        head.setStyleSheet(SPLIT_HEADER)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(6, 2, 2, 2)
        hl.addWidget(QLabel("오른쪽:"))
        self.split_combo = QComboBox()
        self.split_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.split_combo.setMinimumContentsLength(12)
        hl.addWidget(self.split_combo, 1)
        self.split_close = QToolButton()
        self.split_close.setText("✕")
        self.split_close.setToolTip("분할 닫기 (Ctrl+\\)")
        self.split_close.setAutoRaise(True)
        hl.addWidget(self.split_close)
        rlay.addWidget(head)
        self.right_layout = rlay
        self.right.hide()

    def show_banner(self, text: str, error: bool = False) -> None:
        self.banner.setText(text)
        self.banner.setStyleSheet(BANNER_ERROR if error else BANNER_INFO)
        self.banner.show()

    def show_failure(self, text: str) -> None:
        self.opening_since = None
        self.cancel_open.hide()
        self.status_label.setText(text)
        self.stack.setCurrentWidget(self.status_page)

    def show_opening(self, now: float) -> None:
        """(O4) 아직 여는 중 — SLOW_OPEN_S 가 지나면 걸린 시간·읽은 양과 취소 단추."""
        if self.opening_since is None or now - self.opening_since < SLOW_OPEN_S:
            return
        secs = int(now - self.opening_since)
        if self.loading and self.loading[1]:
            done, total = self.loading
            head = f"불러오는 중… {done / 2**20:.1f} / {total / 2**20:.1f}MB ({secs}초)"
        else:
            head = f"불러오는 중… {secs}초"
        self.status_label.setText(head + "\n" + MSG_SLOW_OPEN)
        self.cancel_open.show()

    def attach_view(self, view: PageView) -> None:
        self.opening_since = None
        self.cancel_open.hide()
        self.view = view
        self.active = view
        self.splitter.insertWidget(0, view)
        self.splitter.addWidget(self.right)
        self.stack.setCurrentWidget(self.splitter)
        view.setFocus()

    def set_split_view(self, view: PageView | None) -> None:
        old = self.split_view
        if old is not None:
            self.right_layout.removeWidget(old)
            old.hide()
            old.deleteLater()
        self.split_view = view
        if view is not None:
            self.right_layout.addWidget(view, 1)
            self.right.show()
            w = max(200, self.splitter.width() // 2)
            self.splitter.setSizes([w, w])
        else:
            self.right.hide()
            if self.active is old:
                self.active = self.view

    def views(self) -> list[PageView]:
        return [v for v in (self.view, self.split_view) if v is not None]

    def active_view(self) -> PageView | None:
        if self.active is not None and self.active in (self.view, self.split_view):
            return self.active
        return self.view


class MainWindow(QMainWindow):
    sessionChanged = Signal()  # 1-6: 탭 열기·닫기·순서·현재 탭·쪽·확대가 바뀜
    closing = Signal()         # 1-6: 닫기가 확정됨(정상 종료)

    def __init__(self, service: ds.DocumentService, bridge: ServiceBridge, recent: RecentDocuments,
                 cache: PageCache | None = None):
        super().__init__()
        self.service, self.recent = service, recent
        if cache is None:
            cache = PageCache(page_cache.default_budget())
            log.info("page cache budget %dMB (screens %s, ram tier %.0fGB)", cache.max_bytes // page_cache.MB,
                     page_cache.budget_screens(), sysinfo.total_ram_gb())
        self.cache = cache
        self.tabs_by_doc: dict[int, DocumentTab] = {}
        # 주석 (1-3)
        self.library = an.AnnotationLibrary(service.data_dir)
        self.stores: dict[int, an.AnnotationStore] = {}  # doc_id -> 저장소 (sha256 을 안 뒤)
        self.text_layer = TextLayer(service)
        self.current_color = an.DEFAULT_COLOR
        self.current_tool = TOOL_SELECT
        self._exports: dict[int, str] = {}  # 진행 중 내보내기 req_id -> 산출물 경로
        self.last_export: tuple[str, dict] | None = None  # (경로, 결과) — 시험·확인용
        self.last_notice: str | None = None
        # 1-4
        self.page_editor: PageEditorDialog | None = None
        self.print_job: PrintJob | None = None
        self.last_print: str | None = None
        self._print_progress: QProgressDialog | None = None
        self._job_watchers: dict[int, object] = {}  # 작업 프로세스 job_id -> on_job_progress/on_job_done 를 가진 것
        self._compress_jobs: dict[int, str] = {}  # job_id -> 산출물 경로
        self._job_progress: QProgressDialog | None = None
        self.last_compress: tuple[str, dict] | None = None
        # 1-5
        self.image_dialog: ImagePdfDialog | None = None
        # 1-6
        self._restore_targets: dict[int, ss.TabState] = {}  # doc_id -> 열리면 맞출 쪽·확대
        bridge.received.connect(self._on_service_event)
        self._opening_timer = QTimer(self)  # (O4) 여는 중인 탭의 "불러오는 중… N초"
        self._opening_timer.setInterval(500)
        self._opening_timer.timeout.connect(self._tick_opening)

        self.setWindowTitle(APP_NAME)
        self.resize(1300, 900)
        self.setAcceptDrops(True)

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setDocumentMode(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self.tabs.tabBar().tabMoved.connect(lambda *_: self.sessionChanged.emit())
        self.empty = QLabel("PDF 파일을 끌어다 놓거나 [파일 › 열기](Ctrl+O)로 여세요.")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.central = QStackedWidget()
        self.central.addWidget(self.empty)
        self.central.addWidget(self.tabs)
        self.setCentralWidget(self.central)

        self.sidebar = Sidebar(service)
        self.sidebar.pageRequested.connect(lambda p: self._with_view(lambda v: v.go_to_page(p)))
        self.side_dock = QDockWidget("탐색", self)
        self.side_dock.setObjectName("sidebar")
        self.side_dock.setWidget(self.sidebar)
        self.side_dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetClosable
                                   | QDockWidget.DockWidgetFeature.DockWidgetMovable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, self.side_dock)

        self.search_panel = SearchPanel(service)
        self.search_panel.doc_provider = lambda: (v.doc if (v := self.current_view()) is not None else None)
        self.search_panel.hitActivated.connect(self._go_to_hit)
        self.search_panel.hitsChanged.connect(self._apply_hits_all)
        self.search_dock = QDockWidget("검색", self)
        self.search_dock.setObjectName("search")
        self.search_dock.setWidget(self.search_panel)
        self.search_dock.setFeatures(self.side_dock.features())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.search_dock)
        self.search_dock.hide()
        self.annot_panel = AnnotationPanel()
        self.annot_panel.itemActivated.connect(self._go_to_annotation)
        self.annot_panel.editRequested.connect(self.edit_annotation)
        self.annot_panel.deleteRequested.connect(self.delete_annotation)
        self.annot_panel.exportRequested.connect(self.export_annotated)
        self.annot_dock = QDockWidget("주석", self)
        self.annot_dock.setObjectName("annotations")
        self.annot_dock.setWidget(self.annot_panel)
        self.annot_dock.setFeatures(self.side_dock.features())
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.annot_dock)
        self.tabifyDockWidget(self.search_dock, self.annot_dock)
        self.annot_dock.hide()
        self.resizeDocks([self.side_dock, self.search_dock], [160, 320], Qt.Orientation.Horizontal)

        self.page_label = QLabel()
        self.zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self.page_label)
        self.statusBar().addPermanentWidget(self.zoom_label)
        self._build_actions()
        self._refresh_recent_menu()
        self._update_chrome()

    # ------------------------------------------------------------------ 메뉴·도구 막대
    def _act(self, text: str, slot, shortcut=None) -> QAction:
        a = QAction(text, self)
        if shortcut is not None:
            if isinstance(shortcut, list):
                a.setShortcuts(shortcut)
            else:
                a.setShortcut(shortcut)
        a.triggered.connect(slot)
        return a

    def _build_actions(self) -> None:
        K = QKeySequence
        self.act_open = self._act("열기…", self.open_dialog, K.StandardKey.Open)
        self.act_close = self._act("탭 닫기", lambda: self.close_tab(self.tabs.currentIndex()), K("Ctrl+W"))
        self.act_quit = self._act("끝내기", self.close, K("Ctrl+Q"))
        self.act_zoom_in = self._act("확대", lambda: self._with_view(PageView.zoom_in), [K("Ctrl+="), K("Ctrl++")])
        self.act_zoom_out = self._act("축소", lambda: self._with_view(PageView.zoom_out), K("Ctrl+-"))
        self.act_actual = self._act("실제 크기(100%)", lambda: self._with_view(lambda v: v.set_zoom(1.0)), K("Ctrl+1"))
        self.act_fit_width = self._act("폭 맞춤", lambda: self._with_view(lambda v: v.set_fit(FIT_WIDTH)), K("Ctrl+2"))
        self.act_fit_page = self._act("쪽 맞춤", lambda: self._with_view(lambda v: v.set_fit(FIT_PAGE)), K("Ctrl+0"))
        self.act_next_tab = self._act("다음 탭", lambda: self._cycle_tab(1), [K("Ctrl+Tab"), K("Ctrl+PgDown")])
        self.act_prev_tab = self._act("이전 탭", lambda: self._cycle_tab(-1),
                                      [K("Ctrl+Shift+Tab"), K("Ctrl+PgUp")])
        self.act_split = self._act("좌우 분할", self.toggle_split, K("Ctrl+\\"))
        self.act_split.setCheckable(True)
        # 쪽 이동 키(PgUp/PgDn/Home/End)는 보기가 직접 처리한다 — 입력칸에서는 글자 이동으로 남도록.
        # 메뉴에는 \t 뒤에 키 이름만 보인다(전역 단축키로 등록하지 않음).
        self.act_prev_page = self._act("이전 쪽\tPgUp", lambda: self._with_view(PageView.prev_page))
        self.act_next_page = self._act("다음 쪽\tPgDn", lambda: self._with_view(PageView.next_page))
        self.act_first_page = self._act("첫 쪽\tHome", lambda: self._with_view(lambda v: v.go_to_page(0)))
        self.act_last_page = self._act("마지막 쪽\tEnd",
                                       lambda: self._with_view(lambda v: v.go_to_page(v.doc.page_count - 1)))
        self.act_goto = self._act("쪽 번호로 이동…", self.focus_page_box, K("Ctrl+G"))
        self.act_find = self._act("찾기…", self.show_search, K.StandardKey.Find)
        self.act_find_next = self._act("다음 찾기", self.find_next, K("F3"))
        self.act_find_prev = self._act("이전 찾기", self.find_prev, K("Shift+F3"))
        # 주석
        self.tool_group = QActionGroup(self)
        self.tool_group.setExclusive(True)
        self.tool_actions: dict[str, QAction] = {}
        for tool, label, tip in ((TOOL_SELECT, "선택", "글자 선택·복사, 주석 고르기(두 번 누르면 편집)"),
                                 (TOOL_HIGHLIGHT, "형광펜", "글자를 끌어 형광펜"),
                                 (TOOL_UNDERLINE, "밑줄", "글자를 끌어 밑줄"),
                                 (TOOL_NOTE, "메모", "누른 자리에 메모"),
                                 (TOOL_BOX, "사각형", "끌어서 사각형")):
            a = self._act(label, lambda _=False, t=tool: self.set_tool(t))
            a.setCheckable(True)
            a.setToolTip(tip)
            self.tool_group.addAction(a)
            self.tool_actions[tool] = a
        self.tool_actions[TOOL_SELECT].setChecked(True)
        self.act_undo = self._act("주석 되돌리기", self.undo_annotation, K.StandardKey.Undo)
        self.act_redo = self._act("주석 다시 하기", self.redo_annotation, [K("Ctrl+Y"), K("Ctrl+Shift+Z")])
        self.act_copy = self._act("선택한 글자 복사", self.copy_selection, K.StandardKey.Copy)
        self.act_edit_annot = self._act("메모·태그 편집…", lambda: self.edit_annotation(self._selected_id() or ""))
        self.act_delete_annot = self._act("주석 삭제\tDel", lambda: self.delete_annotation(self._selected_id() or ""))
        self.act_export = self._act("메모 포함 PDF로 내보내기…", self.export_annotated, K("Ctrl+Shift+E"))
        # 1-4
        self.act_page_edit = self._act("페이지 편집…", self.open_page_editor, K("Ctrl+E"))
        self.act_page_edit.setToolTip("회전·삭제·순서·추출 — 결과는 새 파일 (Ctrl+E)")
        self.act_merge = self._act("문서 합치기…", lambda: self.open_page_editor("merge"))
        self.act_split_doc = self._act("나누기…", lambda: self.open_page_editor("split"))
        self.act_print = self._act("인쇄…", self.print_document, K.StandardKey.Print)
        self.act_compress = self._act("용량 압축본 만들기…", self.compress_document)
        # 1-5
        self.act_images = self._act("사진 → PDF…", lambda: self.open_image_dialog())
        self.act_images.setToolTip("사진(jpg·png·webp·heic)을 묶어 PDF 로 — 원본 사진은 그대로, 위치정보는 넣지 않음")
        self.act_default_app = self._act("기본 PDF 프로그램으로 지정…", self.open_default_apps)
        self.act_default_app.setToolTip("Windows 설정의 기본 앱 화면을 엽니다 — .pdf 에서 bom-pdf 를 고르세요")

        m = self.menuBar().addMenu("파일(&F)")
        m.addAction(self.act_open)
        self.recent_menu = m.addMenu("최근 문서")
        m.addSeparator()
        m.addAction(self.act_export)
        m.addAction(self.act_compress)
        m.addAction(self.act_images)
        m.addSeparator()
        m.addAction(self.act_print)
        m.addSeparator()
        m.addAction(self.act_close)
        m.addSeparator()
        m.addAction(self.act_default_app)
        m.addSeparator()
        m.addAction(self.act_quit)
        m = self.menuBar().addMenu("편집(&E)")
        for a in (self.act_undo, self.act_redo):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_copy)
        m = self.menuBar().addMenu("주석(&A)")
        for a in self.tool_actions.values():
            m.addAction(a)
        m.addSeparator()
        for a in (self.act_edit_annot, self.act_delete_annot):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_export)
        m = self.menuBar().addMenu("페이지(&P)")
        for a in (self.act_page_edit, self.act_merge, self.act_split_doc):
            m.addAction(a)
        m.addSeparator()
        m.addAction(self.act_compress)
        m.addAction(self.act_print)
        m = self.menuBar().addMenu("찾기(&S)")
        for a in (self.act_find, self.act_find_next, self.act_find_prev):
            m.addAction(a)
        m = self.menuBar().addMenu("이동(&G)")
        for a in (self.act_prev_page, self.act_next_page, self.act_first_page, self.act_last_page, self.act_goto):
            m.addAction(a)
        v = self.menuBar().addMenu("보기(&V)")
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_actual, self.act_fit_width, self.act_fit_page):
            v.addAction(a)
        v.addSeparator()
        v.addAction(self.act_split)
        side = self.side_dock.toggleViewAction()
        side.setText("사이드바(쪽·책갈피)")
        v.addAction(side)
        search = self.search_dock.toggleViewAction()
        search.setText("검색 패널")
        v.addAction(search)
        annots = self.annot_dock.toggleViewAction()
        annots.setText("주석 목록")
        v.addAction(annots)
        v.addSeparator()
        v.addAction(self.act_next_tab)
        v.addAction(self.act_prev_tab)
        h = self.menuBar().addMenu("도움말(&H)")
        self.act_env_check = self._act("환경 점검…", self.open_env_check)
        self.act_env_check.setToolTip("이 PC 환경(Windows·RAM·화면)과 자가 점검 결과 — 문제를 알릴 때 복사해 보내기")
        h.addAction(self.act_env_check)
        h.addSeparator()
        self.act_about = self._act(f"{APP_NAME} 정보…", self.open_about)
        h.addAction(self.act_about)

        tb = QToolBar("기본")
        tb.setObjectName("main-toolbar")
        tb.setMovable(False)
        tb.addAction(self.act_open)
        tb.addSeparator()
        tb.addAction(self.act_zoom_out)
        tb.addAction(self.act_zoom_in)
        tb.addAction(self.act_fit_width)
        tb.addAction(self.act_fit_page)
        tb.addSeparator()
        tb.addWidget(QLabel(" 쪽 "))
        self.page_spin = QSpinBox()
        self.page_spin.setMinimum(1)
        self.page_spin.setKeyboardTracking(False)
        self.page_spin.valueChanged.connect(lambda n: self._with_view(lambda v: v.go_to_page(n - 1)))
        self.page_spin.editingFinished.connect(self._focus_view)
        tb.addWidget(self.page_spin)
        tb.addSeparator()
        tb.addAction(self.act_split)
        tb.addAction(self.act_find)
        tb.addSeparator()
        tb.addAction(self.act_page_edit)
        tb.addAction(self.act_print)
        tb.addAction(self.act_images)
        self.addToolBar(tb)

        at = QToolBar("주석")
        at.setObjectName("annotation-toolbar")
        at.setMovable(False)
        for a in self.tool_actions.values():
            at.addAction(a)
        self.color_combo = QComboBox()
        self.color_combo.setToolTip("새 주석 색 (주석을 고른 상태면 그 주석의 색을 바꿈)")
        for key, (label, _rgb) in an.COLORS.items():
            self.color_combo.addItem(color_icon(key), label, key)
        self.color_combo.activated.connect(self._on_color_chosen)
        at.addWidget(self.color_combo)
        at.addSeparator()
        at.addAction(self.act_undo)
        at.addAction(self.act_redo)
        at.addSeparator()
        at.addAction(self.act_export)
        self.addToolBarBreak()  # 기본 도구 막대 아래 줄 — 한 줄에 다 넣으면 1200px 창에서 넘친다
        self.addToolBar(at)

    def _refresh_recent_menu(self) -> None:
        self.recent_menu.clear()
        items = self.recent.list()
        for p in items:
            a = self.recent_menu.addAction(p)
            a.triggered.connect(lambda _=False, p=p: self.open_paths([p]))
        if not items:
            a = self.recent_menu.addAction("(없음)")
            a.setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("목록 지우기", self._clear_recent)

    def _clear_recent(self) -> None:
        self.recent.clear()
        self._refresh_recent_menu()

    # ------------------------------------------------------------------ 열기·닫기
    def open_dialog(self) -> None:
        start = os.path.dirname(self.recent.list()[0]) if self.recent.list() else ""
        files, _ = QFileDialog.getOpenFileNames(self, "PDF 열기", start, "PDF 파일 (*.pdf);;모든 파일 (*)")
        self.open_paths(files)

    def open_paths(self, paths: list[str]) -> None:
        for p in paths:
            p = os.path.abspath(p)
            existing = self._tab_for_path(p)
            if existing is not None:
                self.tabs.setCurrentWidget(existing)
                continue
            doc_id = self.service.open(p)
            tab = DocumentTab(doc_id, p)
            tab.cancel_open.clicked.connect(lambda _=False, t=tab: self.cancel_opening(t))
            tab.split_combo.activated.connect(lambda _i, t=tab: self._on_split_doc_chosen(t))
            tab.split_close.clicked.connect(lambda _=False, t=tab: self.close_split(t))
            self.tabs_by_doc[doc_id] = tab
            i = self.tabs.addTab(tab, os.path.basename(p))
            self.tabs.setTabToolTip(i, p)
            self.tabs.setCurrentIndex(i)
        if paths and not self._opening_timer.isActive():
            self._opening_timer.start()
        self._update_chrome()
        self.sessionChanged.emit()

    def _tick_opening(self) -> None:
        now = time.monotonic()
        waiting = False
        for tab in self.tabs_by_doc.values():
            if tab.opening_since is not None:
                waiting = True
                tab.show_opening(now)
        if not waiting:
            self._opening_timer.stop()

    def cancel_opening(self, tab: "DocumentTab") -> None:
        """(O4) 여는 중인 탭 닫기 — 원격 파일을 메모리로 읽는 중이면 작업자가 그만 읽는다."""
        i = self.tabs.indexOf(tab)
        if i >= 0 and tab.view is None:
            log.info("open cancelled doc=%s", tab.doc_id)
            self.close_tab(i)

    def _tab_for_path(self, path: str) -> DocumentTab | None:
        key = os.path.normcase(path)
        for tab in self.tabs_by_doc.values():
            if os.path.normcase(tab.path) == key:
                return tab
        return None

    def close_tab(self, index: int) -> None:
        tab = self.tabs.widget(index)
        if not isinstance(tab, DocumentTab):
            return
        doc_id = tab.doc_id
        if self.print_job is not None and self.print_job.doc.doc_id == doc_id:
            self.print_job.cancel()
        self.tabs.removeTab(index)
        self.tabs_by_doc.pop(doc_id, None)
        for v in tab.views():
            v.release()
        for other in self.tabs_by_doc.values():  # 다른 탭 오른쪽 칸에 이 문서가 떠 있으면 닫는다
            if other.split_view is not None and other.split_view.doc.doc_id == doc_id:
                self.close_split(other)
        self.search_panel.clear_for_doc(doc_id)
        self._release_store(doc_id)
        self.text_layer.drop_doc(doc_id)
        self.sidebar.drop_document(doc_id)
        self.cache.drop_doc(doc_id)
        self.service.close(doc_id)
        tab.deleteLater()
        self._restore_targets.pop(doc_id, None)
        self._refresh_split_combos()
        self._sync_active()
        self.sessionChanged.emit()

    def ask_password(self, path: str, wrong: bool) -> str | None:
        """암호 입력 대화상자. 시험에서는 바꿔 끼운다. 암호는 기록하지 않는다."""
        prompt = (ds.MSG_WRONG_PASSWORD + " 다시 입력하세요." if wrong else ds.MSG_NEEDS_PASSWORD)
        text, ok = QInputDialog.getText(self, os.path.basename(path), prompt, QLineEdit.EchoMode.Password)
        return text if ok and text else None

    # ------------------------------------------------------------------ 보기 만들기·분할
    def _make_view(self, tab: DocumentTab, doc: ds.Document) -> PageView:
        view = PageView(self.service, self.cache, doc)
        view.pageChanged.connect(lambda cur, n, v=view: self._on_page_changed(v, cur, n))
        view.zoomChanged.connect(lambda z, v=view: self._on_zoom_changed(v, z))
        view.activated.connect(lambda t=tab, v=view: self._on_view_activated(t, v))
        view.createRequested.connect(lambda data, v=view: self._on_create_requested(v, data))
        view.annotationClicked.connect(lambda item_id, v=view: self._on_annotation_clicked(v, item_id))
        view.annotationEditRequested.connect(self.edit_annotation)
        view.annotationDeleteRequested.connect(self.delete_annotation)
        view.text_layer = self.text_layer
        view.set_tool(self.current_tool)
        view.set_annotation_store(self.stores.get(doc.doc_id))
        self._apply_hits(view)
        return view

    def all_views(self) -> list[PageView]:
        return [v for t in self.tabs_by_doc.values() for v in t.views()]

    def views_of(self, doc_id: int) -> list[PageView]:
        return [v for v in self.all_views() if v.doc.doc_id == doc_id]

    def toggle_split(self) -> None:
        tab = self.tabs.currentWidget()
        if not isinstance(tab, DocumentTab) or tab.view is None:
            self.act_split.setChecked(False)
            return
        if tab.split_view is not None:
            self.close_split(tab)
        else:
            self.open_split(tab, tab.doc_id)

    def open_split(self, tab: DocumentTab, doc_id: int) -> PageView | None:
        """오른쪽 칸에 doc_id 문서를 띄운다(열린 문서만)."""
        doc = self.service.document(doc_id)
        if doc is None or tab.view is None:
            return None
        if tab.split_view is not None:
            if tab.split_view.doc.doc_id == doc_id:
                return tab.split_view
            tab.split_view.release()
        view = self._make_view(tab, doc)
        if tab.view.fit_mode == FIT_NONE:  # 처음에는 왼쪽과 같은 확대 방식
            view.set_zoom(tab.view.zoom)
        else:
            view.set_fit(tab.view.fit_mode)
        tab.set_split_view(view)
        if doc_id == tab.doc_id and tab.view is not None:
            QTimer.singleShot(0, lambda v=view, p=tab.view.current_page(): v.go_to_page(p))
        self._refresh_split_combos()
        self._update_chrome()
        return view

    def close_split(self, tab: DocumentTab) -> None:
        v = tab.split_view
        if v is None:
            return
        was_active = tab.active is v
        v.release()
        tab.set_split_view(None)
        if was_active and tab is self.tabs.currentWidget() and tab.view is not None:
            tab.view.setFocus()
        self._sync_active()

    def _on_split_doc_chosen(self, tab: DocumentTab) -> None:
        doc_id = tab.split_combo.currentData()
        if isinstance(doc_id, int):
            v = self.open_split(tab, doc_id)
            if v is not None:
                v.setFocus()

    def _refresh_split_combos(self) -> None:
        docs = [(t.doc_id, os.path.basename(t.path)) for t in
                (self.tabs.widget(i) for i in range(self.tabs.count()))
                if isinstance(t, DocumentTab) and t.view is not None]
        for t in self.tabs_by_doc.values():
            if t.split_view is None:
                continue
            c = t.split_combo
            c.blockSignals(True)
            c.clear()
            for did, name in docs:
                c.addItem(name, did)
            c.setCurrentIndex(max(0, c.findData(t.split_view.doc.doc_id)))
            c.blockSignals(False)

    # ------------------------------------------------------------------ 검색
    def show_search(self) -> None:
        self.search_dock.show()
        self.search_dock.raise_()
        self.search_panel.focus_query()

    def find_next(self) -> None:
        p = self.search_panel
        if p.model.hits:
            p.next_hit()
        elif p.query.text().strip() and not p.running:
            self.search_dock.show()
            p.start_search()
        else:
            self.show_search()

    def find_prev(self) -> None:
        p = self.search_panel
        if p.model.hits:
            p.prev_hit()
        else:
            self.find_next()

    def _apply_hits(self, view: PageView) -> None:
        p = self.search_panel
        if p.doc_id is not None and view.doc.doc_id == p.doc_id:
            view.set_search_hits(p.hits_by_page, p.current_hit())
        else:
            view.set_search_hits(None)

    def _apply_hits_all(self, _doc_id: int = 0) -> None:
        for v in self.all_views():
            self._apply_hits(v)

    def _go_to_hit(self, hit) -> None:
        doc_id = self.search_panel.doc_id
        if doc_id is None:
            return
        view = self.current_view()
        if view is None or view.doc.doc_id != doc_id:
            cur = self.tabs.currentWidget()
            view = next((v for v in (cur.views() if isinstance(cur, DocumentTab) else []) if v.doc.doc_id == doc_id),
                        None)
            if view is None:
                tab = self.tabs_by_doc.get(doc_id)
                if tab is None or tab.view is None:
                    return
                self.tabs.setCurrentWidget(tab)
                view = tab.view
        self._apply_hits_all()
        view.reveal(hit.page, hit.rects[0] if hit.rects else None)

    # ------------------------------------------------------------------ 서비스 이벤트
    def _on_service_event(self, ev) -> None:
        if self.page_editor is not None and isinstance(ev, (ds.DocumentOpened, ds.DocumentOpenFailed,
                                                            ds.DocumentHashed, ds.Outline, ds.EngineRestarted)):
            self.page_editor.on_service_event(ev)
        if isinstance(ev, RenderedPage):
            if ev.lane == ds.THUMB_LANE:
                self.sidebar.on_thumb(ev)
            elif ev.lane == ds.EDIT_LANE:
                if self.page_editor is not None:
                    self.page_editor.on_thumb(ev)
            elif ev.lane == ds.PRINT_LANE:
                if self.print_job is not None and self.print_job.doc.doc_id == ev.doc_id:
                    self.print_job.on_page(ev)
            else:
                for v in self.views_of(ev.doc_id):
                    v.on_page_ready(ev.page, ev.scale, ev.seq, ev.image, ev.tile)
        elif isinstance(ev, ds.PageError):
            if ev.lane == ds.THUMB_LANE:
                self.sidebar.on_thumb_error(ev)
            elif ev.lane == ds.EDIT_LANE:
                if self.page_editor is not None:
                    self.page_editor.on_thumb_error(ev)
            elif ev.lane == ds.PRINT_LANE:
                if self.print_job is not None and self.print_job.doc.doc_id == ev.doc_id:
                    self.print_job.on_page_error(ev)
            else:
                for v in self.views_of(ev.doc_id):
                    v.on_page_error(ev.page, ev.seq)
        elif isinstance(ev, ds.SearchResults):
            self.search_panel.on_results(ev)
        elif isinstance(ev, ds.SearchDone):
            self.search_panel.on_done(ev)
        elif isinstance(ev, ds.Outline):
            self.sidebar.set_outline(ev.doc_id, ev.items)
        elif isinstance(ev, ds.PageChars):
            self.text_layer.on_chars(ev)
            for v in self.views_of(ev.doc_id):
                v.on_chars(ev.page)
        elif isinstance(ev, ds.DocumentHashed):
            doc = self.service.document(ev.doc_id)
            if doc is not None:
                self._attach_store(doc)
        elif isinstance(ev, ds.DocumentLoading):
            tab = self.tabs_by_doc.get(ev.doc_id)
            if tab is not None and tab.view is None:
                tab.loading = (ev.done, ev.total)
                tab.show_opening(time.monotonic())
        elif isinstance(ev, ds.ExportDone):
            self._on_export_done(ev)
        elif isinstance(ev, ds.JobProgress):
            w = self._job_watchers.get(ev.job_id)
            if w is not None:
                w.on_job_progress(ev)
        elif isinstance(ev, ds.JobDone):
            w = self._job_watchers.pop(ev.job_id, None)
            if w is not None:
                w.on_job_done(ev)
        elif isinstance(ev, ds.DocumentOpened):
            self._on_opened(ev)
        elif isinstance(ev, ds.DocumentOpenFailed):
            self._on_open_failed(ev)
        elif isinstance(ev, ds.DocumentCrashed):
            tab = self.tabs_by_doc.get(ev.doc_id)
            if tab is not None:
                tab.show_banner(ev.message, error=True)
                if tab.view is None:
                    tab.show_failure(ev.message)
            for v in self.views_of(ev.doc_id):
                v.set_dead()
            self.sidebar.set_doc_dead(ev.doc_id)
            if self.print_job is not None and self.print_job.doc.doc_id == ev.doc_id:
                self.print_job.on_engine_lost()
        elif isinstance(ev, ds.EngineRestarted):
            if self.print_job is not None:
                self.print_job.on_engine_lost()
            for v in self.all_views():
                v.on_engine_restarted()
            self.sidebar.on_engine_restarted()
            self.text_layer.on_engine_restarted()
        elif isinstance(ev, ds.EngineFailed):
            self.statusBar().showMessage(ev.message)
            for tab in self.tabs_by_doc.values():
                tab.show_banner(ev.message, error=True)
            box = QMessageBox(QMessageBox.Icon.Critical, APP_NAME, ev.message, parent=self)
            box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            box.open()  # 모달 exec 로 이벤트 루프를 붙잡지 않는다

    def _on_opened(self, ev: ds.DocumentOpened) -> None:
        tab = self.tabs_by_doc.get(ev.doc.doc_id)
        if tab is None:
            return
        view = self._make_view(tab, ev.doc)
        tab.attach_view(view)
        target = self._restore_targets.pop(ev.doc.doc_id, None)
        if target is not None:  # 배치가 창 크기를 안 뒤에 맞춘다
            QTimer.singleShot(0, lambda v=view, t=target: self._apply_tab_state(v, t))
        self.service.request_outline(ev.doc.doc_id)
        if ev.doc.sha256 is not None:  # 해시 캐시가 맞으면 DocumentHashed 보다 먼저 알 수 있다
            self._attach_store(ev.doc)
        if ev.notice:
            tab.show_banner(ev.notice)
        if ev.doc.embedded_annots:
            log.info("doc=%s has embedded annots=%d (display only)", ev.doc.doc_id, ev.doc.embedded_annots)
            self.statusBar().showMessage(f"이 PDF 에 원래 든 주석 {ev.doc.embedded_annots}개는 보기만 됩니다", 8000)
        try:
            self.recent.add(ev.doc.path)
        except OSError:
            log.exception("recent save failed")
        self._refresh_recent_menu()
        self._refresh_split_combos()
        self._sync_active()

    def _on_open_failed(self, ev: ds.DocumentOpenFailed) -> None:
        tab = self.tabs_by_doc.get(ev.doc_id)
        if tab is None:
            return
        if ev.kind in (ds.NEEDS_PASSWORD, ds.WRONG_PASSWORD):
            tab.show_failure(ds.MSG_NEEDS_PASSWORD)
            pw = self.ask_password(ev.path, ev.kind == ds.WRONG_PASSWORD)
            if pw is not None and ev.doc_id in self.tabs_by_doc:
                tab.show_failure("여는 중…")
                tab.opening_since = time.monotonic()
                self._opening_timer.start()
                self.service.open(ev.path, pw, doc_id=ev.doc_id)
                return
            tab.show_failure(ds.MSG_NEEDS_PASSWORD + "\n(탭을 닫고 다시 열면 암호를 다시 물어봅니다)")
            return
        tab.show_failure(ev.message)

    # ------------------------------------------------------------------ 활성 보기·상태 표시
    def current_view(self) -> PageView | None:
        tab = self.tabs.currentWidget()
        return tab.active_view() if isinstance(tab, DocumentTab) else None

    def _with_view(self, fn) -> None:
        v = self.current_view()
        if v is not None:
            fn(v)

    def _focus_view(self) -> None:
        v = self.current_view()
        if v is not None:
            v.setFocus()

    def focus_page_box(self) -> None:
        if self.page_spin.isEnabled():
            self.page_spin.setFocus(Qt.FocusReason.ShortcutFocusReason)
            self.page_spin.selectAll()

    def _cycle_tab(self, step: int) -> None:
        n = self.tabs.count()
        if n:
            self.tabs.setCurrentIndex((self.tabs.currentIndex() + step) % n)

    def _on_tab_changed(self, _index: int) -> None:
        self._sync_active()
        self.sessionChanged.emit()

    def _on_view_activated(self, tab: DocumentTab, view: PageView) -> None:
        if tab.active is view:
            return
        tab.active = view
        if tab is self.tabs.currentWidget():
            self._sync_active()

    def _sync_active(self) -> None:
        """활성 보기가 바뀜 → 사이드바·상태 표시줄·도구 막대를 그 보기에 맞춘다."""
        view = self.current_view()
        self.sidebar.set_document(view.doc if view is not None else None,
                                  view.current_page() if view is not None else 0)
        self._sync_annotation_panel()
        self._update_chrome()

    def _on_page_changed(self, view: PageView, cur: int, n: int) -> None:
        if view is self.current_view():
            self._show_page(cur, n)
            self.sidebar.set_current_page(cur)
        self.sessionChanged.emit()

    def _on_zoom_changed(self, view: PageView, z: float) -> None:
        if view is self.current_view():
            self.zoom_label.setText(f"{round(z * 100)}%")
        self.sessionChanged.emit()

    def _show_page(self, cur: int, n: int) -> None:
        self.page_label.setText(f"{cur + 1} / {n}쪽")
        self.page_spin.blockSignals(True)
        self.page_spin.setMaximum(max(1, n))
        self.page_spin.setValue(cur + 1)
        self.page_spin.blockSignals(False)

    def _update_chrome(self) -> None:
        has_tabs = self.tabs.count() > 0
        self.central.setCurrentWidget(self.tabs if has_tabs else self.empty)
        tab = self.tabs.currentWidget()
        view = self.current_view()
        self.act_close.setEnabled(has_tabs)
        for a in (self.act_page_edit, self.act_merge, self.act_split_doc, self.act_compress):
            a.setEnabled(view is not None and self.page_editor is None)
        self.act_print.setEnabled(view is not None and self.print_job is None)
        for a in (self.act_zoom_in, self.act_zoom_out, self.act_actual, self.act_fit_width, self.act_fit_page,
                  self.act_split, self.act_prev_page, self.act_next_page, self.act_first_page,
                  self.act_last_page, self.act_goto):
            a.setEnabled(view is not None)
        self.act_split.setChecked(isinstance(tab, DocumentTab) and tab.split_view is not None)
        self.page_spin.setEnabled(view is not None)
        if isinstance(tab, DocumentTab):
            self.setWindowTitle(f"{os.path.basename(tab.path)} — {APP_NAME}")
        else:
            self.setWindowTitle(APP_NAME)
        if view is not None:
            self._show_page(view.current_page(), view.doc.page_count)
            self.zoom_label.setText(f"{round(view.zoom * 100)}%")
        else:
            self.page_label.setText("")
            self.zoom_label.setText("")
        self._update_annotation_actions()

    # ------------------------------------------------------------------ 주석 (1-3)
    def _attach_store(self, doc: ds.Document) -> None:
        """sha256 을 알았다 — 사이드카 저장소를 붙이고 보기·목록·도구를 켠다."""
        if doc.doc_id in self.stores or doc.doc_id not in self.tabs_by_doc or doc.sha256 is None:
            return
        store = self.library.acquire(doc.sha256, doc.path)
        store.add_listener(self._on_store_changed)
        self.stores[doc.doc_id] = store
        for v in self.views_of(doc.doc_id):
            v.set_annotation_store(store)
        tab = self.tabs_by_doc.get(doc.doc_id)
        if store.notice and tab is not None:
            tab.show_banner(store.notice, error=not store.read_only)
        self._sync_annotation_panel()
        self._update_annotation_actions()

    def _release_store(self, doc_id: int) -> None:
        store = self.stores.pop(doc_id, None)
        if store is None:
            return
        if store not in self.stores.values():
            store.remove_listener(self._on_store_changed)
        self.library.release(store.sha256)
        if self.annot_panel.store is store and store not in self.stores.values():
            self.annot_panel.set_store(None)

    def store_of(self, view: PageView | None) -> an.AnnotationStore | None:
        return self.stores.get(view.doc.doc_id) if view is not None else None

    def current_store(self) -> an.AnnotationStore | None:
        return self.store_of(self.current_view())

    def _views_of_store(self, store) -> list[PageView]:
        return [v for v in self.all_views() if v.store is store]

    def _on_store_changed(self, store: an.AnnotationStore) -> None:
        for v in self._views_of_store(store):
            if v.selected_id and store.get(v.selected_id) is None:
                v.set_selected(None)
            v.viewport().update()
        if self.annot_panel.store is store:
            self.annot_panel.refresh()
            v = self.current_view()
            self.annot_panel.select_item(v.selected_id if v is not None else None)
        for doc_id, s in self.stores.items():
            if s is store and store.save_error and (tab := self.tabs_by_doc.get(doc_id)) is not None:
                tab.show_banner(store.save_error, error=True)
        self._update_annotation_actions()

    def _sync_annotation_panel(self) -> None:
        view = self.current_view()
        store = self.store_of(view)
        if view is None:
            status = ""
        elif store is None:
            status = MSG_HASHING
        else:
            status = (store.notice or an.MSG_NEWER) if store.read_only else ""
            if view.doc.embedded_annots:
                status = "\n\n".join(s for s in (status, MSG_EMBEDDED.format(n=view.doc.embedded_annots)) if s)
        if self.annot_panel.store is not store or self.annot_panel.status.text() != status:
            self.annot_panel.set_store(store, status)
        self.annot_panel.select_item(view.selected_id if view is not None else None)

    def _selected_id(self) -> str | None:
        v = self.current_view()
        return v.selected_id if v is not None else None

    def _update_annotation_actions(self) -> None:
        view = self.current_view()
        store = self.store_of(view)
        writable = store is not None and not store.read_only
        for tool, a in self.tool_actions.items():
            a.setEnabled(view is not None and (tool == TOOL_SELECT or writable))
        if not writable and self.current_tool != TOOL_SELECT and view is not None:
            self.set_tool(TOOL_SELECT)
        self.color_combo.setEnabled(writable)
        self.act_undo.setEnabled(writable and store.can_undo())
        self.act_redo.setEnabled(writable and store.can_redo())
        has_sel = writable and bool(self._selected_id())
        self.act_edit_annot.setEnabled(has_sel)
        self.act_delete_annot.setEnabled(has_sel)
        self.act_copy.setEnabled(view is not None)
        self.act_export.setEnabled(store is not None and bool(store.items) and not self._exports)
        self.annot_panel.export_btn.setEnabled(self.act_export.isEnabled())

    def set_tool(self, tool: str) -> None:
        self.current_tool = tool
        self.tool_actions[tool].setChecked(True)
        for v in self.all_views():
            v.set_tool(tool)
        if tool != TOOL_SELECT:
            self._show_annotation_dock()

    def _show_annotation_dock(self) -> None:
        if not self.annot_dock.isVisible():
            self.annot_dock.show()
            self.annot_dock.raise_()

    def ask_annotation(self, title: str, note: str, tags: list[str], color: str) -> dict | None:
        """메모·태그·색 입력 — 시험에서는 바꿔 끼운다. 결과 {"note", "tags", "color"} 또는 None(취소)."""
        dlg = AnnotationDialog(title, note, tags, color, self)
        return dlg.values() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def _select_annotation(self, store, item_id: str | None) -> None:
        cur = self.tabs.currentWidget()
        for v in self._views_of_store(store):
            v.set_selected(item_id if isinstance(cur, DocumentTab) and v in cur.views() else None)
        it = store.get(item_id) if item_id else None
        # 고른 주석이 있으면 그 색, 없으면 새 주석 색을 보인다(고르기를 풀었는데 앞 주석 색이 남아 헷갈리지 않게)
        shown = it["color"] if it is not None else self.current_color
        self.color_combo.setCurrentIndex(max(0, self.color_combo.findData(shown)))
        if self.annot_panel.store is store:
            self.annot_panel.select_item(item_id)
        self._update_annotation_actions()

    def _on_create_requested(self, view: PageView, data: dict) -> None:
        store = self.store_of(view)
        if store is None or store.read_only:
            return
        data = dict(data, color=self.current_color)
        if data["type"] == an.NOTE:
            res = self.ask_annotation("메모", "", [], self.current_color)
            if res is None:
                return
            data.update(res)
        item = store.add(data)
        self._show_annotation_dock()
        self._select_annotation(store, item["id"])

    def _on_annotation_clicked(self, view: PageView, item_id: str) -> None:
        store = self.store_of(view)
        if store is not None:
            self._select_annotation(store, item_id or None)

    def _go_to_annotation(self, page: int, item_id: str) -> None:
        view = self.current_view()
        store = self.store_of(view)
        if store is None or store.get(item_id) is None:
            return
        view.reveal(page, an.item_bounds(store.get(item_id)))
        self._select_annotation(store, item_id)

    def edit_annotation(self, item_id: str) -> None:
        store = self.current_store()
        it = store.get(item_id) if store is not None and item_id else None
        if it is None or store.read_only:
            return
        res = self.ask_annotation(f"{an.TYPE_LABELS[it['type']]} 편집", it["note"], it["tags"], it["color"])
        if res is not None:
            store.update(item_id, **res)

    def delete_annotation(self, item_id: str) -> None:
        store = self.current_store()
        if store is None or store.read_only or not item_id or store.get(item_id) is None:
            return
        store.remove(item_id)
        self.statusBar().showMessage("주석을 지웠습니다 (Ctrl+Z 로 되돌리기)", 4000)

    def undo_annotation(self) -> None:
        store = self.current_store()
        if store is not None and store.undo():
            self.statusBar().showMessage("되돌렸습니다", 2000)

    def redo_annotation(self) -> None:
        store = self.current_store()
        if store is not None and store.redo():
            self.statusBar().showMessage("다시 했습니다", 2000)

    def copy_selection(self) -> None:
        v = self.current_view()
        if v is not None and v.copy_selection():
            self.statusBar().showMessage("선택한 글자를 복사했습니다", 2000)

    def _on_color_chosen(self, _index: int) -> None:
        self.current_color = self.color_combo.currentData()
        store, item_id = self.current_store(), self._selected_id()
        if store is not None and item_id and not store.read_only and store.get(item_id) is not None:
            store.update(item_id, color=self.current_color)

    # ---- 메모 포함 PDF
    def documents_dir(self) -> str:
        return QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DocumentsLocation)

    def ask_export_path(self, default_path: str) -> str | None:
        """저장 대화상자 — 시험에서는 바꿔 끼운다."""
        path, _ = QFileDialog.getSaveFileName(self, "메모 포함 PDF로 내보내기", default_path, "PDF 파일 (*.pdf)")
        return path or None

    def export_annotated(self) -> None:
        view = self.current_view()
        store = self.store_of(view)
        if store is None or self._exports:
            return
        if not store.items:
            self.statusBar().showMessage("내보낼 주석이 없습니다", 4000)
            return
        doc = view.doc
        last = outputs.last_export_dir(self.service.data_dir)
        default = outputs.default_export_path(doc.path, outputs.SUFFIX_NOTES, last, self.documents_dir())
        path = self.ask_export_path(str(default))
        if not path:
            return
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        protected = [t.path for t in self.tabs_by_doc.values()]
        if outputs.is_protected(path, protected):
            self._notify(EXPORT_MESSAGES[ds.EXPORT_REFUSED], error=True)
            return
        if not self.space_ok([path], diskspace.file_size(doc.path)):
            return
        try:
            outputs.set_last_export_dir(os.path.dirname(os.path.abspath(path)), self.service.data_dir)
        except OSError:
            log.exception("settings save failed")
        req = self.service.export_annotated(doc.doc_id, path, store.export_items())
        if req is None:
            return
        self._exports[req] = path
        self.statusBar().showMessage("메모 포함 PDF를 만드는 중…")
        self._update_annotation_actions()

    def _on_export_done(self, ev) -> None:
        path = self._exports.pop(ev.req_id, None)
        if path is None:
            return
        r = ev.result
        if r.get("status") == ds.EXPORT_OK:
            msg = (f"메모 포함 PDF를 만들었습니다: {os.path.basename(path)}\n"
                   f"넣은 주석 {r['added']}개" + (f", 원본에 있던 주석 {r['before']}개 포함 모두 {r['after']}개"
                                               if r.get("before") else ""))
            if r.get("skipped"):
                msg += f"\n쪽 범위를 벗어나 뺀 주석 {r['skipped']}개"
            self._notify(msg)
            self.last_export = (path, r)
        else:
            self._notify(EXPORT_MESSAGES.get(r.get("status"), EXPORT_MESSAGES[ds.EXPORT_ERROR]), error=True)
            self.last_export = (path, r)
        self._update_annotation_actions()

    def _notify(self, text: str, error: bool = False) -> None:
        """상태 표시줄 + 모달이 아닌 알림 상자(이벤트 루프를 붙잡지 않는다)."""
        self.statusBar().showMessage(text.split("\n")[0], 8000)
        box = QMessageBox(QMessageBox.Icon.Warning if error else QMessageBox.Icon.Information, APP_NAME, text,
                          parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()
        self.last_notice = text

    # ------------------------------------------------------------------ 1-4 산출물 공통
    def open_documents(self) -> list[ds.Document]:
        """탭 차례대로 열린 문서."""
        out = []
        for i in range(self.tabs.count()):
            t = self.tabs.widget(i)
            if isinstance(t, DocumentTab) and t.view is not None and (d := self.service.document(t.doc_id)):
                out.append(d)
        return out

    def document_for_path(self, path: str) -> ds.Document | None:
        tab = self._tab_for_path(os.path.abspath(path))
        return self.service.document(tab.doc_id) if tab is not None else None

    def protected_paths(self) -> list[str]:
        """산출물로 쓸 수 없는 경로 — 열린 원본 모두."""
        return [t.path for t in self.tabs_by_doc.values()]

    def default_output_dir(self, original: str) -> str:
        return outputs.default_export_dir(original, outputs.last_export_dir(self.service.data_dir),
                                          self.documents_dir())

    def default_output_path(self, original: str, suffix: str):
        return outputs.default_export_path(original, suffix, outputs.last_export_dir(self.service.data_dir),
                                           self.documents_dir())

    def space_ok(self, out_paths: list[str], estimate: int, parent=None) -> bool:
        """(O4) 시작 전 여유 공간 확인 — 모자라면 알리고 False(아무것도 시작하지 않음)."""
        c = diskspace.check_outputs(out_paths, estimate)
        if c.ok:
            return True
        log.warning("not enough space need=%d free=%s", c.need, c.free)
        self.last_space_check = c
        notify = getattr(parent, "_notify", None) or self._notify
        notify(diskspace.message(c), error=True)
        return False

    def remember_output_dir(self, folder: str) -> None:
        try:
            outputs.set_last_export_dir(folder, self.service.data_dir)
        except OSError:
            log.exception("settings save failed")

    def annotation_items(self, sha256: str | None) -> list | None:
        """그 원본(sha256)의 사이드카 주석(표준 주석으로 넣을 모양). 없으면 None."""
        if not sha256:
            return None
        store = self.library.get(sha256)
        if store is None:
            store = an.AnnotationStore(sha256, self.library.folder)  # 읽기만 — 열린 탭이 없는 병합 원본
        return store.export_items() or None

    def watch_job(self, job_id: int, watcher) -> None:
        self._job_watchers[job_id] = watcher

    def unwatch(self, watcher) -> None:
        """닫힌 창의 작업 결과는 더 넘기지 않는다(작업 프로세스는 각자 취소·정리)."""
        for k in [k for k, w in self._job_watchers.items() if w is watcher]:
            del self._job_watchers[k]

    def ask_output_path(self, title: str, default_path: str) -> str | None:
        """산출물 저장 대화상자 — 시험에서는 바꿔 끼운다. 같은 이름이 있으면 대화상자가 덮어쓸지 묻는다."""
        path, _ = QFileDialog.getSaveFileName(self, title, default_path, "PDF 파일 (*.pdf)")
        return path or None

    # ---- 페이지 편집
    def open_page_editor(self, then: str | None = None) -> PageEditorDialog | None:
        """활성 보기의 문서로 편집 창을 연다. then: "merge" / "split" 이면 열자마자 그 대화상자."""
        view = self.current_view()
        if view is None:
            return None
        if self.page_editor is not None:
            self.page_editor.raise_()
            return self.page_editor
        ed = PageEditorDialog(self, view.doc, self)
        ed.setWindowModality(Qt.WindowModality.WindowModal)
        ed.closed.connect(self._on_editor_closed)
        self.page_editor = ed
        ed.show()
        self._update_chrome()
        if then == "merge":
            QTimer.singleShot(0, ed.merge)
        elif then == "split":
            QTimer.singleShot(0, ed.split)
        return ed

    def _on_editor_closed(self) -> None:
        ed = self.page_editor
        self.page_editor = None
        if ed is not None:
            ed.deleteLater()
        self._update_chrome()

    # ---- 인쇄
    def ask_print(self, printer: QPrinter, page_count: int) -> bool:
        """인쇄 대화상자 — 시험에서는 바꿔 끼운다(QPrinter 를 PDF 출력으로 설정)."""
        dlg = QPrintDialog(printer, self)
        dlg.setWindowTitle("인쇄")
        dlg.setMinMax(1, page_count)
        O = QAbstractPrintDialog.PrintDialogOption
        dlg.setOptions(O.PrintPageRange | O.PrintCurrentPage | O.PrintCollateCopies | O.PrintShowPageSize)
        return dlg.exec() == QDialog.DialogCode.Accepted

    def open_env_check(self) -> None:
        from bom_pdf.ui.env_dialog import EnvDialog

        EnvDialog(self, cache_mb=self.cache.max_bytes // page_cache.MB).exec()

    def about_text(self) -> str:
        """도움말 › 정보 (O6) — 버전·설치 위치·데이터 폴더·업데이트 방법·라이선스. 파일 이름·문서 내용 없음."""
        from bom_pdf import SOURCE_URL, __version__

        return (f"{APP_NAME} {__version__}\n"
                "법률문서용 PDF 리더/편집기\n\n"
                f"설치: {sysinfo.install_location()}\n"
                f"설정·주석·기록: {sysinfo.short_path(str(self.service.data_dir))} (제거해도 지우지 않음)\n\n"
                "업데이트: 새 설치 파일(bom-pdf-Setup-<판>.exe)을 실행하면 덮어써집니다. 주석·설정은 그대로입니다.\n\n"
                "이 프로그램은 GNU AGPL v3 로 배포되는 자유 소프트웨어입니다(보증 없음). "
                "설치 폴더의 LICENSE·THIRD_PARTY_NOTICES.md 를 보십시오.\n"
                f"소스 코드: {SOURCE_URL}\n"
                "구성 요소: PyMuPDF·MuPDF(AGPL v3), Qt(PySide6, LGPL v3), Pillow, pi-heif(libheif, LGPL), "
                "OpenCV(Apache-2.0), NumPy(BSD).")

    def open_about(self) -> None:
        box = QMessageBox(QMessageBox.Icon.Information, f"{APP_NAME} 정보", self.about_text(), parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def open_default_apps(self) -> None:
        from bom_pdf.ui.desktop import open_default_apps_settings

        if not open_default_apps_settings():
            self.statusBar().showMessage("Windows 설정을 열지 못했습니다 — 설정 → 앱 → 기본 앱에서 bom-pdf 를 찾아 주세요",
                                         10000)

    def print_document(self) -> PrintJob | None:
        view = self.current_view()
        if view is None or self.print_job is not None:
            return None
        doc = view.doc
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(doc.name)
        if not self.ask_print(printer, doc.page_count):
            return None
        seq = print_sequence(printer, doc.page_count, view.current_page())
        job = PrintJob(self.service, doc, printer, seq)
        dlg = QProgressDialog("인쇄할 쪽을 준비하는 중…", "취소", 0, max(1, len(seq)), self)
        dlg.setWindowTitle("인쇄")
        dlg.setMinimumDuration(300)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(job.cancel)
        job.progress.connect(lambda done, total: (dlg.setValue(done),
                                                  dlg.setLabelText(f"인쇄 중… {done}/{total}쪽")))
        job.finished.connect(self._on_print_done)
        self._print_progress = dlg
        self.print_job = job
        self._update_chrome()
        job.start()
        return job

    def _on_print_done(self, status: str) -> None:
        job = self.print_job
        self.print_job = None
        self.last_print = status
        dlg, self._print_progress = self._print_progress, None
        if dlg is not None:
            dlg.blockSignals(True)
            dlg.close()
            dlg.deleteLater()
        if status == PRINT_OK:
            self.statusBar().showMessage(f"인쇄를 보냈습니다 ({len(job.sequence)}쪽)", 6000)
        elif status == PRINT_CANCELLED:
            self.statusBar().showMessage("인쇄를 취소했습니다", 4000)
        else:
            self._notify("인쇄하지 못했습니다. 프린터 상태를 확인하고 다시 시도해 주세요.", error=True)
        self._update_chrome()

    # ---- 용량 압축본
    def ask_compress(self, doc: ds.Document) -> str | None:
        """압축 방식 — 시험에서는 바꿔 끼운다."""
        try:
            size = os.path.getsize(doc.path)
        except OSError:
            size = 0
        dlg = CompressDialog(doc.name, size, self)
        return dlg.preset() if dlg.exec() == QDialog.DialogCode.Accepted else None

    def compress_document(self) -> int | None:
        view = self.current_view()
        if view is None or self._compress_jobs:
            return None
        doc = view.doc
        if doc.sha256 is None:
            self.statusBar().showMessage(MSG_HASHING, 4000)
            return None
        preset = self.ask_compress(doc)
        if preset is None:
            return None
        path = self.ask_output_path("용량 압축본 만들기", str(self.default_output_path(doc.path, po.SUFFIX_COMPRESS)))
        if not path:
            return None
        if not path.lower().endswith(".pdf"):
            path += ".pdf"
        if outputs.is_protected(path, self.protected_paths()):
            self._notify(JOB_MESSAGES[ds.EXPORT_REFUSED], error=True)
            return None
        if not self.space_ok([path], diskspace.file_size(doc.path)):  # 압축본은 원본보다 크지 않다 — 최댓값으로
            return None
        self.remember_output_dir(os.path.dirname(os.path.abspath(path)))
        job_id = self.service.compress(doc.doc_id, path, preset)
        if job_id is None:
            return None
        self._compress_jobs[job_id] = path
        self.watch_job(job_id, self)
        dlg = QProgressDialog("용량 압축본을 만드는 중… (큰 스캔본은 몇십 초 걸릴 수 있습니다)", "취소", 0, 0, self)
        dlg.setWindowTitle("용량 압축본")
        dlg.setMinimumDuration(500)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.canceled.connect(lambda j=job_id: self.service.cancel_job(j))
        self._job_progress = dlg
        return job_id

    def on_job_progress(self, ev) -> None:
        if self._job_progress is not None and ev.total:
            self._job_progress.setRange(0, ev.total)
            self._job_progress.setValue(ev.done)

    def on_job_done(self, ev) -> None:
        path = self._compress_jobs.pop(ev.job_id, None)
        dlg, self._job_progress = self._job_progress, None
        if dlg is not None:
            dlg.blockSignals(True)
            dlg.close()
            dlg.deleteLater()
        if path is None:
            return
        r = ev.result
        self.last_compress = (path, r)
        st = r.get("status")
        if st == ds.EXPORT_OK:
            pct = round(100 * (1 - r["after"] / r["before"])) if r.get("before") else 0
            self._notify(f"용량 압축본을 만들었습니다: {os.path.basename(path)}\n"
                         f"{human_size(r['before'])} → {human_size(r['after'])} ({pct}% 줄임, {r['seconds']:.1f}초)")
        elif st == ds.JOB_NOT_SMALLER:
            self._notify(f"압축해도 줄지 않아 파일을 만들지 않았습니다.\n"
                         f"원본 {human_size(r['before'])}, 압축 결과 {human_size(r['after'])}", error=True)
        else:
            self._notify(JOB_MESSAGES.get(st, JOB_MESSAGES[ds.EXPORT_ERROR]), error=True)

    # ------------------------------------------------------------------ 1-5 사진 → PDF
    def open_image_dialog(self, paths: list[str] | None = None) -> ImagePdfDialog:
        """사진 → PDF 창(하나만). paths 가 있으면 목록에 더한다. 창 모달이 아니다 — 열린 PDF 를 보며 쓸 수 있다."""
        dlg = self.image_dialog
        if dlg is None:
            dlg = ImagePdfDialog(self, self.service, self)
            dlg.closed.connect(self._on_image_dialog_closed)
            self.image_dialog = dlg
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()
        if paths:
            dlg.add_paths(paths)
        return dlg

    def _on_image_dialog_closed(self) -> None:
        dlg, self.image_dialog = self.image_dialog, None
        if dlg is not None:
            self.unwatch(dlg)
            dlg.deleteLater()

    # ------------------------------------------------------------------ 세션·다른 실행 (1-6)
    def session_tabs(self) -> tuple[list[ss.TabState], int]:
        """탭 순서대로 (경로·왼쪽 칸의 쪽·확대). 아직 안 열린 탭은 맞출 예정이던 값을 그대로."""
        out = []
        for i in range(self.tabs.count()):
            tab = self.tabs.widget(i)
            if not isinstance(tab, DocumentTab):
                continue
            if tab.view is not None:
                v = tab.view
                out.append(ss.TabState(tab.path, v.current_page(), round(v.zoom, 4), v.fit_mode))
            else:
                t = self._restore_targets.get(tab.doc_id)
                out.append(ss.TabState(tab.path, t.page, t.zoom, t.fit) if t else ss.TabState(tab.path))
        return out, max(0, self.tabs.currentIndex())

    def restore_session(self, tabs: list[ss.TabState], current: int = 0) -> list[ss.TabState]:
        """있는 파일만 다시 연다(쪽·확대는 열린 뒤 맞춤). 없어진 파일 목록을 돌려준다."""
        ok, missing = ss.split_existing(tabs)
        current_path = tabs[current].path if 0 <= current < len(tabs) else None
        for t in ok:
            if self._tab_for_path(os.path.abspath(t.path)) is not None:
                continue
            self.open_paths([t.path])
            tab = self._tab_for_path(os.path.abspath(t.path))
            if tab is not None:
                self._restore_targets[tab.doc_id] = t
        if current_path is not None and (tab := self._tab_for_path(os.path.abspath(current_path))) is not None:
            self.tabs.setCurrentWidget(tab)
        return missing

    def _apply_tab_state(self, view: PageView, t: ss.TabState) -> None:
        if view.doc.doc_id not in self.tabs_by_doc:
            return
        if t.fit in (FIT_PAGE, FIT_WIDTH):
            view.set_fit(t.fit)
        else:
            view.set_zoom(t.zoom)
        view.go_to_page(t.page)

    def ask_restore(self, session: ss.Session) -> bool:
        """비정상 종료 뒤 복원 질문 — 시험에서는 바꿔 끼운다."""
        names = [os.path.basename(t.path) for t in session.tabs]
        shown = "\n".join(f"  · {n}" for n in names[:10])
        if len(names) > 10:
            shown += f"\n  … 외 {len(names) - 10}개"
        r = QMessageBox.question(
            self, APP_NAME,
            f"지난번에 {APP_NAME}가 정상적으로 끝나지 않았습니다.\n열려 있던 문서 {len(names)}개를 다시 열까요?\n\n"
            f"{shown}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        return r == QMessageBox.StandardButton.Yes

    def report_missing(self, missing: list[ss.TabState]) -> None:
        """복원하지 못한 파일 알림 — 시험에서는 바꿔 끼운다."""
        names = "\n".join(f"  · {os.path.basename(t.path)}" for t in missing[:10])
        more = f"\n  … 외 {len(missing) - 10}개" if len(missing) > 10 else ""
        self.last_notice = f"찾을 수 없는 파일 {len(missing)}개는 열지 않았습니다."
        box = QMessageBox(QMessageBox.Icon.Information, APP_NAME,
                          f"{self.last_notice} (옮겨졌거나 지워졌을 수 있습니다)\n\n{names}{more}", parent=self)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def receive_paths(self, paths: list[str]) -> None:
        """다른 실행(`bom-pdf.exe 파일.pdf`)이 넘긴 경로 — 끌어놓기와 같게: 사진은 사진 → PDF 창, 나머지는 탭."""
        images = [p for p in paths if itp.is_image(p)]
        others = [p for p in paths if not itp.is_image(p)]
        if others:
            self.open_paths(others)
        if images:
            self.open_image_dialog(images)

    # ------------------------------------------------------------------ 끌어놓기·종료
    def dragEnterEvent(self, ev) -> None:  # noqa: N802
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:  # noqa: N802
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        pdfs = [f for f in files if f.lower().endswith(".pdf")]
        images = [f for f in files if itp.is_image(f)]
        if pdfs or images:
            ev.acceptProposedAction()
        if pdfs:
            QTimer.singleShot(0, lambda: self.open_paths(pdfs))
        if images:  # 사진을 끌어 놓으면 사진 → PDF 창으로
            QTimer.singleShot(0, lambda: self.open_image_dialog(images))

    def confirm_quit_image_job(self) -> bool:
        """사진 → PDF 를 만드는 중에 끝낼지 — 시험에서는 바꿔 끼운다."""
        r = QMessageBox.question(self, APP_NAME, "사진 → PDF 를 만드는 중입니다. 취소하고 끝낼까요?\n"
                                 "(만들던 파일은 지우고, 원본 사진은 그대로입니다)")
        return r == QMessageBox.StandardButton.Yes

    def closeEvent(self, ev) -> None:  # noqa: N802
        if (self.image_dialog is not None and self.image_dialog.job_id is not None
                and not self.confirm_quit_image_job()):
            ev.ignore()
            return
        if self.page_editor is not None:
            if not self.page_editor.can_close():
                ev.ignore()
                return
            self.page_editor.force_close()
        if self.image_dialog is not None:
            self.image_dialog.force_close()
        if self.print_job is not None:
            self.print_job.cancel()
        self.closing.emit()
        self.service.shutdown()
        super().closeEvent(ev)
