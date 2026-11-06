"""포장본 점검 (1-6): `bom-pdf.exe --self-test <보고.json> <pdf> [사진 …]`

창을 띄우지 않고 포장본 안에서 다음이 되는지 확인한다(빌드 스크립트가 합성 자료로 부른다).
1. 렌더 작업자 프로세스(spawn, D3): PDF 열기 → 첫 쪽 렌더
2. 작업 프로세스(spawn, 1-4/1-5): 사진 정보 읽기(Pillow·pillow-heif) → 사진 → PDF(문서 사진 모드 = OpenCV)
3. 이 프로세스(UI 쪽)는 PyMuPDF·Pillow·OpenCV 를 불러오지 않았는가

데이터 폴더는 임시 폴더(사용자 `%APPDATA%` 를 건드리지 않음), 산출 PDF 도 임시 폴더. 보고에는 수·시간·상태만 적는다.
"""
from __future__ import annotations

import json
import os
import queue
import shutil
import sys
import tempfile
import time

HEAVY = ("pymupdf", "fitz", "PIL", "cv2", "numpy", "pillow_heif", "pi_heif")


class _Sink:
    def __init__(self):
        self.q: queue.Queue = queue.Queue()

    def __call__(self, ev) -> None:
        self.q.put(ev)

    def wait(self, pred, timeout: float = 60.0):
        end = time.monotonic() + timeout
        while True:
            left = end - time.monotonic()
            if left <= 0:
                raise TimeoutError
            try:
                ev = self.q.get(timeout=left)
            except queue.Empty:
                continue
            if pred(ev):
                return ev


def run(pdf_path: str, photos: list[str]) -> dict:
    from bom_pdf.engine import jobs
    from bom_pdf.engine import worker as eng
    from bom_pdf.services import document as ds
    from bom_pdf.services import imagetopdf as itp

    rep: dict = {"frozen": bool(getattr(sys, "frozen", False)), "ok": False}
    home = tempfile.mkdtemp(prefix="bom-pdf-selftest-")
    sink = _Sink()
    service = ds.DocumentService(sink, data_dir=__import__("pathlib").Path(home))
    try:
        t0 = time.perf_counter()
        service.start()
        doc_id = service.open(pdf_path)
        ev = sink.wait(lambda e: isinstance(e, (ds.DocumentOpened, ds.DocumentOpenFailed)))
        rep["open"] = "ok" if isinstance(ev, ds.DocumentOpened) else getattr(ev, "kind", "fail")
        if isinstance(ev, ds.DocumentOpened):
            rep["pages"] = ev.doc.page_count
            service.request_pages(doc_id, 1.0, [0], 0)
            img = sink.wait(lambda e: isinstance(e, (eng.PageImage, eng.PageError)))
            rep["render"] = "ok" if isinstance(img, eng.PageImage) else "error"
        rep["first_page_ms"] = round((time.perf_counter() - t0) * 1000)

        if photos:
            t1 = time.perf_counter()
            service.scan_images(photos)
            done = sink.wait(lambda e: isinstance(e, jobs.JobDone))
            res = done.result
            rep["scan"] = res.get("status")
            rep["scan_read"] = sum(1 for r in res.get("items", []) if r.get("width"))
            out = os.path.join(home, "selftest_photos.pdf")
            plist = [itp.Photo(p, doc_mode=True) for p in photos]
            service.images_to_pdf(itp.plan(plist, itp.Options(), out, [], None))
            done = sink.wait(lambda e: isinstance(e, jobs.JobDone), timeout=120)
            r = done.result
            rep["image_pdf"] = r.get("status")
            rep["image_pdf_pages"] = r.get("pages")
            rep["doc_mode_fixed"] = r.get("doc_fixed")
            rep["doc_mode_kept"] = r.get("doc_kept")
            rep["image_failed"] = r.get("failed")
            rep["jobs_ms"] = round((time.perf_counter() - t1) * 1000)
        rep["ui_process_heavy_modules"] = sorted(m for m in sys.modules if m.split(".")[0] in HEAVY)
        rep["ok"] = (rep.get("render") == "ok" and not rep["ui_process_heavy_modules"]
                     and (not photos or (rep.get("scan") == "ok" and rep.get("image_pdf") == "ok"
                                         and rep.get("scan_read") == len(photos) and not rep.get("image_failed"))))
    except Exception as e:  # 보고만 하고 끝낸다
        rep["error"] = type(e).__name__
    finally:
        service.shutdown()
        shutil.rmtree(home, ignore_errors=True)
    return rep


def tiny_pdf() -> bytes:
    """한 쪽짜리 합성 PDF(파란 사각형) — 이 프로세스는 PyMuPDF 를 쓰지 않으므로 직접 짠다."""
    content = b"0.2 0.3 0.8 rg 20 20 160 160 re f"
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def tiny_png(w: int = 160, h: int = 120) -> bytes:
    """어두운 바탕 위 흰 종이 모양의 합성 사진(PNG) — zlib 만 쓴다."""
    import struct
    import zlib

    rows = []
    for y in range(h):
        row = bytearray(b"\x00")
        for x in range(w):
            paper = w // 5 <= x < w * 4 // 5 and h // 6 <= y < h * 5 // 6
            row += b"\xf0\xf0\xf0" if paper else b"\x30\x34\x38"
        rows.append(bytes(row))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))


def run_builtin() -> dict:
    """합성 PDF·사진을 임시 폴더에 만들어 점검한다(도움말 › 환경 점검). 사용자 파일은 쓰지 않는다."""
    tmp = tempfile.mkdtemp(prefix="bom-pdf-check-")
    try:
        pdf_path = os.path.join(tmp, "check.pdf")
        png_path = os.path.join(tmp, "check.png")
        with open(pdf_path, "wb") as f:
            f.write(tiny_pdf())
        with open(png_path, "wb") as f:
            f.write(tiny_png())
        return run(pdf_path, [png_path])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        if sys.stderr is not None:  # 창 EXE 에서는 None
            print("usage: --self-test <report.json> <pdf> [photo ...]", file=sys.stderr)
        return 2
    report, pdf_path, photos = argv[0], argv[1], argv[2:]
    rep = run(os.path.abspath(pdf_path), [os.path.abspath(p) for p in photos])
    with open(report, "w", encoding="utf-8") as f:
        json.dump(rep, f, ensure_ascii=False, indent=1)
    return 0 if rep["ok"] else 1
