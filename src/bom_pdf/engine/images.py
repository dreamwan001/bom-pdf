"""사진 → PDF (1-5) — 작업 프로세스(engine.jobs) 안에서만 쓴다(Pillow·pillow-heif·OpenCV·PyMuPDF).

- 원본 사진은 읽기만 한다. 화소를 새로 인코딩해 넣으므로 EXIF(GPS·기기·촬영시각)·XMP·ICC 는 결과에 들어가지 않는다(D9).
  EXIF 회전은 화소에 적용하고, 색 프로필(ICC)이 있으면 sRGB 로 바꾼 뒤 프로필은 버린다.
- 문서 사진 모드: OpenCV 로 가장 큰 사각형 테두리를 찾아 원근 보정. 못 찾거나 오류면 **원본 사진 그대로**(작업은 계속).
- 캡션은 TTF 를 임베드하고 subset_fonts() (D10). 내장 CJK(`korea`)는 쓰지 않는다.
- 결과는 산출물 옆 임시 파일(TMP_SUFFIX) → 다시 열어 쪽 수 확인 → replace. 경로·본문은 결과에 넣지 않는다(수만).
"""
from __future__ import annotations

import io
import os
import time

from PIL import Image, ImageOps

from bom_pdf.engine.exportstatus import EXPORT_ERROR, EXPORT_OK, EXPORT_REFUSED, TMP_SUFFIX
from bom_pdf.paths import long_path, replace_with_retry
from bom_pdf.services import imagetopdf as itp

THUMB_PX = 160
BPP_SAMPLES = 6
# O5 큰 사진: 원본 헤더 화소가 HARD_MAX_PX 를 넘거나, 줄여 읽은 뒤에도(JPEG draft 는 최대 1/8) 풀 화소가
# FULL_DECODE_MAX_PX 를 넘으면 그 사진만 건너뛴다. PNG·WEBP·HEIC 는 줄여 읽기가 안 돼 이 값이 곧 한도
# (실측 약 9.4바이트/화소 → 1억 화소면 작업 프로세스 최고 약 0.95GB).
HARD_MAX_PX = 1_000_000_000
FULL_DECODE_MAX_PX = 100_000_000
DOC_MODE_MAX_SIDE = 4000  # 문서 사진 모드 보정에 쓸 긴 변 최대(화질 우선 300DPI A4 긴 변 3508px 보다 큼)
Image.MAX_IMAGE_PIXELS = None  # Pillow 기본 폭탄 검사(약 1.79억 화소에서 오류) 대신 위 한도로 직접 거른다
SKIP_TOO_LARGE = "too_large"
SKIP_UNREADABLE = "unreadable"
_TAG_ORIENTATION = 0x0112
_TAG_DATETIME = 0x0132
_IFD_EXIF = 0x8769
_TAG_DT_ORIGINAL = 0x9003
_TAG_DT_DIGITIZED = 0x9004

_heif_ready = False


def _register_heif() -> None:
    """HEIC 읽기. 포장본은 읽기 전용 pi-heif(O7 — 인코더 libx265 22MB 없음), 없으면 pillow-heif(개발 환경).
    둘 다 같은 libheif·libde265 로 풀어 화소가 같다(O7 비교)."""
    global _heif_ready
    if _heif_ready:
        return
    for name in ("pi_heif", "pillow_heif"):
        try:
            __import__(name).register_heif_opener()
            break
        except Exception:  # 없으면 HEIC 만 못 읽는다
            continue
    _heif_ready = True


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


# --------------------------------------------------------------------------- 읽기


def _taken(exif) -> str | None:
    """EXIF 촬영시각 → "YYYY-MM-DD HH:MM:SS". 원본시각 → 디지털화 시각 → 파일 IFD0 DateTime 순."""
    vals = []
    try:
        sub = exif.get_ifd(_IFD_EXIF)
        vals += [sub.get(_TAG_DT_ORIGINAL), sub.get(_TAG_DT_DIGITIZED)]
    except Exception:
        pass
    vals += [exif.get(_TAG_DT_ORIGINAL), exif.get(_TAG_DATETIME)]
    for v in vals:
        if isinstance(v, bytes):
            v = v.decode("ascii", "ignore")
        if not isinstance(v, str):
            continue
        v = v.strip().strip("\x00")
        if len(v) >= 19 and v[4] == ":" and v[7] == ":" and v[:4].isdigit() and v[:4] != "0000":
            return f"{v[:4]}-{v[5:7]}-{v[8:10]} {v[11:19]}"
    return None


