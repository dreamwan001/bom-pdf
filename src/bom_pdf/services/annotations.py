"""사이드카 주석 저장소 (결정 D1). PySide6 를 import하지 않는다.

- 파일: `<데이터 폴더>\\annotations\\<sha256>.json` (데이터 폴더는 `BOM_PDF_HOME` 우선). 원본 PDF 는 건드리지 않는다.
- 같은 내용의 사본(경로만 다름)은 sha256 이 같으므로 주석을 함께 쓴다. `knownPaths` 에 열어 본 경로를 모은다.
- 바뀔 때마다 원자적으로 저장한다(같은 폴더 임시 파일 → fsync → replace). 급사해도 마지막으로 저장된 상태가 남는다.
- 읽다가 깨졌으면 `.damaged` 로 옮겨 보존하고 빈 상태로 시작한다(notice 로 사용자에게 알린다).
  읽을 때 정규화한다: 모르는 키는 버리고, 형식이 틀린 항목은 빼고(이 경우 원래 파일을 `.damaged` 로 복사해 둔다),
  색은 정해진 팔레트로 맞춘다. 더 새 schemaVersion 이면 덮어써서 잃지 않도록 읽기 전용으로 연다.
- 좌표는 PDF pt, **회전이 반영된 쪽 좌표**(page.rect 기준 — 화면·검색 강조와 같은 방향). page 는 0부터.
  highlight·underline 은 `quads`(줄마다 [x0, y0, x1, y1]), note·box 는 `rect`([x0, y0, x1, y1]).
- `text`(선택한 원문)·`note` 는 사이드카에만 둔다 — 로그에 쓰지 않는다(개수·종류·id 만).
- 되돌리기/다시 하기: 문서(sha256)마다 바꾼 기록을 메모리에 둔다(프로그램을 닫으면 사라짐). 되돌린 결과도 바로 저장.
- 이 모듈의 객체는 UI 스레드 한 곳에서만 바꾼다(잠금 없음).
"""
from __future__ import annotations

import copy
import logging
import math
import os
import secrets
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from bom_pdf import paths

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
HIGHLIGHT, UNDERLINE, NOTE, BOX = "highlight", "underline", "note", "box"
TYPES = (HIGHLIGHT, UNDERLINE, NOTE, BOX)
TEXT_TYPES = (HIGHLIGHT, UNDERLINE)  # 글자 선택으로 만드는 종류 (quads)
TYPE_LABELS = {HIGHLIGHT: "형광펜", UNDERLINE: "밑줄", NOTE: "메모", BOX: "사각형"}

# 작은 고정 팔레트 — 이름으로 저장하고 RGB(0~1)는 그릴 때·내보낼 때 쓴다
COLORS = {
    "yellow": ("노랑", (1.0, 0.86, 0.0)),
    "green": ("초록", (0.36, 0.82, 0.36)),
    "blue": ("파랑", (0.33, 0.62, 1.0)),
    "pink": ("분홍", (1.0, 0.47, 0.72)),
    "red": ("빨강", (0.9, 0.16, 0.16)),
}
DEFAULT_COLOR = "yellow"
NOTE_SIZE = 20.0  # 메모 아이콘 크기(pt) — 다른 뷰어의 기본 메모 아이콘과 비슷하게

MAX_KNOWN_PATHS = 20
MAX_UNDO = 200

MSG_DAMAGED = ("주석 파일이 손상되어 읽지 못했습니다. 손상된 파일은 '{name}'(으)로 보관했고 "
               "빈 주석으로 시작합니다.")
MSG_PARTIAL = ("주석 파일의 일부 항목({n}개)이 형식에 맞지 않아 뺐습니다. 원래 파일은 '{name}'(으)로 보관했습니다.")
MSG_NEWER = "더 새 버전의 bom-pdf 로 만든 주석이라 읽기만 합니다(바꾸면 내용을 잃을 수 있음)."
MSG_UNREADABLE = ("주석 파일을 지금 읽거나 보관할 수 없어(다른 프로그램이 쓰는 중이거나 권한 문제) 읽기 전용으로 엽니다. "
                  "파일은 그대로 두었습니다. 탭을 닫고 다시 열어 보세요.")
