"""페이지 편집 결과·용량 압축본 만들기 (1-4) — 작업 프로세스(engine.jobs) 안에서만 쓴다(PyMuPDF).

- 원본은 경로로 **다시 열어** 읽기만 한다. 결과는 항상 새 파일: 산출물 옆 임시 파일(`TMP_SUFFIX`)에 쓰고
  다시 열어 쪽 수·회전을 확인한 뒤 replace. 여러 산출물(분할)은 모두 확인한 뒤에야 한꺼번에 replace.
- 산출물 경로가 원본 중 하나와 같으면 거절(refused), 원본 sha256 이 연 때와 다르면 거절(changed).
- 책갈피(TOC)·쪽 안 링크는 남은 쪽으로 다시 잇고, 지운 쪽을 가리키는 것은 뺀다.
- 쪽 내용·경로·제목은 결과에 넣지 않는다(수와 상태만).
"""
from __future__ import annotations

import os
import time

import pymupdf

from bom_pdf import storage
from bom_pdf.engine import pdf
from bom_pdf.engine.exportstatus import (
    EXPORT_CHANGED, EXPORT_ERROR, EXPORT_OK, EXPORT_REFUSED, JOB_NOT_SMALLER, TMP_SUFFIX,
)
from bom_pdf.paths import long_path, replace_with_retry

# 압축 방식: 이미지 해상도·JPEG 품질 (None = 이미지는 그대로, 구조 정리만)
COMPRESS_PRESETS = {
    "submit": {"dpi": 150, "quality": 75},   # 제출용(전자소송 등) — 기본
    "small": {"dpi": 100, "quality": 60},    # 최소 용량 — 화면으로 읽을 정도
    "clean": {"dpi": None, "quality": None},  # 화질 그대로 — 안 쓰는 객체 정리·압축·글꼴 부분 집합만
}


def _same(a: str, b: str) -> bool:
    a, b = long_path(a), long_path(b)
    try:
        if os.path.exists(a) and os.path.exists(b):
            return os.path.samefile(a, b)
    except OSError:
        pass
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _unlink(p: str) -> None:
    try:
        os.unlink(long_path(p))
    except OSError:
        pass


def _open_source(path: str, password: str | None) -> pymupdf.Document | None:
    data = storage.read_small_remote(path)  # 원격 작은 파일은 한 번에 순서대로(O4)
    doc = pymupdf.open(stream=data, filetype="pdf") if data else pymupdf.open(long_path(path), filetype="pdf")
    if doc.needs_pass and not doc.authenticate(password or ""):
        doc.close()
        return None
    return doc


# --------------------------------------------------------------------------- 링크·책갈피


def _copy_links(src_page: pymupdf.Page, out_page: pymupdf.Page, si: int, first: dict) -> int:
    """원본 쪽의 링크를 새 쪽에 다시 넣는다. 문서 안 링크는 남은 쪽으로 다시 잇고, 지운 쪽이면 뺀다."""
    n = 0
    for lnk in src_page.get_links():
        kind = lnk.get("kind")
        d = {k: v for k, v in lnk.items() if k not in ("xref", "id")}
        if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
            target = lnk.get("page", -1)
            new = first.get((si, target)) if isinstance(target, int) and target >= 0 else None
            if new is None:
                continue
            d = {"kind": pymupdf.LINK_GOTO, "from": lnk["from"], "page": new,
                 "to": lnk.get("to", pymupdf.Point(0, 0)), "zoom": lnk.get("zoom", 0)}
        elif kind not in (pymupdf.LINK_URI, pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR):
            continue
        try:
            out_page.insert_link(d)
            n += 1
        except Exception:
            pass  # 이상한 링크 하나로 저장 전체를 실패시키지 않는다
    return n


def _remap_toc(docs: list, order: list[int], first: dict) -> list[list]:
    """원본들의 책갈피를 새 쪽 번호로. 지운 쪽 항목은 빼고, 빠진 항목의 자식은 남은 조상 바로 아래로 올린다.

    원본이 여럿이면(병합) 원본마다 처음 나오는 차례로 잇는다. 같은 쪽이 두 번 나오면 첫 번째를 가리킨다.
    """
    out: list[list] = []
    for si in order:
        try:
            toc = docs[si].get_toc(simple=True)
        except Exception:
            continue
        stack: list[tuple[int, bool]] = []  # (원래 수준, 남김?) — 지금 항목의 조상들
        for lvl, title, page in toc:
            lvl = int(lvl)
            while stack and stack[-1][0] >= lvl:
                stack.pop()
            new = first.get((si, int(page) - 1))
            if new is not None:
                out.append([1 + sum(1 for _l, kept in stack if kept), str(title or ""), new + 1])
            stack.append((lvl, new is not None))
    return out


