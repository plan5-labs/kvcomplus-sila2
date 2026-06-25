"""32-bit KV COM+ bridge server (msl-loadlib ``Server32``) — persistent connection.

The KV COM+ Library ``DBCommManager`` is a **32-bit** in-process COM component
and cannot be loaded into a 64-bit process, while the SiLA stack (grpcio, ...) is
effectively 64-bit-only on Windows. This module runs inside the 32-bit Python
interpreter that ``msl-loadlib`` ships (``server32-windows.exe``) and holds the
COM object there; the 64-bit :mod:`kvcomplus_sila2.kvcomplus` talks to it over RPC.

``msl-loadlib``'s COM support (``libtype='com'``) is implemented with **comtypes**
(bundled in the 32-bit server), *not* pywin32.

**Persistent-connection variant (experiment/persistent-com).** Unlike the
stateless server (connect -> operate -> disconnect per call), this one opens the
KV COM+ connection once and *holds it* for the lifetime of the bridge process.
Measurements showed the per-call COM connect/disconnect to the PLC dominates
latency (~2.3 s/op), so reusing one open session is expected to cut per-operation
time to tens of milliseconds. This is safe here because all COM access happens on
this single 32-bit process/thread and the 64-bit side serializes calls with a
lock, so the original "gRPC threadpool vs COM thread-affinity" objection to a
stateful connection no longer applies.

If a held connection drops (network blip, PLC restart, session timeout), the
operation methods reconnect once and retry, preserving stateless-like robustness.

**Connection parameters are bound once.** A persistent connection targets exactly
one PLC, so ``plc_id`` and ``peer`` are supplied once (as ``Server32`` constructor
kwargs, forwarded from ``Client64`` at bridge creation) and held here. The device
operations therefore take only the device type/number — they never carry, and can
never silently rebind, the connection target. The 64-bit side enforces a matching
``(progid, plc_id, peer)`` before reusing a bridge.

This file is executed by ``msl-loadlib`` inside the 32-bit interpreter; it is not
run directly and is never imported by the 64-bit process.
"""

import sys

from msl.loadlib import Server32

DEFAULT_PROGID = "DataBuilderAxLB.DBCommManager"


class KvComServer32(Server32):
    """Hold a persistent ``DBCommManager`` connection and serve device operations."""

    def __init__(self, host, port, **kwargs):
        # Drop the 64-bit site-packages from sys.path so the 32-bit interpreter
        # cannot accidentally import 64-bit extension modules (e.g. numpy), which
        # can crash comtypes. Restored afterwards in case anything else needs it.
        site = Server32.remove_site_packages_64bit()
        # kwargs arrive as strings from Client64; progid is the COM ProgID.
        progid = kwargs.get("progid", DEFAULT_PROGID)
        # libtype='com' makes Server32 create the COM object via comtypes and
        # store it as ``self.lib``.
        super(KvComServer32, self).__init__(progid, "com", host, port)
        if site:
            sys.path.append(site)
        # Connection target, bound once at bridge creation. A persistent
        # connection addresses exactly one PLC, so these are held here rather
        # than passed per operation. plc_id arrives as a string from Client64.
        self._plc_id = int(kwargs["plc_id"]) if kwargs.get("plc_id") is not None else None
        self._peer = kwargs.get("peer")
        self._connected = False

    # ---- connection lifecycle ----

    def _open(self):
        """Open the held KV COM+ connection (raises ``RuntimeError`` if not active)."""
        if self._plc_id is None or not self._peer:
            raise RuntimeError(
                "KV COM+ bridge was started without plc_id/peer; cannot connect."
            )
        self.lib.PLC = int(self._plc_id)
        self.lib.Peer = str(self._peer)
        self.lib.Connect()
        if not bool(self.lib.Active):
            raise RuntimeError(
                "Connect to %r returned but the connection is not active." % (self._peer,)
            )
        self._connected = True

    def connect(self):
        """Open and hold the connection (idempotent). Returns active flag + mode."""
        if not self._connected:
            self._open()
        return {
            "active": self._connected,
            "mode": "RUN" if bool(self.lib.GetMode) else "PROGRAM",
        }

    def disconnect(self):
        """Close the held connection if open. Safe to call repeatedly."""
        if self._connected:
            try:
                self.lib.Disconnect()
            except Exception:
                pass
            self._connected = False
        return True

    def _run(self, fn):
        """Run ``fn`` on the open connection; reconnect once and retry on failure.

        The held connection can silently drop, so a single op failure is treated
        as a possibly-dead connection: reconnect and retry once before giving up.
        """
        if not self._connected:
            self._open()
        try:
            return fn()
        except Exception:
            self._connected = False
            try:
                self.lib.Disconnect()
            except Exception:
                pass
            self._open()  # if this fails, the error propagates
            return fn()

    # ---- device operations (on the held connection) ----

    def read_device(self, device_type, device_number):
        """Read one device word and return it as an int."""
        return self._run(
            lambda: int(self.lib.ReadDevice(int(device_type), str(device_number))),
        )

    def write_device(self, device_type, device_number, value):
        """Write one device word. Returns None."""
        def _w():
            self.lib.WriteDevice(int(device_type), str(device_number), int(value))
        return self._run(_w)

    def get_mode(self):
        """Return the PLC operating mode (True = RUN, False = PROGRAM)."""
        return self._run(lambda: bool(self.lib.GetMode))
