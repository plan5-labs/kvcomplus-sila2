"""KV COM+ device access, bridged from 64-bit to a 32-bit subprocess.

The KV COM+ Library ``DBCommManager`` is a **32-bit** in-process COM component
and cannot be loaded into a 64-bit process, while the SiLA stack (grpcio, ...) is
effectively 64-bit-only on Windows. To run both, the COM object lives in a 32-bit
helper process and this module reaches it over RPC using `msl-loadlib
<https://msl-loadlib.readthedocs.io/>`_ (``Client64`` here, ``Server32`` in
:mod:`kvcomplus_sila2.kvcomplus_server32`). See ``external/kv-scripts/64bit化方針.md``
for the rationale.

The public surface is unchanged from the original in-process implementation:
:func:`read_device`, :func:`write_device`, :func:`get_mode`, and the
:class:`KvComError` exception. Feature implementations, ``server.py`` and
``config.py`` therefore need no changes. Each call still has stateless,
connect-per-operation semantics (the 32-bit side opens and closes the COM
connection for every operation); only the bridge *process* is long-lived.

A single ``Client64`` (and thus one 32-bit subprocess) is created lazily on first
use and reused for the server's lifetime, because starting the 32-bit server
costs a few seconds. Call :func:`shutdown` when the SiLA server stops.
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


def _make_bridge(progid: str) -> Any:
    """Start the 32-bit bridge subprocess and return a connected client.

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
            # progid is forwarded to KvComServer32.__init__ as a kwarg (arrives
            # as a string on the 32-bit side).
            super().__init__(module32=_SERVER32_PATH, progid=progid)

        def read_device_once(self, plc_id, peer, device_type, device_number):
            return self.request32(
                "read_device_once", plc_id, peer, device_type, device_number
            )

        def write_device_once(self, plc_id, peer, device_type, device_number, value):
            return self.request32(
                "write_device_once", plc_id, peer, device_type, device_number, value
            )

        def get_mode_once(self, plc_id, peer):
            return self.request32("get_mode_once", plc_id, peer)

    try:
        return _Bridge()
    except Exception as e:  # pragma: no cover - bundled 32-bit server failed to start
        raise KvComError(
            "Failed to start the 32-bit KV COM+ bridge server "
            f"(progid={progid!r}): {e!r}"
        )


# A single bridge process serves all commands; serialize access because the
# 32-bit RPC server handles one request at a time and the gRPC server may call in
# from multiple threads.
_lock = threading.Lock()
_bridge: Optional[Any] = None
_bridge_progid: Optional[str] = None


def _bridge_for(cfg: PlcConfig) -> Any:
    """Return the shared bridge, creating it on first use. Caller holds ``_lock``."""
    global _bridge, _bridge_progid
    if _bridge is None:
        _bridge = _make_bridge(cfg.progid)
        _bridge_progid = cfg.progid
    elif cfg.progid != _bridge_progid:
        raise KvComError(
            f"KV COM+ bridge already started with progid {_bridge_progid!r}; "
            f"cannot switch to {cfg.progid!r} without restarting the server."
        )
    return _bridge


def read_device(cfg: PlcConfig, device_type: int, device_number: str) -> int:
    """Read one device word via ReadDevice and return it as an int.

    ``device_number`` is passed to the library as a string, as KV COM+ requires.
    """
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return int(
                bridge.read_device_once(
                    cfg.plc_id, cfg.peer, int(device_type), str(device_number)
                )
            )
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
            bridge.write_device_once(
                cfg.plc_id, cfg.peer, int(device_type), str(device_number), int(value)
            )
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(
                f"WriteDevice(type={device_type}, number={device_number!r}, "
                f"value={value}) failed: {e}"
            )


def get_mode(cfg: PlcConfig) -> bool:
    """Connect and return the PLC operating mode (True = RUN, False = PROGRAM)."""
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return bool(bridge.get_mode_once(cfg.plc_id, cfg.peer))
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(f"GetMode failed: {e}")


def shutdown() -> None:
    """Stop the 32-bit bridge subprocess if it is running. Safe to call repeatedly."""
    global _bridge, _bridge_progid
    with _lock:
        if _bridge is not None:
            try:
                _bridge.shutdown_server32()
            except Exception:
                pass
            _bridge = None
            _bridge_progid = None
