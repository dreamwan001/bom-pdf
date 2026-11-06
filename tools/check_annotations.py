"""실물 PDF 로 주석 1-3 을 offscreen 확인 — 숫자만 출력한다(본문·선택 글자·메모를 출력하지 않음).

    .venv/Scripts/python.exe tools/check_annotations.py <pdf>

- 데이터 폴더(BOM_PDF_HOME)와 내보내기 폴더는 임시 폴더 — 원본 폴더·실제 %APPDATA% 에 쓰지 않고, 끝나면 지운다.
- 실제 PageView 에 마우스로 형광펜·밑줄(글자 끌기)·메모(누르기)·사각형(끌기)을 하나씩 만들고,
  메모 포함 PDF 로 내보낸 뒤 다시 열어 주석 수를 센다. 원본 sha256·mtime·폴더 목록 전후를 비교한다.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    # main 안에서 정한다 — 렌더 작업자(spawn)가 이 파일을 다시 import할 때 빈 폴더를 또 만들지 않도록
    os.environ["BOM_PDF_HOME"] = tempfile.mkdtemp(prefix="bom-pdf-check-home-")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    src = Path(sys.argv[1]).resolve()
    before = (_sha(src), src.stat().st_mtime_ns, sorted(os.listdir(src.parent)))

    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from bom_pdf.services import annotations as an
    from bom_pdf.services import document as ds
    from bom_pdf.services.recent import RecentDocuments
    from bom_pdf.ui.bridge import ServiceBridge
    from bom_pdf.ui.main_window import MainWindow
    from bom_pdf.ui.page_view import TOOL_BOX, TOOL_HIGHLIGHT, TOOL_NOTE, TOOL_UNDERLINE

    app = QApplication([sys.argv[0]])
    bridge = ServiceBridge()
    service = ds.DocumentService(bridge)
    service.start()
    win = MainWindow(service, bridge, RecentDocuments())
    win.resize(1200, 900)
    win.show()
    out_dir = Path(tempfile.mkdtemp(prefix="bom-pdf-check-out-"))
    win.documents_dir = lambda: str(out_dir)
    win.ask_export_path = lambda default: default
    win.ask_annotation = lambda *a: {"note": "check", "tags": ["t"], "color": "pink"}

    def wait(cond, timeout=60.0):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise TimeoutError
            app.processEvents()
            time.sleep(0.005)

    def settle(n=10):
        for _ in range(n):
            app.processEvents()

    result: dict = {"pages": None}
    try:
        t0 = time.perf_counter()
        win.open_paths([str(src)])
        wait(lambda: win.current_view() is not None)
        view = win.current_view()
        result["pages"] = view.doc.page_count
        wait(lambda: win.store_of(view) is not None)
        result["store_ready_s"] = round(time.perf_counter() - t0, 2)
        store = win.store_of(view)
        view.set_zoom(1.0)
        # 글자가 있는 첫 쪽에서 줄 두 개를 고른다
        page = lines = None
        for p in range(view.doc.page_count):
            wait(lambda p=p: win.text_layer.get(view.doc.doc_id, p) is not None)
            chars = win.text_layer.get(view.doc.doc_id, p)
            by_line: dict[int, list] = {}
            for c in chars:
                if not c[0].isspace():
                    by_line.setdefault(c[5], []).append(c)
            good = [v for v in by_line.values() if len(v) >= 6]
            if len(good) >= 2:
                page, lines = p, good
                break
        result["text_page"] = page
        view.go_to_page(page if page is not None else 0)
        settle()
        page = page if page is not None else 0

        def drag(a, b):
            pa = view.page_point_to_view(page, *a).toPoint()
            pb = view.page_point_to_view(page, *b).toPoint()
            QTest.mousePress(view.viewport(), Qt.MouseButton.LeftButton, pos=pa)
            QTest.mouseMove(view.viewport(), QPoint((pa.x() + pb.x()) // 2, (pa.y() + pb.y()) // 2))
            QTest.mouseMove(view.viewport(), pb)
            QTest.mouseRelease(view.viewport(), Qt.MouseButton.LeftButton, pos=pb)
            settle()

        def mid(c):
            return ((c[1] + c[3]) / 2, (c[2] + c[4]) / 2)

        if lines:
            view.reveal(page, [lines[0][0][1], lines[0][0][2], lines[0][5][3], lines[0][5][4]])
            settle()
            win.set_tool(TOOL_HIGHLIGHT)
            drag(mid(lines[0][0]), mid(lines[0][5]))
            view.reveal(page, [lines[1][0][1], lines[1][0][2], lines[1][5][3], lines[1][5][4]])
            settle()
            win.set_tool(TOOL_UNDERLINE)
            drag(mid(lines[1][0]), mid(lines[1][5]))
        w, h = view.doc.page_sizes[page]
        view.reveal(page, [w * 0.4, h * 0.4, w * 0.6, h * 0.6])
        settle()
        win.set_tool(TOOL_NOTE)
        p = view.page_point_to_view(page, w * 0.5, h * 0.5).toPoint()
        QTest.mouseClick(view.viewport(), Qt.MouseButton.LeftButton, pos=p)
        settle()
        win.set_tool(TOOL_BOX)
        drag((w * 0.42, h * 0.42), (w * 0.58, h * 0.48))
        result["created"] = {t: sum(1 for it in store.items if it["type"] == t) for t in an.TYPES}
        result["selected_chars"] = [len(it["text"]) for it in store.items if it["type"] in an.TEXT_TYPES]
        result["sidecar_items_on_disk"] = len(json.loads(store.path.read_text(encoding="utf-8"))["items"])

        t1 = time.perf_counter()
        win.act_export.trigger()
        wait(lambda: win.last_export is not None, timeout=300)
        out, r = win.last_export
        result["export_s"] = round(time.perf_counter() - t1, 2)
        result["export"] = {k: r.get(k) for k in ("status", "added", "before", "after", "by_type")}
        import pymupdf

        chk = pymupdf.open(out)
        result["reopen_pages"] = chk.page_count
        result["reopen_annots"] = sum(1 for pg in chk for _ in pg.annots())
        chk.close()
        result["export_in_source_folder"] = Path(out).parent == src.parent
    finally:
        win.close()
        service.shutdown()
        # 실물 문서의 선택 글자가 든 사이드카·산출물을 남기지 않는다
        shutil.rmtree(out_dir, ignore_errors=True)
        shutil.rmtree(os.environ["BOM_PDF_HOME"], ignore_errors=True)
    after = (_sha(src), src.stat().st_mtime_ns, sorted(os.listdir(src.parent)))
    result["source_unchanged"] = after == before
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["source_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