def _orientation(im: Image.Image) -> int:
    try:
        return int(im.getexif().get(_TAG_ORIENTATION, 1) or 1)
    except Exception:
        return 1


_TRANSPOSE = {2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180,
              4: Image.Transpose.FLIP_TOP_BOTTOM, 5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270,
              7: Image.Transpose.TRANSVERSE, 8: Image.Transpose.ROTATE_90}  # ImageOps.exif_transpose 와 같은 표


class TooLarge(Exception):
    """줄여 읽을 수 없는데 너무 큰 사진 — 그 사진만 건너뛴다(이유: too_large)."""

    def __init__(self, megapixels: int):
        super().__init__(megapixels)
        self.megapixels = megapixels


def _check_size(src: Image.Image, want: tuple[int, int] | None) -> None:
    """헤더 크기로 미리 거른다(풀기 전에). JPEG 는 1/8 까지 줄여 읽을 수 있어 그만큼 봐준다."""
    w, h = src.size
    if w * h > HARD_MAX_PX:
        raise TooLarge(w * h // 1_000_000)
    decoded = w * h
    if src.format == "JPEG" and want:
        s = 1  # draft 가 고를 축소 배율(결과가 want 이상인 가장 큰 1/2·1/4·1/8)
        while s < 8 and w // (s * 2) >= want[0] and h // (s * 2) >= want[1]:
            s *= 2
        decoded = -(-w // s) * -(-h // s)
    if decoded > FULL_DECODE_MAX_PX:
        raise TooLarge(w * h // 1_000_000)


def _to_rgb(im: Image.Image, orient: int | None = None) -> Image.Image:
    """회전 적용·색 프로필 sRGB 변환·투명은 흰 바탕·메타데이터 없는 그림. im 은 호출자 소유(파일에 묶이지 않은 것)."""
    if orient is None:
        orient = _orientation(im)
    if orient in _TRANSPOSE:
        im = im.transpose(_TRANSPOSE[orient])
    icc = im.info.get("icc_profile")
    if icc and im.mode == "RGB":
        try:
            from PIL import ImageCms

            src = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            im = ImageCms.profileToProfile(im, src, ImageCms.createProfile("sRGB"), outputMode="RGB")
        except Exception:
            pass
    if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.getchannel("A"))
        im = bg
    elif im.mode not in ("RGB", "L"):
        im = im.convert("RGB")
    im.info = {}  # EXIF·XMP·ICC·주석 — 아무것도 넘기지 않는다
    return im


def _decode(src: Image.Image, want: tuple[int, int] | None) -> Image.Image:
    """열린 사진을 풀어 호출자 소유 그림으로. want(저장 방향 기준 화소)의 2배 이상 크면 정수 배로 줄여 읽는다:
    JPEG 는 draft(DCT 단계에서 1/2·1/4·1/8 — 큰 사진도 메모리 적게), 다른 형식은 푼 뒤 reduce."""
    _check_size(src, want)
    if want and src.format == "JPEG":
        try:
            src.draft("RGB", (max(1, want[0]), max(1, want[1])))
        except Exception:
            pass
    src.load()
    im = src
    if want:
        f = min(src.width // max(1, want[0]), src.height // max(1, want[1]))
        if f >= 2:
            im = src.reduce(f)
    if im is src:
        im = src.copy()  # with 블록이 닫으면 원래 그림은 못 쓴다
    return im


def load_image(path: str, rotate: int = 0, draft_px: tuple[int, int] | None = None) -> Image.Image:
    """원본 사진 → 회전(EXIF + 사용자 각도)을 적용한 RGB/L 그림.

    draft_px: 회전 뒤 기준으로 이 크기 이상만 남기고 줄여 읽기(JPEG draft, 그 밖 reduce). 너무 크면 TooLarge.
    """
    _register_heif()
    with Image.open(long_path(path)) as src:
        orient = _orientation(src)
        want = None
        if draft_px:
            w, h = draft_px
            want = (h, w) if orient in (5, 6, 7, 8) else (w, h)
        im = _decode(src, want)
    im = _to_rgb(im, orient)
    rotate %= 360
    if rotate:
        im = im.rotate(-rotate, expand=True)  # 사용자 각도는 시계 방향
    return im


def _encode_jpeg(im: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def _fit_pixels(im: Image.Image, size: tuple[int, int]) -> Image.Image:
    if size[0] < im.width or size[1] < im.height:
        return im.resize(size, Image.Resampling.LANCZOS)
    return im


# --------------------------------------------------------------------------- 훑기(대화상자용)


def scan(paths: list[str], progress=None) -> dict:
    """사진 정보(회전 뒤 크기·촬영시각)·작은 미리보기·예상 용량용 바이트/화소.

    결과 {"status", "items": [{"path", "width", "height", "taken", "thumb"}], "failed": [경로],
          "skipped": [{"path", "reason": too_large/unreadable, "mp": 백만 화소}], "bpp": {preset: 값}}.
    """
    _register_heif()
    items, failed, skipped = [], [], []
    n = len(paths)
    for k, p in enumerate(paths):
        try:
            with Image.open(long_path(p)) as src:
                w, h = src.size
                _check_size(src, (THUMB_PX * 2, THUMB_PX * 2))  # PNG 의 getexif 는 그림을 통째로 풀므로 그 전에
                exif = src.getexif()
                orient = int(exif.get(_TAG_ORIENTATION, 1) or 1)
                taken = _taken(exif)
                th = _decode(src, (THUMB_PX * 2, THUMB_PX * 2))
            th = _to_rgb(th, orient)
            if orient in (5, 6, 7, 8):
                w, h = h, w
            th.thumbnail((THUMB_PX, THUMB_PX))
            items.append({"path": p, "width": w, "height": h, "taken": taken, "thumb": _encode_jpeg(th, 70)})
        except TooLarge as e:
            failed.append(p)
            skipped.append({"path": p, "reason": SKIP_TOO_LARGE, "mp": e.megapixels})
        except Exception:
            failed.append(p)
            skipped.append({"path": p, "reason": SKIP_UNREADABLE})
        if progress is not None:
            progress(k + 1, n)
    return {"status": EXPORT_OK, "items": items, "failed": failed, "skipped": skipped, "bpp": _sample_bpp(items)}


def _sample_bpp(items: list[dict]) -> dict:
    """사진 몇 장을 실제 화질 설정으로 줄여 인코딩해 본 바이트/화소 — 예상 용량에 쓴다."""
    if not items:
        return {}
    step = max(1, len(items) // BPP_SAMPLES)
    sample = items[::step][:BPP_SAMPLES]
    out = {}
    for preset, cfg in itp.QUALITY_PRESETS.items():
        nbytes = npx = 0
        for it in sample:
            pages = itp.layout([(it["width"], it["height"])], itp.Options(preset=preset))
            rect = pages[0]["slots"][0][1]
            tw, th = itp.target_pixels(it["width"], it["height"], rect, preset)
            try:
                im = _fit_pixels(load_image(it["path"], 0, (tw, th)), (tw, th))
                nbytes += len(_encode_jpeg(im, cfg["quality"]))
                npx += im.width * im.height
            except Exception:
                continue
        if npx:
            out[preset] = nbytes / npx
    return out


# --------------------------------------------------------------------------- 문서 사진 모드


def _order_quad(pts):
    import numpy as np

    s = pts.sum(axis=1)
    d = np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], dtype="float32")


def straighten_document(im: Image.Image) -> Image.Image | None:
    """종이 테두리를 찾아 원근 보정한 그림. 못 찾거나 오류면 None(호출자는 원본을 쓴다)."""
    try:
        import cv2
        import numpy as np

        arr = np.asarray(im.convert("RGB"))
        h, w = arr.shape[:2]
        scale = min(1.0, 1000.0 / max(h, w))
        small = cv2.resize(arr, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else arr
        gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_RGB2GRAY), (5, 5), 0)
        edges = cv2.dilate(cv2.Canny(gray, 50, 150), np.ones((3, 3), np.uint8), iterations=2)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area = small.shape[0] * small.shape[1]
        quad = None
        for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
            approx = cv2.approxPolyDP(c, 0.02 * cv2.arcLength(c, True), True)
            if len(approx) == 4 and cv2.isContourConvex(approx) and cv2.contourArea(approx) > 0.2 * area:
                quad = approx.reshape(4, 2).astype("float32") / scale
                break
        if quad is None:
            return None
        tl, tr, br, bl = _order_quad(quad)
        out_w = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
        out_h = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
        if out_w < 50 or out_h < 50:
            return None
        dst = np.array([[0, 0], [out_w - 1, 0], [out_w - 1, out_h - 1], [0, out_h - 1]], dtype="float32")
        m = cv2.getPerspectiveTransform(np.array([tl, tr, br, bl], dtype="float32"), dst)
        warped = cv2.warpPerspective(arr, m, (out_w, out_h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE)
        out = ImageOps.autocontrast(Image.fromarray(warped), cutoff=0.5)
        return out.convert("L") if im.mode == "L" else out
    except Exception:
        return None


# --------------------------------------------------------------------------- PDF 만들기


def _caption(page, font, fontfile: str | None, text: str, rect) -> bool:
    """사진 사각형 바로 아래 가운데에 캡션. 글꼴이 없고 한글이 섞였으면 쓰지 않고 False."""
    import pymupdf

    if not text:
        return True
    if fontfile:
        fontname = "bomcap"
        page.insert_font(fontname=fontname, fontfile=fontfile)
        measure = font.text_length
    elif text.isascii():
        fontname = "helv"
        helv = pymupdf.Font("helv")
        measure = helv.text_length
    else:
        return False
    x0, _, x1, y1 = rect
    width = max(x1 - x0, 120.0)
    size = itp.CAPTION_PT
    while size > 6 and measure(text, fontsize=size) > width:
        size -= 0.5
    while len(text) > 4 and measure(text, fontsize=size) > width:
        text = text[:-2] + "…"
    tw = measure(text, fontsize=size)
    cx = (x0 + x1) / 2
    page.insert_text((cx - tw / 2, y1 + 2 + size), text, fontname=fontname, fontsize=size, color=(0.15, 0.15, 0.15))
    return True


def build_pdf(job: dict, progress=None) -> dict:
    """services.imagetopdf.plan() 의 작업 → 새 PDF. 결과는 수와 상태만.

    {"status", "pages", "images", "failed", "doc_fixed", "doc_kept", "caption_skipped", "size", "seconds"}
    """
    import pymupdf

    from bom_pdf.engine import pdf

    pdf._quiet_mupdf()
    t0 = time.perf_counter()
    out = job["out"]
    if any(_same(out, p) for p in job.get("protected", ())):
        return {"status": EXPORT_REFUSED}
    opts = itp.Options(page=job["page"], per_page=job["per_page"], margin_mm=job["margin_mm"],
                       caption=("on",) if job.get("caption") else (), preset=job["preset"])
    quality = itp.QUALITY_PRESETS[opts.preset]["quality"]
    fontfile = job.get("font")
    font = pymupdf.Font(fontfile=fontfile) if fontfile else None
    items = job["items"]
    per = opts.effective_per_page
    tmp = long_path(out + TMP_SUFFIX)
    stats = {"failed": 0, "too_large": 0, "doc_fixed": 0, "doc_kept": 0, "caption_skipped": 0}
    doc = pymupdf.open()
    try:
        done = 0
        for start in range(0, len(items), per):
            chunk = []
            for it in items[start:start + per]:
                done += 1
                try:
                    chunk.append((it, _prepare(it, opts, stats)))
                except TooLarge:
                    stats["failed"] += 1
                    stats["too_large"] += 1
                except Exception:
                    stats["failed"] += 1
                if progress is not None:
                    progress(done, len(items))
            if not chunk:
                continue
            pg = itp.layout([im.size for _, im in chunk], opts)[0]
            page = doc.new_page(width=pg["size"][0], height=pg["size"][1])
            for (it, im), (_, rect) in zip(chunk, pg["slots"]):
                tw, th = itp.target_pixels(im.width, im.height, rect, opts.preset)
                data = _encode_jpeg(_fit_pixels(im, (tw, th)), quality)
                page.insert_image(pymupdf.Rect(rect), stream=data, keep_proportion=True)
                if opts.caption and not _caption(page, font, fontfile, it.get("caption", ""), rect):
                    stats["caption_skipped"] += 1
            chunk.clear()
        n_pages = doc.page_count
        if n_pages == 0:
            doc.close()
            return {"status": EXPORT_ERROR, "reason": "no_images", **stats}
        doc.set_metadata({"producer": "bom-pdf", "creator": "bom-pdf"})
        try:
            doc.del_xml_metadata()
        except Exception:
            pass
        if fontfile:
            doc.subset_fonts()
        doc.save(tmp, garbage=3, deflate=True)
    except BaseException:
        doc.close()
        _unlink(tmp)
        raise
    doc.close()
    try:
        chk = pymupdf.open(tmp)
        ok = chk.page_count == n_pages
        chk.close()
        if not ok:
            _unlink(tmp)
            return {"status": EXPORT_ERROR, "reason": "verify"}
        size = os.path.getsize(tmp)
        replace_with_retry(tmp, long_path(out))
    except BaseException:
        _unlink(tmp)
        raise
    return {"status": EXPORT_OK, "pages": n_pages, "images": len(items) - stats["failed"], "size": size,
            "seconds": round(time.perf_counter() - t0, 3), **stats}


def _prepare(it: dict, opts: itp.Options, stats: dict) -> Image.Image:
    """한 장 읽기 → (문서 모드면 보정). 필요한 크기 이상으로만 줄여 읽어 빠르고 메모리 적게(O5).

    문서 모드는 보정 뒤 잘라낼 크기를 미리 모르므로 긴 변 DOC_MODE_MAX_SIDE 이상만 남긴다. 너무 크면 TooLarge.
    """
    draft = _draft_size(it["path"], it.get("rotate", 0), opts, bool(it.get("doc_mode")))
    im = load_image(it["path"], it.get("rotate", 0), draft)
    if it.get("doc_mode"):
        side = max(im.size)
        if side > DOC_MODE_MAX_SIDE * 1.2:  # 정수 배로 못 줄인 몫 — 보정(OpenCV) 전에 긴 변 DOC_MODE_MAX_SIDE 로
            k = DOC_MODE_MAX_SIDE / side
            im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.Resampling.LANCZOS,
                           reducing_gap=2.0)
        fixed = straighten_document(im)
        if fixed is None:
            stats["doc_kept"] += 1
        else:
            stats["doc_fixed"] += 1
            im = fixed
    return im


def _draft_size(path: str, rotate: int, opts: itp.Options, doc_mode: bool = False) -> tuple[int, int] | None:
    """EXIF 회전 뒤 기준으로 남길 최소 화소. 모르면 None(줄이지 않음)."""
    try:
        with Image.open(long_path(path)) as src:
            w, h = src.size
            if _orientation(src) in (5, 6, 7, 8):
                w, h = h, w
    except Exception:
        return None
    if doc_mode:
        s = DOC_MODE_MAX_SIDE / max(w, h)
        return (max(1, int(w * s)), max(1, int(h * s))) if s < 1 else None
    if rotate % 180:
        w, h = h, w
    rect = itp.layout([(w, h)], opts)[0]["slots"][0][1]
    tw, th = itp.target_pixels(w, h, rect, opts.preset)
    if rotate % 180:
        tw, th = th, tw
    return tw, th
