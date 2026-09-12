"""Offline test suite: runs the client against a local fake Hot Aisle server.

Verifies the parts that are easy to get wrong against the real spec:
  * ``Authorization: Token <key>`` header format and key normalisation
  * PascalCase wrapper keys on the /available/ endpoints
  * flattened specs on VM listings vs nested ``specs`` on bare metal
  * exact request bodies for create, and ?force= query encoding
  * DELETE paths (deployment_id, not name) and 204 handling
  * status -> exception mapping, retries on 5xx, team resolution

No API key or network access needed. Run: python3 tests/test_client.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hotaisle import Client, errors, models  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

from hotaisle.client import _read_body_limited  # noqa: E402

from hotaisle.auth import (  # noqa: E402
    mask, normalize_key, resolve_api_key, warn_if_world_readable,
)

KEY = "abc123-def456-ghi789"

_KEY_VARS = ("HOTAISLE_API_KEY", "HOTAISLE_TOKEN", "HOTAISLE_API_KEY_FILE",
             "HOTAISLE_API_KEY_COMMAND", "HOTAISLE_KEYRING")


@contextmanager
def no_env_keys():
    """Temporarily hide every key source in the environment.

    Without this the HOTAISLE_API_KEY set by setUp() outranks the config-file
    sources a test is trying to exercise.
    """
    saved = {k: os.environ.pop(k, None) for k in _KEY_VARS}
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)

VM_AVAILABLE = [
    {
        "Quantity": 4,
        "MinimumReservationMinutes": 30,
        "OnDemandPrice": 350,
        "Specs": {
            "cpu_cores": 8,
            "ram_capacity": 34359738368,
            "disk_capacity": 107374182400,
            "cpus": [{"count": 1, "manufacturer": "AMD", "model": "EPYC 9334",
                      "cores": 32, "frequency": 2600000000}],
            "gpus": [],
        },
    },
    {
        "Quantity": 0,
        "MinimumReservationMinutes": 30,
        "OnDemandPrice": 1200,
        "Specs": {
            "cpu_cores": 32,
            "ram_capacity": 137438953472,
            "disk_capacity": 1099511627776,
            "gpus": [{"count": 1, "manufacturer": "NVIDIA", "model": "L40S"}],
        },
    },
]

BM_AVAILABLE = [
    {
        "Quantity": 2,
        "MinimumReservationMinutes": 480,
        "OnDemandPrice": 2100,
        "Specs": {
            "cpu_cores": 64,
            "ram_capacity": 549755813888,
            "disk_capacity": 4398046511104,
            "cpus": [{"count": 2, "manufacturer": "Intel", "model": "Xeon 8470Q",
                      "cores": 32, "frequency": 2600000000}],
            "memory_modules": [{"count": 16, "manufacturer": "Samsung",
                                "model": "DDR5-4800", "capacity": 34359738368}],
            "disks": [{"count": 4, "manufacturer": "Micron", "model": "7450",
                       "capacity": 1099511627776, "type": "NVMe"}],
            "gpus": [{"count": 8, "manufacturer": "AMD", "model": "MI300X"}],
        },
    }
]

# VM details are VirtualMachine + VirtualMachineSpecs flattened into one object.
VMS = [
    {
        "deployment_id": "195116dc-32ed-49e5-a738-5e2ad0cdd141",
        "name": "vm-01",
        "description": "Production web server",
        "ip_address": "192.168.1.200",
        "ssh_access": {"ip_address": "203.0.113.10", "port": 2222,
                       "dns_name": "vm01.example.com"},
        "cpu_cores": 8,
        "ram_capacity": 34359738368,
        "disk_capacity": 107374182400,
        "gpus": [],
    }
]

# BareMetalServerDetails is allOf[BareMetalServer, BareMetalServerSpecs, {os_status}],
# so the LIST endpoint returns specs flattened alongside the identity fields.
BMS = [
    {
        "deployment_id": "77b3e2a2-5a67-4c07-9f2e-9d5d6f0a1b2c",
        "name": "server-01",
        "description": "Production database server",
        "ip_address": "192.168.1.100",
        "manufacturer": "Dell",
        "model": "PowerEdge XE9680",
        "support_access_enabled": False,
        "ssh_access": {"ip_address": "203.0.113.11", "port": 22},
        "cpu_cores": 64,
        "ram_capacity": 549755813888,
        "disk_capacity": 4398046511104,
        "gpus": [{"count": 8, "manufacturer": "AMD", "model": "MI300X"}],
        "os_status": {"status": "installing"},
    }
]

# BareMetalServerReservationResponse is allOf[BareMetalServer, {os_status, specs}],
# so the CREATE response nests specs under "specs" instead.
BM_CREATED_NESTED = {
    "deployment_id": "new-bm-id",
    "name": "server-new",
    "ip_address": "10.0.0.9",
    "manufacturer": "Dell",
    "model": "PowerEdge XE9680",
    "support_access_enabled": False,
    "specs": {
        "cpu_cores": 64,
        "ram_capacity": 549755813888,
        "disk_capacity": 4398046511104,
        "gpus": [{"count": 8, "manufacturer": "AMD", "model": "MI300X"}],
    },
}

TEAMS = [
    {"handle": "acme-corp", "name": "Acme Corporation", "roles": ["owner"],
     "effective_roles": ["owner", "operator"], "maximum_virtual_machines": 10,
     "maximum_bare_metal_servers": 5}
]


class FakeAPI(BaseHTTPRequestHandler):
    """Records requests and replays canned spec-shaped responses."""

    calls = []
    fail_times = 0  # number of leading 500s to return for the current path

    def log_message(self, *a):  # silence
        pass

    def _send(self, code, payload=None, raw=None, ctype="application/json"):
        body = b"" if raw is None else (raw if isinstance(raw, bytes) else raw.encode())
        if payload is not None:
            body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        entry = {"method": self.command, "path": self.path,
                 "auth": self.headers.get("Authorization"),
                 "content_type": self.headers.get("Content-Type"),
                 "body": body.decode() if body else None}
        FakeAPI.calls.append(entry)
        return entry

    def _route(self):
        rec = self._record()
        path, method = rec["path"], rec["method"]
        clean = path.split("?")[0]

        if FakeAPI.fail_times > 0:
            FakeAPI.fail_times -= 1
            return self._send(500, raw="boom")

        table = {
            ("GET", "/api/user/"): (200, {"id": 1, "email": "ops@acme.test",
                                          "name": "Ops"}),
            ("GET", "/api/teams/"): (200, TEAMS),
            ("GET", "/api/teams/acme-corp/balance/"): (200, {"balance": 25000}),
            ("GET", "/api/teams/acme-corp/virtual_machines/"): (200, VMS),
            ("GET", "/api/teams/acme-corp/virtual_machines/available/"): (200, VM_AVAILABLE),
            ("GET", "/api/teams/acme-corp/bare_metal/"): (200, BMS),
            ("GET", "/api/teams/acme-corp/bare_metal/available/"): (200, BM_AVAILABLE),
            ("GET", "/api/user/ssh_keys/"): (200, [{"fingerprint": "AA:BB", "key": "ssh-rsa AAA"}]),
        }
        if (method, clean) in table:
            code, payload = table[(method, clean)]
            return self._send(code, payload)

        if clean.startswith("/api/teams/acme-corp/virtual_machines/") and clean.endswith("/state/"):
            return self._send(200, {"state": "running", "host": "vm-host-01"})

        if method == "POST" and clean == "/api/teams/acme-corp/virtual_machines/":
            sent = json.loads(rec["body"] or "{}")
            return self._send(200, {
                "deployment_id": "new-vm-id", "name": "vm-new",
                "ip_address": "10.0.0.5", "cpu_cores": sent.get("specs", sent).get("cpu_cores"),
                "ram_capacity": sent.get("specs", sent).get("ram_capacity"),
                "disk_capacity": sent.get("specs", sent).get("disk_capacity"),
            })
        if method == "POST" and clean == "/api/teams/acme-corp/bare_metal/":
            return self._send(201, {
                "deployment_id": "new-bm-id", "name": "server-new",
                "manufacturer": "Dell", "model": "PowerEdge XE9680",
                "ip_address": "10.0.0.9",
                "specs": json.loads(rec["body"] or "{}").get("specs", {}),
            })
        if method == "DELETE" and clean.startswith("/api/teams/acme-corp/virtual_machines/"):
            return self._send(204)
        if method == "DELETE" and clean.startswith("/api/teams/acme-corp/bare_metal/"):
            return self._send(204)
        if method == "POST" and "/power/power_on/" in clean:
            return self._send(200, {"state": "On"})
        if method == "GET" and "/power/" in clean:
            return self._send(200, {"state": "On"})
        if clean == "/api/teams/locked/virtual_machines/":
            return self._send(403, raw="Forbidden: Permission denied", ctype="text/plain")
        if clean == "/api/teams/broke/bare_metal/":
            return self._send(400, raw="Bad Request: minimum usage not met",
                              ctype="text/plain")
        if clean.endswith("/insufficient/"):
            return self._send(402, raw="Payment required", ctype="text/plain")
        if clean == "/api/bad-key/":
            return self._send(401, raw="Unauthorized", ctype="text/plain")
        if clean == "/api/no-ssh-key/":
            return self._send(428, raw="Team has no accepted member with an SSH key",
                              ctype="text/plain")
        if clean.startswith("/api/teams/missing/"):
            return self._send(404, raw="Not Found", ctype="text/plain")
        return self._send(404, raw="Not Found", ctype="text/plain")

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _route


class HotAisleTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), FakeAPI)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = "http://127.0.0.1:%d/api" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeAPI.calls = []
        FakeAPI.fail_times = 0
        os.environ["HOTAISLE_API_KEY"] = KEY
        os.environ.pop("HOTAISLE_TEAM", None)
        self.client = Client(base_url=self.base, team="acme-corp", max_retries=1,
                             timeout=10)

    # ------------------------------------------------------------- auth

    def test_token_header_format(self):
        self.client.list_teams()
        self.assertEqual(FakeAPI.calls[0]["auth"], "Token %s" % KEY)

    def test_preformatted_token_is_normalised(self):
        c = Client(api_key="Token  " + KEY, base_url=self.base)
        self.assertEqual(c.api_key, KEY)
        self.assertEqual(c.auth_header, "Token %s" % KEY)

    def test_bearer_prefix_stripped_too(self):
        self.assertEqual(normalize_key("Bearer xyz"), "xyz")
        self.assertEqual(normalize_key('  "abc"  '), "abc")

    def test_key_source_reported(self):
        self.assertIn("environment", self.client.credential.source)

    def test_mask_never_leaks_full_key(self):
        m = mask(KEY)
        self.assertNotIn(KEY, m)
        self.assertTrue(m.startswith(KEY[:4]))

    def test_missing_key_raises_with_hint(self):
        for var in ("HOTAISLE_API_KEY", "HOTAISLE_TOKEN", "HOTAISLE_API_KEY_FILE",
                    "HOTAISLE_API_KEY_COMMAND"):
            os.environ.pop(var, None)
        with self.assertRaises(errors.ConfigurationError) as ctx:
            Client(base_url=self.base)
        self.assertIn("HOTAISLE_API_KEY", str(ctx.exception))
        os.environ["HOTAISLE_API_KEY"] = KEY

    def test_key_file_source(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as fh:
            fh.write(KEY + "\n")
            path = fh.name
        os.environ.pop("HOTAISLE_API_KEY")
        os.environ["HOTAISLE_API_KEY_FILE"] = path
        try:
            c = Client(base_url=self.base)
            self.assertEqual(c.api_key, KEY)
            self.assertIn("file", c.credential.source)
        finally:
            os.environ.pop("HOTAISLE_API_KEY_FILE")
            os.environ["HOTAISLE_API_KEY"] = KEY
            os.unlink(path)

    def test_key_command_source(self):
        os.environ.pop("HOTAISLE_API_KEY")
        os.environ["HOTAISLE_API_KEY_COMMAND"] = "printf %s" % KEY
        try:
            c = Client(base_url=self.base)
            self.assertEqual(c.api_key, KEY)
            self.assertIn("KEY_COMMAND", c.credential.source.upper())
        finally:
            os.environ.pop("HOTAISLE_API_KEY_COMMAND")
            os.environ["HOTAISLE_API_KEY"] = KEY

    def test_failing_key_command_is_loud(self):
        # HOTAISLE_API_KEY outranks key_command, so remove it to exercise this path.
        os.environ.pop("HOTAISLE_API_KEY")
        os.environ["HOTAISLE_API_KEY_COMMAND"] = "false"
        try:
            with self.assertRaises(errors.ConfigurationError):
                Client(base_url=self.base)
        finally:
            os.environ.pop("HOTAISLE_API_KEY_COMMAND")
            os.environ["HOTAISLE_API_KEY"] = KEY

    def test_no_env_keys_helper_restores_state(self):
        with no_env_keys():
            self.assertNotIn("HOTAISLE_API_KEY", os.environ)
        self.assertEqual(os.environ.get("HOTAISLE_API_KEY"), KEY)

    def test_key_source_precedence_env_beats_command(self):
        os.environ["HOTAISLE_API_KEY"] = KEY
        os.environ["HOTAISLE_API_KEY_COMMAND"] = "printf should-not-be-used"
        try:
            c = Client(base_url=self.base)
            self.assertEqual(c.api_key, KEY)
            self.assertIn("environment", c.credential.source)
        finally:
            os.environ.pop("HOTAISLE_API_KEY_COMMAND")

    def test_config_key_command_then_key_file_then_inline(self):
        """Within the config file: key_command > key_file > api_key."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as fh:
            fh.write("file-key-value\n")
            path = fh.name
        try:
            with no_env_keys():
                c = resolve_api_key(config={"key_command": "printf cmd-key-value",
                                            "key_file": path, "api_key": "inline"})
                self.assertEqual(c.api_key, "cmd-key-value")
                self.assertIn("config", c.source)
                c = resolve_api_key(config={"key_file": path, "api_key": "inline"})
                self.assertEqual(c.api_key, "file-key-value")
                c = resolve_api_key(config={"api_key": "inline-value"})
                self.assertEqual(c.api_key, "inline-value")
                with self.assertRaises(errors.ConfigurationError):
                    resolve_api_key(config={"key_file": "/nope/missing.key"})
        finally:
            os.unlink(path)

    def test_env_command_beats_config_entries(self):
        os.environ.pop("HOTAISLE_API_KEY")
        os.environ["HOTAISLE_API_KEY_COMMAND"] = "printf env-wins"
        try:
            c = resolve_api_key(config={"key_file": "/nope/missing.key",
                                        "api_key": "inline"})
            self.assertEqual(c.api_key, "env-wins")
        finally:
            os.environ.pop("HOTAISLE_API_KEY_COMMAND")
            os.environ["HOTAISLE_API_KEY"] = KEY

    def test_empty_key_file_is_an_error(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as fh:
            path = fh.name
        try:
            with no_env_keys():
                with self.assertRaises(errors.ConfigurationError):
                    resolve_api_key(config={"key_file": path})
        finally:
            os.unlink(path)

    def test_config_file_is_world_readable_warning(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('api_key = "k"\n')
            path = fh.name
        try:
            os.chmod(path, 0o644)
            self.assertIn("readable by group", warn_if_world_readable(_Path(path)))
            os.chmod(path, 0o600)
            self.assertIsNone(warn_if_world_readable(_Path(path)))
        finally:
            os.unlink(path)

    def test_401_becomes_auth_error(self):
        with self.assertRaises(errors.AuthError) as ctx:
            self.client.raw("GET", "/bad-key/")
        self.assertEqual(ctx.exception.status_code, 401)

    # ---------------------------------------------------------- listings

    def test_list_vms_flattened_specs(self):
        vms = self.client.list_virtual_machines()
        self.assertEqual(len(vms), 1)
        vm = vms[0]
        self.assertEqual(vm.deployment_id, "195116dc-32ed-49e5-a738-5e2ad0cdd141")
        self.assertEqual(vm.specs.cpu_cores, 8)
        self.assertEqual(vm.specs.ram_capacity, 34359738368)
        self.assertAlmostEqual(vm.specs.ram_gib, 32.0)
        self.assertEqual(vm.ssh_access.ssh_command, "ssh -p 2222 root@vm01.example.com")
        self.assertIn("vm-01", vm.name)

    def test_list_bm_flattened_specs_and_hardware(self):
        """GET /bare_metal/ flattens specs (allOf), not nested."""
        servers = self.client.list_bare_metal()
        s = servers[0]
        self.assertEqual(s.hardware, "Dell PowerEdge XE9680")
        self.assertEqual(s.specs.cpu_cores, 64)
        self.assertEqual(s.specs.ram_capacity, 549755813888)
        self.assertEqual(s.specs.gpu_count, 8)
        self.assertIn("MI300X", s.specs.gpu_summary)
        self.assertIs(s.support_access_enabled, False)
        self.assertIn("installing", str(s.os_status))

    def test_bm_create_response_nested_specs(self):
        """POST /bare_metal/ nests specs under "specs"; that must parse too."""
        s = models.BareMetalServer.from_dict(BM_CREATED_NESTED)
        self.assertEqual(s.specs.cpu_cores, 64)
        self.assertEqual(s.specs.ram_capacity, 549755813888)
        self.assertEqual(s.specs.gpu_count, 8)
        self.assertEqual(s.hardware, "Dell PowerEdge XE9680")

    def test_available_pascalcase_wrapper(self):
        avail = self.client.list_available_virtual_machines()
        self.assertEqual(len(avail), 2)
        self.assertEqual(avail[0].quantity, 4)
        self.assertEqual(avail[0].specs.cpu_cores, 8)
        self.assertEqual(avail[0].minimum_reservation, "30m")
        self.assertEqual(avail[0].price_per_hour, "$3.50/hr")
        self.assertEqual(avail[1].specs.gpu_count, 1)

    def test_available_bm_min_reservation_hours(self):
        avail = self.client.list_available_bare_metal()
        self.assertEqual(avail[0].minimum_reservation, "8h")
        self.assertEqual(avail[0].price_per_hour, "$21.00/hr")
        self.assertEqual(avail[0].specs.gpu_count, 8)

    def test_unknown_fields_are_tolerated(self):
        models.VirtualMachine.from_dict({"name": "x", "brand_new_field": {"a": 1},
                                         "deployment_id": "d"})
        models.Specs.from_dict({"cpu_cores": 2, "ram_capacity": 1, "disk_capacity": 1,
                                "quantum_cores": 4})

    def test_team_defaults_from_env(self):
        os.environ["HOTAISLE_TEAM"] = "acme-corp"
        try:
            c = Client(base_url=self.base, max_retries=0)
            self.assertEqual(len(c.list_virtual_machines()), 1)
        finally:
            os.environ.pop("HOTAISLE_TEAM")

    def test_missing_team_raises_helpful_error(self):
        c = Client(base_url=self.base, max_retries=0)
        with self.assertRaises(errors.ConfigurationError) as ctx:
            c.list_virtual_machines()
        self.assertIn("team", str(ctx.exception))

    # ----------------------------------------------------------- creation

    def test_create_vm_body_shape(self):
        vm = self.client.create_virtual_machine(cpu_cores=8, ram_capacity=34359738368,
                                               disk_capacity=107374182400,
                                               description="worker")
        body = json.loads(FakeAPI.calls[-1]["body"])
        # VM bodies carry specs flattened at top level, not nested.
        self.assertEqual(body["cpu_cores"], 8)
        self.assertEqual(body["description"], "worker")
        self.assertNotIn("specs", body)
        self.assertEqual(FakeAPI.calls[-1]["content_type"], "application/json")
        self.assertEqual(vm.deployment_id, "new-vm-id")

    def test_create_vm_sends_user_data_url(self):
        self.client.create_virtual_machine(cpu_cores=8, ram_capacity=1, disk_capacity=1,
                                          user_data_url="https://x.test/ud.yaml")
        body = json.loads(FakeAPI.calls[-1]["body"])
        self.assertEqual(body["user_data_url"], "https://x.test/ud.yaml")

    def test_create_vm_requires_all_specs(self):
        with self.assertRaises(errors.ConfigurationError):
            self.client.create_virtual_machine(cpu_cores=8)

    def test_create_bm_wraps_specs_and_returns_201(self):
        s = self.client.create_bare_metal(cpu_cores=64, ram_capacity=549755813888,
                                         disk_capacity=4398046511104,
                                         description="gpu box")
        body = json.loads(FakeAPI.calls[-1]["body"])
        self.assertEqual(body["specs"]["cpu_cores"], 64)
        self.assertEqual(body["description"], "gpu box")
        self.assertEqual(s.deployment_id, "new-bm-id")
        self.assertEqual(s.hardware, "Dell PowerEdge XE9680")

    def test_force_query_encoding(self):
        self.client.create_virtual_machine(cpu_cores=1, ram_capacity=1, disk_capacity=1,
                                          force=True)
        self.assertIn("force=true", FakeAPI.calls[-1]["path"])
        FakeAPI.calls.clear()
        self.client.create_virtual_machine(cpu_cores=1, ram_capacity=1, disk_capacity=1)
        self.assertNotIn("force", FakeAPI.calls[-1]["path"])

    def test_specs_to_selector_matches_server_expectation(self):
        avail = self.client.list_available_bare_metal()[0]
        sel = self.client_specs_selector(avail)
        self.assertEqual(sel["cpu_cores"], 64)
        self.assertEqual(sel["gpus"][0]["model"], "MI300X")
        self.assertNotIn("memory_modules", sel)  # not required to match

    @staticmethod
    def client_specs_selector(avail):
        from hotaisle.client import specs_to_selector
        return specs_to_selector(avail)

    # ----------------------------------------------------------- deletion

    def test_delete_vm_uses_deployment_id_path(self):
        self.client.delete_virtual_machine("195116dc-32ed-49e5-a738-5e2ad0cdd141")
        rec = FakeAPI.calls[-1]
        self.assertEqual(rec["method"], "DELETE")
        self.assertEqual(rec["path"],
                         "/api/teams/acme-corp/virtual_machines/"
                         "195116dc-32ed-49e5-a738-5e2ad0cdd141/")
        self.assertIsNone(rec["body"])

    def test_delete_bm_204_is_success(self):
        self.client.delete_bare_metal("77b3e2a2-5a67-4c07-9f2e-9d5d6f0a1b2c")
        self.assertEqual(FakeAPI.calls[-1]["method"], "DELETE")

    def test_delete_vm_with_force(self):
        self.client.delete_virtual_machine("abc", force=True)
        self.assertIn("force=true", FakeAPI.calls[-1]["path"])

    # ------------------------------------------------------- error mapping

    def test_forbidden_is_auth_error(self):
        with self.assertRaises(errors.AuthError) as ctx:
            self.client.list_virtual_machines(team="locked")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_404_is_not_found(self):
        with self.assertRaises(errors.NotFoundError):
            self.client.list_virtual_machines(team="missing")

    def test_400_is_validation_error(self):
        with self.assertRaises(errors.ValidationError) as ctx:
            self.client.list_bare_metal(team="broke")
        self.assertIn("minimum usage", str(ctx.exception))

    def test_402_is_insufficient_balance(self):
        with self.assertRaises(errors.InsufficientBalanceError):
            self.client.raw("GET", "/teams/acme-corp/insufficient/")

    def test_428_is_precondition(self):
        with self.assertRaises(errors.PreconditionFailedError):
            self.client.raw("POST", "/no-ssh-key/")

    def test_retry_then_success(self):
        FakeAPI.fail_times = 2
        c = Client(base_url=self.base, team="acme-corp", max_retries=3, backoff=0.01)
        self.assertEqual(len(c.list_teams()), 1)
        self.assertEqual(FakeAPI.fail_times, 0)

    def test_retry_exhausted_raises(self):
        FakeAPI.fail_times = 5
        c = Client(base_url=self.base, team="acme-corp", max_retries=1, backoff=0.01)
        with self.assertRaises(errors.APIError) as ctx:
            c.list_teams()
        self.assertEqual(ctx.exception.status_code, 500)

    # ------------------------------------------------- body-read timeout

    def test_body_read_timeout_raises_clear_api_error(self):
        """A body that stalls past the deadline becomes a clear error, not a hang."""
        import socket as _socket

        class StalledResponse:
            """Mimics an http.client.HTTPResponse whose .read() never returns data."""

            class _Raw:
                def __init__(self):
                    self._sock = None

                def settimeout(self, t):
                    self._sock = t  # record it so the only observable effect is time

            raw = _Raw()
            fp = None

            def read(self, n=-1):
                raise _socket.timeout("timed out")

            def close(self):
                pass

        fake = StalledResponse()
        with self.assertRaises(errors.APIError) as ctx:
            _read_body_limited(fake, "http://example/api", timeout=0.2)
        self.assertIn("Timed out reading the response body", str(ctx.exception))
        self.assertEqual(ctx.exception.status_code, 0)

    def test_body_read_multi_chunk_success(self):
        """A body that returns data in chunks still completes under the deadline."""
        class ChunkedResponse:
            def __init__(self):
                self._chunks = iter([b"a" * 70000, b"b" * 1000])
                self.raw = self
                self.fp = None

            def read(self, n=-1):
                return next(self._chunks, b"")

            def settimeout(self, t):
                pass

        fake = ChunkedResponse()
        body = _read_body_limited(fake, "http://example/api", timeout=2.0)
        self.assertEqual(body, b"a" * 70000 + b"b" * 1000)

    def test_request_success_path_uses_bounded_read(self):
        """The real request() path still returns correct bodies (no regression)."""
        teams = self.client.list_teams()
        self.assertEqual([t.handle for t in teams], ["acme-corp"])

    # ------------------------------------------------------------- misc

    def test_balance_model(self):
        self.assertEqual(str(self.client.get_balance()), "$250.00")

    def test_vm_state(self):
        st = self.client.get_virtual_machine_state("195116dc-32ed-49e5-a738-5e2ad0cdd141")
        self.assertEqual(st.state, "running")
        self.assertEqual(st.host, "vm-host-01")

    def test_bm_power_action(self):
        self.assertEqual(self.client.get_bare_metal_power("x"), "On")
        out = self.client.bare_metal_action("x", "power/power_on")
        self.assertEqual(out["state"], "On")

    def test_base_url_normalisation(self):
        for raw in ["https://admin.hotaisle.app", "https://admin.hotaisle.app/",
                    "https://admin.hotaisle.app/api", "https://admin.hotaisle.app/api/",
                    "https://admin.hotaisle.app/api/docs", "admin.hotaisle.app/api"]:
            c = Client(api_key=KEY, base_url=raw)
            self.assertEqual(c.base_url, "https://admin.hotaisle.app/api", raw)

    def test_parse_size(self):
        self.assertEqual(models.parse_size("16G"), 16 * 1024 ** 3)
        self.assertEqual(models.parse_size("512GiB"), 512 * 1024 ** 3)
        self.assertEqual(models.parse_size("1.5T"), int(1.5 * 1024 ** 4))
        self.assertEqual(models.parse_size("34359738368"), 34359738368)
        with self.assertRaises(ValueError):
            models.parse_size("banana")

    def test_human_bytes(self):
        self.assertEqual(models.human_bytes(34359738368), "32 GiB")
        self.assertEqual(models.human_bytes(4398046511104), "4.0 TiB")
        self.assertEqual(models.human_bytes(None), "-")

    def test_repr_does_not_leak_key(self):
        self.assertNotIn(KEY, repr(self.client.credential))

    def test_read_body_limited_returns_empty_for_no_data(self):
        """An empty body (204-style) should return b'' without error."""
        class EmptyResponse:
            raw = self
            fp = None

            def read(self, n=-1):
                return b""

            def close(self):
                pass

            def settimeout(self, t):
                pass

        self.assertEqual(_read_body_limited(EmptyResponse(), "http://x/", 1.0), b"")


if __name__ == "__main__":
    unittest.main(verbosity=2)
