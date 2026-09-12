# hotaisle

Python module + CLI for the [Hot Aisle](https://admin.hotaisle.app/) API
(hotManager). Manage virtual machines and bare metal servers.

Built against the published spec: <https://admin.hotaisle.app/api/docs/swagger.json>
(Swagger 2.0, `basePath: /api/`).

**No third-party dependencies** — stdlib only (`urllib`, `argparse`, `tomllib`).
Drop it on a bastion host, a slim container or a Fedora box with no venv and it
just runs. Python 3.9+ (3.11+ uses `tomllib`; older versions use a small fallback parser).

---

## 1. Installing

The package has no dependencies, so you do not need to install it at all:

```sh
# Run straight from the source tree
PYTHONPATH=/path/to/hotaisle python3 -m hotaisle --help
```

For a `hotaisle` command on your PATH, use the launcher in `bin/` — it just sets
`PYTHONPATH` and execs `python3 -m hotaisle`, so no build backend
(setuptools/pip) is required:

```sh
mkdir -p ~/.local/bin
sed "s#^HOTAISLE_SRC=.*#HOTAISLE_SRC=\"$PWD\"#" bin/hotaisle > ~/.local/bin/hotaisle
chmod 700 ~/.local/bin/hotaisle
hotaisle --version
```

Optionally install it properly (needs `setuptools` on the system):

```sh
python3 -m pip install --user .          # or: pipx install .
python3 -m pip install --user '.[keyring]'   # + OS keyring support
```

---

## 2. Injecting the API key  ← read this

The API authenticates with a single header:

```
Authorization: Token <your-api-key>
```

Note the literal `Token ` prefix and the **space**. Keys are created in the web
console at <https://admin.hotaisle.app/>; the API can also manage them
(`GET/POST /user/api_keys/`, exposed as `hotaisle api-keys`).

The module searches for a key in this order — **first match wins**, and
environment variables always beat the config file so a shell, unit file or cron
entry can override what is on disk:

| # | Source | How |
|---|--------|-----|
| 1 | Argument | `Client(api_key="...")` / `hotaisle --api-key ...` |
| 2 | Environment | `HOTAISLE_API_KEY` (alias `HOTAISLE_TOKEN`) |
| 3 | Key file | `HOTAISLE_API_KEY_FILE=/path/to/key` |
| 4 | Command | `HOTAISLE_API_KEY_COMMAND='pass show hotaisle/api'` |
| 5 | Config file | `key_command` → `key_file` → `api_key` in `~/.config/hotaisle/config.toml` |
| 6 | OS keyring | optional `keyring` package + `HOTAISLE_KEYRING=1` |

A bare key, `"quoted"`, `Token abc`, and `Bearer abc` are all accepted; the
prefix is normalised away internally and re-added on the wire.

### Recommended: environment variable

Best default for an interactive shell, CI, or a systemd unit.

```sh
# ~/.bashrc or ~/.zshrc  — but see the caveat below for shared machines
export HOTAISLE_API_KEY='...'
```

Because an API key can spend money, prefer not to park it in a world-readable
dotfile. Better options, in rough order of preference:

**Keyring command** — nothing secret ever lands on disk in plaintext:

```sh
export HOTAISLE_API_KEY_COMMAND='pass show hotaisle/api'
# 1Password:  op read op://personal/hotaisle/api
# macOS:      security find-generic-password -s hotaisle -w
# AWS:        aws secretsmanager get-secret-value --secret-id hotaisle \
#               --query SecretString --output text | jq -r .hotaisle_api_key
```

**Key file** — good for cron, containers and systemd:

```sh
umask 077
printf '%s' 'your-key' > ~/.config/hotaisle/api_key
export HOTAISLE_API_KEY_FILE=~/.config/hotaisle/api_key
```

**Config file** — convenient for a personal workstation. Copy the template:

```sh
mkdir -p ~/.config/hotaisle
cp config.example.toml ~/.config/hotaisle/config.toml
chmod 600 ~/.config/hotaisle/config.toml       # the CLI warns if you forget
```

```toml
team = "your-team-handle"
key_command = "pass show hotaisle/api"   # preferred
# api_key = "your-key"                   # or inline (protect the file!)
```

The CLI prints the *source* and a masked preview of whichever key it picked, so
you can confirm where a key came from without leaking it:

```
$ hotaisle whoami
Alice  alice@example.com
api key from key_command: pass****
```

### systemd example

```ini
# ~/.config/systemd/user/hotaisle-sync.service
[Service]
Environment=HOTAISLE_API_KEY_COMMAND=/usr/bin/pass show hotaisle/api
Environment=HOTAISLE_TEAM=acme-corp
ExecStart=%h/.local/bin/hotaisle vm list --json
```

### Other environment variables

| Variable | Purpose |
|----------|---------|
| `HOTAISLE_TEAM` | default team handle for `vm`/`bm` commands |
| `HOTAISLE_BASE_URL` | override `https://admin.hotaisle.app/api` |
| `HOTAISLE_CONFIG` | path to the config file |
| `HOTAISLE_TIMEOUT` | per-request timeout in seconds (default 60) |
| `HOTAISLE_KEYRING` | set to `1` to try the OS keyring last |
| `NO_COLOR` | disable ANSI colour |

### Never put a key on a command line

`hotaisle --api-key` exists for scripts that already have the value in memory.
Do **not** use `hotaisle --api-key $(pass show ...)` — arguments are visible to
every other process via `ps`. Prefer the env vars above.

---

## 3. CLI

Every verb exists for both resource kinds: `hotaisle vm ...` and
`hotaisle bm ...` (aliases: `vms`, `metal`, `servers`).

### Listing what you already have

```console
$ hotaisle vm list
NAME    DEPLOYMENT ID                         SHAPE                      SSH                    DESCRIPTION
------  ------------------------------------  -------------------------  ---------------------  ---------------------
vm-01   195116dc-32ed-49e5-a738-5e2ad0cdd141  8 vCPU / 32 GiB / 100 GiB  vm01.example.com:2222  Production web server

$ hotaisle bm list
NAME       DEPLOYMENT ID                         HARDWARE               SHAPE                        GPUs           IP            SUPPORT ACCESS
---------  ------------------------------------  ---------------------  ---------------------------  -------------  ------------  ----------------
server-01  77b3e2a2-5a67-4c07-9f2e-9d5d6f0a1b2c  Dell PowerEdge XE9680  64 vCPU / 512 GiB / 4.0 TiB  8x AMD MI300X  192.168.1.100  no
```

`--json` emits the untouched server payload; `--csv` emits CSV. Both work on any
listing command, before or after the verb (`hotaisle --json vm list`).

### Listing what is available to deploy

```console
$ hotaisle vm available
#  QTY  VCPU  RAM      DISK     GPUs            PRICE      MIN RESV
-  ---  ----  -------  -------  --------------  ---------  --------
1  4    8     32 GiB   100 GiB  -               $3.50/hr   30m
2  0    32    128 GiB  1.0 TiB  1x NVIDIA L40S  $12.00/hr  30m
```

`QTY` is live inventory. `MIN RESV` is the minimum billable reservation — bare
metal is commonly 8h+ and the API refuses an early release.

### Creating

`POST /teams/{team}/virtual_machines/` and `/bare_metal/` require an exact spec
match against real inventory. Rather than make you hand-type byte counts, the
CLI matches a shape by **specs** from the `available` listing:

```sh
hotaisle vm create --cpu-cores 8 --ram 224GiB --disk 12TiB --gpus 1 --description "build box"
hotaisle bm create --cpu-cores 64 --ram 512G --disk 4T --gpus 8 --description "training node"
```

Or describe minimums and let the CLI pick the cheapest thing that fits:

```sh
hotaisle vm create --cpu-cores 4 --ram 16G --disk 200G --gpus 1
# no exact shape; smallest available that fits: 8 vCPU / 32 GiB / 100 GiB ...
```

- `--ram`/`--disk` accept `16G`, `512GiB`, `1.5T`, or raw bytes.
- `--exact` sends your numbers verbatim instead of snapping to inventory.
- `--user-data-url <url>` (VMs only) applies cloud-init; it noticeably slows provisioning.
- `--json-body '{...}'` bypasses all of the above and sends your body verbatim.
- Every create prints the request body first and asks for confirmation.

**Always check the money before a create:** `hotaisle balance`.

### Deleting

```sh
hotaisle vm delete vm-01              # names are resolved via a lookup first
hotaisle bm delete server-01 --yes
hotaisle vm delete <deployment_id> --dry-run
```

- Paths use the **`deployment_id`**, not the name. The CLI looks the name up in
  the list first and fails loudly if it is ambiguous.
- Deletion is destructive and irreversible, so it is always confirmation-gated.
- Without a TTY (cron, CI, pipes) a missing `--yes` **aborts instead of hanging**.
- Bare metal release fails with HTTP 400 until `MIN RESV` has elapsed.
- `--force` sends `?force=true` for both create and delete.

### Extras

```sh
hotaisle whoami | teams | balance | api-keys | ssh-keys
hotaisle vm get <id> | state <id>
hotaisle vm update <id-or-name> --description "..."
hotaisle vm action <id-or-name> start|stop|shutdown|reboot|hard-reset|rebuild|console
hotaisle bm power <id>
hotaisle bm action <id-or-name> reinstall|console|support_access_enable|power/...
hotaisle raw GET /user/                  # any endpoint not wrapped yet
```

### Availability tracking (read-only)

Hot Aisle shapes are scarce, so ``availability`` records what the ``/available/``
endpoints report over time into a local SQLite history, then lets you see *when*
 each shape is free. It only ever **reads** the API — it never creates, deletes or
reserves anything.

```sh
# 1) Collect: poll every 2 minutes, keep 30 days (run in a terminal)
hotaisle availability watch --interval 120 --retention-days 30

# 2) View: heatmap + per-shape stats, rare shapes flagged first
hotaisle availability report --days 7
hotaisle availability report --kind bm --days 30
hotaisle availability report --shape mi300x --json

# 3) Optional: tiny HTTP view in a terminal (browser at http://127.0.0.1:8301/)
hotaisle availability serve --port 8301
```

The data lives in ``~/.local/state/hotaisle/availability.db`` (override with
``--db``). A shape that disappears from a listing is recorded as quantity 0, so
the history stays continuous; old samples beyond ``--retention-days`` are pruned on
each sweep. Transient API errors during a sweep are printed as warnings and skipped —
one bad sweep never loses the other kind's data.

Provisioning returns **HTTP 428** unless the team has an accepted user-role
member with an SSH key. Add one first:

```sh
hotaisle raw POST /user/ssh_keys/ --body '{"authorized_key":"ssh-ed25519 AAAA... you@host"}'
```

---

## 4. Python API

```python
from hotaisle import Client

client = Client(team="acme-corp")          # key resolved from env/config

# Current inventory
for vm in client.list_virtual_machines():
    print(vm.name, vm.deployment_id, vm.ip_address, vm.specs.label)
    print("  ", vm.ssh_command)

for srv in client.list_bare_metal():
    print(srv.name, srv.hardware, srv.specs.gpu_summary, srv.ip_address)

# What can I deploy right now, and what does it cost?
for avail in client.list_available_bare_metal():
    print(avail.quantity, avail.specs.full_label,
          avail.price_per_hour, avail.minimum_reservation)

# Create (byte counts, as the API wants them)
from hotaisle.client import specs_to_selector
avail = client.list_available_virtual_machines()[0]
vm = client.create_virtual_machine(**{
    "cpu_cores": avail.specs.cpu_cores,
    "ram_capacity": avail.specs.ram_capacity,
    "disk_capacity": avail.specs.disk_capacity,
    "description": "worker-1",
})

# Delete — deployment_id, not name
client.delete_virtual_machine(vm.deployment_id)
client.delete_bare_metal("77b3e2a2-...", force=False)
```

Escape hatch for anything not wrapped: `client.raw("GET", "/user/")`.

### Error handling

```python
from hotaisle import APIError, AuthError, NotFoundError, InsufficientBalanceError
from hotaisle import PreconditionFailedError, ValidationError, ConfigurationError

try:
    client.create_virtual_machine(cpu_cores=8, ram_capacity=2**34,
                                  disk_capacity=2**37)
except AuthError as e:               # 401 / 403 — bad key or insufficient role
    ...
except InsufficientBalanceError:     # 402 — team is out of credit
    ...
except NotFoundError:                # 404 — bad team, or no inventory of that shape
    ...
except PreconditionFailedError:      # 428 — team has no member with an SSH key
    ...
except ValidationError as e:         # 400 / 422 — malformed request
    ...
except APIError as e:
    print(e.status_code, e.body)
```

`429`/`5xx` are retried with exponential backoff + jitter and honour
`Retry-After` (`max_retries=3`, `--retries`). `verify_ssl=False` exists for
self-signed test endpoints; don't use it in production.

---

## 5. Notes on this particular API

Things worth knowing that shaped the implementation:

- **`Token` prefix, not `Bearer`.** `securityDefinitions` is an `apiKey` scheme
  named `Authorization`.
- **Errors are `text/plain`, not JSON.** `401` returns the literal string
  `Unauthorized`. Never assume `error.json()` on failure.
- **The `/available/` endpoints use PascalCase** wrapper keys (`Quantity`,
  `Specs`, `OnDemandPrice`, `MinimumReservationMinutes`) while the nested specs
  stay snake_case. Everything else in the API is snake_case. `models.pick()`
  reads both spellings, which is also why unknown future fields are tolerated.
- **Flattened vs nested specs.** `VirtualMachineDetails` and
  `BareMetalServerDetails` are `allOf` merges, so a *listing* returns specs
  flattened into the object; the bare-metal *create* response nests them under
  `specs`. Both are parsed.
- **Request bodies differ by kind.** VM provisioning takes specs flattened at
  the top level; bare metal requires them wrapped in `{"specs": {...}}`.
- **`{vm}` and `{server}` path params are deployment IDs** (UUIDs).
- **`?force=` is a query param** on both create and delete, not a body field.
- **Create can return 200 (VM) or 201 (bare metal)** — both are success.
- **Bare metal has a minimum reservation window**; early release → HTTP 400.
- **`402` means the team is out of credit**, `401` on VM create is documented as
  "maximum VMs already provisioned", and `428` means no SSH key is on the team.
- **VM create accepts no `description`** (`VMProvisionRequest` has no such
  field), so the CLI/`create_virtual_machine` set it with a follow-up
  `PATCH /teams/{team}/virtual_machines/{vm}/` after the create succeeds.
- **`GET /user/` nests the identity** under a `user` key alongside a top-level
  `teams` array (`GetUserResponse`), unlike most endpoints where the payload
  is the object itself.
- **`api_keys` entries use `label`** (not `name`) and return no created/expiry
  timestamps; `user_role` and per-team `roles` describe what a key can do.
- **Adding an SSH key takes `authorized_key`** — one string in
  `<type> <base64> <comment>` format (`SSHKeyRequest`) — not separate
  `key`/`name` fields.

---

## 6. Example: inventory script

`examples/inventory.py` shows how to build on the module for a real task: it
writes a JSON inventory of every VM and bare metal server, and — because a list
of machines is not the same as a list of *healthy* machines — cross-references
each IP against Prometheus `up{}`.

```sh
./examples/inventory.py --team acme-corp -o inventory.json
./examples/inventory.py --prometheus http://host.containers.internal:9091
./examples/inventory.py --quiet          # cron-friendly: silent unless something is wrong
```

It is deliberately conservative about failure:

- Prometheus is a bonus. If it is unreachable the inventory is still written,
  with `"monitored": null` rather than a bogus `false`.
- The file is written to a temp path, `fsync`ed, then `os.replace`d, so a reader
  or a monitoring poller can never observe a half-written file.
- Exit codes: `0` wrote the inventory, `1` could not reach the Hot Aisle API.
- **Nothing is scheduled for you.** Run it by hand, or install the systemd user
  units described below if you want it periodic.

If you do want it periodic, a systemd user timer is the reliable way (cron is
often absent in containers, and systemd hands the service a non-TTY stdin, which
this CLI is built to tolerate):

```ini
# ~/.config/systemd/user/hotaisle-inventory.service
[Service]
Type=oneshot
Environment=HOTAISLE_API_KEY_COMMAND=/usr/bin/pass show hotaisle/api
Environment=HOTAISLE_TEAM=acme-corp
Environment=HOTAISLE_PROMETHEUS=http://127.0.0.1:9091
ExecStart=%h/src/hotaisle/examples/inventory.py --quiet -o %h/inventory.json

# ~/.config/systemd/user/hotaisle-inventory.timer
[Timer]
OnCalendar=hourly
Persistent=true

[Install]
WantedBy=timers.target
```

```sh
systemctl --user daemon-reload && systemctl --user start hotaisle-inventory.timer
```

---

## 7. Tests

`tests/` runs against a local fake server that replays the exact payload shapes
from the spec — no API key, no network, no billable calls:

```sh
python3 -m unittest discover -s tests -t . -v
```

The suite covers the header format, every key-resolution path and its precedence,
the PascalCase/snake_case split, flattened-vs-nested specs, exact request bodies,
`?force` encoding, deployment-id delete paths, status→exception mapping, retry
behaviour, and every CLI verb including `--dry-run`, `--json`, `--csv` and the
confirmation gate. The availability tests cover the SQLite history: schema
init, stable shape identity, absent-shape → quantity 0, pruning, per-shape
summary and kind filtering.

## 8. Security notes

- The key is never logged; `Credential.__repr__` and `whoami` mask it.
- Config/key files that are group- or world-readable trigger a warning with the
  exact `chmod` to run.
- `--api-key` on a command line is visible in `ps` — prefer env vars.
- `--yes` on a delete is opt-in per invocation and never defaulted.
- Confirmations never block on a non-TTY stdin; they abort.

---

## 9. License

Distributed under the Apache License 2.0. See [LICENSE](LICENSE).
