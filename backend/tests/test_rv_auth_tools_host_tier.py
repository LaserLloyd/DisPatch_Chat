"""RV-auth: 4.3 A14 moved the local file viewer to the host tier (the Owner only), but
PUT /api/tools (manage_plugins, every Admin) still lets a second Admin add a `static` panel
rooted at ANY host directory and read its files through /tools/<id>/<file> - the same
"arbitrary bytes off the host" power, one route over."""
from __future__ import annotations

from test_i43_host_tier import _install


def test_second_admin_cannot_root_a_static_tool_on_a_host_folder(world, tmp_path_factory):
    base, ann, _ = _install(world)
    host_dir = tmp_path_factory.mktemp("host-secrets")       # outside the data folder
    (host_dir / "notes.txt").write_text("host-only secret\n")
    row = {"id": "peek", "kind": "static", "title": "Peek", "root": str(host_dir),
           "entry": "notes.txt"}
    cur = ann.get("/api/tools").json().get("tools") or []
    r = ann.put("/api/tools", json={"tools": [*cur, row]})
    if r.status_code == 200:
        got = ann.get("/tools/peek/notes.txt")
        assert "host-only secret" not in got.text, "a second Admin read a host file"
    assert r.status_code == 403 and r.json()["error"]["code"] == "host_owner_only", r.text