STALE_TMP_S = 600  # 이보다 오래된 저장 임시 파일은 죽은 저장의 찌꺼기로 본다
MSG_SAVE_FAILED = "주석을 저장하지 못했습니다. 디스크 공간·권한을 확인해 주세요. 바뀐 내용은 다시 바꿀 때 함께 저장을 시도합니다."


def annotations_dir(data_dir: Path | None = None) -> Path:
    p = (data_dir or paths.data_dir()) / "annotations"
    p.mkdir(parents=True, exist_ok=True)
    return p


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford


def new_id() -> str:
    """ULID(26자): 48비트 밀리초 시각 + 80비트 난수. 시각순으로 정렬된다."""
    n = (int(time.time() * 1000) << 80) | secrets.randbits(80)
    return "".join(_B32[(n >> (5 * i)) & 31] for i in range(25, -1, -1))


def color_rgb(name: str) -> tuple[float, float, float]:
    return COLORS.get(name, COLORS[DEFAULT_COLOR])[1]


# --------------------------------------------------------------------------- 정규화


def _rect(v) -> list[float] | None:
    if not isinstance(v, (list, tuple)) or len(v) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(a) for a in v)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(a) for a in (x0, y0, x1, y1)):
        return None
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    return [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)]


def clean_tags(tags) -> list[str]:
    if isinstance(tags, str):
        tags = tags.split(",")
    if not isinstance(tags, (list, tuple)):
        return []
    out: list[str] = []
    for t in tags:
        if isinstance(t, str):
            t = " ".join(t.split()).lstrip("#")
            if t and t not in out:
                out.append(t)
    return out


def normalize_item(raw) -> dict | None:
    """저장 형식에 맞춘 새 dict. 쓸 수 없는 항목이면 None. 모르는 키는 버린다."""
    if not isinstance(raw, dict):
        return None
    typ = raw.get("type")
    if typ not in TYPES:
        return None
    page = raw.get("page")
    if isinstance(page, bool) or not isinstance(page, int) or page < 0:
        return None
    item: dict = {"id": raw["id"] if isinstance(raw.get("id"), str) and raw["id"] else new_id(),
                  "type": typ, "page": page}
    if typ in TEXT_TYPES:
        src = raw.get("quads")
        quads = [q for q in (_rect(v) for v in (src if isinstance(src, list) else [])) if q is not None]
        if not quads:
            return None
        item["quads"] = quads
    else:
        r = _rect(raw.get("rect"))
        if r is None:
            return None
        item["rect"] = r
    color = raw.get("color")
    item["color"] = color if color in COLORS else DEFAULT_COLOR
    item["text"] = raw.get("text") if isinstance(raw.get("text"), str) else ""
    item["note"] = raw.get("note") if isinstance(raw.get("note"), str) else ""
    item["tags"] = clean_tags(raw.get("tags"))
    now = now_iso()
    item["createdAt"] = raw["createdAt"] if isinstance(raw.get("createdAt"), str) else now
    item["updatedAt"] = raw["updatedAt"] if isinstance(raw.get("updatedAt"), str) else item["createdAt"]
    return item


def item_bounds(item: dict) -> list[float]:
    """항목 전체를 감싸는 사각형(pt)."""
    if "rect" in item:
        return list(item["rect"])
    qs = item["quads"]
    return [min(q[0] for q in qs), min(q[1] for q in qs), max(q[2] for q in qs), max(q[3] for q in qs)]


# --------------------------------------------------------------------------- 저장소


