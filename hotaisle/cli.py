"""Command line interface for the Hot Aisle API.

    hotaisle vm list
    hotaisle vm available
    hotaisle vm create --cpu-cores 8 --ram 224GiB --disk 12TiB --gpus 1 --description "build box"
    hotaisle vm delete <deployment_id> --yes
    hotaisle bm list
    hotaisle --json bm available

Run ``hotaisle --help`` or ``hotaisle vm --help`` for full options.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import models
from .auth import NO_KEY_HINT, mask
from .client import Client, specs_to_selector
from .availability import (
    AvailabilityDB,
    ShapeStats,
    default_db_path,
    summarize,
)
from .errors import APIError, AuthError, ConfigurationError, HotAisleError
from .models import AvailableType, BareMetalServer, VirtualMachine

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2

# ANSI helpers (disabled when not a tty or NO_COLOR is set)
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if _COLOR else text


def _dim(t: str) -> str:
    return _c("2", t)


def _bold(t: str) -> str:
    return _c("1", t)


def _red(t: str) -> str:
    return _c("31", t)


def _green(t: str) -> str:
    return _c("32", t)


def _yellow(t: str) -> str:
    return _c("33", t)


# ------------------------------------------------------------------ rendering


def _display_width(s: str) -> int:
    return len(s)


def print_table(
    rows: Sequence[Sequence[Any]], headers: Sequence[str], out=None
) -> None:
    # Resolve sys.stdout at call time (not as a default arg) so that
    # redirect_stdout / an embedded caller actually captures the table.
    out = out if out is not None else sys.stdout
    rows = [["" if v is None else str(v) for v in r] for r in rows]
    headers = list(headers)
    widths = [
        max([_display_width(h)] + [_display_width(r[i]) for r in rows] or [0])
        for i, h in enumerate(headers)
    ]
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip()
    print(_bold(line), file=out)
    print(_dim("  ".join("-" * w for w in widths)), file=out)
    for r in rows:
        print(
            "  ".join(r[i].ljust(widths[i]) for i in range(len(headers))).rstrip(),
            file=out,
        )


def emit(
    rows: Sequence[Sequence[Any]], headers: Sequence[str], args, json_data: Any = None
) -> None:
    """Honour --json / --csv / default table output."""
    out = sys.stdout
    if getattr(args, "json", False):
        payload = (
            json_data
            if json_data is not None
            else [dict(zip(headers, r)) for r in rows]
        )
        json.dump(payload, out, indent=2, default=str, sort_keys=False)
        out.write("\n")
        return
    if getattr(args, "csv", False):
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(headers)
        w.writerows(rows)
        out.write(buf.getvalue())
        return
    if not rows:
        print(_dim("(none)"))
        return
    print_table(rows, headers)


def die(message: str, code: int = EXIT_ERROR) -> "NoReturn":  # type: ignore[valid-type]
    print(_red("error: %s" % message), file=sys.stderr)
    sys.exit(code)


# ------------------------------------------------------------------ resolution


def make_client(args: argparse.Namespace) -> Client:
    try:
        client = Client(
            api_key=args.api_key,
            base_url=args.base_url,
            team=args.team,
            timeout=args.timeout,
            max_retries=args.retries,
            verify_ssl=not args.insecure,
        )
    except ConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(EXIT_ERROR)
    if client.security_warning and not args.json:
        print(_yellow("warning: %s" % client.security_warning), file=sys.stderr)
    return client


def resolve_team(client: Client, args: argparse.Namespace) -> str:
    team = getattr(args, "team", None) or client.team
    if team:
        return team
    try:
        teams = client.list_teams()
    except HotAisleError as exc:
        die(str(exc))
    if not teams:
        die("Your account is not a member of any team.")
    if len(teams) == 1:
        print(_dim("using only team: %s" % teams[0].handle), file=sys.stderr)
        return teams[0].handle or ""
    print(_yellow("Multiple teams available; pass --team. Options:"), file=sys.stderr)
    for t in teams:
        print(
            "  %s  (%s)  roles=%s" % (t.handle, t.name, ",".join(t.roles)),
            file=sys.stderr,
        )
    sys.exit(EXIT_USAGE)


def choose_shape(
    listing: List[AvailableType],
    cpu: Optional[int],
    ram: Optional[int],
    disk: Optional[int],
    gpus: Optional[int],
) -> Optional[AvailableType]:
    """Smallest available shape that meets or exceeds the requested minimums."""
    candidates = []
    for a in listing:
        s = a.specs
        if a.quantity in (0,):
            continue
        if cpu is not None and (s.cpu_cores or 0) < cpu:
            continue
        if ram is not None and (s.ram_capacity or 0) < ram:
            continue
        if disk is not None and (s.disk_capacity or 0) < disk:
            continue
        if gpus is not None and s.gpu_count < gpus:
            continue
        candidates.append(a)
    if not candidates:
        return None
    candidates.sort(
        key=lambda a: (
            (a.specs.cpu_cores or 0)
            + (a.specs.ram_gib or 0) / 8.0
            + (a.specs.disk_gib or 0) / 100.0,
            a.on_demand_price if a.on_demand_price is not None else 1 << 60,
        )
    )
    return candidates[0]


def confirm(args: argparse.Namespace, prompt: str) -> bool:
    """Ask before an irreversible action.

    Anything other than an interactive terminal (cron, CI, a pipe, a closed
    stdin) answers "no" rather than blocking forever waiting for input.
    """
    # Not every subcommand defines --yes/--dry-run, so read them defensively.
    if getattr(args, "yes", False) or getattr(args, "dry_run", False):
        return True
    stream = sys.stdin
    if not _is_interactive(stream):
        print(
            _red("refusing to proceed without --yes and no interactive terminal"),
            file=sys.stderr,
        )
        return False
    try:
        answer = _read_line(stream, prompt)
    except EOFError:
        print(_red("no answer received; not proceeding"), file=sys.stderr)
        return False
    return answer.strip().lower() in ("y", "yes")


def _is_interactive(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        # Captured/closed streams (redirect_stdout in tests, detached cron jobs).
        return False


def _read_line(stream, prompt: str) -> str:
    sys.stderr.write(_yellow("%s [y/N] " % prompt))
    sys.stderr.flush()
    line = stream.readline()
    sys.stderr.write("\n")
    if not line:
        raise EOFError
    return line


# ------------------------------------------------------------------ commands


def cmd_whoami(args: argparse.Namespace) -> int:
    client = make_client(args)
    user = client.get_user()
    teams = client.list_teams()
    if args.json:
        emit(
            [], [], args, json_data={"user": user.raw, "teams": [t.raw for t in teams]}
        )
        return EXIT_OK
    print("%s  %s" % (_bold(user.name or "?"), _dim(user.email or "")))
    print(
        _dim("api key from %s: %s" % (client.credential.source, mask(client.api_key)))
    )
    if teams:
        print()
        print_table(
            [
                [
                    t.handle,
                    t.name,
                    ",".join(t.roles) or "-",
                    ",".join(t.effective_roles) or "-",
                ]
                for t in teams
            ],
            ["TEAM HANDLE", "NAME", "ROLES", "EFFECTIVE"],
        )
    return EXIT_OK


def cmd_teams(args: argparse.Namespace) -> int:
    client = make_client(args)
    teams = client.list_teams()
    emit(
        [
            [
                t.handle,
                t.name,
                ",".join(t.roles) or "-",
                t.maximum_virtual_machines,
                t.maximum_bare_metal_servers,
                "pending invite" if t.invitation else "",
            ]
            for t in teams
        ],
        ["HANDLE", "NAME", "ROLES", "MAX VM", "MAX BM", "NOTE"],
        args,
        json_data=[t.raw for t in teams],
    )
    return EXIT_OK


def cmd_balance(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    bal = client.get_balance(team)
    if args.json:
        emit([], [], args, json_data=bal.raw)
    else:
        print("%s balance: %s" % (_bold(team), _green(str(bal))))
    return EXIT_OK


# ---- listings ----


def cmd_vm_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    vms = client.list_virtual_machines(team)
    if args.detail:
        rows = [
            [
                v.name,
                v.deployment_id,
                v.specs.full_label,
                v.ip_address or "-",
                v.description or "-",
            ]
            for v in vms
        ]
        headers = ["NAME", "DEPLOYMENT ID", "SHAPE", "IP", "DESCRIPTION"]
    else:
        rows = [
            [v.name, v.deployment_id, v.specs.label, v.ssh_target, v.description or "-"]
            for v in vms
        ]
        headers = ["NAME", "DEPLOYMENT ID", "SHAPE", "SSH", "DESCRIPTION"]
    emit(rows, headers, args, json_data=[v.raw for v in vms])
    return EXIT_OK


def cmd_bm_list(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    servers = client.list_bare_metal(team)
    rows = [
        [
            s.name,
            s.deployment_id,
            s.hardware,
            s.specs.label,
            s.specs.gpu_summary,
            s.ip_address or "-",
            "yes" if s.support_access_enabled else "no",
        ]
        for s in servers
    ]
    emit(
        rows,
        ["NAME", "DEPLOYMENT ID", "HARDWARE", "SHAPE", "GPUs", "IP", "SUPPORT ACCESS"],
        args,
        json_data=[s.raw for s in servers],
    )
    return EXIT_OK


def _available_rows(items: Iterable[AvailableType]) -> List[List[Any]]:
    return [
        [
            i + 1,
            a.quantity,
            a.specs.cpu_cores,
            models.human_bytes(a.specs.ram_capacity),
            models.human_bytes(a.specs.disk_capacity),
            a.specs.gpu_summary,
            a.price_per_hour,
            a.minimum_reservation,
        ]
        for i, a in enumerate(items)
    ]


def cmd_vm_available(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    items = client.list_available_virtual_machines(team)
    emit(
        _available_rows(items),
        ["#", "QTY", "VCPU", "RAM", "DISK", "GPUs", "PRICE", "MIN RESV"],
        args,
        json_data=[a.raw for a in items],
    )
    return EXIT_OK


def cmd_bm_available(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    items = client.list_available_bare_metal(team)
    emit(
        _available_rows(items),
        ["#", "QTY", "VCPU", "RAM", "DISK", "GPUs", "PRICE", "MIN RESV"],
        args,
        json_data=[a.raw for a in items],
    )
    return EXIT_OK


# ---- creation ----


def _create_common(
    args: argparse.Namespace, client: Client, kind: str, team: str
) -> Dict[str, Any]:
    """Build the specs payload for either resource kind."""
    if args.json_body:
        try:
            body = json.loads(args.json_body)
        except json.JSONDecodeError as exc:
            die("--json-body is not valid JSON: %s" % exc)
        if kind == "bm" and args.description:
            body.setdefault("description", args.description)
        return body if kind == "vm" else {"specs": body.get("specs", body)}

    try:
        cpu = args.cpu_cores
        ram = models.parse_size(args.ram)
        disk = models.parse_size(args.disk)
    except ValueError as exc:
        die(str(exc))
    gpus = args.gpus
    if cpu is None and ram is None and disk is None and gpus is None:
        die("Specify a shape (--cpu-cores/--ram/--disk/--gpus) or pass --json-body.")
    listing = (
        client.list_available_virtual_machines(team)
        if kind == "vm"
        else client.list_available_bare_metal(team)
    )
    exact = (
        [
            a
            for a in listing
            if (a.specs.cpu_cores == cpu if cpu is not None else True)
            and (a.specs.ram_capacity == ram if ram is not None else True)
            and (a.specs.disk_capacity == disk if disk is not None else True)
            and (a.specs.gpu_count == gpus if gpus is not None else True)
        ]
        if (cpu is not None or ram is not None or disk is not None or gpus is not None)
        else []
    )
    if exact:
        avail = exact[0]
        specs = specs_to_selector(avail)
        print(
            _dim(
                "matched available type: %s (qty %s, %s/hr, min %s)"
                % (
                    avail.specs.full_label,
                    avail.quantity,
                    avail.price_per_hour,
                    avail.minimum_reservation,
                )
            ),
            file=sys.stderr,
        )
    elif args.exact:
        specs = _specs_or_die(cpu, ram, disk, gpus)
        print(
            _yellow(
                "warning: no available type matches these specs exactly; "
                "the API will likely 404"
            ),
            file=sys.stderr,
        )
    else:
        best = choose_shape(listing, cpu, ram, disk, gpus)
        if best is None:
            die(
                "No available %s type satisfies cpu=%s ram=%s disk=%s gpus=%s. "
                "See 'hotaisle %s available'."
                % (
                    kind,
                    cpu,
                    ram and models.human_bytes(ram),
                    disk and models.human_bytes(disk),
                    gpus,
                    kind,
                )
            )
        specs = specs_to_selector(best)
        print(
            _yellow(
                "no exact shape; smallest available that fits: %s "
                "(%s/hr, min %s). Re-run with --exact to force raw specs."
                % (best.specs.full_label, best.price_per_hour, best.minimum_reservation)
            ),
            file=sys.stderr,
        )

    body: Dict[str, Any] = {"specs": specs} if kind == "bm" else dict(specs)
    if kind == "bm" and args.description:
        body["description"] = args.description
    if kind == "vm" and args.user_data_url:
        body["user_data_url"] = args.user_data_url
    return body


def _specs_or_die(cpu, ram, disk, gpus) -> Dict[str, Any]:
    missing = [
        n for n, v in (("cpu_cores", cpu), ("ram", ram), ("disk", disk)) if v is None
    ]
    if missing:
        die(
            "--exact needs all of --cpu-cores, --ram and --disk (missing: %s)"
            % ", ".join(missing)
        )
    specs: Dict[str, Any] = {
        "cpu_cores": int(cpu),
        "ram_capacity": int(ram),
        "disk_capacity": int(disk),
    }
    if gpus:
        specs["gpus"] = [{"count": int(gpus)}]
    return specs


def _preview(body: Dict[str, Any], kind: str, team: str, args) -> bool:
    print(_bold("About to create a %s in team %s:" % (kind, team)))
    print(json.dumps(body, indent=2, sort_keys=True))
    if getattr(args, "dry_run", False):
        print(_dim("dry run: nothing was sent."))
        return False
    if not confirm(args, "Proceed?"):
        print(_dim("aborted."))
        return False
    return True


def cmd_vm_create(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    body = _create_common(args, client, "vm", team)
    if not _preview(body, "virtual machine", team, args):
        return EXIT_OK
    vm = client.create_virtual_machine(
        body=body, description=args.description, force=args.force, team=team
    )
    _report_created_vm(vm)
    return EXIT_OK


def cmd_bm_create(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    body = _create_common(args, client, "bm", team)
    if not _preview(body, "bare metal server", team, args):
        return EXIT_OK
    server = client.create_bare_metal(body=body, force=args.force, team=team)
    _report_created_bm(server)
    return EXIT_OK


def _report_created_vm(vm: VirtualMachine) -> None:
    print(_green("\nVirtual machine provisioned."))
    _emit_created(
        vm.name, vm.deployment_id, vm.specs.full_label, vm.ip_address, vm.ssh_command
    )


def _report_created_bm(s: BareMetalServer) -> None:
    print(_green("\nBare metal server reserved."))
    _emit_created(
        "%s (%s)" % (s.name, s.hardware),
        s.deployment_id,
        s.specs.full_label,
        s.ip_address,
        s.ssh_command,
    )


def _emit_created(name, dep_id, shape, ip, ssh_cmd) -> None:
    print("  name:          %s" % (name or "-"))
    print("  deployment_id: %s" % dep_id)
    print("  shape:         %s" % shape)
    print("  ip:            %s" % (ip or "-"))
    if ssh_cmd:
        print("  ssh:           %s" % _bold(ssh_cmd))
    print(_dim("\nremember the deployment_id - deleting takes it, not the name."))


# ---- deletion ----


def cmd_vm_delete(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    target = _resolve_vm_identity(client, team, args.target)
    if args.dry_run:
        print(_dim("dry run: nothing was sent."))
        return EXIT_OK
    if not confirm(
        args, "Delete VM %s (%s)? Data will be lost." % (target["name"], target["id"])
    ):
        print(_dim("aborted."))
        return EXIT_OK
    client.delete_virtual_machine(target["id"], team=team, force=args.force)
    print(_green("Deleted VM %s (%s)." % (target["name"], target["id"])))
    return EXIT_OK


def cmd_bm_delete(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    target = _resolve_bm_identity(client, team, args.target)
    if args.dry_run:
        print(_dim("dry run: nothing was sent."))
        return EXIT_OK
    if not confirm(
        args, "Release bare metal server %s (%s)?" % (target["name"], target["id"])
    ):
        print(_dim("aborted."))
        return EXIT_OK
    client.delete_bare_metal(target["id"], team=team, force=args.force)
    print(_green("Released server %s (%s)." % (target["name"], target["id"])))
    return EXIT_OK


def _resolve_identity(
    client: Client, team: str, target: str, kind: str
) -> Dict[str, str]:
    """Accept a deployment_id or a friendly name; return {'id','name'}."""
    if kind == "vm":
        listing = client.list_virtual_machines(team)
    else:
        listing = client.list_bare_metal(team)
    by_id = {str(getattr(x, "deployment_id", "")): x for x in listing}
    if target in by_id:
        obj = by_id[target]
        return {"id": target, "name": obj.name or target}
    matches = [x for x in listing if (x.name or "").lower() == target.lower()]
    if not matches:
        # Fall back to trusting the string as an id (e.g. an id we can't list).
        return {"id": target, "name": target}
    if len(matches) > 1:
        die(
            "%r matches %d %ss; use the deployment_id instead."
            % (target, len(matches), kind)
        )
    return {"id": str(matches[0].deployment_id), "name": matches[0].name or target}


def _resolve_vm_identity(client, team, target):
    return _resolve_identity(client, team, target, "vm")


def _resolve_bm_identity(client, team, target):
    return _resolve_identity(client, team, target, "bare metal server")


# ---- misc ----


def cmd_vm_get(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    vm = client.get_virtual_machine(args.deployment_id, team=team)
    if args.json:
        emit([], [], args, json_data=vm.raw)
    else:
        print_table(
            [
                [
                    vm.name,
                    vm.deployment_id,
                    vm.specs.full_label,
                    vm.ip_address,
                    vm.description or "-",
                ]
            ],
            ["NAME", "DEPLOYMENT ID", "SHAPE", "IP", "DESCRIPTION"],
        )
        if vm.ssh_command:
            print(_dim("\n%s" % vm.ssh_command))
    return EXIT_OK


def cmd_vm_update(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    if not args.description:
        die("vm update needs --description")
    target = _resolve_vm_identity(client, team, args.target)
    client.update_virtual_machine(target["id"], description=args.description, team=team)
    print(_green("updated %s: %s" % (target["id"], args.description)))
    return EXIT_OK


def cmd_vm_state(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    state = client.get_virtual_machine_state(args.deployment_id, team=team)
    if args.json:
        emit([], [], args, json_data=state.raw)
    else:
        print("%s on %s" % (_green(state.state or "?"), state.host or "?"))
    return EXIT_OK


def cmd_bm_power(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    state = client.get_bare_metal_power(args.deployment_id, team=team)
    if args.json:
        emit([], [], args, json_data={"state": state})
    else:
        print(_green(state))
    return EXIT_OK


_ACTIONS = {
    "vm": ["start", "stop", "shutdown", "reboot", "hard-reset", "rebuild", "console"],
    "bm": [
        "console",
        "reinstall",
        "support_access_enable",
        "power/power_on",
        "power/graceful_shutdown",
        "power/force_shutdown",
        "power/warm_reboot",
        "power/cold_reboot",
        "power/ac_reset",
    ],
}


def cmd_vm_action(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    target = _resolve_identity(client, team, args.target, "vm")
    if args.action == "rebuild":
        if not confirm(
            args, "Rebuild VM %s? This reinstalls the image." % target["name"]
        ):
            print(_dim("aborted."))
            return EXIT_OK
    body = {"user_data_url": args.user_data_url} if args.user_data_url else None
    result = client.vm_action(target["id"], args.action, team=team, body=body)
    print(_green("%s: %s" % (args.action, "ok")))
    if result:
        print(json.dumps(result, indent=2, default=str))
    return EXIT_OK


def cmd_bm_action(args: argparse.Namespace) -> int:
    client = make_client(args)
    team = resolve_team(client, args)
    target = _resolve_identity(client, team, args.target, "bare metal server")
    if args.action in (
        "reinstall",
        "power/force_shutdown",
        "power/ac_reset",
        "power/cold_reboot",
        "power/warm_reboot",
    ):
        if not confirm(args, "Send '%s' to %s?" % (args.action, target["name"])):
            print(_dim("aborted."))
            return EXIT_OK
    result = client.bare_metal_action(target["id"], args.action, team=team)
    print(_green("%s: ok" % args.action))
    if result:
        print(json.dumps(result, indent=2, default=str))
    return EXIT_OK


def cmd_ssh_keys(args: argparse.Namespace) -> int:
    client = make_client(args)
    keys = client.list_ssh_keys()
    emit(
        [
            [
                k.get("comment") or "-",
                k.get("fingerprint") or "-",
                k.get("type") or "-",
                (k.get("public_key") or "")[:36] + "...",
            ]
            for k in keys
        ],
        ["NAME", "FINGERPRINT", "TYPE", "KEY"],
        args,
        json_data=keys,
    )
    return EXIT_OK


# ------------------------------------------------------------ availability


def _open_avail_db(args: argparse.Namespace) -> AvailabilityDB:
    return AvailabilityDB(args.db)


def _sweep_once(client: Client, db: AvailabilityDB, team: str, ts: float) -> None:
    """Record one snapshot of both VM and bare-metal availability."""
    kinds = [
        (kind, getattr(client, method), team)
        for kind, method in (
            ("vm", "list_available_virtual_machines"),
            ("bm", "list_available_bare_metal"),
        )
    ]
    for kind, fn, team in kinds:
        try:
            listings = fn(team)
        except HotAisleError as exc:
            # A transient failure to one endpoint must not kill the sweep or
            # lose the other half. Print and move on.
            print("warning: %s availability failed: %s" % (kind, exc), file=sys.stderr)
            continue
        db.record(kind, listings, ts=ts)


def cmd_avail_watch(args: argparse.Namespace) -> int:
    """Loop every ``--interval`` seconds, recording availability to SQLite."""
    client = make_client(args)
    team = resolve_team(client, args)
    db = _open_avail_db(args)
    interval = max(1, int(args.interval))
    keep = args.retention_days * 86400.0
    i = 0
    try:
        while True:
            ts = time.time()
            _sweep_once(client, db, team, ts)
            removed = db.prune(keep)
            if not getattr(args, "quiet", False):
                print(
                    "[%s] sweep #%d done (pruned %d rows)"
                    % (
                        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
                        i + 1,
                        removed,
                    )
                )
            i += 1
            time.sleep(interval)
    except KeyboardInterrupt:
        print(_dim("\navailability watch stopped"), file=sys.stderr)
        return 130
    finally:
        db.close()


def _fmt_ts(ts: Optional[float]) -> str:
    if ts is None:
        return "-"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _bucket_key(ts: float, bucket_minutes: int) -> int:
    """Floor a timestamp to a local-time bucket boundary (minutes)."""
    lt = time.localtime(ts)
    day = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    minutes_into_day = lt.tm_hour * 60 + lt.tm_min
    bucket = minutes_into_day // bucket_minutes
    return int(day + bucket * bucket_minutes * 60)


def _heatmap_rows(
    db: AvailabilityDB,
    since: float,
    bucket_minutes: int = 60,
    kind: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Build availability heatmap rows for the report."""
    merged: List[Dict[str, Any]] = []
    for shape in db.shapes(kind=kind):
        sid = int(shape["id"])
        # Map bucketed ts -> max quantity seen in that bucket, so a shape that
        # was briefly free inside a bucket counts as available in it.
        buckets: Dict[int, int] = {}
        for row in db.series(sid, since):
            bucket = _bucket_key(row["ts"], bucket_minutes)
            if row["quantity"] > 0:
                buckets[bucket] = max(buckets.get(bucket, 0), row["quantity"])
        merged.append(
            {
                "shape_id": sid,
                "kind": shape["kind"],
                "label": shape["label"],
                "gpu_count": shape["gpu_count"] or 0,
                "buckets": sorted(buckets.items()),
                "max_quantity": max((q for _, q in buckets.items()), default=0),
            }
        )
    return merged


