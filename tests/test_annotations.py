"""1-3 사이드카 주석 저장소(서비스 계층, Qt 없음): 왕복·원자적 저장·손상 복구·정규화·사본 공유·되돌리기·급사."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from bom_pdf import paths
from bom_pdf.services import annotations as an
from bom_pdf.services import outputs
from bom_pdf.services.document import HashCache
from conftest import ROOT

SHA = "a" * 64


def hl(page=0, **kw):
    return {"type": an.HIGHLIGHT, "page": page, "quads": [[10, 20, 110, 32], [10, 34, 60, 46]],
            "text": "합성 원문", **kw}


def test_round_trip_schema(tmp_path):
    s = an.AnnotationStore(SHA, tmp_path)
    assert not s.path.exists()  # 빈 저장소는 파일을 만들지 않는다
    a = s.add(hl(note="반박 포인트", tags=["쟁점2", " 쟁점2 ", "#반박"], color="green"))
    b = s.add({"type": an.NOTE, "page": 3, "rect": [100, 100, 120, 120], "note": "메모"})
    c = s.add({"type": an.BOX, "page": 1, "rect": [300, 200, 50, 40]})  # 뒤집힌 사각형은 바로잡는다
    d = s.add({"type": an.UNDERLINE, "page": 0, "quads": [[1, 2, 3, 4]], "color": "nope"})
    raw = json.loads(s.path.read_text(encoding="utf-8"))
    assert raw["schemaVersion"] == an.SCHEMA_VERSION and raw["docSha256"] == SHA
    assert set(raw["items"][0]) == {"id", "type", "page", "quads", "color", "text", "note", "tags",
                                   "createdAt", "updatedAt"}
    assert raw["items"][0]["tags"] == ["쟁점2", "반박"]
    assert len(a["id"]) == 26 and a["id"] != b["id"]
    assert c["rect"] == [50, 40, 300, 200]
    assert d["color"] == an.DEFAULT_COLOR

    s2 = an.AnnotationStore(SHA, tmp_path)
    assert s2.items == s.items
    assert s2.notice is None
    s2.update(a["id"], note="바뀐 메모", type="box", page=9)  # type·page 는 바꾸지 않는다
    s2.remove(b["id"])
    s3 = an.AnnotationStore(SHA, tmp_path)
    got = s3.get(a["id"])
    assert got["note"] == "바뀐 메모" and got["type"] == an.HIGHLIGHT and got["page"] == 0
    assert s3.get(b["id"]) is None and len(s3.items) == 3


def test_atomic_save_keeps_previous_file_on_failure(tmp_path, monkeypatch):
    s = an.AnnotationStore(SHA, tmp_path)
    s.add(hl(note="첫째"))
    before = s.path.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full (simulated)")

    monkeypatch.setattr(paths.os, "replace", boom)
    s.add(hl(note="둘째"))  # 저장 실패 — 메모리에는 있고 오류를 알린다
    assert s.save_error == an.MSG_SAVE_FAILED
    assert s.path.read_bytes() == before  # 기존 파일은 온전
    assert not [p for p in tmp_path.iterdir() if p.suffix == ".tmp"]  # 임시 파일을 남기지 않는다
    monkeypatch.undo()
    s.add(hl(note="셋째"))  # 다음 변경 때 전체를 다시 저장
    assert s.save_error is None
    assert len(an.AnnotationStore(SHA, tmp_path).items) == 3


def test_replace_retried_when_target_briefly_locked(tmp_path, monkeypatch):
    """Windows: 백신·색인이 대상 파일을 잠깐 잡고 있으면 os.replace 가 PermissionError — 다시 시도해 저장한다."""
    s = an.AnnotationStore(SHA, tmp_path)
    s.add(hl(note="첫째"))
    real, calls = os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise PermissionError(13, "sharing violation (simulated)")
        return real(src, dst)

    monkeypatch.setattr(paths.os, "replace", flaky)
    s.add(hl(note="둘째"))
    monkeypatch.undo()
    assert len(calls) == 3 and s.save_error is None
    assert [it["note"] for it in an.AnnotationStore(SHA, tmp_path).items] == ["첫째", "둘째"]
    assert not list(tmp_path.glob("*.tmp"))


def test_locked_sidecar_is_not_treated_as_damaged(tmp_path, monkeypatch):
    """읽기가 잠깐 막히면 다시 읽고, 계속 막히면 옮기지도 덮어쓰지도 않고 읽기 전용."""
    s = an.AnnotationStore(SHA, tmp_path)
    s.add(hl(note="보존"))
    good = s.path.read_bytes()
    real, calls = paths.read_json_strict, []

    def locked_once(path):
        calls.append(1)
        if len(calls) == 1:
            raise PermissionError(13, "locked (simulated)")
        return real(path)

    monkeypatch.setattr(paths, "read_json_strict", locked_once)
    s2 = an.AnnotationStore(SHA, tmp_path)
    assert len(calls) == 2 and s2.notice is None and [it["note"] for it in s2.items] == ["보존"]

    monkeypatch.setattr(paths, "read_json_strict", lambda path: (_ for _ in ()).throw(PermissionError(13, "x")))
    s3 = an.AnnotationStore(SHA, tmp_path)
    assert s3.read_only and s3.notice == an.MSG_UNREADABLE and s3.items == []
    with pytest.raises(PermissionError):
        s3.add(hl())
    monkeypatch.undo()
    assert s.path.read_bytes() == good and not list(tmp_path.glob("*.damaged"))


def test_damaged_sidecar_that_cannot_be_kept_opens_read_only(tmp_path, monkeypatch):
    p = tmp_path / f"{SHA}.json"
    p.write_bytes(b"{broken")

    def deny(*a, **k):
        raise PermissionError(13, "denied (simulated)")

    monkeypatch.setattr(paths.os, "replace", deny)
    monkeypatch.setattr(an.shutil, "copy2", deny)
    s = an.AnnotationStore(SHA, tmp_path)
    monkeypatch.undo()
    assert s.read_only and s.notice == an.MSG_UNREADABLE
    assert p.read_bytes() == b"{broken"  # 보존 못 했으면 덮어쓰지 않는다


def test_stale_tmp_files_are_cleaned_on_load(tmp_path):
    s = an.AnnotationStore(SHA, tmp_path)
    s.add(hl())
    old = tmp_path / f"{SHA}.json.abc123.tmp"
    fresh = tmp_path / f"{SHA}.json.def456.tmp"
    old.write_text("{half", encoding="utf-8")
    fresh.write_text("{half", encoding="utf-8")
    t = time.time() - an.STALE_TMP_S - 60
    os.utime(old, (t, t))
    s2 = an.AnnotationStore(SHA, tmp_path)
    assert len(s2.items) == 1 and s2.notice is None
    assert not old.exists() and fresh.exists()  # 지금 쓰는 중일 수 있는 새 것은 둔다


def test_damaged_sidecar_is_kept_and_store_starts_empty(tmp_path):
    p = tmp_path / f"{SHA}.json"
    p.write_bytes(b'{"schemaVersion": 1, "items": [ {"type": "hi')
    s = an.AnnotationStore(SHA, tmp_path)
    assert s.items == [] and s.notice and "손상" in s.notice
    damaged = tmp_path / f"{SHA}.json.damaged"
    assert damaged.read_bytes().startswith(b'{"schemaVersion": 1')
    assert not p.exists()
    s.add(hl())  # 새로 시작한 것은 정상 저장
    assert len(an.AnnotationStore(SHA, tmp_path).items) == 1

    p.write_text("[1, 2, 3]", encoding="utf-8")  # 모양이 틀린 것도 손상 — 앞 손상본은 덮어쓰지 않는다
    s = an.AnnotationStore(SHA, tmp_path)
    assert s.items == [] and s.notice
    assert damaged.exists() and len(list(tmp_path.glob("*.damaged"))) == 2


def test_normalize_drops_unknown_keys_and_bad_items(tmp_path):
    good = {"id": "X1", "type": "box", "page": 2, "rect": [0, 0, 10, 10], "color": "pink", "future": 1,
            "tags": "가, 나,가"}
    data = {"schemaVersion": 1, "docSha256": "wrong", "knownPaths": ["C:/x.pdf", 5], "extra": True,
            "items": [good,
                      {"type": "highlight", "page": 0, "quads": []},          # 사각형 없음
                      {"type": "stamp", "page": 0, "rect": [0, 0, 1, 1]},     # 모르는 종류
                      {"type": "note", "page": -1, "rect": [0, 0, 1, 1]},     # 쪽 번호 틀림
                      {"type": "note", "page": True, "rect": [0, 0, 1, 1]},
                      {"type": "box", "page": 1, "rect": [0, 0, "x", 1]},
                      {"id": "X1", "type": "note", "page": 1, "rect": [5, 5, 25, 25]}]}  # id 중복
    (tmp_path / f"{SHA}.json").write_text(json.dumps(data), encoding="utf-8")
    s = an.AnnotationStore(SHA, tmp_path)
    assert len(s.items) == 2
    assert s.items[0] == {**{k: v for k, v in good.items() if k != "future"}, "tags": ["가", "나"],
                          "text": "", "note": "", "createdAt": s.items[0]["createdAt"],
                          "updatedAt": s.items[0]["updatedAt"]}
    assert s.items[1]["id"] != "X1"
    assert s.known_paths == ["C:/x.pdf"]
    assert s.notice and "5개" in s.notice  # 뺀 항목 수를 알리고 원래 파일은 보관
    assert (tmp_path / f"{SHA}.json.damaged").exists()
    s.add(hl())
    raw = json.loads((tmp_path / f"{SHA}.json").read_text(encoding="utf-8"))
    assert raw["docSha256"] == SHA and "extra" not in raw and "future" not in raw["items"][0]


def test_newer_schema_opens_read_only(tmp_path):
    p = tmp_path / f"{SHA}.json"
    original = json.dumps({"schemaVersion": 99, "items": [hl()], "newField": {"keep": 1}})
    p.write_text(original, encoding="utf-8")
    s = an.AnnotationStore(SHA, tmp_path)
    assert s.read_only and s.notice == an.MSG_NEWER and len(s.items) == 1
    with pytest.raises(PermissionError):
        s.add(hl())
    s.note_path(str(tmp_path / "a.pdf"))
    assert p.read_text(encoding="utf-8") == original  # 덮어쓰지 않는다


def test_copies_share_annotations_by_sha256(tmp_path, sample_pdf):
    a = tmp_path / "사건A" / "증거.pdf"
    b = tmp_path / "사건B" / "증거 사본.pdf"
    for p in (a, b):
        p.parent.mkdir()
        shutil.copy(sample_pdf, p)
    cache = HashCache(tmp_path / "home" / "hash_cache.json")
    sa, sb = cache.sha256_of(str(a)), cache.sha256_of(str(b))
    assert sa == sb
    lib = an.AnnotationLibrary(tmp_path / "home")
    st_a = lib.acquire(sa, str(a))
    st_b = lib.acquire(sb, str(b))
    assert st_a is st_b  # 같이 열려 있으면 저장소 하나
    st_a.add(hl(note="공유"))
    lib.release(sa)
    assert lib.get(sa) is st_b
    lib.release(sb)
    assert lib.get(sa) is None
    again = an.AnnotationLibrary(tmp_path / "home").acquire(sb, str(b))  # 다시 시작해도
    assert [it["note"] for it in again.items] == ["공유"]
    assert {os.path.normcase(p) for p in again.known_paths} == {os.path.normcase(str(a)), os.path.normcase(str(b))}
    assert not (a.parent / f"{sa}.json").exists()  # 원본 폴더에는 아무것도 없다
    assert sorted(os.listdir(a.parent)) == ["증거.pdf"]


def test_known_path_alone_does_not_create_sidecar(tmp_path):
    s = an.AnnotationLibrary(tmp_path).acquire(SHA, str(tmp_path / "x.pdf"))
    assert s.known_paths and not s.path.exists()


def test_undo_redo(tmp_path):
    s = an.AnnotationStore(SHA, tmp_path)
    a = s.add(hl(note="1"))
    b = s.add({"type": an.BOX, "page": 0, "rect": [0, 0, 50, 50]})
    s.update(a["id"], note="2", tags=["t"])
    s.remove(b["id"])
    assert [it["id"] for it in s.items] == [a["id"]]

    assert s.undo() and [it["id"] for it in s.items] == [a["id"], b["id"]]  # 삭제 되돌리기 — 자리도 그대로
    assert s.undo() and s.get(a["id"])["note"] == "1" and s.get(a["id"])["tags"] == []
    assert s.undo() and s.get(b["id"]) is None
    assert an.AnnotationStore(SHA, tmp_path).items == s.items  # 되돌린 결과도 저장
    assert s.redo() and s.get(b["id"]) is not None
    assert s.redo() and s.get(a["id"])["note"] == "2"
    assert s.redo() and s.get(b["id"]) is None
    assert not s.redo()
    s.undo()
    s.add(hl())  # 새 변경은 다시 하기 기록을 지운다
    assert not s.can_redo()
    while s.undo():
        pass
    assert s.items == []
    assert an.AnnotationStore(SHA, tmp_path).items == []


def test_update_without_change_is_not_recorded(tmp_path):
    s = an.AnnotationStore(SHA, tmp_path)
    a = s.add(hl(note="같음"))
    s.update(a["id"], note="같음")
    assert len(s._undo) == 1


_CHILD = textwrap.dedent("""
    import os, sys
    sys.path.insert(0, {src!r})
    from pathlib import Path
    from bom_pdf.services import annotations as an
    s = an.AnnotationStore({sha!r}, Path({folder!r}))
    mode = sys.argv[1]
    if mode == "once":
        s.add({{"type": "note", "page": 0, "rect": [1, 1, 21, 21], "note": "급사 직전"}})
        if s.save_error:
            print("save_error", file=sys.stderr, flush=True)
        os._exit(9)  # 정리 없이 즉시 끝남(강제 종료 흉내)
    n = 0
    fails = 0
    while True:  # 계속 바꾸는 중에 부모가 죽인다
        it = s.add({{"type": "box", "page": 0, "rect": [0, 0, 10, 10], "note": str(n)}})
        fails += bool(s.save_error)
        if n == 0:
            print("first saved=%s" % (s.save_error is None), flush=True)
        s.update(it["id"], note=str(n) + "!")
        fails += bool(s.save_error)
        if fails and fails < 5:
            print("save_error n=%d" % n, file=sys.stderr, flush=True)
        n += 1