class AnnotationStore:
    """sha256 하나의 주석. 바꾸는 메서드는 모두 바로 원자적으로 저장하고 listener 를 부른다."""

    def __init__(self, sha256: str, folder: Path | None = None):
        self.sha256 = sha256
        self.folder = folder if folder is not None else annotations_dir()
        self.path = self.folder / f"{sha256}.json"
        self.items: list[dict] = []  # 바꿀 때는 새 dict 로 갈아 끼운다(밖에서 고치지 않는다)
        self.known_paths: list[str] = []
        self.notice: str | None = None  # 열 때 사용자에게 알릴 것(손상·일부 제외·읽기 전용)
        self.read_only = False
        self.save_error: str | None = None
        self._listeners: list[Callable[[AnnotationStore], None]] = []
        self._undo: list[tuple] = []
        self._redo: list[tuple] = []
        self._load()

    # ---- 읽기
    def _keep_damaged(self, move: bool) -> str:
        name = self.path.name + ".damaged"
        dst = self.path.with_name(name)
        if dst.exists():
            name = f"{self.path.name}.{time.strftime('%Y%m%d-%H%M%S')}.damaged"
            dst = self.path.with_name(name)
        try:
            if move:
                paths.replace_with_retry(self.path, dst)
            else:
                shutil.copy2(self.path, dst)
            return name
        except OSError:
            log.exception("could not keep damaged sidecar")
        if move:  # 옮기지 못하면 복사라도
            try:
                shutil.copy2(self.path, dst)
                return name
            except OSError:
                log.exception("could not copy damaged sidecar")
        return None

    def _damaged(self, why: str) -> None:
        """손상본을 보존하고 빈 상태로 시작. 보존하지 못했으면 덮어쓰지 않도록 읽기 전용."""
        name = self._keep_damaged(move=True)
        if name is None:
            self.read_only = True
            self.notice = MSG_UNREADABLE
        else:
            self.notice = MSG_DAMAGED.format(name=name)
        log.warning("annotation sidecar %s sha=%s… kept as %s", why, self.sha256[:12], name)

    def _clean_stale_tmp(self) -> None:
        """저장 도중 죽어 남은 임시 파일(`<sha>.json.<임의>.tmp`) 중 오래된 것을 지운다 — 지금 쓰는 중일 수 있는 새 것은 둔다."""
        cutoff = time.time() - STALE_TMP_S
        for p in self.folder.glob(self.path.name + ".*.tmp"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _load(self) -> None:
        self._clean_stale_tmp()
        raw = None
        for delay in (*paths.READ_RETRY_DELAYS, None):
            try:
                raw = paths.read_json_strict(self.path)
                break
            except FileNotFoundError:
                return
            except ValueError:  # 내용이 깨짐(JSON·UTF-8 오류)
                self._damaged("damaged")
                return
            except OSError:  # 잠김·권한 — 파일은 멀쩡할 수 있다. 옮기지도 덮어쓰지도 않는다
                if delay is None:
                    self.read_only = True
                    self.notice = MSG_UNREADABLE
                    log.warning("annotation sidecar unreadable sha=%s… (read-only)", self.sha256[:12])
                    return
                time.sleep(delay)
        if not isinstance(raw, dict) or not isinstance(raw.get("items", []), list):
            self._damaged("has wrong shape")
            return
        ver = raw.get("schemaVersion")
        if isinstance(ver, int) and not isinstance(ver, bool) and ver > SCHEMA_VERSION:
            self.read_only = True
            self.notice = MSG_NEWER
        kp = raw.get("knownPaths")
        if isinstance(kp, list):
            self.known_paths = [p for p in kp if isinstance(p, str)][:MAX_KNOWN_PATHS]
        raw_items = raw.get("items", [])
        seen: set[str] = set()
        dropped = 0
        for r in raw_items:
            it = normalize_item(r)
            if it is None:
                dropped += 1
                continue
            if it["id"] in seen:
                it["id"] = new_id()
            seen.add(it["id"])
            self.items.append(it)
        if dropped and not self.read_only:
            name = self._keep_damaged(move=False)
            if name is None:  # 원래 파일을 보존하지 못함 — 저장하면 뺀 항목을 잃으므로 읽기 전용
                self.read_only = True
                self.notice = MSG_UNREADABLE
            else:
                self.notice = MSG_PARTIAL.format(n=dropped, name=name)
        log.info("annotations loaded sha=%s… items=%d dropped=%d read_only=%s",
                 self.sha256[:12], len(self.items), dropped, self.read_only)

    # ---- 저장
    def to_json(self) -> dict:
        return {"schemaVersion": SCHEMA_VERSION, "docSha256": self.sha256,
                "knownPaths": list(self.known_paths), "items": copy.deepcopy(self.items)}

    def save(self) -> bool:
        if self.read_only:
            return False
        try:
            paths.atomic_write_json(self.path, self.to_json())
        except OSError:
            log.exception("annotation save failed sha=%s…", self.sha256[:12])
            self.save_error = MSG_SAVE_FAILED
            return False
        self.save_error = None
        return True

    def note_path(self, path: str) -> None:
        """이 sha256 의 파일을 이 경로에서 열었음. 주석 파일이 이미 있으면 그때만 저장한다(빈 파일을 만들지 않음)."""
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        if any(os.path.normcase(p) == key for p in self.known_paths):
            return
        self.known_paths.insert(0, path)
        del self.known_paths[MAX_KNOWN_PATHS:]
        if self.path.exists():
            self.save()

    # ---- 알림
    def add_listener(self, cb: Callable[["AnnotationStore"], None]) -> None:
        if cb not in self._listeners:
            self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _changed(self) -> None:
        self.save()
        for cb in list(self._listeners):
            try:
                cb(self)
            except Exception:
                log.exception("annotation listener failed")

    # ---- 조회
    def get(self, item_id: str) -> dict | None:
        return next((it for it in self.items if it["id"] == item_id), None)

    def _index(self, item_id: str) -> int:
        for i, it in enumerate(self.items):
            if it["id"] == item_id:
                return i
        raise KeyError(item_id)

    def on_page(self, page: int) -> list[dict]:
        return [it for it in self.items if it["page"] == page]

    def all_tags(self) -> list[str]:
        out: list[str] = []
        for it in self.items:
            for t in it["tags"]:
                if t not in out:
                    out.append(t)
        return out

    # ---- 바꾸기 (되돌리기 기록)
    def _check_writable(self) -> None:
        if self.read_only:
            raise PermissionError("annotation store is read-only")

    def _record(self, op: tuple) -> None:
        self._undo.append(op)
        del self._undo[:-MAX_UNDO]
        self._redo.clear()

    def add(self, raw: dict) -> dict:
        self._check_writable()
        raw = dict(raw)
        raw["id"] = new_id()
        raw["createdAt"] = raw["updatedAt"] = now_iso()
        item = normalize_item(raw)
        if item is None:
            raise ValueError("invalid annotation")
        self.items.append(item)
        self._record(("add", len(self.items) - 1, None, item))
        log.info("annotation added type=%s page=%d id=%s", item["type"], item["page"], item["id"])
        self._changed()
        return item

    def update(self, item_id: str, **changes) -> dict:
        """note·tags·color 등을 바꾼다. id·type·page·createdAt 은 바꾸지 않는다."""
        self._check_writable()
        i = self._index(item_id)
        before = self.items[i]
        raw = {**before, **{k: v for k, v in changes.items() if k not in ("id", "type", "page", "createdAt")}}
        raw["updatedAt"] = now_iso()
        after = normalize_item(raw)
        if after is None:
            raise ValueError("invalid annotation")
        if {k: v for k, v in after.items() if k != "updatedAt"} == {k: v for k, v in before.items() if k != "updatedAt"}:
            return before  # 바뀐 것이 없음
        self.items[i] = after
        self._record(("update", i, before, after))
        log.info("annotation updated id=%s", item_id)
        self._changed()
        return after

    def remove(self, item_id: str) -> None:
        self._check_writable()
        i = self._index(item_id)
        before = self.items.pop(i)
        self._record(("remove", i, before, None))
        log.info("annotation removed id=%s", item_id)
        self._changed()

    def can_undo(self) -> bool:
        return bool(self._undo) and not self.read_only

    def can_redo(self) -> bool:
        return bool(self._redo) and not self.read_only

    def _apply(self, idx: int, old: dict | None, new: dict | None) -> None:
        """old 상태를 new 상태로 바꾼다 (old None = 끼워 넣기, new None = 빼기)."""
        if old is None:
            self.items.insert(min(idx, len(self.items)), new)
        elif new is None:
            self.items.pop(self._index(old["id"]))
        else:
            self.items[self._index(old["id"])] = new

    def undo(self) -> bool:
        if not self.can_undo():
            return False
        op, idx, before, after = self._undo.pop()
        self._apply(idx, after, before)
        self._redo.append((op, idx, before, after))
        log.info("annotation undo op=%s", op)
        self._changed()
        return True

    def redo(self) -> bool:
        if not self.can_redo():
            return False
        op, idx, before, after = self._redo.pop()
        self._apply(idx, before, after)
        self._undo.append((op, idx, before, after))
        log.info("annotation redo op=%s", op)
        self._changed()
        return True

    # ---- 내보내기용
    def export_items(self) -> list[dict]:
        """작업자에게 보낼 평범한 자료(색은 RGB)."""
        out = []
        for it in self.items:
            d = {"type": it["type"], "page": it["page"], "color": color_rgb(it["color"]),
                 "note": it["note"], "tags": list(it["tags"]), "label": TYPE_LABELS[it["type"]]}
            if "quads" in it:
                d["quads"] = [list(q) for q in it["quads"]]
            else:
                d["rect"] = list(it["rect"])
            out.append(d)
        return out


class AnnotationLibrary:
    """열린 문서들의 저장소 — 같은 sha256 은 같은 저장소 하나를 함께 쓴다(참조 수 세기)."""

    def __init__(self, data_dir: Path | None = None):
        self.folder = annotations_dir(data_dir)
        self._stores: dict[str, tuple[AnnotationStore, int]] = {}

    def acquire(self, sha256: str, path: str | None = None) -> AnnotationStore:
        entry = self._stores.get(sha256)
        if entry is None:
            store = AnnotationStore(sha256, self.folder)
            self._stores[sha256] = (store, 1)
        else:
            store, n = entry
            self._stores[sha256] = (store, n + 1)
        if path:
            store.note_path(path)
        return store

    def release(self, sha256: str) -> None:
        entry = self._stores.get(sha256)
        if entry is None:
            return
        store, n = entry
        if n <= 1:
            del self._stores[sha256]
        else:
            self._stores[sha256] = (store, n - 1)

    def get(self, sha256: str) -> AnnotationStore | None:
        e = self._stores.get(sha256)
        return e[0] if e else None


# --------------------------------------------------------------------------- 글자 선택 (작업자가 준 글자 상자)
#
# chars: [(글자, x0, y0, x1, y1, 줄 번호)] — 회전 반영 쪽 좌표, 읽는 순서(PDF 내용 순서).


def char_at(chars: list, x: float, y: float) -> int | None:
    """(x, y) 아래의 글자, 없으면 가장 가까운 줄의 가장 가까운 글자."""
    if not chars:
        return None
    for i, (c, x0, y0, x1, y1, _ln) in enumerate(chars):
        if x0 <= x <= x1 and y0 <= y <= y1:
            return i
    best, best_d = None, None
    for i, (c, x0, y0, x1, y1, _ln) in enumerate(chars):
        dy = 0.0 if y0 <= y <= y1 else min(abs(y - y0), abs(y - y1))
        dx = 0.0 if x0 <= x <= x1 else min(abs(x - x0), abs(x - x1))
        d = (dy, dx)  # 먼저 줄(세로), 그다음 가로
        if best_d is None or d < best_d:
            best, best_d = i, d
    return best


def selection(chars: list, a: int, b: int) -> tuple[list[list[float]], str]:
    """글자 a~b(양 끝 포함)를 줄마다 합친 사각형과 글자. 줄이 바뀌면 줄바꿈을 넣는다."""
    if a > b:
        a, b = b, a
    lines: dict[int, list[float]] = {}
    parts: list[str] = []
    prev_ln = None
    for c, x0, y0, x1, y1, ln in chars[a:b + 1]:
        if prev_ln is not None and ln != prev_ln:
            parts.append("\n")
        prev_ln = ln
        parts.append(c)
        if c.isspace():
            continue
        r = lines.get(ln)
        if r is None:
            lines[ln] = [x0, y0, x1, y1]
        else:
            r[0], r[1], r[2], r[3] = min(r[0], x0), min(r[1], y0), max(r[2], x1), max(r[3], y1)
    rects = [[round(v, 2) for v in r] for r in lines.values()]
    text = "\n".join(" ".join(s.split()) for s in "".join(parts).split("\n")).strip()
    return rects, text
