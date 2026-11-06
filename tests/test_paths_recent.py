from __future__ import annotations

import json
import os

from bom_pdf import paths
from bom_pdf.services.recent import MAX_RECENT, RecentDocuments


def test_data_dir_uses_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(paths.ENV_HOME, str(tmp_path / "home"))
    assert paths.data_dir() == tmp_path / "home"
    assert paths.logs_dir().is_dir()


def test_atomic_write_and_damaged_preserved(tmp_path):
    p = tmp_path / "x.json"
    paths.atomic_write_json(p, {"a": 1})
    assert paths.read_json(p, None) == {"a": 1}
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    p.write_text("{broken", encoding="utf-8")
    assert paths.read_json(p, {"d": 0}) == {"d": 0}
    assert (tmp_path / "x.json.damaged").read_text(encoding="utf-8") == "{broken"


def test_recent_documents_order_dedup_cap(tmp_path):
    r = RecentDocuments(tmp_path)
    for i in range(MAX_RECENT + 3):
        r.add(str(tmp_path / f"f{i}.pdf"))
    r.add(str(tmp_path / "f5.pdf"))
    items = r.list()
    assert len(items) == MAX_RECENT
    assert items[0].endswith("f5.pdf")
    assert sum(1 for x in items if x.endswith("f5.pdf")) == 1
    data = json.loads((tmp_path / "recent.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    r.remove(items[0])
    assert not any(x.endswith("f5.pdf") for x in r.list())
