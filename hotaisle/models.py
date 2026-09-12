"""Lightweight dataclasses over the Hot Aisle JSON payloads.

Parsing is deliberately tolerant: unknown fields are kept in ``raw`` rather than
raising, so the module keeps working when the API adds fields.

One quirk worth knowing: the ``/available/`` endpoints serialise their wrapper
keys in PascalCase (``Quantity``, ``Specs``, ``OnDemandPrice``,
``MinimumReservationMinutes``) while nested specs stay snake_case. :func:`pick`
handles both spellings everywhere.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

GIB = 1024**3
MIB = 1024**2


def pick(data: Dict[str, Any], *names: str, default: Any = None) -> Any:
    """Return the first present key from ``names`` (case-insensitively)."""
    if not isinstance(data, dict):
        return default
    lowered = {str(k).lower(): v for k, v in data.items()}
    for name in names:
        key = name.lower()
        if key in lowered and lowered[key] is not None:
            return lowered[key]
    return default


def _as_list(value: Any) -> List[Any]:
    """Coerce a single dict into a one-element list, leaving lists unchanged."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def as_bytes(value: Any) -> Optional[int]:
    """Coerce a byte count that may have arrived as an int or a string."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def human_bytes(n: Optional[int]) -> str:
    """Render a byte count as e.g. ``128 GiB`` / ``1.5 TiB``."""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(n) < 1024 or unit == "PiB":
            text = (
                "%.0f %s" % (n, unit)
                if unit == "B" or n >= 10
                else "%.1f %s" % (n, unit)
            )
            return text.rstrip(".0").rstrip()
        n /= 1024.0
    return "%s B" % n


def cents_to_usd(cents: Optional[int]) -> str:
    """``350`` -> ``$3.50``."""
    if cents is None:
        return "-"
    return "$%.2f" % (int(cents) / 100.0)


class _Model:
    """Base class that keeps the untouched server payload around."""

    def to_dict(self) -> Dict[str, Any]:
        return self.raw  # type: ignore[attr-defined]

    def __repr__(self) -> str:
        bits = []
        for key, value in sorted(self.__dict__.items()):
            if key in ("raw",) or value in (None, [], {}, ""):
                continue
            bits.append("%s=%r" % (key, value))
        return "%s(%s)" % (type(self).__name__, ", ".join(bits))


class Component(_Model):
    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        count: Any = None,
        manufacturer: Optional[str] = None,
        model: Optional[str] = None,
        **extra: Any,
    ):
        self.raw = raw or {}
        self.count = as_bytes(pick(self.raw, "count", default=count))
        self.manufacturer = pick(self.raw, "manufacturer", default=manufacturer)
        self.model = pick(self.raw, "model", default=model)
        self.cores = as_bytes(pick(self.raw, "cores"))
        self.frequency = as_bytes(pick(self.raw, "frequency"))
        self.capacity = as_bytes(pick(self.raw, "capacity"))
        self.type = pick(self.raw, "type")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Component":
        return cls(raw=data)

    @property
    def label(self) -> str:
        who = " ".join(p for p in (self.manufacturer, self.model) if p) or "component"
        bits = []
        if self.count:
            bits.append("%dx" % self.count)
        bits.append(who)
        if self.cores:
            bits.append("(%s cores" % self.cores)
            if self.frequency:
                bits[-1] += ", %g GHz" % (self.frequency / 1e9)
            bits[-1] += ")"
        if self.capacity:
            bits.append("%s" % human_bytes(self.capacity))
        if self.type:
            bits.append(str(self.type))
        return " ".join(bits)


class GPU(Component):
    @property
    def label(self) -> str:
        who = " ".join(p for p in (self.manufacturer, self.model) if p) or "GPU"
        return "%dx %s" % (self.count or 1, who)


class Specs(_Model):
    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        cpu_cores: Any = None,
        ram_capacity: Any = None,
        disk_capacity: Any = None,
        **extra: Any,
    ):
        self.raw = raw or {}
        self.cpu_cores = as_bytes(
            pick(self.raw, "cpu_cores", "CPUCores", "cpuCores", default=cpu_cores)
        )
        self.ram_capacity = as_bytes(
            pick(
                self.raw,
                "ram_capacity",
                "RAMCapacity",
                "ramCapacity",
                default=ram_capacity,
            )
        )
        self.disk_capacity = as_bytes(
            pick(
                self.raw,
                "disk_capacity",
                "DiskCapacity",
                "diskCapacity",
                default=disk_capacity,
            )
        )
        self.cpus = [
            Component.from_dict(c) for c in _as_list(pick(self.raw, "cpus", "CPUs"))
        ]
        self.memory_modules = [
            Component.from_dict(m)
            for m in _as_list(pick(self.raw, "memory_modules", "MemoryModules"))
        ]
        self.disks = [
            Component.from_dict(d) for d in _as_list(pick(self.raw, "disks", "Disks"))
        ]
        self.gpus = [GPU.from_dict(g) for g in _as_list(pick(self.raw, "gpus", "GPUs"))]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Specs":
        return cls(raw=data)

    @property
    def ram_gib(self) -> Optional[float]:
        return None if self.ram_capacity is None else self.ram_capacity / GIB

    @property
    def disk_gib(self) -> Optional[float]:
        return None if self.disk_capacity is None else self.disk_capacity / GIB

    @property
    def gpu_count(self) -> int:
        return sum(g.count or 0 for g in self.gpus)

    @property
    def gpu_summary(self) -> str:
        if not self.gpus:
            return "-"
        return "; ".join(g.label for g in self.gpus)

    @property
    def label(self) -> str:
        """Short one-line shape, e.g. ``64c / 256 GiB / 3.8 TiB``."""
        return "%s vCPU / %s / %s" % (
            self.cpu_cores if self.cpu_cores is not None else "?",
            human_bytes(self.ram_capacity),
            human_bytes(self.disk_capacity),
        )

    @property
    def full_label(self) -> str:
        text = self.label
        if self.gpus:
            text += " / %s" % self.gpu_summary
        return text


DEFAULT_SSH_USER = "hotaisle"


class ExternalService(_Model):
    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        ssh_user: Optional[str] = None,
        **extra: Any,
    ):
        self.raw = raw or {}
        self.ip_address = pick(self.raw, "ip_address", "IPAddress")
        self.port = as_bytes(pick(self.raw, "port", "Port"))
        self.dns_name = pick(self.raw, "dns_name", "DNSName", "DnsName")
        self.ssh_user = ssh_user or DEFAULT_SSH_USER

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], ssh_user: Optional[str] = None
    ) -> "ExternalService":
        return cls(raw=data, ssh_user=ssh_user)

    @property
    def ssh_target(self) -> str:
        host = self.dns_name or self.ip_address
        if not host:
            return "-"
        target = "%s@%s" % (self.ssh_user, host)
        return "%s:%s" % (target, self.port) if self.port else target

    @property
    def ssh_command(self) -> Optional[str]:
        host = self.dns_name or self.ip_address
        if not host:
            return None
        if self.port and int(self.port) != 22:
            return "ssh -p %s %s@%s" % (self.port, self.ssh_user, host)
        return "ssh %s@%s" % (self.ssh_user, host)


class VirtualMachine(_Model):
    """A VM assigned to a team (VirtualMachineDetails = VirtualMachine + specs)."""

    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        ssh_user: Optional[str] = None,
        **extra: Any,
    ):
        self.raw = raw or {}
        self.deployment_id = pick(self.raw, "deployment_id", "DeploymentID")
        self.name = pick(self.raw, "name", "Name")
        self.description = pick(self.raw, "description", "Description")
        self.ip_address = pick(self.raw, "ip_address", "IPAddress")
        self.ssh_user = ssh_user or DEFAULT_SSH_USER
        ssh = pick(self.raw, "ssh_access", "SSHAccess")
        self.ssh_access = (
            ExternalService.from_dict(ssh, ssh_user=self.ssh_user) if ssh else None
        )
        # Specs arrive flattened into the same object for VMs.
        self.specs = Specs(raw=self.raw)

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], ssh_user: Optional[str] = None
    ) -> "VirtualMachine":
        return cls(raw=data, ssh_user=ssh_user)

    @property
    def ssh_target(self) -> str:
        if self.ssh_access:
            return self.ssh_access.ssh_target
        return self.ip_address or "-"

    @property
    def ssh_command(self) -> Optional[str]:
        if self.ssh_access:
            return self.ssh_access.ssh_command
        return (
            "ssh %s@%s" % (self.ssh_user, self.ip_address) if self.ip_address else None
        )

    @property
    def id_or_name(self) -> str:
        return str(self.deployment_id or self.name or "?")


class BareMetalServer(_Model):
    """A reserved bare metal server (BareMetalServerDetails = server + specs)."""

    def __init__(
        self,
        raw: Optional[Dict[str, Any]] = None,
        ssh_user: Optional[str] = None,
        **extra: Any,
    ):
        self.raw = raw or {}
        self.deployment_id = pick(self.raw, "deployment_id", "DeploymentID")
        self.name = pick(self.raw, "name", "Name")
        self.description = pick(self.raw, "description", "Description")
        self.ip_address = pick(self.raw, "ip_address", "IPAddress")
        self.manufacturer = pick(self.raw, "manufacturer", "Manufacturer")
        self.model = pick(self.raw, "model", "Model")
        self.support_access_enabled = pick(
            self.raw, "support_access_enabled", "SupportAccessEnabled", default=False
        )
        self.ssh_user = ssh_user or DEFAULT_SSH_USER
        ssh = pick(self.raw, "ssh_access", "SSHAccess")
        self.ssh_access = (
            ExternalService.from_dict(ssh, ssh_user=self.ssh_user) if ssh else None
        )
        nested = pick(self.raw, "specs", "Specs")
        self.specs = (
            Specs.from_dict(nested) if isinstance(nested, dict) else Specs(raw=self.raw)
        )
        self.os_status = pick(self.raw, "os_status", "OSStatus", "OsStatus")

    @classmethod
    def from_dict(
        cls, data: Dict[str, Any], ssh_user: Optional[str] = None
    ) -> "BareMetalServer":
        return cls(raw=data, ssh_user=ssh_user)

    @property
    def hardware(self) -> str:
        vendor_model = " ".join(p for p in (self.manufacturer, self.model) if p)
        return vendor_model or "-"

    @property
    def ssh_command(self) -> Optional[str]:
        if self.ssh_access:
            return self.ssh_access.ssh_command
        return (
            "ssh %s@%s" % (self.ssh_user, self.ip_address) if self.ip_address else None
        )

    @property
    def id_or_name(self) -> str:
        return str(self.deployment_id or self.name or "?")


class AvailableType(_Model):
    """One row of an ``/available/`` listing: a shape, how many, and the price."""

    def __init__(self, raw: Optional[Dict[str, Any]] = None, **extra: Any):
        self.raw = raw or {}
        self.quantity = as_bytes(pick(self.raw, "Quantity", "quantity"))
        self.minimum_reservation_minutes = as_bytes(
            pick(self.raw, "MinimumReservationMinutes", "minimum_reservation_minutes")
        )
        self.on_demand_price = as_bytes(
            pick(self.raw, "OnDemandPrice", "on_demand_price")
        )
        specs = pick(self.raw, "Specs", "specs")
        # Some responses may already be flattened; fall back to the row itself.
        self.specs = (
            Specs.from_dict(specs) if isinstance(specs, dict) else Specs(raw=self.raw)
        )
        self.label_override = pick(self.raw, "name", "Name", "type", "Type", "id", "ID")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AvailableType":
        return cls(raw=data)

    @property
    def price_per_hour(self) -> str:
        return (
            "%s/hr" % cents_to_usd(self.on_demand_price)
            if self.on_demand_price is not None
            else "-"
        )

    @property
    def minimum_reservation(self) -> str:
        mins = self.minimum_reservation_minutes
        if mins is None:
            return "-"
        if mins < 60:
            return "%dm" % mins
        hours, rest = divmod(int(mins), 60)
        return "%dh%02dm" % (hours, rest) if rest else "%dh" % hours

    @property
    def label(self) -> str:
        return str(self.label_override or self.specs.full_label)


class Team(_Model):
    def __init__(self, raw: Optional[Dict[str, Any]] = None, **extra: Any):
        self.raw = raw or {}
        self.handle = pick(self.raw, "handle", "Handle")
        self.name = pick(self.raw, "name", "Name")
        self.description = pick(self.raw, "description", "Description")
        self.roles = pick(self.raw, "roles", "Roles") or []
        self.effective_roles = pick(self.raw, "effective_roles", "EffectiveRoles") or []
        self.maximum_virtual_machines = as_bytes(
            pick(self.raw, "maximum_virtual_machines", "MaximumVirtualMachines")
        )
        self.maximum_bare_metal_servers = as_bytes(
            pick(self.raw, "maximum_bare_metal_servers", "MaximumBareMetalServers")
        )
        self.invitation = pick(self.raw, "invitation", "Invitation", default=False)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Team":
        return cls(raw=data)


class Balance(_Model):
    def __init__(self, raw: Optional[Dict[str, Any]] = None, **extra: Any):
        self.raw = raw or {}
        self.balance = pick(
            self.raw,
            "balance",
            "Balance",
            "current_balance",
            "CurrentBalance",
            "available_balance",
            "amount",
            "Amount",
        )
        self.formatted_balance = pick(self.raw, "formatted_balance", "FormattedBalance")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Balance":
        return cls(raw=data)

    def __str__(self) -> str:
        if self.formatted_balance:
            return str(self.formatted_balance)
        if self.balance is None:
            return "<unknown>"
        try:
            return cents_to_usd(int(self.balance))
        except (TypeError, ValueError):
            return str(self.balance)


class User(_Model):
    def __init__(self, raw: Optional[Dict[str, Any]] = None, **extra: Any):
        self.raw = raw or {}
        identity = (
            pick(self.raw, "user")
            if isinstance(pick(self.raw, "user"), dict)
            else self.raw
        )
        self.id = pick(identity, "id", "ID", "user_id", "UserID")
        self.email = pick(identity, "email", "Email")
        self.name = pick(identity, "name", "Name")
        self.teams = [
            Team.from_dict(t) for t in (pick(self.raw, "teams", "Teams") or [])
        ]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "User":
        return cls(raw=data)


class VMState(_Model):
    def __init__(self, raw: Optional[Dict[str, Any]] = None, **extra: Any):
        self.raw = raw or {}
        self.state = pick(self.raw, "state", "State")
        self.host = pick(self.raw, "host", "Host")

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "VMState":
        return cls(raw=data)


def parse_size(text: Any) -> Optional[int]:
    """Parse ``16G``, ``512GiB``, ``1.5T``, ``34359738368`` into a byte count.

    Binary (GiB/TiB) semantics are used for bare single-letter suffixes, which is
    what machine sizing means in practice.
    """
    if text is None:
        return None
    s = str(text).strip().upper().replace(" ", "")
    if not s:
        return None
    if s.isdigit():
        return int(s)
    units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}
    for suffix, multiplier in (
        ("KIB", 1024),
        ("MIB", 1024**2),
        ("GIB", 1024**3),
        ("TIB", 1024**4),
        ("PIB", 1024**5),
        ("KB", 1000),
        ("MB", 1000**2),
        ("GB", 1000**3),
        ("TB", 1000**4),
        ("PB", 1000**5),
        ("B", 1),
    ):
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "0"
            return int(float(num) * multiplier)
    for suffix, multiplier in units.items():
        if s.endswith(suffix):
            num = s[: -len(suffix)] or "0"
            return int(float(num) * multiplier)
    raise ValueError(
        "Could not parse size %r (try e.g. 16G, 500G, 1.5T, or raw bytes)" % text
    )