""")


def _diag(folder: Path, store=None, **extra) -> str:
    """실패했을 때 본 것: 폴더 파일(이름·크기), 저장소 안내·항목 수, 그 밖의 값."""
    files = sorted((p.name.replace(SHA, "<A>").replace("b" * 64, "<B>"), p.stat().st_size)
                   for p in folder.iterdir())
    info = {"files": files, **extra}
    if store is not None:
        info.update(notice=store.notice, items=len(store.items), read_only=store.read_only)
    return repr(info)


def test_annotation_survives_forced_kill(tmp_path):
    folder = tmp_path / "annotations"
    folder.mkdir()
    code = _CHILD.format(src=str(ROOT / "src"), sha=SHA, folder=str(folder))
    r = subprocess.run([sys.executable, "-c", code, "once"], timeout=60, capture_output=True, text=True)
    assert r.returncode == 9, _diag(folder, rc=r.returncode, stderr=r.stderr[-2000:])
    s = an.AnnotationStore(SHA, folder)
    assert [it["note"] for it in s.items] == ["급사 직전"] and s.notice is None,         _diag(folder, s, stderr=r.stderr[-2000:])

    sha2 = "b" * 64
    code = _CHILD.format(src=str(ROOT / "src"), sha=sha2, folder=str(folder))
    proc = subprocess.Popen([sys.executable, "-c", code, "loop"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    try:
        first = proc.stdout.readline().strip()
        if first == "first saved=True":
            time.sleep(0.5)  # 저장을 여러 번 하는 중에
    finally:
        proc.kill()  # TerminateProcess — 쓰던 중이어도 바로 끝난다
        proc.wait(10)
        err = proc.stderr.read()[-2000:]
        proc.stdout.close()
        proc.stderr.close()
    assert first == "first saved=True", _diag(folder, first=first, rc=proc.returncode, stderr=err)
    s = an.AnnotationStore(sha2, folder)
    d = _diag(folder, s, rc=proc.returncode, stderr=err)
    assert s.notice is None, d  # 깨지지 않았다
    assert len(s.items) >= 1, d
    assert not list(folder.glob("*.damaged")), d


# --------------------------------------------------------------------------- 글자 선택 계산

CHARS = [  # 두 줄: "가나 다" / "라마"
    ("가", 10, 10, 20, 22, 0), ("나", 20, 10, 30, 22, 0), (" ", 30, 10, 34, 22, 0), ("다", 34, 10, 44, 22, 0),
    ("라", 10, 30, 20, 42, 1), ("마", 20, 30, 30, 42, 1),
]


def test_char_at_and_selection():
    assert an.char_at(CHARS, 25, 15) == 1
    assert an.char_at(CHARS, 100, 16) == 3  # 줄 오른쪽 바깥 → 그 줄 마지막 글자
    assert an.char_at(CHARS, 0, 40) == 4
    assert an.char_at([], 1, 1) is None
    rects, text = an.selection(CHARS, 5, 1)  # 거꾸로 끌어도
    assert text == "나 다\n라마"
    assert rects == [[20, 10, 44, 22], [10, 30, 30, 42]]


# --------------------------------------------------------------------------- 산출물 이름·폴더 (§3-3, D4)


def test_output_name_and_default_folder(tmp_path):
    case = tmp_path / "사건"
    docs = tmp_path / "문서"
    case.mkdir()
    docs.mkdir()
    orig = case / "갑1 계약서.pdf"
    orig.write_bytes(b"%PDF")
    p = outputs.default_export_path(str(orig), outputs.SUFFIX_NOTES, None, str(docs))
    assert p == docs / "갑1 계약서_메모.pdf"
    p.write_bytes(b"x")
    assert outputs.default_export_path(str(orig), outputs.SUFFIX_NOTES, None, str(docs)).name == "갑1 계약서_메모(2).pdf"
    (docs / "갑1 계약서_메모(2).pdf").write_bytes(b"x")
    assert outputs.unique_path(docs, "갑1 계약서_메모.pdf").name == "갑1 계약서_메모(3).pdf"
    # 마지막 내보낸 폴더가 원본 폴더면 쓰지 않는다 (D4)
    assert outputs.default_export_dir(str(orig), str(case), str(docs)) == str(docs)
    other = tmp_path / "보낸 곳"
    other.mkdir()
    assert outputs.default_export_dir(str(orig), str(other), str(docs)) == str(other)
    assert outputs.default_export_dir(str(orig), None, str(case)) != str(case)  # 문서 폴더가 원본 폴더여도
    assert outputs.is_protected(str(case / "갑1 계약서.PDF"), [str(orig)])
    assert not outputs.is_protected(str(docs / "갑1 계약서_메모.pdf"), [str(orig)])
    outputs.set_last_export_dir(str(other), tmp_path / "home")
    assert outputs.last_export_dir(tmp_path / "home") == str(other)
