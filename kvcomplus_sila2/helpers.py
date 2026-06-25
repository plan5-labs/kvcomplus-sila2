"""Reusable KV COM+ word/bit convenience helpers.

These are intentionally NOT used by the bare DeviceService feature (which only
forwards the library's single-word ReadDevice/WriteDevice). They are provided
for higher-level orchestration that opens a single connection and then performs
many word/bit/2-word operations against a live ``DBCommManager`` object.

Each function operates on a live ``mgr`` object — any object exposing KV COM+'s
``ReadDevice`` / ``WriteDevice`` (e.g. ``Server32.lib`` in
:mod:`kvcomplus_sila2.kvcomplus_server32`). Because the ``DBCommManager`` COM
object is 32-bit and lives only inside the 32-bit bridge subprocess (it cannot
cross the msl-loadlib RPC boundary as a live object), **this logic must run on
the 32-bit side**, inside a ``Server32`` method — not on the 64-bit SiLA side.
Issuing one RPC per word would also be far too slow for loops/polling, so any
sequence (e.g. a move with position polling) belongs in a single ``Server32``
method that calls these helpers locally and returns only the result::

    # inside a Server32 subclass method, running in the 32-bit interpreter:
    from kvcomplus_sila2 import helpers

    pos = helpers.read_dword(self.lib, 18, 6010)   # signed 32-bit from DM 6010/6011
    helpers.write_bit(self.lib, 18, 5000, 0, True)  # set D5000.0

2-word values follow the convention confirmed on the KV-8000: the low 16 bits
live at the address and the high 16 bits at address + 1, interpreted as a signed
32-bit integer. Device numbers are passed to the library as strings, as KV COM+
requires.
"""

from __future__ import annotations

from typing import Any


def read_word(mgr: Any, device_type: int, no: int) -> int:
    """Read one word (16-bit, unsigned 0..65535)."""
    return int(mgr.ReadDevice(device_type, str(no))) & 0xFFFF


def write_word(mgr: Any, device_type: int, no: int, value: int) -> None:
    """Write one word (only the low 16 bits of ``value`` are used)."""
    mgr.WriteDevice(device_type, str(no), int(value) & 0xFFFF)


def read_dword(mgr: Any, device_type: int, no: int) -> int:
    """Read two words as a signed 32-bit value (low = ``no``, high = ``no + 1``)."""
    low = read_word(mgr, device_type, no)
    high = read_word(mgr, device_type, no + 1)
    u = (high << 16) | low
    return u - 0x100000000 if u >= 0x80000000 else u


def write_dword(mgr: Any, device_type: int, no: int, value: int) -> None:
    """Write a signed 32-bit value as low = ``no``, high = ``no + 1``."""
    u = int(value) & 0xFFFFFFFF
    write_word(mgr, device_type, no, u & 0xFFFF)
    write_word(mgr, device_type, no + 1, (u >> 16) & 0xFFFF)


def read_bit(mgr: Any, device_type: int, word_no: int, bit: int) -> int:
    """Read a single bit (0 or 1) of a word."""
    return (read_word(mgr, device_type, word_no) >> bit) & 1


def write_bit(mgr: Any, device_type: int, word_no: int, bit: int, on: bool) -> None:
    """Set or clear a single bit, reading the word and writing it back.

    Reads the whole word first and changes only the target bit, so the other
    bits sharing the word are preserved.
    """
    w = read_word(mgr, device_type, word_no)
    if on:
        w |= (1 << bit)
    else:
        w &= ~(1 << bit)
    write_word(mgr, device_type, word_no, w)
