"""PyMuPDF 얇은 래퍼 — 렌더 작업자 프로세스 안에서만 쓴다 (D3).

UI 프로세스는 이 모듈을 import하지 않는다. 원본은 읽기 전용으로만 연다(저장 함수 없음).
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import pymupdf

from bom_pdf import storage
from bom_pdf.engine.textnorm import fold as _fold
from bom_pdf.paths import long_path, replace_with_retry

# 열기 결과 종류 (D11)
OK = "ok"
NEEDS_PASSWORD = "needs_password"
WRONG_PASSWORD = "wrong_password"
CANNOT_OPEN = "cannot_open"

# 열기 실패 사유
REASON_NOT_FOUND = "not_found"
REASON_NO_PERMISSION = "no_permission"
REASON_EMPTY = "empty"
REASON_DAMAGED = "damaged"  # PDF 형식이 아니거나 복구 불가
REASON_NO_PAGES = "no_pages"

_quiet = False


def _quiet_mupdf() -> None:
    global _quiet
    if not _quiet:
        # MuPDF 경고를 표준 오류로 내보내지 않는다 (pythonw 에서는 stderr 가 없다)
        pymupdf.TOOLS.mupdf_display_errors(False)
        pymupdf.TOOLS.mupdf_display_warnings(False)
        _quiet = True


@dataclass
class OpenResult:
    status: str
    doc: pymupdf.Document | None = None
    reason: str = ""
    repaired: bool = False
    page_sizes: list[tuple[float, float]] | None = None  # 회전 반영한 (폭, 높이) pt
    embedded_annots: int = 0  # 원본에 이미 든 주석 수(링크·양식 칸·팝업 제외) — 표시만 되고 편집 안 됨


def open_pdf(path: str, password: str | None = None, data: bytes | None = None) -> OpenResult:
    """data 를 주면(O4 원격 파일을 메모리로 읽은 것) 그것으로 연다. 긴 경로(260자↑)는 paths.long_path 형식으로."""
    _quiet_mupdf()
    path = long_path(path)
    if data is None and not os.path.isfile(path):  # filetype 을 주면 MuPDF 가 없는 파일도 일반 오류로 낸다
        return OpenResult(CANNOT_OPEN, reason=REASON_NOT_FOUND)
    if data is not None and not data:
        return OpenResult(CANNOT_OPEN, reason=REASON_EMPTY)
    try:
        doc = pymupdf.open(stream=data, filetype="pdf") if data is not None else pymupdf.open(path, filetype="pdf")
    except FileNotFoundError:
        return OpenResult(CANNOT_OPEN, reason=REASON_NOT_FOUND)
    except PermissionError:
        return OpenResult(CANNOT_OPEN, reason=REASON_NO_PERMISSION)
    except pymupdf.EmptyFileError:
        return OpenResult(CANNOT_OPEN, reason=REASON_EMPTY)
    except Exception:  # FileDataError, MuPDF RuntimeError 등 — 형식 아님·복구 불가
        return OpenResult(CANNOT_OPEN, reason=REASON_DAMAGED)

    if doc.needs_pass:
        if not password:
            doc.close()
            return OpenResult(NEEDS_PASSWORD)
        if not doc.authenticate(password):
            doc.close()
            return OpenResult(WRONG_PASSWORD)
    try:
        n = doc.page_count
        sizes = []
        embedded = 0
        for i in range(n):
            page = doc[i]
            r = page.rect
            sizes.append((r.width, r.height))
            embedded += _count_page_annots(page)
    except Exception:
        doc.close()
        return OpenResult(CANNOT_OPEN, reason=REASON_DAMAGED)
    if n == 0:
        doc.close()
        return OpenResult(CANNOT_OPEN, reason=REASON_NO_PAGES)
    return OpenResult(OK, doc=doc, repaired=bool(doc.is_repaired), page_sizes=sizes, embedded_annots=embedded)


def _count_page_annots(page: pymupdf.Page) -> int:
    """그 쪽에 원래 든 주석 수 — 팝업(메모 창)은 짝인 주석과 하나로 센다. 읽다 틀리면 0(열기를 막지 않음)."""
    try:
        if page.first_annot is None:
            return 0
        return sum(1 for a in page.annots() if a.type[0] != pymupdf.PDF_ANNOT_POPUP)
    except Exception:
        return 0


def render_page(doc: pymupdf.Document, page: int, scale: float) -> pymupdf.Pixmap:
    """RGB(알파 없음) 픽스맵. 호출자가 samples_mv 를 바로 보낸다."""
    return doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)


def display_list(doc: pymupdf.Document, page: int) -> pymupdf.DisplayList:
    """쪽 내용을 한 번 해석해 둔 것 — 같은 쪽의 타일 여러 장을 그릴 때 다시 해석하지 않는다(원본 주석 포함)."""
    return doc[page].get_displaylist()


def render_tile(dl: pymupdf.DisplayList, scale: float, tx: int, ty: int, tile_px: int) -> pymupdf.Pixmap:
    """확대한 쪽의 (tx, ty) 타일(장치 px 로 tile_px 칸) — 쪽 전체 렌더의 같은 자리와 같은 화소.

    clip 은 회전 반영 쪽 좌표(pt). 결과 pixmap 의 (x, y) 가 쪽 전체 그림 안의 왼쪽 위 장치 px 이다
    (실측: 글자 쪽은 쪽 전체 렌더와 바이트까지 같음, 스캔 그림은 확대 보간 위상 차이로 평균 1/255 미만 차이).
    """
    k = tile_px / scale
    clip = pymupdf.Rect(tx * k, ty * k, (tx + 1) * k, (ty + 1) * k) & dl.rect
    return dl.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False, clip=clip)


TRIM_EVERY = 16  # 렌더 몇 번마다 MuPDF 캐시를 비울지
_renders = 0


def trim_store() -> None:
    """MuPDF 캐시(기본 상한 256MB)를 주기적으로 비운다 — 연속 스크롤은 재사용이 적다.

    PyMuPDF 2.1 의 `TOOLS.store_size()`·`store_maxsize()`는 구현이 비어 None 을 돌려주므로
    크기 기준이 아니라 횟수 기준으로 줄인다.
    """
    global _renders
    _renders += 1
    if _renders % TRIM_EVERY:
        return
    try:
        pymupdf.TOOLS.store_shrink(100)
    except Exception:
        pass  # 캐시 정리 실패로 작업자가 죽으면 안 된다


def page_text(doc: pymupdf.Document, page: int) -> str:
    return doc[page].get_text("text")


# --------------------------------------------------------------------------- 책갈피(TOC)


def outline(doc: pymupdf.Document) -> list[tuple[int, str, int]]:
    """[(수준 1부터, 제목, 쪽 0부터 — 없으면 -1)]. 손상된 목차는 빈 목록."""
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return []
    n = doc.page_count
    out = []
    for item in toc:
        lvl, title, page = int(item[0]), str(item[1] or ""), int(item[2]) - 1
        out.append((max(1, lvl), title, page if 0 <= page < n else -1))
    return out


# --------------------------------------------------------------------------- 검색
#
# 쪽의 글자를 글자 단위 상자(rawdict)와 함께 읽어 정규화한 문자열에서 찾고, 찾은 범위를 원래 글자
# 상자로 되돌려 줄마다 합친 사각형을 돌려준다.
#   - 대소문자 무시(글자 하나씩 lower — 길이가 바뀌는 글자는 그대로)
#   - 기본: 공백·줄바꿈 연속은 공백 하나로 본다
#   - ignore_space: 공백·줄바꿈을 모두 지우고 비교한다(스캔 OCR 의 끼어든 줄바꿈 대응)
# 한계는 docs/worklog/2026-11-04-phase1-2-*.md 에 적는다(글자 순서는 PDF 내용 순서, 자모 분리 NFD 미정규화 등).

# 그림 블록은 읽지 않고, 합자(ﬁ 등)는 낱글자로 풀어 "fi" 검색에 걸리게 한다
_SEARCH_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_IMAGES & ~pymupdf.TEXT_PRESERVE_LIGATURES
CONTEXT_CHARS = 24


def _page_chars(page: pymupdf.Page) -> tuple[list[str], list]:
    """글자 목록과 같은 길이의 (bbox, 줄 번호) 목록. 줄 끝마다 '\\n'(상자 None)을 넣는다."""
    chars: list[str] = []
    boxes: list = []
    line_no = 0
    for b in page.get_text("rawdict", flags=_SEARCH_FLAGS)["blocks"]:
        if b.get("type", 0) != 0:
            continue
        for ln in b["lines"]:
            for sp in ln["spans"]:
                for ch in sp["chars"]:
                    for c in ch["c"] or " ":  # 여러 코드포인트 글자도 같은 상자를 나눠 쓴다
                        chars.append(c)
                        boxes.append((ch["bbox"], line_no))
            chars.append("\n")
            boxes.append(None)
            line_no += 1
    return chars, boxes


def _squash(s: str) -> str:
    return " ".join(s.split())


def search_page(doc: pymupdf.Document, pno: int, needle: str, ignore_space: bool,
                limit: int) -> list[tuple[list[tuple[float, float, float, float]], str, str, str]]:
    """한 쪽에서 needle(textnorm.normalize_query 결과)을 찾는다. [(줄별 사각형 pt, 앞 문맥, 일치, 뒤 문맥)].

    사각형은 회전이 반영된 쪽 좌표(page.rect 기준, 렌더한 그림과 같은 방향)다.
    """
    if not needle or limit <= 0:
        return []
    page = doc[pno]
    chars, boxes = _page_chars(page)
    norm: list[str] = []
    idx: list[int] = []
    prev_space = True  # 맨 앞 공백은 버린다
    for i, c in enumerate(chars):
        if c.isspace():
            if ignore_space or prev_space:
                continue
            norm.append(" ")
            idx.append(i)
            prev_space = True
            continue
        prev_space = False
        norm.append(_fold(c))
        idx.append(i)
    text = "".join(norm)
    rot = page.rotation_matrix
    rotated = page.rotation != 0
    hits = []
    start = 0
    while len(hits) < limit:
        s = text.find(needle, start)
        if s < 0:
            break
        e = s + len(needle)
        start = e
        a, z = idx[s], idx[e - 1]
        lines: dict[int, list[float]] = {}
        for k in range(a, z + 1):
            bx = boxes[k]
            if bx is None or chars[k].isspace():
                continue
            (x0, y0, x1, y1), ln = bx
            r = lines.get(ln)
            if r is None:
                lines[ln] = [x0, y0, x1, y1]
            else:
                r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
        rects = []
        for r in lines.values():
            if rotated:
                rr = pymupdf.Rect(r) * rot
                r = [rr.x0, rr.y0, rr.x1, rr.y1]
            rects.append((round(r[0], 2), round(r[1], 2), round(r[2], 2), round(r[3], 2)))
        pre = "".join(chars[max(0, a - CONTEXT_CHARS * 2):a])
        post = "".join(chars[z + 1:z + 1 + CONTEXT_CHARS * 2])
        before = _squash(pre)[-CONTEXT_CHARS:] + (" " if pre[-1:].isspace() else "")
        after = (" " if post[:1].isspace() else "") + _squash(post)[:CONTEXT_CHARS]
        hits.append((rects, before.lstrip(), _squash("".join(chars[a:z + 1])), after.rstrip()))
    return hits


# --------------------------------------------------------------------------- 글자 상자(선택용)


def page_chars(doc: pymupdf.Document, pno: int) -> list[tuple[str, float, float, float, float, int]]:
    """[(글자, x0, y0, x1, y1, 줄 번호)] — 회전 반영 쪽 좌표(검색 강조와 같은 방향), PDF 내용 순서.

    글자 선택(복사·형광펜·밑줄)에 쓴다. 검색과 같은 rawdict 추출(그림 블록 제외·합자 풀기)을 쓴다.
    """
    page = doc[pno]
    chars, boxes = _page_chars(page)
    rot = page.rotation_matrix if page.rotation else None
    out = []
    for c, bx in zip(chars, boxes):
        if bx is None:
            continue
        (x0, y0, x1, y1), ln = bx
        if rot is not None:
            r = pymupdf.Rect(x0, y0, x1, y1) * rot
            x0, y0, x1, y1 = r.x0, r.y0, r.x1, r.y1
        out.append((c, round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2), ln))
    return out


# --------------------------------------------------------------------------- 메모 포함 PDF 내보내기 (D1·D4)
#
# 원본을 다시 읽기 전용으로 열어 표준 PDF 주석(Highlight·Underline·Text·Square)을 더한 뒤 **새 파일**로 저장한다.
# 메모 글은 주석의 /Contents(유니코드 문자열)에 들어가 다른 뷰어가 자기 글꼴로 팝업에 보여 준다 — 쪽 위에
# 한글 글자를 그리지 않으므로 글꼴을 넣을 일이 없다(D10 은 쪽 위에 글자를 그릴 때 적용).
# 원본 경로에는 쓰지 않고, 같은 폴더 임시 파일 → 다시 열어 주석 수 확인 → replace 로 끝낸다.

from bom_pdf.engine.exportstatus import (  # noqa: E402  UI 도 쓰는 상수 — PyMuPDF 없는 모듈
    EXPORT_CHANGED, EXPORT_ERROR, EXPORT_OK, EXPORT_REFUSED, TMP_SUFFIX,
)
EXPORT_AUTHOR = "bom-pdf"
ANNOT_TYPE_NAMES = {"highlight": "Highlight", "underline": "Underline", "note": "Text", "box": "Square"}


def _count_annots(doc: pymupdf.Document) -> dict[str, int]:
    out: dict[str, int] = {}
    for page in doc:
        for a in page.annots():
            name = a.type[1]
            out[name] = out.get(name, 0) + 1
    return out


def _content(item: dict) -> str:
    note = item.get("note") or ""
    tags = item.get("tags") or []
    if tags:
        note = (note + "\n" if note else "") + "태그: " + ", ".join(tags)
    return note


def _add_annot(page: pymupdf.Page, item: dict) -> None:
    der = page.derotation_matrix  # 회전 반영 좌표 → 쪽 원래 좌표
    rgb = tuple(item["color"])
    kind = item["type"]
    if kind in ("highlight", "underline"):
        rects = [pymupdf.Rect(q) * der for q in item["quads"]]
        a = page.add_highlight_annot(rects) if kind == "highlight" else page.add_underline_annot(rects)
    elif kind == "note":
        x0, y0, _x1, _y1 = item["rect"]
        a = page.add_text_annot(pymupdf.Point(x0, y0) * der, _content(item), icon="Note")
    elif kind == "box":
        a = page.add_rect_annot(pymupdf.Rect(item["rect"]) * der)
        a.set_border(width=1.5)
    else:
        raise ValueError(kind)
    a.set_colors(stroke=rgb)
    a.set_info(content=_content(item), title=EXPORT_AUTHOR, subject=item.get("label", kind))
    a.update()


def export_annotated(path: str, password: str | None, out_path: str, items: list[dict],
                     expect_sha256: str | None) -> dict:
    """원본 + 주석 → out_path. 결과 {"status", "added", "skipped", "before", "after", "by_type"}."""
    _quiet_mupdf()
    path, out_path = long_path(path), long_path(out_path)
    if os.path.exists(out_path) and os.path.samefile(out_path, path):
        return {"status": EXPORT_REFUSED}
    if os.path.normcase(os.path.abspath(out_path)) == os.path.normcase(os.path.abspath(path)):
        return {"status": EXPORT_REFUSED}
    if not storage.source_unchanged(path, expect_sha256):  # 해시 캐시가 맞으면 다시 읽지 않는다(O4)
        return {"status": EXPORT_CHANGED}
    data = storage.read_small_remote(path)  # 원격 작은 파일은 한 번에 순서대로(O4)
    src = pymupdf.open(stream=data, filetype="pdf") if data else pymupdf.open(path, filetype="pdf")
    tmp = out_path + TMP_SUFFIX
    try:
        if src.needs_pass and not src.authenticate(password or ""):
            return {"status": EXPORT_ERROR, "reason": "password"}
        before = _count_annots(src)
        added = skipped = 0
        n = src.page_count
        for it in items:
            if not 0 <= it["page"] < n:
                skipped += 1
                continue
            _add_annot(src[it["page"]], it)
            added += 1
        src.save(tmp, garbage=1, deflate=True, encryption=pymupdf.PDF_ENCRYPT_KEEP)
    except BaseException:
        src.close()
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    src.close()
    try:
        chk = pymupdf.open(tmp, filetype="pdf")
        if chk.needs_pass:
            chk.authenticate(password or "")
        after = _count_annots(chk)
        pages_ok = chk.page_count == n
        chk.close()
        if not pages_ok or sum(after.values()) != sum(before.values()) + added:
            os.unlink(tmp)
            return {"status": EXPORT_ERROR, "reason": "verify"}
        replace_with_retry(tmp, out_path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"status": EXPORT_OK, "added": added, "skipped": skipped,
            "before": sum(before.values()), "after": sum(after.values()), "by_type": after}
