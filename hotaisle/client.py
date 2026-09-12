"""HTTP client for the Hot Aisle (hotManager) REST API.

Spec: https://admin.hotaisle.app/api/docs/  (Swagger 2.0, basePath ``/api/``)
Auth: ``Authorization: Token <api-key>`` header.

Stdlib-only by design (``urllib.request``) so the module can be dropped onto any
machine with Python 3.9+ and no pip installs.

    from hotaisle import Client
    client = Client()                      # key from env / config file
    for vm in client.list_virtual_machines("acme-corp"):
        print(vm.name, vm.ip_address)
"""

from __future__ import annotations

import json
import os
import random
import socket
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from . import models
from .auth import (
    ENV_BASE_URL,
    Credential,
    load_config,
    resolve_api_key,
    warn_if_world_readable,
)
from .errors import APIError, AuthError, ConfigurationError, error_for_status

DEFAULT_BASE_URL = "https://admin.hotaisle.app/api"
USER_AGENT = "hotaisle-python/1.0 (+https://admin.hotaisle.app/api/docs/)"

# Statuses worth retrying: throttling and transient server failures.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
# Bare-metal reservation replies can be slow while the IPMI reservation is placed.
DEFAULT_TIMEOUT = 60.0


def _to_bytes(value: Union[str, bytes]) -> bytes:
    return value.encode("utf-8") if isinstance(value, str) else value


@dataclass
class Response:
    status_code: int
    headers: Dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    @property
    def json(self) -> Any:
        if not self.body.strip():
            return None
        return json.loads(self.body.decode("utf-8"))


