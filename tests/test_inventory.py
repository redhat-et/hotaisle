"""Tests for examples/inventory.py (pure helpers + end-to-end against a fake API)."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location("inventory",
                                              os.path.join(ROOT, "examples", "inventory.py"))
inventory = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inventory)

from hotaisle import Client  # noqa: E402

VM_LIST = [{"deployment_id": "vm-id-1", "name": "vm-01", "description": "web",
            "ip_address": "10.0.0.5", "cpu_cores": 8, "ram_capacity": 34359738368,
            "disk_capacity": 107374182400, "gpus": [],
            "ssh_access": {"ip_address": "203.0.113.10", "port": 22}}]
BM_LIST = [{"deployment_id": "bm-id-1", "name": "srv-01", "ip_address": "10.0.0.9",
            "manufacturer": "Dell", "model": "XE9680", "support_access_enabled": False,
            "cpu_cores": 64, "ram_capacity": 549755813888,
            "disk_capacity": 4398046511104,
            "gpus": [{"count": 8, "manufacturer": "AMD", "model": "MI300X"}]}]


class API(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self):
        p = self.path.split("?")[0]
        if p.endswith("/virtual_machines/"):
            payload = VM_LIST
        elif p.endswith("/bare_metal/"):
            payload = BM_LIST
        elif p == "/api/teams/":
            payload = [{"handle": "acme", "name": "Acme", "roles": ["owner"],
                        "effective_roles": ["owner"]}]
        else:
            self.send_response(404)
            self.send_header("Content-Length", "9")
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = do_DELETE = reply


class PROM(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps({"status": "success", "data": {"result": [
            {"metric": {"instance": "10.0.0.5:9100"}, "value": [0, "1"]},
            {"metric": {"instance": "10.0.0.9:9100"}, "value": [0, "0"]},
        ]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


class InventoryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.api = HTTPServer(("127.0.0.1", 0), API)
        cls.prom = HTTPServer(("127.0.0.1", 0), PROM)
        for s in (cls.api, cls.prom):
            threading.Thread(target=s.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/api" % cls.api.server_port
        cls.prom_url = "http://127.0.0.1:%d" % cls.prom.server_port

    @classmethod
    def tearDownClass(cls):
        cls.api.shutdown()
        cls.api.server_close()
        cls.prom.shutdown()
        cls.prom.server_close()

    def setUp(self):
        for k in list(os.environ):
            if k.startswith("HOTAISLE_"):
                del os.environ[k]
        os.environ["HOTAISLE_API_KEY"] = "test-key"
        os.environ["HOTAISLE_BASE_URL"] = self.base
        os.environ["HOTAISLE_CONFIG"] = "/nonexistent/x.toml"

    def test_match_up_strips_port(self):
        up = {"10.0.0.5:9100": True, "10.0.0.9:9100": False}
        self.assertIs(inventory.match_up("10.0.0.5", up), True)
        self.assertIs(inventory.match_up("10.0.0.9", up), False)
        self.assertIsNone(inventory.match_up("10.9.9.9", up))
        self.assertIsNone(inventory.match_up("", up))
        self.assertIsNone(inventory.match_up("10.0.0.5", None))

    def test_prometheus_up_parses_instances(self):
        up = inventory.prometheus_up(self.prom_url)
        self.assertEqual(up["10.0.0.5:9100"], True)
        self.assertEqual(up["10.0.0.9:9100"], False)

    def test_prometheus_unreachable_returns_none_and_still_writes(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "inv.json")
            # Port 1 is not listening.
            code = inventory.main(["--team", "acme", "-o", out, "--quiet",
                                   "--prometheus", "http://127.0.0.1:1"])
            self.assertEqual(code, 0, "Prometheus outage must not fail the run")
            data = read_json(out)
            self.assertIsNone(data["virtual_machines"][0]["monitored"])

    def test_end_to_end_writes_inventory(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "inv.json")
            code = inventory.main(["--team", "acme", "-o", out, "--quiet",
                                   "--prometheus", self.prom_url])
            self.assertEqual(code, 0)
            data = read_json(out)
            self.assertEqual(data["counts"], {"virtual_machines": 1, "bare_metal": 1})
            vm = data["virtual_machines"][0]
            self.assertEqual(vm["deployment_id"], "vm-id-1")
            self.assertEqual(vm["ram_bytes"], 34359738368)
            self.assertIs(vm["monitored"], True)
            bm = data["bare_metal"][0]
            self.assertEqual(bm["manufacturer"], "Dell")
            self.assertEqual(bm["gpus"][0]["model"], "MI300X")
            self.assertIs(bm["monitored"], False)   # this one is down
            self.assertTrue(data["generated_at"].endswith("+00:00"))

    def test_atomic_write_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            inventory.atomic_write(path, "hello")
            with open(path) as fh:
                self.assertEqual(fh.read(), "hello")
            self.assertEqual(os.listdir(d), ["out.json"])

    @staticmethod
    def _run_capturing_stdout(argv):
        """Run main() with stdout captured so JSON never pollutes test output."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = inventory.main(argv)
        return code, buf.getvalue()

    def test_no_api_key_exits_1(self):
        del os.environ["HOTAISLE_API_KEY"]
        code, _ = self._run_capturing_stdout(["--team", "acme", "-o", "-", "--quiet"])
        self.assertEqual(code, 1)

    def test_single_team_is_auto_selected(self):
        # The fixture returns exactly one team, so --team may be omitted.
        code, text = self._run_capturing_stdout(["-o", "-", "--quiet"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(text)["team"], "acme")

    def test_stdout_mode_emits_valid_json(self):
        code, text = self._run_capturing_stdout(["--team", "acme", "-o", "-",
                                                 "--quiet"])
        self.assertEqual(code, 0)
        data = json.loads(text)
        self.assertEqual(data["counts"]["virtual_machines"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
