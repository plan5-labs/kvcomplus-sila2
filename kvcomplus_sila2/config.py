"""Server configuration loaded from a TOML file.

PLC connection parameters (peer, plc_id, progid) are supplied at startup via a
TOML file rather than as SiLA command parameters, so the commands stay free of
connection details. This mirrors the configuration philosophy of bcap-sila2;
only the section name differs ([plc] instead of [controller]).
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class ConfigError(Exception):
    """Raised when the configuration file is missing, invalid, or incomplete."""


# Default KV COM+ DBCommManager COM ProgID (KV-DH1L). Confirmed working on the
# KV-8000 setup; override via [plc].progid for other registrations.
DEFAULT_PROGID = "DataBuilderAxLB.DBCommManager"


@dataclass
class PlcConfig:
    """KV COM+ connection parameters for the PLC.

    The SiLA server is stateless: every command opens a fresh COM connection
    using these parameters, performs the operation, and disconnects.
    """

    peer: str
    plc_id: int
    progid: str = DEFAULT_PROGID

    def __post_init__(self) -> None:
        if not self.peer:
            raise ConfigError("[plc].peer must not be empty.")
        if not self.progid:
            raise ConfigError("[plc].progid must not be empty.")


@dataclass
class ServerConfig:
    """SiLA server listening settings."""

    host: str = "0.0.0.0"
    port: int = 50052


@dataclass
class Config:
    plc: PlcConfig
    server: ServerConfig = field(default_factory=ServerConfig)


def _build(cls: type, data: dict[str, Any], section: str) -> Any:
    """Construct a config dataclass, rejecting unknown keys to catch typos."""
    known = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - known
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in [{section}]: {', '.join(sorted(unknown))}"
        )
    return cls(**data)


def load_config(path: str | Path) -> Config:
    """Load and validate the server configuration from a TOML file."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Configuration file not found: {path}")

    with path.open("rb") as f:
        data: dict[str, Any] = tomllib.load(f)

    plc_data = data.get("plc")
    if not isinstance(plc_data, dict):
        raise ConfigError("Missing required [plc] section in configuration file.")
    for key in ("peer", "plc_id"):
        if key not in plc_data:
            raise ConfigError(f"Missing required [plc].{key} in configuration file.")

    plc = _build(PlcConfig, plc_data, "plc")
    server = _build(ServerConfig, data.get("server", {}), "server")
    return Config(plc=plc, server=server)