class Client:
    """Synchronous Hot Aisle API client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        team: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = 3,
        backoff: float = 0.7,
        verify_ssl: bool = True,
        config: Optional[dict] = None,
        user_agent: str = USER_AGENT,
        session: Optional["urllib.request.OpenerDirector"] = None,
    ):
        self.config = config if config is not None else load_config()
        self.credential: Credential = resolve_api_key(api_key, config=self.config)
        self.api_key: str = self.credential.api_key

        self.base_url = self._resolve_base_url(base_url)
        self.team = team or os.environ.get("HOTAISLE_TEAM") or self.config.get("team")
        self.ssh_user = (
            os.environ.get("HOTAISLE_SSH_USER")
            or self.config.get("ssh_user")
            or models.DEFAULT_SSH_USER
        )
        self.timeout = float(timeout)
        self.max_retries = int(max_retries)
        self.backoff = float(backoff)
        self.verify_ssl = verify_ssl
        self.user_agent = user_agent
        self._opener = session
        #: warning text if the config file permissions look too loose, else None
        self.security_warning: Optional[str] = self._check_config_perms()

    # ---------------------------------------------------------------- plumbing

    def _resolve_base_url(self, base_url: Optional[str]) -> str:
        raw = (
            base_url
            or os.environ.get(ENV_BASE_URL)
            or self.config.get("base_url")
            or DEFAULT_BASE_URL
        )
        raw = str(raw).rstrip("/")
        # Be forgiving: accept the docs URL or a host with no /api suffix.
        if raw.endswith("/api/docs"):
            raw = raw[: -len("/docs")]
        parsed = urllib.parse.urlsplit(raw)
        if not parsed.scheme:
            raw = "https://" + raw
        if urllib.parse.urlsplit(raw).path in ("", "/"):
            raw = raw.rstrip("/") + "/api"
        return raw.rstrip("/")

    def _check_config_perms(self) -> Optional[str]:
        from .auth import default_config_path

        return warn_if_world_readable(default_config_path())

    @property
    def auth_header(self) -> str:
        """The exact header value the API expects: ``Token <key>``."""
        return "Token %s" % self.api_key

    def request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> Response:
        """Perform a request, retrying transient failures, and raise on HTTP errors."""
        method = method.upper()
        url = self.base_url + (path if path.startswith("/") else "/" + path)
        query = {
            k: _encode_bool(v) if isinstance(v, bool) else v
            for k, v in (params or {}).items()
            if v is not None
        }
        if query:
            url = "%s?%s" % (url, urllib.parse.urlencode(query, doseq=True))

        data = None
        req_headers = {
            "Authorization": self.auth_header,
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        if json_body is not None:
            data = _to_bytes(json.dumps(json_body))
            req_headers["Content-Type"] = "application/json"
        # POST with no body still needs a length so the Go server doesn't hang up.
        if method in ("POST", "PUT", "PATCH") and data is None:
            data = b""
            req_headers["Content-Length"] = "0"
        req_headers.update(headers or {})

        last_error: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            if attempt:
                delay = self.backoff * (2 ** (attempt - 1))
                # Honour Retry-After when present, and always add jitter.
                if isinstance(last_error, tuple):
                    retry_after = _parse_retry_after(last_error[1].headers)
                    delay = max(delay, retry_after) if retry_after else delay
                time.sleep(delay + random.uniform(0, 0.25))

            req = urllib.request.Request(url, data=data, method=method)
            for k, v in req_headers.items():
                req.add_header(k, v)

            try:
                resp = self._open(req)
            except urllib.error.HTTPError as exc:
                body = exc.read() if exc.fp else b""
                hdrs = {k.lower(): v for k, v in (exc.headers.items() if exc.headers else [])}
                if exc.code in RETRY_STATUSES and attempt < self.max_retries:
                    last_error = (exc, Response(exc.code, hdrs, body))
                    continue
                return self._handle_error(exc.code, body, method, path)
            except urllib.error.URLError as exc:
                reason = getattr(exc, "reason", exc)
                if attempt < self.max_retries and isinstance(
                    reason, (socket.timeout, TimeoutError, OSError)
                ):
                    last_error = exc
                    continue
                _raise_connection(url, reason)
            except (socket.timeout, TimeoutError) as exc:
                if attempt < self.max_retries:
                    last_error = exc
                    continue
                _raise_connection(url, exc)
            else:
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
                status = resp.status if hasattr(resp, "status") else resp.code
                body = _read_body_limited(resp, url, self.timeout)
                response = Response(status, hdrs, body)
                if response.status_code >= 400:
                    return self._handle_error(response.status_code, response.body,
                                              method, path)
                return response

        # Retries exhausted.
        if isinstance(last_error, tuple):
            return self._handle_error(last_error[1].status_code, last_error[1].body,
                                      method, path)
        raise AuthError("Request to %s failed after %d retries" % (url, self.max_retries),
                        status_code=0)

    def _open(self, req: urllib.request.Request):
        if self._opener is not None:
            return self._opener.open(req, timeout=self.timeout)
        if not self.verify_ssl:  # pragma: no cover - opt-in for self-signed setups
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            opener = urllib.request.build_opener(
                urllib.request.HTTPSHandler(context=ctx)
            )
            return opener.open(req, timeout=self.timeout)
        return urllib.request.build_opener().open(req, timeout=self.timeout)

    def _handle_error(self, status: int, body: bytes, method: str, path: str) -> Response:
        text = body.decode("utf-8", "replace") if body else ""
        if status in (401, 403):
            raise AuthError(
                "%s %s -> HTTP %d: %s" % (method, path, status, text.strip() or "Unauthorized"),
                status_code=status,
                body=text,
            )
        raise error_for_status(status, text, method=method, path=path)

    def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Response:
        return self.request("GET", path, params=params)

    def post(self, path: str, json_body: Optional[Any] = None,
             params: Optional[Dict[str, Any]] = None) -> Response:
        return self.request("POST", path, params=params, json_body=json_body)

    def patch(self, path: str, json_body: Optional[Any] = None) -> Response:
        return self.request("PATCH", path, json_body=json_body)

    def delete(self, path: str, params: Optional[Dict[str, Any]] = None) -> Response:
        return self.request("DELETE", path, params=params)

    def _team_path(self, team: Optional[str], suffix: str = "") -> str:
        handle = team or self.team
        if not handle:
            raise ConfigurationError(
                "No team specified. Pass team=... , set HOTAISLE_TEAM, set "
                "team = \"handle\" in ~/.config/hotaisle/config.toml, or call "
                "list_teams() / `hotaisle teams` to see your options."
            )
        return "/teams/%s%s" % (urllib.parse.quote(str(handle), safe=""), suffix)

    # ------------------------------------------------------------ account/team

    def get_user(self) -> models.User:
        return models.User.from_dict(self.get("/user/").json or {})

    def list_teams(self) -> List[models.Team]:
        return [models.Team.from_dict(t) for t in (self.get("/teams/").json or [])]

    def get_balance(self, team: Optional[str] = None) -> models.Balance:
        return models.Balance.from_dict(self.get(self._team_path(team, "/balance/")).json or {})

    # ------------------------------------------------------- virtual machines

    def list_virtual_machines(self, team: Optional[str] = None) -> List[models.VirtualMachine]:
        """GET /teams/{team}/virtual_machines/ — VMs currently assigned to the team."""
        data = self.get(self._team_path(team, "/virtual_machines/")).json or []
        return [models.VirtualMachine.from_dict(vm, ssh_user=self.ssh_user) for vm in data]

    def list_available_virtual_machines(
        self, team: Optional[str] = None
    ) -> List[models.AvailableType]:
        """GET /teams/{team}/virtual_machines/available/ — deployable VM types + pricing."""
        data = self.get(self._team_path(team, "/virtual_machines/available/")).json or []
        return [models.AvailableType.from_dict(a) for a in data]

    def get_virtual_machine(
        self, deployment_id: str, team: Optional[str] = None
    ) -> models.VirtualMachine:
        path = self._team_path(team, "/virtual_machines/%s/" % _ident(deployment_id))
        return models.VirtualMachine.from_dict(self.get(path).json or {}, ssh_user=self.ssh_user)

    def get_virtual_machine_state(
        self, deployment_id: str, team: Optional[str] = None
    ) -> models.VMState:
        path = self._team_path(team, "/virtual_machines/%s/state/" % _ident(deployment_id))
        return models.VMState.from_dict(self.get(path).json or {})

    def create_virtual_machine(
        self,
        cpu_cores: Optional[int] = None,
        ram_capacity: Optional[int] = None,
        disk_capacity: Optional[int] = None,
        description: Optional[str] = None,
        user_data_url: Optional[str] = None,
        gpus: Optional[List[Dict[str, Any]]] = None,
        force: bool = False,
        team: Optional[str] = None,
        specs: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> models.VirtualMachine:
        """POST /teams/{team}/virtual_machines/ — provision a VM.

        ``ram_capacity``/``disk_capacity`` are raw bytes (as the API requires).
        Pass a full ``body`` dict to bypass the convenience arguments entirely.
        """
        payload = body if body is not None else _vm_body(
            cpu_cores=cpu_cores, ram_capacity=ram_capacity, disk_capacity=disk_capacity,
            description=description, user_data_url=user_data_url, gpus=gpus, specs=specs,
        )
        resp = self.post(self._team_path(team, "/virtual_machines/"),
                         json_body=payload, params={"force": force} if force else None)
        vm = models.VirtualMachine.from_dict(resp.json or {}, ssh_user=self.ssh_user)
        requested = description if description is not None else (
            (body or {}).get("description") if body is not None else None)
        if requested:
            self.update_virtual_machine(vm.deployment_id, description=requested, team=team)
            vm.description = requested
        return vm

    def update_virtual_machine(
        self, deployment_id: str, description: Optional[str] = None,
        team: Optional[str] = None,
    ) -> None:
        """PATCH /teams/{team}/virtual_machines/{vm}/ — update a VM's description."""
        self.patch(self._team_path(team, "/virtual_machines/%s/" % _ident(deployment_id)),
                 json_body={"description": description})

    def delete_virtual_machine(
        self, deployment_id: str, team: Optional[str] = None, force: bool = False
    ) -> None:
        """DELETE /teams/{team}/virtual_machines/{vm}/ — ``{vm}`` is the deployment_id."""
        self.delete(
            self._team_path(team, "/virtual_machines/%s/" % _ident(deployment_id)),
            params={"force": force} if force else None,
        )

    def vm_action(
        self, deployment_id: str, action: str, team: Optional[str] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """One of start|stop|shutdown|reboot|hard-reset|rebuild|console."""
        path = self._team_path(team, "/virtual_machines/%s/%s/" % (_ident(deployment_id), action))
        resp = self.post(path, json_body=body) if body else self.post(path)
        return resp.json

    # ------------------------------------------------------------- bare metal

    def list_bare_metal(self, team: Optional[str] = None) -> List[models.BareMetalServer]:
        """GET /teams/{team}/bare_metal/ — servers currently reserved by the team."""
        data = self.get(self._team_path(team, "/bare_metal/")).json or []
        return [models.BareMetalServer.from_dict(s, ssh_user=self.ssh_user) for s in data]

    def list_available_bare_metal(self, team: Optional[str] = None) -> List[models.AvailableType]:
        """GET /teams/{team}/bare_metal/available/ — reservable server types + pricing."""
        data = self.get(self._team_path(team, "/bare_metal/available/")).json or []
        return [models.AvailableType.from_dict(a) for a in data]

    def get_bare_metal(
        self, deployment_id: str, team: Optional[str] = None
    ) -> models.BareMetalServer:
        path = self._team_path(team, "/bare_metal/%s/" % _ident(deployment_id))
        return models.BareMetalServer.from_dict(self.get(path).json or {}, ssh_user=self.ssh_user)

    def create_bare_metal(
        self,
        cpu_cores: Optional[int] = None,
        ram_capacity: Optional[int] = None,
        disk_capacity: Optional[int] = None,
        description: Optional[str] = None,
        gpus: Optional[List[Dict[str, Any]]] = None,
        force: bool = False,
        team: Optional[str] = None,
        specs: Optional[Dict[str, Any]] = None,
        body: Optional[Dict[str, Any]] = None,
    ) -> models.BareMetalServer:
        """POST /teams/{team}/bare_metal/ — reserve a bare metal server.

        Returns 201 with the reservation on success. ``specs`` is required by the API.
        """
        payload = body if body is not None else {
            "specs": specs if specs is not None else _specs_body(
                cpu_cores=cpu_cores, ram_capacity=ram_capacity,
                disk_capacity=disk_capacity, gpus=gpus,
            )
        }
        if description is not None:
            payload.setdefault("description", description)
        resp = self.post(self._team_path(team, "/bare_metal/"),
                         json_body=payload, params={"force": force} if force else None)
        return models.BareMetalServer.from_dict(resp.json or {}, ssh_user=self.ssh_user)

    def delete_bare_metal(
        self, deployment_id: str, team: Optional[str] = None, force: bool = False
    ) -> None:
        """DELETE /teams/{team}/bare_metal/{server}/ — release the reservation.

        The API rejects release before the minimum reservation window has elapsed.
        """
        self.delete(
            self._team_path(team, "/bare_metal/%s/" % _ident(deployment_id)),
            params={"force": force} if force else None,
        )

    def bare_metal_action(
        self, deployment_id: str, action: str, team: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Power/other action: power/... | reinstall | console | support_access_enable."""
        path = self._team_path(team, "/bare_metal/%s/%s/" % (_ident(deployment_id), action))
        return self.post(path).json

    def get_bare_metal_power(
        self, deployment_id: str, team: Optional[str] = None
    ) -> str:
        path = self._team_path(team, "/bare_metal/%s/power/" % _ident(deployment_id))
        return (self.get(path).json or {}).get("state", "Unknown")

    # ------------------------------------------------------------- SSH keys

    def list_ssh_keys(self) -> List[Dict[str, Any]]:
        return self.get("/user/ssh_keys/").json or []

    def add_ssh_key(self, key: str, name: Optional[str] = None) -> Dict[str, Any]:
        body = {"key": key}
        if name:
            body["name"] = name
        return self.post("/user/ssh_keys/", json_body=body).json or {}

    # ------------------------------------------------------------------ misc

    def raw(self, method: str, path: str, params: Optional[Dict[str, Any]] = None,
            json_body: Optional[Any] = None) -> Any:
        """Escape hatch for endpoints this module does not wrap yet."""
        return self.request(method, path, params=params, json_body=json_body).json


# ------------------------------------------------------------------- helpers


def _encode_bool(value: bool) -> str:
    return "true" if value else "false"


def _ident(value: Any) -> str:
    """URL-encode a deployment_id / name path segment."""
    return urllib.parse.quote(str(value), safe="")


def _parse_retry_after(headers: Dict[str, str]) -> Optional[float]:
    raw = headers.get("retry-after")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _set_socket_timeout(resp, timeout: float) -> None:
    """Apply ``timeout`` to the response's underlying socket so a stalled body
    read cannot block forever.

    ``urllib`` bounds the connect and the initial read via the ``timeout`` passed
    to ``open()``, but the body ``.read()`` that follows is only bounded by the
    socket's own timeout. ``http.client.HTTPResponse`` exposes the socket at
    ``.raw`` (and under ``.fp.raw`` in some layering), so try both
    defensively and never raise -- if we cannot reach the socket we simply fall
    through to the overall deadline in :func:`_read_body_limited`.
    """
    candidates = []
    raw = getattr(resp, "raw", None)
    if raw is not None:
        candidates.append(raw)
    fp = getattr(resp, "fp", None)
    if fp is not None:
        candidates.append(getattr(fp, "raw", None))
    for sock in candidates:
        if sock is None:
            continue
        sock = getattr(sock, "_sock", None) or sock
        try:
            sock.settimeout(timeout)
            return
        except (AttributeError, OSError, ValueError):
            continue


def _read_body_limited(resp, url: str, timeout: float) -> bytes:
    """Read a response body, bounding every read *and* the total time.

    A server that stalls or trickles the body after the headers have arrived can
    otherwise hold the process open indefinitely. Each chunk read is capped at the
    remaining time budget, and an overall deadline is enforced as a backstop, so a
    stuck body becomes a clear :class:`~hotaisle.errors.APIError` instead of a
    hang.
    """
    deadline = time.monotonic() + timeout
    chunks = []
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise APIError(
                "Timed out reading the response body from %s after %ss"
                % (url, timeout), status_code=0, path=url)
        _set_socket_timeout(resp, max(0.05, min(timeout, remaining)))
        try:
            chunk = resp.read(65536)
        except (socket.timeout, TimeoutError) as exc:
            raise APIError(
                "Timed out reading the response body from %s after %ss (%s)"
                % (url, timeout, exc), status_code=0, path=url) from exc
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def _raise_connection(url: str, reason: Any):
    raise APIError(
        "Could not reach %s: %s" % (url, reason), status_code=0, method="", path=url
    )


def _specs_body(
    cpu_cores: Optional[int] = None,
    ram_capacity: Optional[int] = None,
    disk_capacity: Optional[int] = None,
    gpus: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    missing = [n for n, v in (("cpu_cores", cpu_cores), ("ram_capacity", ram_capacity),
                              ("disk_capacity", disk_capacity)) if v is None]
    if missing:
        raise ConfigurationError(
            "Missing required spec field(s): %s. The API requires cpu_cores, "
            "ram_capacity and disk_capacity (bytes). Copy them from the "
            "'available' listing."
            % ", ".join(missing)
        )
    specs: Dict[str, Any] = {
        "cpu_cores": int(cpu_cores),
        "ram_capacity": int(ram_capacity),
        "disk_capacity": int(disk_capacity),
    }
    if gpus:
        specs["gpus"] = gpus
    return specs


def _vm_body(**kw: Any) -> Dict[str, Any]:
    body = _specs_body(
        cpu_cores=kw.get("cpu_cores"),
        ram_capacity=kw.get("ram_capacity"),
        disk_capacity=kw.get("disk_capacity"),
        gpus=kw.get("gpus"),
    )
    if kw.get("user_data_url"):
        body["user_data_url"] = kw["user_data_url"]
    return body


def specs_to_selector(avail: "models.AvailableType") -> Dict[str, Any]:
    """Build the minimal identifying specs the server matches on."""
    s = avail.specs
    specs: Dict[str, Any] = {
        "cpu_cores": s.cpu_cores,
        "ram_capacity": s.ram_capacity,
        "disk_capacity": s.disk_capacity,
    }
    if s.gpus:
        specs["gpus"] = [{"count": g.count, "model": g.model,
                         **({"manufacturer": g.manufacturer} if g.manufacturer else {})}
                        for g in s.gpus]
    return specs