def cmd_avail_report(args: argparse.Namespace) -> int:
    """Print an availability heatmap + per-shape stats."""
    db = _open_avail_db(args)
    since = time.time() - args.days * 86400.0
    kind = args.kind
    shape_filter = args.shape.lower() if args.shape else None
    try:
        stats = summarize(db, since, kind=kind)
        if shape_filter:
            stats = [s for s in stats if shape_filter in s.label.lower()]
        if args.json:
            emit(
                [],
                [],
                args,
                json_data=[
                    {
                        "shape_id": s.shape_id,
                        "kind": s.kind,
                        "label": s.label,
                        "availability_pct": round(s.availability_pct, 2),
                        "observations": s.observations,
                        "avails": s.avails,
                        "last_quantity": s.last_quantity,
                        "max_quantity": s.max_quantity,
                        "first_seen": _fmt_ts(s.first_seen),
                        "last_seen": _fmt_ts(s.last_seen),
                    }
                    for s in stats
                ],
            )
            return EXIT_OK
        if not stats:
            print(_dim("no availability data yet; run `hotaisle availability watch`"))
            return EXIT_OK
        # Heatmap across the requested window.
        rows = _heatmap_rows(db, since, kind=kind)
        if shape_filter:
            rows = [r for r in rows if shape_filter in r["label"].lower()]
        for r in rows:
            days = sorted({k for k, _ in r["buckets"]})
            r["free_days"] = len(days)
        print(
            _bold(
                "availability over the last %d day(s) (X = some quantity seen):"
                % args.days
            )
        )
        for r in sorted(rows, key=lambda x: (x["kind"], -x["gpu_count"])):
            day = time.localtime(since)
            day0 = time.mktime(
                (day.tm_year, day.tm_mon, day.tm_mday, 0, 0, 0, 0, 0, -1)
            )
            day0_end = day0 + 86400 * args.days
            slots = [(b, q) for b, q in r["buckets"] if b >= day0 and b < day0_end]
            markers = []
            cur_day = None
            for b, q in slots:
                d = time.localtime(b)
                if d.tm_mday != cur_day:
                    markers.append((d.tm_mday, time.strftime("%d", d)))
                    cur_day = d.tm_mday
            header_days = " ".join("%02d" % d for d, _ in markers if d)
            hdr = "%-32s | " % r["label"]
            print(hdr + header_days)
            print(" " * len(hdr) + "  ".join("X" if q > 0 else "." for _b, q in slots))
        print()
        print(_bold("rare shapes (lowest availability):"))
        rare = sorted(stats, key=lambda s: s.availability_pct)
        for s in rare[: args.top]:
            print(
                "  %-34s %5.0f%% avail  (last %s, max seen %d)"
                % (s.label, s.availability_pct, _fmt_ts(s.last_seen), s.max_quantity)
            )
        return EXIT_OK
    finally:
        db.close()


