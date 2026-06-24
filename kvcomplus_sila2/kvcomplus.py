"""Stateless KV COM+ execution helpers.

The SiLA server keeps no session state between commands. Every operation
initializes COM on the calling thread, creates the KV COM+ ``DBCommManager``
object, connects to the PLC, performs the operation, then disconnects and
uninitializes COM. These helpers encapsulate that ``CoInitialize -> Dispatch ->
Connect -> operate -> Disconnect -> CoUninitialize`` lifecycle so the feature
implementations can stay focused on data conversion.

The connection is held only for the duration of a single operation (a single
``with`` block); no COM object or socket survives between calls. This mirrors
the stateless design of bcap-sila2.

KV COM+ is a Windows-only ActiveX/COM component driven via pywin32, so this
module imports ``pythoncom`` / ``win32com.client`` lazily inside the session so
the package can at least be imported on non-Windows machines.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

from .config import PlcConfig


class KvComError(Exception):
    """Raised when a KV COM+ connection or device operation fails.

    Wraps the underlying ``pythoncom.com_error`` (and import/availability
    errors) so feature implementations can catch a single, library-agnostic
    exception type.
    """


@contextmanager
def plc_session(cfg: PlcConfig) -> Iterator[Any]:
    """Open a KV COM+ connection to the PLC for the duration of the block.

    On entry: initialize COM on this thread, create the ``DBCommManager`` COM
    object, set the PLC model id and peer, and connect. On exit: disconnect and
    uninitialize COM. Yields the connected ``DBCommManager`` object.

    Raises :class:`KvComError` if COM is unavailable, the object cannot be
    created, or the connection does not become active.
    """
    try:
        import pythoncom
        import win32com.client
    except ImportError as e:  # pragma: no cover - non-Windows / no pywin32
        raise KvComError(
            "KV COM+ requires Windows with pywin32 installed: " + str(e)
        )

    pythoncom.CoInitialize()
    mgr = None
    connected = False
    try:
        try:
            mgr = win32com.client.Dispatch(cfg.progid)
        except pythoncom.com_error as e:
            raise KvComError(
                f"Failed to create KV COM+ object {cfg.progid!r}: {e}"
            )

        try:
            mgr.PLC = cfg.plc_id
            mgr.Peer = cfg.peer
            mgr.Connect()
        except pythoncom.com_error as e:
            raise KvComError(f"Failed to connect to PLC at {cfg.peer!r}: {e}")

        if not bool(mgr.Active):
            raise KvComError(
                f"Connect to {cfg.peer!r} returned but the connection is not active."
            )
        connected = True

        yield mgr
    finally:
        if mgr is not None and connected:
            try:
                mgr.Disconnect()
            except Exception:
                pass
        # Drop the only reference before uninitializing COM.
        del mgr
        pythoncom.CoUninitialize()


def read_device(cfg: PlcConfig, device_type: int, device_number: str) -> int:
    """Read one device word via ReadDevice and return it as an int.

    ``device_number`` is passed to the library as a string, as KV COM+ requires.
    """
    with plc_session(cfg) as mgr:
        try:
            return int(mgr.ReadDevice(device_type, str(device_number)))
        except Exception as e:
            raise KvComError(
                f"ReadDevice(type={device_type}, number={device_number!r}) failed: {e}"
            )


def write_device(cfg: PlcConfig, device_type: int, device_number: str, value: int) -> None:
    """Write one device word via WriteDevice."""
    with plc_session(cfg) as mgr:
        try:
            mgr.WriteDevice(device_type, str(device_number), int(value))
        except Exception as e:
            raise KvComError(
                f"WriteDevice(type={device_type}, number={device_number!r}, "
                f"value={value}) failed: {e}"
            )


def get_mode(cfg: PlcConfig) -> bool:
    """Connect and return the PLC operating mode (True = RUN, False = PROGRAM)."""
    with plc_session(cfg) as mgr:
        try:
            return bool(mgr.GetMode)
        except Exception as e:
            raise KvComError(f"GetMode failed: {e}")
