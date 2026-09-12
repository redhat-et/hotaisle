#!/usr/bin/env python3
"""Write a JSON inventory of your Hot Aisle machines, optionally enriched with
Prometheus ``up`` status so you can see which machines are actually reachable.

This is an *example* script -- nothing is scheduled for you. Run it by hand, or
wire it into cron / a systemd timer on your own host (see the units in
``systemd/`` next to this file).

    ./inventory.py --team acme-corp -o inventory.json
    ./inventory.py --prometheus http://host.containers.internal:9091
    ./inventory.py --quiet            # cron-friendly: silent unless something fails

Exit codes: 0 = wrote inventory, 1 = could not talk to the Hot Aisle API,
2 = could not parse arguments. Prometheus failures are always non-fatal and
leave ``"monitored": null`` in the output.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from hotaisle import Client, HotAisleError  # noqa: E402


def prometheus_up(base_url: str, timeout: float = 8.0) -> dict:
    """Return {instance: up} from Prometheus. Raises on any failure."""
    url = base_url.rstrip("/") + "/api/v1/query?query=" + urllib.parse.quote("up")
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        payload = json.load(resp)
    result = {}
    for series in payload.get("data", {}).get("result", []):
        instance = series.get("metric", {}).get("instance", "")
        try:
            result[instance] = bool(float(series["value"][1]))
        except (KeyError, IndexError, TypeError, ValueError):
            continue
    return result


def match_up(address, up_map):
    """Match an inventory IP against Prometheus instance labels (ip:port or ip).

    Returns True/False when the machine is monitored, or None when it is not
    (no Prometheus configured, no matching target, or an empty address).
    """
    if not address or not up_map:
        return None
    for instance, state in up_map.items():
        host = instance.rsplit(":", 1)[0] if ":" in instance else instance
        if host == address:
            return state
    return None


def collect(client: Client, team: str, up_map) -> dict:
    vms = client.list_virtual_machines(team)
    servers = client.list_bare_metal(team)

    def vm_row(vm):
        return {
            "name": vm.name,
            "deployment_id": vm.deployment_id,
            "description": vm.description,
            "ip_address": vm.ip_address,
            "cpu_cores": vm.specs.cpu_cores,
            "ram_bytes": vm.specs.ram_capacity,
            "disk_bytes": vm.specs.disk_capacity,
            "gpus": [{"count": g.count, "manufacturer": g.manufacturer,
                      "model": g.model} for g in vm.specs.gpus],
            "ssh": vm.ssh_command,
            "monitored": match_up(vm.ip_address, up_map) if up_map is not None else None,
        }

    def bm_row(s):
        return {
            "name": s.name,
            "deployment_id": s.deployment_id,
            "description": s.description,
            "ip_address": s.ip_address,
            "manufacturer": s.manufacturer,
            "model": s.model,
            "cpu_cores": s.specs.cpu_cores,
            "ram_bytes": s.specs.ram_capacity,
            "disk_bytes": s.specs.disk_capacity,
            "gpus": [{"count": g.count, "manufacturer": g.manufacturer,
                      "model": g.model} for g in s.specs.gpus],
            "support_access_enabled": s.support_access_enabled,
            "ssh": s.ssh_command,
            "monitored": match_up(s.ip_address, up_map) if up_map is not None else None,
        }

    return {
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "hostname": socket.gethostname(),
        "team": team,
        "counts": {"virtual_machines": len(vms), "bare_metal": len(servers)},
        "virtual_machines": [vm_row(v) for v in vms],
        "bare_metal": [bm_row(s) for s in servers],
    }


def atomic_write(path: str, text: str) -> None:
    """Write via a temp file + rename so a reader never sees a half-written file."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--team", "-t", help="team handle (default: $HOTAISLE_TEAM/config)")
    ap.add_argument("-o", "--output", default="inventory.json",
                    help="output path, or '-' for stdout (default inventory.json)")
    ap.add_argument("--prometheus",
                    default=os.environ.get("HOTAISLE_PROMETHEUS", ""),
                    help="Prometheus base URL to cross-reference up{} status")
    ap.add_argument("--no-prometheus", action="store_true",
                    help="skip the up{} lookup even if a URL is configured")
    ap.add_argument("--quiet", "-q", action="store_true",
                    help="only print problems (suitable for cron)")
    args = ap.parse_args(argv)

    try:
        client = Client(team=args.team)
    except HotAisleError as exc:
        print("hotaisle: %s" % exc, file=sys.stderr)
        return 1

    team = args.team or client.team
    if not team:
        teams = client.list_teams()
        if len(teams) != 1:
            print("hotaisle: pass --team (options: %s)"
                  % ", ".join(t.handle or "?" for t in teams), file=sys.stderr)
            return 1
        team = teams[0].handle

    up_map = None
    if args.prometheus and not args.no_prometheus:
        try:
            up_map = prometheus_up(args.prometheus)
        except (urllib.error.URLError, OSError, ValueError, KeyError) as exc:
            # Monitoring is a bonus; never fail the inventory over it.
            print("warning: Prometheus unreachable at %s (%s); writing inventory "
                  "with monitored=null" % (args.prometheus, exc), file=sys.stderr)
            up_map = None

    try:
        inventory = collect(client, team, up_map)
    except HotAisleError as exc:
        print("hotaisle: %s" % exc, file=sys.stderr)
        return 1

    text = json.dumps(inventory, indent=2, sort_keys=False) + "\n"
    if args.output == "-":
        sys.stdout.write(text)
    else:
        atomic_write(args.output, text)
        if not args.quiet:
            down = [r for section in ("virtual_machines", "bare_metal")
                    for r in inventory[section] if r["monitored"] is False]
            print("wrote %s: %s VM(s), %s bare metal(s), %d monitored-down"
                  % (args.output, inventory["counts"]["virtual_machines"],
                     inventory["counts"]["bare_metal"], len(down)))
            for row in down:
                print("  DOWN %s %s (%s)" % (row["name"], row["ip_address"],
                                             row["deployment_id"]), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