# --------------------------------------------------------------------------- 페이지 편집 결과


def build(job: dict, progress=None) -> dict:
    """job = {"sources": [{"path", "password", "sha256"}],
              "outputs": [{"path", "pages": [[원본 번호, 쪽(0부터), 추가 회전]]}],
              "annots": [원본마다 주석 항목 목록 | None] (선택), "toc": bool}
    결과 {"status", "outputs": [{"pages", "toc", "links", "annots", "bytes"}], "seconds"}.
    """
    pdf._quiet_mupdf()
    t0 = time.perf_counter()
    sources, outputs = job["sources"], job["outputs"]
    annots = job.get("annots") or [None] * len(sources)
    if not outputs or any(not o["pages"] for o in outputs):
        return {"status": EXPORT_ERROR, "reason": "empty"}
    paths = [o["path"] for o in outputs]
    if len({os.path.normcase(os.path.abspath(p)) for p in paths}) != len(paths):
        return {"status": EXPORT_ERROR, "reason": "duplicate_output"}
    for o in outputs:
        if any(_same(o["path"], s["path"]) for s in sources):
            return {"status": EXPORT_REFUSED}
    for i, s in enumerate(sources):
        if not storage.source_unchanged(s["path"], s.get("sha256")):  # 해시 캐시가 맞으면 다시 읽지 않는다(O4)
            return {"status": EXPORT_CHANGED, "source": i}

    docs: list = []
    tmps: list[str] = []
    try:
        for s in sources:
            d = _open_source(s["path"], s.get("password"))
            if d is None:
                return {"status": EXPORT_ERROR, "reason": "password"}
            docs.append(d)
        total = sum(len(o["pages"]) for o in outputs)
        done = 0
        results = []
        for o in outputs:
            out = pymupdf.open()
            first: dict = {}
            expect_rot = []
            for si, p, _rot in o["pages"]:
                out.insert_pdf(docs[si], from_page=p, to_page=p, links=False, annots=True, final=False)
                first.setdefault((si, p), out.page_count - 1)
            links = added = 0
            for i, (si, p, rot) in enumerate(o["pages"]):
                page = out[i]
                links += _copy_links(docs[si][p], page, si, first)
                for it in annots[si] or ():
                    if it["page"] == p:
                        pdf._add_annot(page, it)  # 1-3 내보내기와 같은 코드 — 원래 회전 기준 좌표
                        added += 1
                r = (page.rotation + int(rot)) % 360
                if r != page.rotation:
                    page.set_rotation(r)
                expect_rot.append(r)
                done += 1
                if progress is not None and (done % 20 == 0 or done == total):
                    progress(done, total)
            order = list(dict.fromkeys(si for si, _p, _r in o["pages"]))
            toc = _remap_toc(docs, order, first) if job.get("toc", True) else []
            if toc:
                try:
                    out.set_toc(toc)
                except Exception:
                    toc = []
            meta = docs[order[0]].metadata or {}
            out.set_metadata({k: meta.get(k) or "" for k in ("title", "author", "subject", "keywords")}
                             | {"producer": "bom-pdf", "creator": "bom-pdf"})
            tmp = long_path(o["path"] + TMP_SUFFIX)
            tmps.append(tmp)
            out.save(tmp, garbage=3, deflate=True)
            out.close()
            chk = pymupdf.open(tmp, filetype="pdf")
            ok = chk.page_count == len(o["pages"]) and [pg.rotation for pg in chk] == expect_rot
            chk.close()
            if not ok:
                raise _VerifyError()
            results.append({"pages": len(o["pages"]), "toc": len(toc), "links": links, "annots": added,
                            "bytes": os.path.getsize(tmp)})
        for o, tmp in zip(outputs, tmps):
            replace_with_retry(tmp, long_path(o["path"]))
        tmps = []
        return {"status": EXPORT_OK, "outputs": results, "seconds": round(time.perf_counter() - t0, 3)}
    except _VerifyError:
        return {"status": EXPORT_ERROR, "reason": "verify"}
    finally:
        for d in docs:
            d.close()
        for t in tmps:
            _unlink(t)


