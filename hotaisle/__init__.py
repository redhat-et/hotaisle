"""Python client + CLI for the Hot Aisle (hotManager) API.

API docs: https://admin.hotaisle.app/api/docs/
Spec:     https://admin.hotaisle.app/api/docs/swagger.json

Authentication
--------------
The API expects ``Authorization: Token <api-key>``. This module looks for the key
in this order (first hit wins):

1. ``Client(api_key="...")`` / CLI ``--api-key``
2. ``$HOTAISLE_API_KEY`` (or ``$HOTAISLE_TOKEN``)
3. ``$HOTAISLE_API_KEY_FILE`` -> path to a file containing just the key
4. ``$HOTAISLE_API_KEY_COMMAND`` (or ``key_command`` in the config file) -> command
   whose stdout is the key, e.g. ``pass show hotaisle`` or ``op read ...``
5. ``api_key`` in ``~/.config/hotaisle/config.toml``
6. OS keyring (optional; needs the ``keyring`` package and ``HOTAISLE_KEYRING=1``)

Quick start
-----------
    from hotaisle import Client

    client = Client(team="acme-corp")
    for vm in client.list_virtual_machines():
        print(vm.name, vm.deployment_id, vm.ip_address)

    # create
    avail = client.list_available_virtual_machines()
    vm = client.create_virtual_machine(
        cpu_cores=avail[0].specs.cpu_cores,
        ram_capacity=avail[0].specs.ram_capacity,
        disk_capacity=avail[0].specs.disk_capacity,
        description="worker-1",
    )
    client.delete_virtual_machine(vm.deployment_id)

Only the standard library is required.
"""

from __future__ import annotations

from .auth import (
    Credential,
    default_config_path,
    load_config,
    mask,
    normalize_key,
    resolve_api_key,
)
from .client import DEFAULT_BASE_URL, Client, specs_to_selector
from .errors import (
    APIError,
    AuthError,
    ConfigurationError,
    HotAisleError,
    InsufficientBalanceError,
    NotFoundError,
    PreconditionFailedError,
    ValidationError,
)
from .models import (
    AvailableType,
    Balance,
    BareMetalServer,
    Component,
    ExternalService,
    GPU,
    Specs,
    Team,
    User,
    VMState,
    VirtualMachine,
    human_bytes,
    parse_size,
)

__version__ = "1.0.0"

__all__ = [
    "Client",
    "DEFAULT_BASE_URL",
    "specs_to_selector",
    "HotAisleError",
    "ConfigurationError",
    "AuthError",
    "APIError",
    "NotFoundError",
    "ValidationError",
    "InsufficientBalanceError",
    "PreconditionFailedError",
    "VirtualMachine",
    "BareMetalServer",
    "AvailableType",
    "Specs",
    "Component",
    "GPU",
    "ExternalService",
    "Team",
    "Balance",
    "User",
    "VMState",
    "human_bytes",
    "parse_size",
    "Credential",
    "resolve_api_key",
    "normalize_key",
    "load_config",
    "mask",
    "default_config_path",
    "__version__",
]
