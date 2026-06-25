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

**Operation deadline.** ``msl-loadlib``'s ``request32`` has no timeout, and a COM
call can block indefinitely; with all access serialized through one lock, a single
wedged call would otherwise freeze every command. Each operation therefore runs
under ``PlcConfig.op_timeout`` on a worker thread. If it overruns, the bridge
subprocess is force-killed (which unblocks the wedged worker) and rebuilt on the
next call, and the operation raises :class:`KvComError`. Bridge *startup* is bound
separately by ``PlcConfig.startup_timeout`` (msl-loadlib's own connection timeout).
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
                timeout=cfg.startup_timeout,
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


def _destroy_bridge_locked() -> None:
    """Forcibly tear down the current bridge subprocess. Caller holds ``_lock``.

    Kills the subprocess directly first: a bridge that timed out is typically
    wedged inside a COM call, so its RPC channel is unresponsive and a graceful
    ``shutdown_server32`` could itself block. Killing the process unblocks any
    worker still waiting on the dead request. ``shutdown_server32`` is then called
    best-effort to release msl-loadlib's connection/temp files.
    """
    global _bridge, _bridge_key
    bridge = _bridge
    _bridge = None
    _bridge_key = None
    if bridge is None:
        return
    proc = getattr(bridge, "_proc", None)  # private in msl-loadlib, guarded
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    try:
        bridge.shutdown_server32(kill_timeout=2)
    except Exception:
        pass


def _invoke(bridge: Any, method: str, args: tuple, timeout: float) -> Any:
    """Call ``bridge.<method>(*args)`` under ``timeout`` seconds. Caller holds ``_lock``.

    Runs the RPC on a daemon worker thread because ``request32`` cannot be
    interrupted. On overrun the bridge is destroyed (freeing the wedged worker)
    and :class:`KvComError` is raised; otherwise the worker's result or exception
    is propagated.
    """
    box: dict = {}

    def _run() -> None:
        try:
            box["value"] = getattr(bridge, method)(*args)
        except BaseException as e:  # propagate whatever the RPC/COM raised
            box["error"] = e

    worker = threading.Thread(target=_run, name=f"kvcom-{method}", daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        _destroy_bridge_locked()
        raise KvComError(
            f"KV COM+ operation {method!r} timed out after {timeout:g}s "
            "(no response from the bridge); the bridge was killed and will be "
            "rebuilt on the next command."
        )
    if "error" in box:
        raise box["error"]
    return box["value"]


def connect(cfg: PlcConfig) -> dict:
    """Open and hold the KV COM+ connection (idempotent).

    Intended to be called once at server startup to pay the connection cost up
    front. Returns a dict with ``active`` and ``mode``. Subsequent operations
    reuse this held connection.
    """
    with _lock:
        bridge = _bridge_for(cfg)
        try:
            return _invoke(bridge, "connect", (), cfg.op_timeout)
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
            return int(
                _invoke(
                    bridge, "read_device",
                    (int(device_type), str(device_number)), cfg.op_timeout,
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
            _invoke(
                bridge, "write_device",
                (int(device_type), str(device_number), int(value)), cfg.op_timeout,
            )
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
            return bool(_invoke(bridge, "get_mode", (), cfg.op_timeout))
        except KvComError:
            raise
        except Exception as e:
            raise KvComError(f"GetMode failed: {e}")


# Short cap on the graceful disconnect during shutdown; the subprocess is killed
# afterwards regardless, so there is no reason to wait long.
_DISCONNECT_TIMEOUT = 5.0


def shutdown() -> None:
    """Disconnect and stop the 32-bit bridge subprocess. Safe to call repeatedly."""
    with _lock:
        bridge = _bridge
        if bridge is None:
            return
        # Best-effort graceful disconnect, but bounded: if it wedges, _invoke
        # kills the bridge itself. Either way _destroy_bridge_locked then ensures
        # the subprocess is gone and the globals are cleared.
        try:
            _invoke(bridge, "disconnect", (), _DISCONNECT_TIMEOUT)
        except Exception:
            pass
        _destroy_bridge_locked()