class _VerifyError(Exception):
    pass


# --------------------------------------------------------------------------- 용량 압축본


def _rewrite_options(dpi: int, quality: int):
    """컬러·회색 이미지를 dpi 로 줄이고 JPEG(quality)로. 흑백(1비트) 스캔은 건드리지 않는다(글자가 뭉개지지 않게).

    `rewrite_images(dpi_target=…)` 기본값은 평균(AVERAGE) 축소인데, MuPDF 는 이것을 **정수 배로만** 줄인다 —
    200DPI 스캔을 150DPI 로 하면 배율이 2 미만이라 줄지 않는다(891쪽 합성본에서 1.3%만 줄어 확인).
    그래서 아무 배율이나 되는 BICUBIC 으로 옵션을 직접 만든다.
    """
    from pymupdf import mupdf

    o = mupdf.PdfImageRewriterOptions()
    for kind in ("color_lossy", "color_lossless", "gray_lossy", "gray_lossless"):
        setattr(o, f"{kind}_image_recompress_method", mupdf.FZ_RECOMPRESS_JPEG)
        setattr(o, f"{kind}_image_recompress_quality", str(quality))
        setattr(o, f"{kind}_image_subsample_method", mupdf.FZ_SUBSAMPLE_BICUBIC)
        setattr(o, f"{kind}_image_subsample_to", dpi)
        setattr(o, f"{kind}_image_subsample_threshold", dpi + dpi // 10)  # 조금 넘는 것은 그대로(다시 줄이면 손해)
    return o


def compress(path: str, password: str | None, expect_sha256: str | None, out_path: str, preset: str,
             progress=None) -> dict:
    """원본 → 용량을 줄인 새 파일. 결과 {"status", "before", "after", "pages", "seconds"}.

    결과가 원본보다 작지 않으면 파일을 남기지 않고 JOB_NOT_SMALLER.
    """
    pdf._quiet_mupdf()
    t0 = time.perf_counter()
    cfg = COMPRESS_PRESETS.get(preset)
    if cfg is None:
        return {"status": EXPORT_ERROR, "reason": "preset"}
    if _same(out_path, path):
        return {"status": EXPORT_REFUSED}
    if not storage.source_unchanged(path, expect_sha256):
        return {"status": EXPORT_CHANGED}
    before = os.path.getsize(long_path(path))
    tmp = long_path(out_path + TMP_SUFFIX)
    doc = _open_source(path, password)
    if doc is None:
        return {"status": EXPORT_ERROR, "reason": "password"}
    try:
        n = doc.page_count
        if progress is not None:
            progress(0, 3)
        if cfg["dpi"]:
            doc.rewrite_images(options=_rewrite_options(cfg["dpi"], cfg["quality"]))
        if progress is not None:
            progress(1, 3)
        try:
            doc.subset_fonts()
        except Exception:
            pass  # 부분 집합을 못 만드는 글꼴은 그대로 둔다
        if progress is not None:
            progress(2, 3)
        doc.save(tmp, garbage=4, deflate=True, deflate_images=True, deflate_fonts=True, use_objstms=1,
                 encryption=pymupdf.PDF_ENCRYPT_KEEP)
    except BaseException:
        doc.close()
        _unlink(tmp)
        raise
    doc.close()
    try:
        after = os.path.getsize(tmp)
        chk = _open_source(tmp, password)
        pages_ok = chk is not None and chk.page_count == n
        if chk is not None:
            chk.close()
        if not pages_ok:
            _unlink(tmp)
            return {"status": EXPORT_ERROR, "reason": "verify"}
        secs = round(time.perf_counter() - t0, 3)
        if after >= before:
            _unlink(tmp)
            return {"status": JOB_NOT_SMALLER, "before": before, "after": after, "pages": n, "seconds": secs}
        replace_with_retry(tmp, long_path(out_path))
    except BaseException:
        _unlink(tmp)
        raise
    if progress is not None:
        progress(3, 3)
    return {"status": EXPORT_OK, "before": before, "after": after, "pages": n, "seconds": secs}
