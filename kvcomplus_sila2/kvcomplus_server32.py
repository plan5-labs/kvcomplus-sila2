"""32-bit KV COM+ bridge server (msl-loadlib ``Server32``).

The KV COM+ Library ``DBCommManager`` is a **32-bit** in-process COM component
and cannot be loaded into a 64-bit process. The SiLA stack (grpcio, cryptography,
...) is effectively 64-bit-only on Windows, so the two cannot share a process.
This module runs inside the 32-bit Python interpreter that ``msl-loadlib`` ships
(``server32-windows.exe``) and holds the COM object there; the 64-bit
:mod:`kvcomplus_sila2.kvcomplus` talks to it over RPC.

``msl-loadlib``'s COM support (``libtype='com'``) is implemented with **comtypes**
(bundled in the 32-bit server), *not* pywin32. The ``DBCommManager``
property/method names (``PLC`` / ``Peer`` / ``Connect`` / ``Active`` /
``GetMode`` / ``ReadDevice`` / ``WriteDevice`` / ``Disconnect``) are accessed the
same way under comtypes.

This file is executed by ``msl-loadlib`` inside the 32-bit interpreter; it is not
run directly and is never imported by the 64-bit process.

Each exposed method follows the **stateless one-shot** pattern (pattern A in the
64bit化方針 doc): connect -> operate -> disconnect, mirroring the original
``plc_session`` design so the SiLA server stays free of session state. The
``Client64`` (and thus this 32-bit subprocess) is kept alive across commands; only
the COM connection is opened and closed per operation.
"""

import sys
from contextlib import contextmanager

from msl.loadlib import Server32

DEFAULT_PROGID = "DataBuilderAxLB.DBCommManager"


class KvComServer32(Server32):
    """Hold the 32-bit ``DBCommManager`` and expose one-shot device operations."""

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

    @contextmanager
    def _connection(self, plc_id, peer):
        """Open a fresh KV COM+ connection for the duration of the block.

        Raises ``RuntimeError`` (carried back to the 64-bit caller by msl-loadlib)
        if the connection does not become active.
        """
        self.lib.PLC = int(plc_id)
        self.lib.Peer = str(peer)
        self.lib.Connect()
        if not bool(self.lib.Active):
            raise RuntimeError(
                "Connect to %r returned but the connection is not active." % (peer,)
            )
        try:
            yield
        finally:
            try:
                self.lib.Disconnect()
            except Exception:
                pass

    # ---- one-shot operations (connect -> operate -> disconnect) ----

    def read_device_once(self, plc_id, peer, device_type, device_number):
        """Read one device word and return it as an int."""
        with self._connection(plc_id, peer):
            return int(self.lib.ReadDevice(int(device_type), str(device_number)))

    def write_device_once(self, plc_id, peer, device_type, device_number, value):
        """Write one device word. Returns None."""
        with self._connection(plc_id, peer):
            self.lib.WriteDevice(int(device_type), str(device_number), int(value))

    def get_mode_once(self, plc_id, peer):
        """Return the PLC operating mode (True = RUN, False = PROGRAM)."""
        with self._connection(plc_id, peer):
            return bool(self.lib.GetMode)
