"""KV COM+ device access, bridged from 64-bit to a 32-bit subprocess.

The KV COM+ Library ``DBCommManager`` is a **32-bit** in-process COM component
and cannot be loaded into a 64-bit process, while the SiLA stack (grpcio, ...) is
effectively 64-bit-only on Windows. To run both, the COM object lives in a 32-bit
helper process and this module reaches it over RPC using `msl-loadlib
<https://msl-loadlib.readthedocs.io/>`_ (``Client64`` here, ``Server32`` in
:mod:`kvcomplus_sila2.kvcomplus_server32`). See ``external/kv-scripts/64bit化方針.md``
for the rationale.

The public surface keeps the original signatures: :func:`read_device`,
:func:`write_device`, :func:`get_mode`, and the :class:`KvComError` exception, so
the feature implementations and ``config.py`` need no changes.

**Persistent-connection variant (experiment/persistent-com).** Earlier the 32-bit
side opened and closed the COM connection on *every* operation; measurements
showed that connect/disconnect dominated latency (~2.3 s/op). Here the connection
is opened once and held for the lifetime of the bridge process, so each operation
is just a single ReadDevice/WriteDevice over the open session plus one RPC hop.
:func:`connect` opens it eagerly (called at server startup); operations also
connect lazily and transparently reconnect once if the held connection drops.

A single ``Client64`` (and thus one 32-bit subprocess holding one COM connection)
is created lazily and reused for the server's lifetime. Call :func:`shutdown` when
the SiLA server stops; it disconnects and stops the subprocess.
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from .config import PlcConfig

# Path to the 32-bit server module, executed inside msl-loadlib's bundled 32-bit
# interpreter. Passing the absolute file path avoids relying on the 32-bit
# server's sys.path resolution.
_SERVER32_PATH = os.path.join(os.path.dirname(__file__), "kvcomplus_server32.py")


class KvComError(Exception):
    """Raised when a KV COM+ connection or device operation fails.

    Wraps the underlying bridge/COM failure (msl-loadlib ``Server32Error``,
    comtypes errors, or an unavailable bridge) so feature implementations can
    catch a single, library-agnostic exception type.
    """


def _make_bridge(cfg: PlcConfig) -> Any:
    """Start the 32-bit bridge subprocess and return a connected client.

    The connection target (``progid``, ``plc_id``, ``peer``) is forwarded once to
    ``KvComServer32`` as constructor kwargs and held there for the bridge's
    lifetime, so the per-operation methods carry only the device type/number.

    Imports ``msl-loadlib`` lazily and defines the ``Client64`` subclass here so
    the package can still be imported where msl-loadlib is absent (e.g. a
    non-Windows dev machine); the dependency is only required to actually talk to
    the PLC.
    """
    try:
        from msl.loadlib import Client64
    except ImportError as e:  # pragma: no cover - msl-loadlib not installed
        raise KvComError(
            "msl-loadlib is required to bridge to the 32-bit KV COM+ component: "
            + str(e)
        )

    class _Bridge(Client64):
        def __init__(self) -> None:
            # Connection params are forwarded to KvComServer32.__init__ as kwargs
            # (they arrive as strings on the 32-bit side) and bound there once.
            super().__init__(
                module32=_SERVER32_PATH,
                progid=cfg.progid,
                plc_id=str(cfg.plc_id),
                peer=cfg.peer,
            )

        def connect(self):
            return self.request32("connect")

        def disconnect(self):
            return self.request32("disconnect")

        def read_device(self, device_type, device_number):
            return self.request32("read_device", device_type, device_number)

        def write_device(self, device_type, device_number, value):
            return self.request32("write_device", device_type, device_number, value)

        def get_mode(self):
            return self.request32("get_mode")

    try:
        return _Bridge()
    except Exception as e:  # pragma: no cover - bundled 32-bit server failed to start
        raise KvComError(
            "Failed to start the 32-bit KV COM+ bridge server "
            f"(progid={cfg.progid!r}): {e!r}"
        )


# A single bridge process serves all commands; serialize access because the
# 32-bit RPC server handles one request at a time and the gRPC server may call in
# from multiple threads.
_lock = threading.Lock()
_bridge: Optional[Any] = None
# The (progid, plc_id, peer) the live bridge was bound to. The bridge holds one
# persistent connection to one PLC, so a config naming a different target cannot
# reuse it — surfaced as an error rather than silently rebinding the connection.
_bridge_key: Optional[tuple] = None


def _key_for(cfg: PlcConfig) -> tuple:
    return (cfg.progid, int(cfg.plc_id), cfg.peer)


def _bridge_for(cfg: PlcConfig) -> Any:
    """Return the shared bridge, creating it on first use. Caller holds ``_lock``."""
    global _bridge, _bridge_key
    key = _key_for(cfg)
    if _bridge is None:
        _bridge = _make_bridge(cfg)
        _bridge_key = key
    elif key != _bridge_key:
        raise KvComError(
            f"KV COM+ bridge already started for {_bridge_key!r}; cannot switch to "
            f"{key!r} (progid, plc_id, peer) without restarting the server."
        )
    return _bridge


def connect(cfg: PlcConfig) -> dict:
    """Open and hold the KV COM+ connection (idempotent).

    Intended to be called once at server startup to pay the connection cost up
    front. Returns a dict with ``active`` and ``mode``. Subsequent operations
    reuse this held connection.
    """
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return bridge.connect()
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(f"Failed to connect to PLC at {cfg.peer!r}: {e}")


def read_device(cfg: PlcConfig, device_type: int, device_number: str) -> int:
    """Read one device word via ReadDevice and return it as an int.

    ``device_number`` is passed to the library as a string, as KV COM+ requires.
    """
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return int(bridge.read_device(int(device_type), str(device_number)))
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(
                f"ReadDevice(type={device_type}, number={device_number!r}) failed: {e}"
            )


def write_device(cfg: PlcConfig, device_type: int, device_number: str, value: int) -> None:
    """Write one device word via WriteDevice."""
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            bridge.write_device(int(device_type), str(device_number), int(value))
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(
                f"WriteDevice(type={device_type}, number={device_number!r}, "
                f"value={value}) failed: {e}"
            )


def get_mode(cfg: PlcConfig) -> bool:
    """Return the PLC operating mode (True = RUN, False = PROGRAM)."""
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return bool(bridge.get_mode())
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(f"GetMode failed: {e}")


def shutdown() -> None:
    """Disconnect and stop the 32-bit bridge subprocess. Safe to call repeatedly."""
    global _bridge, _bridge_key
    with _lock:
        if _bridge is not None:
            try:
                _bridge.disconnect()
            except Exception:
                pass
            try:
                _bridge.shutdown_server32()
            except Exception:
                pass
            _bridge = None
            _bridge_key = None
