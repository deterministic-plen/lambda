"""Viewer hardening: allowlist, Host/Origin checks, NUL handling (S1, S8).

The viewer used to serve the whole parent directory of the opened file and
accepted any ``Host`` header (DNS rebinding).  These tests start the real HTTP
handler on a loopback ephemeral port and probe the boundaries.
"""

from __future__ import annotations

import http.client
import importlib.util
import json
import struct
import threading
from pathlib import Path

import pytest

from conftest import REPO_ROOT

VIEWER_PATH = REPO_ROOT / "tools" / "viewer.py"

_spec = importlib.util.spec_from_file_location("pi_viewer", VIEWER_PATH)
viewer = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(viewer)


def _make_data_dir(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    root.mkdir()
    frames = root / "field.frames.bin"
    frames.write_bytes(struct.pack("<III", 2, 2, 1) + struct.pack("<ffff", 1, 2, 3, 4))
    (root / "results.json").write_text(
        json.dumps({"artifacts": [{"kind": "grid", "name": "field"}]}), encoding="utf-8")
    (root / "field.csv").write_text("row,col,value\n0,0,1\n", encoding="utf-8")
    (root / "secret.txt").write_text("do not serve", encoding="utf-8")
    (root / ".hidden").write_text("dot file", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "private.txt").write_text("nested", encoding="utf-8")
    return root


class _Server:
    def __init__(self, root: Path):
        viewer._SERVED_FILES.clear()
        self.manifest = viewer.load_result_dir(root)
        self.allowed = viewer._allowlist_for(root)
        self.handler = viewer.Handler
        self.httpd = viewer.ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.port = self.httpd.server_address[1]
        self.handler.root = root
        self.handler.manifest = self.manifest
        self.handler.allowed = self.allowed
        self.handler.port = self.port
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=10)

    def get(self, path: str, *, headers: dict | None = None,
            skip_host: bool = False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            if skip_host:
                conn.putrequest("GET", path, skip_host=True)
                for key, value in (headers or {}).items():
                    conn.putheader(key, value)
                conn.endheaders()
            else:
                conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()


def test_allowlist_contains_only_files_the_viewer_read(tmp_path):
    root = _make_data_dir(tmp_path)
    viewer._SERVED_FILES.clear()
    manifest, served_root = viewer.load_source(root)
    allowed = viewer._allowlist_for(served_root)
    assert "field.frames.bin" in allowed
    assert "secret.txt" not in allowed
    assert ".hidden" not in allowed
    assert "results.json" not in allowed
    assert "sub/private.txt" not in allowed

    # A single data file: only that file is servable, never its siblings.
    viewer._SERVED_FILES.clear()
    _, served_root = viewer.load_source(root / "field.csv")
    allowed = viewer._allowlist_for(served_root)
    assert allowed == {"field.csv"}


def test_handler_serves_allowlist_and_rejects_others(tmp_path, capfd):
    root = _make_data_dir(tmp_path)
    server = _Server(root)
    try:
        status, _ = server.get("/viewer.html")
        assert status == 200
        status, body = server.get("/manifest.json")
        assert status == 200 and json.loads(body)["grids"]

        status, body = server.get("/data/field.frames.bin")
        assert status == 200
        assert body == (root / "field.frames.bin").read_bytes()

        assert server.get("/data/secret.txt")[0] == 403
        assert server.get("/data/results.json")[0] == 403
        assert server.get("/data/.hidden")[0] == 404
        assert server.get("/data/sub/private.txt")[0] in (403, 404)
        assert server.get("/data/..%2Fsecret.txt")[0] in (403, 404)

        # S1: a page on any other origin cannot reach the server via DNS rebinding.
        assert server.get("/viewer.html",
                          headers={"Host": "evil.example:1234"}, skip_host=True)[0] == 403
        assert server.get("/manifest.json",
                          headers={"Origin": "http://evil.example"})[0] == 403
        assert server.get("/viewer.html",
                          headers={"Origin": f"http://127.0.0.1:{server.port}"})[0] == 200

        conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
        try:
            conn.request("HEAD", "/viewer.html")
            assert conn.getresponse().status == 405
        finally:
            conn.close()
    finally:
        server.close()
    assert "Traceback" not in capfd.readouterr().err


def test_nul_request_is_rejected_without_killing_the_server(tmp_path, capfd):
    """S8: ``%00`` used to raise an uncaught exception in the handler."""
    server = _Server(_make_data_dir(tmp_path))
    try:
        status, _ = server.get("/data/%00")
        assert status == 400
        # The server survives and keeps serving.
        status, _ = server.get("/manifest.json")
        assert status == 200
        status, _ = server.get("/data/field.frames.bin")
        assert status == 200
    finally:
        server.close()
    assert "Traceback" not in capfd.readouterr().err
