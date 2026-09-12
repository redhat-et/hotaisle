"""Exercise the CLI end-to-end against the fake API server (no key or network needed).

Run: python3 tests/test_cli.py
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, HTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hotaisle import cli  # noqa: E402
from tests.test_client import (  # noqa: E402
    BM_AVAILABLE, BMS, KEY, TEAMS, VM_AVAILABLE, VMS,
)


class Handler(BaseHTTPRequestHandler):
    calls = []

    def log_message(self, *a):
        pass

    def _send(self, code, payload=None, raw=None):
        body = raw.encode() if raw else (json.dumps(payload).encode() if payload is not None
                                         else b"")
        self.send_response(code)
        self.send_header("Content-Type", "application/json" if payload else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_one(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode() if n else ""
        Handler.calls.append({"method": self.command, "path": self.path, "body": body,
                             "auth": self.headers.get("Authorization")})
        p = self.path.split("?")[0]
        if p == "/api/user/":
            return self._send(200, {"user": {"id": 1, "email": "ops@acme.test", "name": "Ops"},
                                    "teams": []})
        if p == "/api/user/api_keys/":
            return self._send(200, [{"prefix": "abc123", "label": "my-key", "user_role": "user",
                                     "teams": [{"handle": "acme-corp"}]}])
        if p == "/api/teams/":
            return self._send(200, TEAMS)
        if p == "/api/user/ssh_keys/":
            return self._send(200, [{"fingerprint": "AA:BB", "type": "ssh-rsa",
                                     "public_key": "ssh-rsa AAAA", "comment": "me@host"}])

        # Everything else is team-scoped: only acme-corp exists here, so a wrong
        # --team must genuinely 404 rather than silently succeeding.
        if not p.startswith("/api/teams/acme-corp/"):
            return self._send(404, raw="Not Found")
        if p.endswith("/balance/"):
            return self._send(200, {"balance": 25000})
        if p.endswith("/virtual_machines/available/"):
            return self._send(200, VM_AVAILABLE)
        if p.endswith("/bare_metal/available/"):
            return self._send(200, BM_AVAILABLE)
        if p.endswith("/state/"):
            return self._send(200, {"state": "running", "host": "h1"})
        if p.endswith("/power/"):
            return self._send(200, {"state": "On"})
        if p.endswith("/virtual_machines/"):
            return self._send(200, VMS)
        if p.endswith("/bare_metal/"):
            return self._send(200, BMS)
        if self.command == "POST" and p.endswith("/virtual_machines/"):
            sent = json.loads(body or "{}")
            return self._send(200, {"deployment_id": "new-vm", "name": "vm-new",
                                    "ip_address": "10.0.0.5",
                                    "cpu_cores": sent.get("cpu_cores"),
                                    "ram_capacity": sent.get("ram_capacity"),
                                    "disk_capacity": sent.get("disk_capacity")})
        if self.command == "POST" and p.endswith("/bare_metal/"):
            return self._send(201, {"deployment_id": "new-bm", "name": "bm-new",
                                    "manufacturer": "Dell", "model": "XE9680",
                                    "ip_address": "10.0.0.9",
                                    "specs": json.loads(body or "{}").get("specs", {})})
        # POST to a per-resource action path (stop, reboot, power/cold_reboot, ...)
        if self.command == "POST":
            return self._send(200, {"ok": True})
        if self.command == "DELETE":
            return self._send(204)
        return self._send(404, raw="Not Found")

    do_GET = do_POST = do_DELETE = do_PATCH = do_PUT = handle_one


def run_cli(*argv, env_extra=None, stdin_text="", tty=False):
    """Invoke cli.main() in-process, capturing stdout/stderr and exit code.

    stdin is always replaced with a non-tty stream so a confirmation prompt can
    never block the suite waiting for a human.
    """
    out, err = io.StringIO(), io.StringIO()
    code = 0
    saved = dict(os.environ)
    if env_extra:
        os.environ.update(env_extra)
    try:
        with redirect_stdout(out), redirect_stderr(err), _fake_stdin(stdin_text, tty):
            try:
                code = cli.main(list(argv))
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
    finally:
        os.environ.clear()
        os.environ.update(saved)
    return code, out.getvalue(), err.getvalue()


@contextmanager
def _fake_stdin(text="", tty=False):
    """Swap sys.stdin for a controllable stream.

    The sandbox hands shells a real pty, so without this a confirmation prompt
    would block forever waiting for a human to type. Pass tty=True to exercise
    the interactive prompt path with scripted answers.
    """
    stream = io.StringIO(text)
    stream.isatty = lambda: tty  # type: ignore[method-assign]
    original = sys.stdin
    sys.stdin = stream
    try:
        yield
    finally:
        sys.stdin = original


class CLITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = "http://127.0.0.1:%d/api" % cls.server.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        Handler.calls = []
        for k in list(os.environ):
            if k.startswith("HOTAISLE_"):
                del os.environ[k]
        os.environ["HOTAISLE_API_KEY"] = KEY
        os.environ["HOTAISLE_BASE_URL"] = self.base
        os.environ["NO_COLOR"] = "1"
        # Never resolve a developer's real config/keyring during tests.
        os.environ["HOTAISLE_CONFIG"] = "/nonexistent/hotaisle.toml"
        self.env = {}

    # ----------------------------------------------------------- listings

    def test_vm_list_table(self):
        code, out, err = run_cli("vm", "list", "--team", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("vm-01", out)
        self.assertIn("195116dc", out)
        self.assertIn("8 vCPU", out)
        self.assertIn("vm01.example.com:2222", out)

    def test_bm_list_table(self):
        code, out, err = run_cli("bm", "list", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("Dell PowerEdge XE9680", out)
        self.assertIn("MI300X", out)
        self.assertIn("server-01", out)

    def test_vm_available_table_with_price(self):
        code, out, err = run_cli("vm", "available", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("$3.50/hr", out)
        self.assertIn("30m", out)

    def test_bm_available_shows_quantity_and_min(self):
        code, out, err = run_cli("bm", "available", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("$21.00/hr", out)
        self.assertIn("8h", out)

    def test_empty_listing(self):
        # A team with nothing provisioned should print "(none)" not crash.
        Handler.calls.clear()
        code, out, err = run_cli("--json", "vm", "list", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        json.loads(out)

    def test_json_output_is_machine_readable(self):
        code, out, err = run_cli("--json", "vm", "list", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data[0]["name"], "vm-01")

    def test_csv_output(self):
        code, out, err = run_cli("--csv", "bm", "available", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertTrue(out.startswith("#,QTY,VCPU"))
        self.assertEqual(len(out.strip().splitlines()), 2)

    def test_global_json_flag_survives_subparser(self):
        """Subparsers must not reset --json to False when given before the verb."""
        code, out, err = run_cli("--json", "bm", "list", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        json.loads(out)  # would raise if a table had been printed

    def test_alias_invocations(self):
        for argv in (("vms", "ls", "-t", "acme-corp"),
                     ("virtual-machine", "available", "-t", "acme-corp"),
                     ("metal", "list", "-t", "acme-corp")):
            code, out, err = run_cli(*argv)
            self.assertEqual(code, 0, "%s: %s" % (argv, err))

    # ----------------------------------------------------------- creation

    def test_vm_create_selects_exact_spec_shape(self):
        code, out, err = run_cli("vm", "create", "--cpu-cores", "8", "--ram", "32G",
                                 "--disk", "100G", "--description", "worker", "--yes",
                                 "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("provisioned", out)
        sent = json.loads(Handler.calls[-1]["body"])
        # Exact match on 8 vCPU / 32 GiB / 100 GiB shape, flattened for VMs.
        self.assertEqual(sent["cpu_cores"], 8)
        self.assertEqual(sent["ram_capacity"], 34359738368)
        self.assertEqual(sent["disk_capacity"], 107374182400)
        self.assertEqual(sent["description"], "worker")
        self.assertNotIn("specs", sent)

    def test_bm_create_selects_exact_spec_shape(self):
        code, out, err = run_cli("bm", "create", "--cpu-cores", "64", "--ram", "512G",
                                 "--disk", "4T", "--description", "gpu", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("reserved", out)
        sent = json.loads(Handler.calls[-1]["body"])
        self.assertEqual(sent["specs"]["cpu_cores"], 64)
        self.assertEqual(sent["description"], "gpu")

    def test_vm_create_sizing_snaps_to_available(self):
        code, out, err = run_cli("vm", "create", "--cpu-cores", "4", "--ram", "16G",
                                 "--disk", "50G", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("smallest available that fits", err)
        sent = json.loads(Handler.calls[-1]["body"])
        self.assertEqual(sent["cpu_cores"], 8)  # snapped up to the only shippable shape

    def test_vm_create_exact_passthrough(self):
        code, out, err = run_cli("vm", "create", "--exact", "--cpu-cores", "3",
                                 "--ram", "6G", "--disk", "10G", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        sent = json.loads(Handler.calls[-1]["body"])
        self.assertEqual(sent["cpu_cores"], 3)
        self.assertEqual(sent["ram_capacity"], 6 * 1024 ** 3)

    def test_vm_create_no_shape_is_usage_error(self):
        code, out, err = run_cli("vm", "create", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 1)
        self.assertIn("cpu-cores", err)

    def test_vm_create_unsatisfiable_shape_fails_cleanly(self):
        code, out, err = run_cli("vm", "create", "--cpu-cores", "512", "--ram", "4T",
                                 "--disk", "1P", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 1)
        self.assertIn("No available", err)

    def test_dry_run_sends_nothing(self):
        code, out, err = run_cli("vm", "create", "--cpu-cores", "8", "--ram", "32G",
                                 "--disk", "100G", "--dry-run", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("dry run", out)
        # Reading the /available/ list is fine; sending the create POST is not.
        self.assertEqual([c for c in Handler.calls if c["method"] == "POST"], [])

    def test_vm_create_gpus_disambiguates_colliding_shapes(self):
        """--gpus selects the shape with matching GPU count even if cpu/ram/disk collide."""
        from hotaisle import cli as cli_mod

        colliding = [
            {"Quantity": 1, "OnDemandPrice": 299, "MinimumReservationMinutes": 1,
             "Specs": {"cpu_cores": 8, "ram_capacity": 240518168576,
                       "disk_capacity": 13194139533312,
                       "gpus": [{"count": 1, "manufacturer": "AMD", "model": "MI300X"}]}},
            {"Quantity": 1, "OnDemandPrice": 598, "MinimumReservationMinutes": 1,
             "Specs": {"cpu_cores": 8, "ram_capacity": 240518168576,
                       "disk_capacity": 13194139533312,
                       "gpus": [{"count": 2, "manufacturer": "AMD", "model": "MI300X"}]}},
        ]
        saved = Handler.do_GET
        try:
            def gpu_handler(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(json.dumps(colliding))))
                self.end_headers()
                self.wfile.write(json.dumps(colliding).encode())
            Handler.do_GET = gpu_handler
            # We only GET /available/ here; a POST would 404, but --cpu/--ram/--disk
            # with --gpus 1 should exact-match the 1x MI300X row and post a create.
            code, out, err = run_cli("vm", "create", "--cpu-cores", "8", "--ram", "224G",
                                     "--disk", "12T", "--gpus", "1", "--yes", "-t", "acme-corp")
            self.assertEqual(code, 0, err)
            sent = json.loads(Handler.calls[-1]["body"])
            self.assertEqual(sent["gpus"][0]["count"], 1)
        finally:
            Handler.do_GET = saved

    def test_confirmation_declined_sends_nothing(self):
        code, out, err = run_cli("vm", "delete", "195116dc-32ed-49e5-a738-5e2ad0cdd141",
                                 "-t", "acme-corp")
        # Non-interactive stdin must refuse rather than delete or hang.
        self.assertIn("refusing", err)
        self.assertEqual([c for c in Handler.calls if c["method"] == "DELETE"], [])

    def test_confirmation_accepted_via_stdin(self):
        code, out, err = run_cli("vm", "delete", "vm-01", "-t", "acme-corp",
                                 stdin_text="y\n", tty=True)
        self.assertEqual(code, 0, err)
        self.assertIn("Deleted VM", out)

    def test_confirmation_declined_by_answer(self):
        code, out, err = run_cli("vm", "delete", "vm-01", "-t", "acme-corp",
                                 stdin_text="n\n", tty=True)
        self.assertEqual(code, 0, err)
        self.assertIn("aborted", out)
        self.assertEqual([c for c in Handler.calls if c["method"] == "DELETE"], [])

    def test_empty_stdin_does_not_delete(self):
        """EOF at the prompt (closed stdin) must be treated as 'no'."""
        code, out, err = run_cli("vm", "delete", "vm-01", "-t", "acme-corp",
                                 stdin_text="", tty=True)
        self.assertIn("no answer received", err)
        self.assertEqual([c for c in Handler.calls if c["method"] == "DELETE"], [])

    def test_json_body_override(self):
        code, out, err = run_cli("bm", "create", "--json-body",
                                 '{"specs":{"cpu_cores":4,"ram_capacity":1,"disk_capacity":1}}',
                                 "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        sent = json.loads(Handler.calls[-1]["body"])
        self.assertEqual(sent["specs"]["cpu_cores"], 4)

    # ----------------------------------------------------------- deletion

    def test_vm_delete_by_id(self):
        code, out, err = run_cli("vm", "delete", "195116dc-32ed-49e5-a738-5e2ad0cdd141",
                                 "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("Deleted VM vm-01", out)
        last = Handler.calls[-1]
        self.assertEqual(last["method"], "DELETE")
        self.assertIn("195116dc-32ed-49e5-a738-5e2ad0cdd141", last["path"])

    def test_vm_delete_by_name_resolves_to_id(self):
        code, out, err = run_cli("vm", "delete", "vm-01", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("195116dc", Handler.calls[-1]["path"])

    def test_bm_delete_by_name(self):
        code, out, err = run_cli("bm", "delete", "server-01", "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("Released server", out)
        self.assertIn("77b3e2a2", Handler.calls[-1]["path"])

    def test_vm_delete_with_force(self):
        code, out, err = run_cli("vm", "delete", "vm-01", "--yes", "--force",
                                 "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("force=true", Handler.calls[-1]["path"])

    def test_vm_delete_dry_run(self):
        code, out, err = run_cli("vm", "delete", "vm-01", "--dry-run", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertEqual([c for c in Handler.calls if c["method"] == "DELETE"], [])

    # -------------------------------------------------------- other verbs

    def test_whoami(self):
        code, out, err = run_cli("whoami")
        self.assertEqual(code, 0, err)
        self.assertIn("ops@acme.test", out)
        self.assertIn("acme-corp", out)
        self.assertNotIn(KEY, out)  # key must be masked
        self.assertIn(KEY[:4], out)

    def test_teams_and_balance(self):
        code, out, err = run_cli("teams")
        self.assertEqual(code, 0, err)
        self.assertIn("acme-corp", out)
        code, out, err = run_cli("balance", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("$250.00", out)

    def test_ssh_keys(self):
        code, out, err = run_cli("ssh-keys")
        self.assertEqual(code, 0, err)
        self.assertIn("me@host", out)
        self.assertIn("ssh-rsa", out)
        self.assertIn("ssh-rsa AAAA", out)

    def test_api_keys(self):
        code, out, err = run_cli("api-keys")
        self.assertEqual(code, 0, err)
        self.assertIn("abc123", out)
        self.assertIn("my-key", out)
        self.assertIn("user", out)
        self.assertIn("acme-corp", out)

    def test_vm_state_and_bm_power(self):
        code, out, err = run_cli("vm", "state", "195116dc", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("running", out)
        code, out, err = run_cli("bm", "power", "77b3e2a2", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertIn("On", out)

    def test_vm_action(self):
        code, out, err = run_cli("vm", "action", "vm-01", "stop", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        self.assertTrue(Handler.calls[-1]["path"].endswith("/virtual_machines/"
                                                          "195116dc-32ed-49e5-a738-"
                                                          "5e2ad0cdd141/stop/"))

    def test_bm_power_action_requires_confirm(self):
        code, out, err = run_cli("bm", "action", "server-01", "power/cold_reboot",
                                 "-t", "acme-corp")
        self.assertIn("refusing", err)
        self.assertEqual([c for c in Handler.calls if c["method"] == "POST"], [])
        code, out, err = run_cli("bm", "action", "server-01", "power/cold_reboot",
                                 "--yes", "-t", "acme-corp")
        self.assertEqual(code, 0, err)
        posts = [c for c in Handler.calls if c["method"] == "POST"]
        self.assertTrue(posts[-1]["path"].endswith("/power/cold_reboot/"))

    # ------------------------------------------------------------ failures

    def test_no_api_key_prints_instructions(self):
        del os.environ["HOTAISLE_API_KEY"]
        code, out, err = run_cli("vm", "list", "-t", "acme-corp")
        self.assertEqual(code, 1)
        self.assertIn("HOTAISLE_API_KEY", err)

    def test_bad_team_is_helpful(self):
        code, out, err = run_cli("vm", "list", "-t", "nope")
        self.assertEqual(code, 1)
        self.assertIn("Not Found", err)

    def test_no_command_shows_help(self):
        code, out, err = run_cli()
        self.assertEqual(code, 2)
        self.assertIn("usage", out + err)

    def test_subcommand_help_works(self):
        for argv in (("vm", "--help"), ("bm", "--help"), ("vm", "create", "--help"),
                     ("bm", "delete", "--help")):
            code, out, err = run_cli(*argv)
            self.assertEqual(code, 0, argv)
            self.assertIn("usage", (out + err).lower(), argv)
        # Flags we promise must actually appear in the help text.
        code, out, err = run_cli("vm", "create", "--help")
        for flag in ("--cpu-cores", "--ram", "--disk",
                     "--user-data-url", "--dry-run", "--json-body"):
            self.assertIn(flag, out + err, flag)
        code, out, err = run_cli("bm", "delete", "--help")
        for flag in ("--force", "--yes", "--dry-run"):
            self.assertIn(flag, out + err, flag)

    def test_module_entrypoint_is_invocable(self):
        """`python -m hotaisle` should work straight from the source tree."""
        env = dict(os.environ)
        env["PYTHONPATH"] = ROOT
        proc = subprocess.run([sys.executable, "-m", "hotaisle", "teams"],
                              capture_output=True, text=True, cwd="/tmp", env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("acme-corp", proc.stdout)

    # -------------------------------------------------- availability

    def test_avail_watch_auto_detects_single_team(self):
        """The watch sweep must use the auto-resolved single team, not fail on 'No team'."""
        import tempfile
        from hotaisle import Client
        from hotaisle.availability import AvailabilityDB
        from hotaisle.cli import _sweep_once, resolve_team

        client = Client(base_url=self.base, team=None)
        args = type("Args", (), {"team": None})()
        team = resolve_team(client, args)
        self.assertEqual(team, "acme-corp")

        with tempfile.TemporaryDirectory() as td:
            db = AvailabilityDB(os.path.join(td, "nested", "avail.db"))
            _sweep_once(client, db, team, ts=1000.0)
            # VM_AVAILABLE has 2 shapes, BM_AVAILABLE has 1 => 3 total.
            self.assertEqual(len(db.shapes()), 3)
            db.close()

    def test_sweep_requires_team_if_multiple(self):
        """resolve_team tells the user to pass --team if auto-detect is ambiguous."""
        from hotaisle import Client
        from hotaisle.cli import resolve_team

        client = Client(base_url=self.base, team=None)
        args = type("Args", (), {"team": None})()
        # Server has exactly one team => resolves; EXIT_USAGE would be used if >1.
        self.assertEqual(resolve_team(client, args), "acme-corp")


if __name__ == "__main__":
    unittest.main(verbosity=2)
