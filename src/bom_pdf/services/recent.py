"""최근 문서 목록 — 데이터 폴더 `recent.json` (원본 폴더에는 쓰지 않는다)."""
from __future__ import annotations

import os
from pathlib import Path

from bom_pdf import paths

MAX_RECENT = 15


class RecentDocuments:
    def __init__(self, data_dir: Path | None = None):
        self.path = (data_dir or paths.data_dir()) / "recent.json"

    def list(self) -> list[str]:
        data = paths.read_json(self.path, {})
        items = data.get("paths", []) if isinstance(data, dict) else []
        return [p for p in items if isinstance(p, str)][:MAX_RECENT]

    def add(self, path: str) -> list[str]:
        path = os.path.abspath(path)
        key = os.path.normcase(path)
        items = [path] + [p for p in self.list() if os.path.normcase(p) != key]
        items = items[:MAX_RECENT]
        paths.atomic_write_json(self.path, {"schema_version": 1, "paths": items})
        return items

    def remove(self, path: str) -> list[str]:
        key = os.path.normcase(os.path.abspath(path))
        items = [p for p in self.list() if os.path.normcase(p) != key]
        paths.atomic_write_json(self.path, {"schema_version": 1, "paths": items})
        return items

    def clear(self) -> None:
        paths.atomic_write_json(self.path, {"schema_version": 1, "paths": []})