def cmd_avail_serve(args: argparse.Namespace) -> int:
    """Serve a simple HTML/monitoring view of the availability DB."""
    import http.server
    import urllib.parse

    db = _open_avail_db(args)

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != "/" and parsed.path != "/":
                self.send_response(404)
                self.end_headers()
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                days = max(1, int((query.get("days") or ["7"])[0]))
            except ValueError:
                days = 7
            since = time.time() - days * 86400.0
            kinds = query.get("kind") or [None]
            kind = kinds[0] if kinds[0] else None
            stats = summarize(db, since, kind=kind)
            body = _render_html(stats, db, since, days, kind)
            encoded = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, fmt, *a):
            pass

    host, port = args.host, int(args.port)
    try:
        http.server.ThreadingHTTPServer((host, port), Handler).serve_forever()
    except KeyboardInterrupt:
        print(_dim("\navailability server stopped"), file=sys.stderr)
        return 130
    finally:
        db.close()
    return EXIT_OK


def _render_html(stats, db, since, days, kind) -> str:
    """Render a compact HTML availability table for the browser view."""
    esc = (
        lambda s: (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
    rows_html = []
    for s in sorted(stats, key=lambda x: (x.kind, x.availability_pct)):
        pct = s.availability_pct
        color = "#e05555" if pct < 25 else ("#e0a93e" if pct < 60 else "#4a9e6a")
        rows_html.append(
            "<tr><td>%s</td><td>%s</td><td style='background:%s'>%.0f%%</td>"
            "<td>%d</td><td>%d</td><td>%s</td></tr>"
            % (
                esc(s.kind),
                esc(s.label),
                color,
                pct,
                s.max_quantity,
                s.last_quantity,
                esc(_fmt_ts(s.last_seen)),
            )
        )
    html = """<!doctype html>
<html><head><meta charset="utf-8"><title>Hot Aisle availability</title></head>
<body>
<h2>Hot Aisle availability &mdash; last %d day(s)%s</h2>
<a href="/">all</a> | <a href="/?kind=vm">VMs</a> | <a href="/?kind=bm">Bare metal</a>
| <a href="/?days=1">1d</a> <a href="/?days=7">7d</a> <a href="/?days=30">30d</a>
<table border="1" cellpadding="4" cellspacing="0" style="border-collapse:collapse;font-family:monospace">
<tr><th>kind</th><th>shape</th><th>avail</th><th>max qty</th><th>last qty</th><th>last seen</th></tr>
%s
</table>
</body></html>""" % (days, (" (kind=%s)" % kind if kind else ""), "\n".join(rows_html))
    return html


def cmd_api_keys(args: argparse.Namespace) -> int:
    client = make_client(args)
    keys = client.raw("GET", "/user/api_keys/") or []
    emit(
        [
            [
                k.get("prefix") or "-",
                k.get("label") or "-",
                k.get("user_role") or "-",
                ",".join(t.get("handle", "") for t in (k.get("teams") or [])) or "-",
            ]
            for k in keys
        ],
        ["PREFIX", "NAME", "ROLE", "TEAMS"],
        args,
        json_data=keys,
    )
    return EXIT_OK


def cmd_raw(args: argparse.Namespace) -> int:
    client = make_client(args)
    body = json.loads(args.body) if args.body else None
    result = client.raw(
        args.method,
        args.path,
        params=dict(kv.split("=", 1) for kv in args.param or []),
        json_body=body,
    )
    print(json.dumps(result, indent=2, default=str))
    return EXIT_OK


# ------------------------------------------------------------------- parser


# Subparser flags use SUPPRESS so they never clobber a value given to the
# equivalent global flag (e.g. `hotaisle --json vm list`).
_S = argparse.SUPPRESS


def _add_listing_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--json", action="store_true", default=_S, help="emit raw JSON from the API"
    )
    p.add_argument("--csv", action="store_true", default=_S, help="emit CSV")


def _add_common_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--team",
        "-t",
        default=_S,
        help="team handle (or $HOTAISLE_TEAM / config team=)",
    )


def _add_create_flags(p: argparse.ArgumentParser, vm: bool) -> None:
    p.add_argument("--cpu-cores", type=int, help="vCPUs / CPU cores")
    p.add_argument("--ram", help="RAM, e.g. 16G, 512GiB (interpreted as binary units)")
    p.add_argument("--disk", help="disk, e.g. 200G, 1.5T")
    p.add_argument("--gpus", type=int, help="minimum number of GPUs")
    p.add_argument(
        "--exact",
        action="store_true",
        help="send cpu/ram/disk verbatim instead of snapping to an available shape",
    )
    p.add_argument("--description", help="human readable name for the resource")
    p.add_argument(
        "--json-body", help="full request body as a JSON string (overrides the above)"
    )
    if vm:
        p.add_argument(
            "--user-data-url", help="URL to cloud-init user-data (slows provisioning)"
        )
    p.add_argument("--force", action="store_true", help="pass ?force=true")
    p.add_argument(
        "--yes", "-y", action="store_true", help="do not ask for confirmation"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print the request, send nothing"
    )


def _add_delete_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("target", help="deployment_id (preferred) or resource name")
    p.add_argument("--force", action="store_true", help="pass ?force=true")
    p.add_argument(
        "--yes", "-y", action="store_true", help="do not ask for confirmation"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="print what would happen, do nothing"
    )


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="hotaisle",
        description="Interact with the Hot Aisle API (https://admin.hotaisle.app/api/docs/).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="API key resolution order:\n  --api-key > $HOTAISLE_API_KEY > "
        "$HOTAISLE_API_KEY_FILE > key_command > config file api_key > keyring\n"
        "Create keys at https://admin.hotaisle.app/ "
        "(GET /user/api_keys/ lists them).\n",
    )
    ap.add_argument(
        "--api-key", help="API key (or $HOTAISLE_API_KEY). Prefer the env var."
    )
    ap.add_argument(
        "--base-url",
        help="API base URL (default %s)" % "https://admin.hotaisle.app/api",
    )
    ap.add_argument(
        "--team", "-t", help="team handle (or $HOTAISLE_TEAM / config team=)"
    )
    ap.add_argument(
        "--timeout", type=float, default=float(os.environ.get("HOTAISLE_TIMEOUT", 60))
    )
    ap.add_argument(
        "--retries", type=int, default=3, help="retries on 429/5xx (default 3)"
    )
    ap.add_argument("--insecure", action="store_true", help="disable TLS verification")
    ap.add_argument("--json", action="store_true", help="machine readable JSON output")
    ap.add_argument("--csv", action="store_true", help="CSV output")
    ap.add_argument("--version", action="version", version="hotaisle 1.0.0")

    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    # --- account
    p = sub.add_parser("whoami", help="show the authenticated user and teams")
    _add_listing_flags(p)
    p.set_defaults(func=cmd_whoami)
    p = sub.add_parser("teams", help="list teams you belong to")
    _add_listing_flags(p)
    p.set_defaults(func=cmd_teams)
    p = sub.add_parser("balance", help="show a team's credit balance")
    _add_listing_flags(p)
    _add_common_flags(p)
    p.set_defaults(func=cmd_balance)
    p = sub.add_parser(
        "ssh-keys", help="list your SSH keys (needed before provisioning)"
    )
    _add_listing_flags(p)
    p.set_defaults(func=cmd_ssh_keys)
    p = sub.add_parser("api-keys", help="list your API keys")
    _add_listing_flags(p)
    p.set_defaults(func=cmd_api_keys)

    # --- virtual machines
    vm = sub.add_parser(
        "vm",
        aliases=["vms", "virtual-machine"],
        help="virtual machines: list | available | create | delete | ...",
    )
    vsub = vm.add_subparsers(dest="subcommand", metavar="ACTION")

    p = vsub.add_parser("list", aliases=["ls"], help="list VMs currently in the team")
    _add_listing_flags(p)
    _add_common_flags(p)
    p.add_argument(
        "--detail", action="store_true", help="show full shape instead of SSH target"
    )
    p.set_defaults(func=cmd_vm_list)

    p = vsub.add_parser(
        "available",
        aliases=["avail"],
        help="list VM shapes available to deploy, with prices",
    )
    _add_listing_flags(p)
    _add_common_flags(p)
    p.set_defaults(func=cmd_vm_available)

    p = vsub.add_parser("create", aliases=["new", "provision"], help="create a VM")
    _add_common_flags(p)
    _add_create_flags(p, vm=True)
    p.set_defaults(func=cmd_vm_create)

    p = vsub.add_parser("delete", aliases=["rm", "destroy"], help="delete a VM")
    _add_common_flags(p)
    _add_delete_flags(p)
    p.set_defaults(func=cmd_vm_delete)

    p = vsub.add_parser("get", help="show one VM")
    _add_common_flags(p)
    p.add_argument("deployment_id")
    p.add_argument("--json", action="store_true", default=_S)
    p.set_defaults(func=cmd_vm_get)

    p = vsub.add_parser("update", aliases=["edit"], help="update a VM's description")
    _add_common_flags(p)
    p.add_argument("target")
    p.add_argument("--description", help="new VM description")
    p.set_defaults(func=cmd_vm_update)

    p = vsub.add_parser("state", help="show a VM's runtime state")
    _add_common_flags(p)
    p.add_argument("deployment_id")
    p.add_argument("--json", action="store_true", default=_S)
    p.set_defaults(func=cmd_vm_state)

    p = vsub.add_parser(
        "action", help="start|stop|shutdown|reboot|hard-reset|rebuild|console"
    )
    _add_common_flags(p)
    p.add_argument("target")
    p.add_argument("action", choices=_ACTIONS["vm"])
    p.add_argument("--user-data-url")
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_vm_action)

    # --- bare metal
    bm = sub.add_parser(
        "bare-metal",
        aliases=["bm", "metal", "servers"],
        help="bare metal servers: list | available | create | delete | ...",
    )
    bsub = bm.add_subparsers(dest="subcommand", metavar="ACTION")

    p = bsub.add_parser(
        "list", aliases=["ls"], help="list servers reserved by the team"
    )
    _add_listing_flags(p)
    _add_common_flags(p)
    p.set_defaults(func=cmd_bm_list)

    p = bsub.add_parser(
        "available",
        aliases=["avail"],
        help="list server types available to reserve, with prices",
    )
    _add_listing_flags(p)
    _add_common_flags(p)
    p.set_defaults(func=cmd_bm_available)

    p = bsub.add_parser(
        "create", aliases=["new", "reserve"], help="reserve a bare metal server"
    )
    _add_common_flags(p)
    _add_create_flags(p, vm=False)
    p.set_defaults(func=cmd_bm_create)

    p = bsub.add_parser(
        "delete", aliases=["rm", "release"], help="release a bare metal server"
    )
    _add_common_flags(p)
    _add_delete_flags(p)
    p.set_defaults(func=cmd_bm_delete)

    p = bsub.add_parser("power", help="show a server's power state")
    _add_common_flags(p)
    p.add_argument("deployment_id")
    p.add_argument("--json", action="store_true", default=_S)
    p.set_defaults(func=cmd_bm_power)

    # --- availability (read-only history of what /available/ reported) ---
    avail = sub.add_parser(
        "availability",
        aliases=["avail"],
        help="track & view shape availability over time (read-only)",
    )
    av = avail.add_subparsers(dest="actions", metavar="ACTION")

    p = av.add_parser("watch", help="poll /available/ on a loop and store to SQLite")
    _add_common_flags(p)
    p.add_argument(
        "--interval",
        type=int,
        default=120,
        help="seconds between sweeps (default 120 = 2 min)",
    )
    p.add_argument("--db", default=None, help="path to the availability DB")
    p.add_argument(
        "--retention-days",
        type=float,
        default=30.0,
        help="prune samples older than this (default 30)",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="only print problems (no per-sweep line)",
    )
    p.set_defaults(func=cmd_avail_watch)

    p = av.add_parser("report", help="print availability heatmap + stats")
    p.add_argument("--db", default=None, help="path to the availability DB")
    p.add_argument(
        "--days", type=float, default=7.0, help="how far back to report (default 7)"
    )
    p.add_argument("--kind", choices=["vm", "bm"], help="only vm or bare metal")
    p.add_argument("--shape", help="only shapes whose label contains this")
    p.add_argument(
        "--top", type=int, default=5, help="how many rarest shapes to list (default 5)"
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=_S,
        help="emit JSON stats instead of a table",
    )
    p.set_defaults(func=cmd_avail_report)

    p = av.add_parser("serve", help="run a simple HTML view in a terminal")
    p.add_argument("--db", default=None, help="path to the availability DB")
    p.add_argument(
        "--host", default="127.0.0.1", help="listen host (default 127.0.0.1)"
    )
    p.add_argument("--port", type=int, default=8301, help="listen port (default 8301)")
    p.set_defaults(func=cmd_avail_serve)

    p = bsub.add_parser(
        "action", help="console|reinstall|support_access_enable|power/..."
    )
    _add_common_flags(p)
    p.add_argument("target")
    p.add_argument("action", choices=_ACTIONS["bm"])
    p.add_argument("--yes", "-y", action="store_true")
    p.set_defaults(func=cmd_bm_action)

    # --- escape hatch
    p = sub.add_parser("raw", help="call any endpoint, e.g. hotaisle raw GET /user/")
    p.add_argument("method")
    p.add_argument("path")
    p.add_argument("-p", "--param", action="append", metavar="K=V")
    p.add_argument("--body", help="request body as JSON")
    p.set_defaults(func=cmd_raw)

    return ap


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not getattr(args, "func", None):
        parser.print_help()
        return EXIT_USAGE
    try:
        return args.func(args)
    except AuthError as exc:
        print(_red(str(exc)), file=sys.stderr)
        print(
            _dim(
                "Check the key ($HOTAISLE_API_KEY), the --team, and whether this key's "
                "role restrictions allow the call."
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR
    except APIError as exc:
        print(_red(str(exc)), file=sys.stderr)
        if exc.status_code == 428:
            print(
                _dim(
                    "Upload an SSH key first: POST /user/ssh_keys/ "
                    '(`hotaisle raw POST /user/ssh_keys/ --body \'{"key":"ssh-rsa ..."}\')'
                ),
                file=sys.stderr,
            )
        elif exc.status_code == 402:
            print(
                _dim("Team balance is too low. See `hotaisle balance`."),
                file=sys.stderr,
            )
        return EXIT_ERROR
    except ConfigurationError as exc:
        print(
            (
                str(exc)
                if "HOTAISLE_API_KEY" in str(exc)
                else "%s\n\n%s" % (exc, NO_KEY_HINT)
            ),
            file=sys.stderr,
        )
        return EXIT_ERROR
    except KeyboardInterrupt:
        print(_dim("\ninterrupted"), file=sys.stderr)
        return 130
    except HotAisleError as exc:  # pragma: no cover - safety net
        die(str(exc))


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
