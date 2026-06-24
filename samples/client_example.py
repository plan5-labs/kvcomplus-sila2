"""Example SiLA client for the KV COM+ SiLA2 server.

This is the SiLA-client equivalent of ``external/kv-scripts/kv_move_test.py``:
the KV-8000 travel-carriage move test, driven over SiLA instead of via a direct
COM connection. The single-word read/write primitives that the original calls
on the ``DBCommManager`` object become helpers here that call
``DeviceService.ReadDevice`` / ``WriteDevice`` on the server; the bit and 2-word
(32-bit, signed) logic lives client-side, which is exactly the intended split.

The word/bit/2-word composition mirrors ``kvcomplus_sila2.helpers`` (which does
the same on a live COM object), reimplemented here over the SiLA client so the
sample stays a self-contained client with no server-side import.

Move sequence (per the Ardea signal proposal), mirroring kv_move_test.py:
  1. confirm the move-complete signal D6000.0 is ON (operable state)
  2. write target D5010 / speed D5020 / accel D5030 (2 words each)
  3. turn the move-request signal D5000.0 ON (start)
  4. poll the moving signal D6000.1 / current position D6010
  5. finish when D6000.0 returns ON, or on timeout
  6. turn the move-request signal D5000.0 back OFF

★ SAFETY: every state-changing WriteDevice call (steps 2, 3, 6) is COMMENTED
OUT, so by default this script only reads and is safe against a live PLC. The
carriage will NOT move. Uncomment the marked lines to actually run the move,
and verify the numbers first.

Start the server first, e.g.:

    uv run python -m kvcomplus_sila2 --config config.toml --insecure

then run this sample:

    uv run python samples/client_example.py --host 127.0.0.1 --port 50052
"""

from __future__ import annotations

import argparse
import time

from sila2.client import SilaClient
from sila2.framework.errors.defined_execution_error import DefinedExecutionError

# ===== device type code (KV-8000) =======================
DKV8K_DM = 18                     # data memory (word)

# ===== signal addresses (from the signal proposal) =======
ADDR_REQ_WORD = 5000              # D5000 : word containing the move-request bit
BIT_MOVE_REQ = 0                  #   .0  : travel-carriage move-request signal
ADDR_DONE_WORD = 6000             # D6000 : word containing complete/moving bits
BIT_MOVE_DONE = 0                 #   .0  : travel-carriage move-complete signal
BIT_MOVING = 1                    #   .1  : travel-carriage moving signal

ADDR_TGT_POS = 5010               # D5010 : target position [mm]   (2 words)
ADDR_SPEED = 5020                 # D5020 : positioning speed [mm/s] (2 words)
ADDR_ACCEL = 5030                 # D5030 : accel/decel [mm/s/ms]   (2 words)
ADDR_CUR_POS = 6010               # D6010 : current position [mm]  (2 words)
ADDR_CUR_SPEED = 6020             # D6020 : current speed [mm/s]    (2 words)

# ===== test parameters (change as needed) ================
TRAVEL_MM = 500                   # move distance relative to current position
RANGE_MIN_MM = 0                  # travel range lower bound
RANGE_MAX_MM = 2600               # travel range upper bound
SPEED_MMPS = 50                   # positioning speed [mm/s] (slowest standard)
ACCEL = 1                         # accel/decel [mm/s/ms]
POLL_SEC = 0.3                    # polling interval
TIMEOUT_SEC = 60                  # completion-wait timeout
# =========================================================


# ---- single-word read/write primitives (over SiLA) ----
def read_word(client: SilaClient, no: int) -> int:
    return client.DeviceService.ReadDevice(DKV8K_DM, str(no)).Value & 0xFFFF


def write_word(client: SilaClient, no: int, value: int) -> None:
    client.DeviceService.WriteDevice(DKV8K_DM, str(no), int(value) & 0xFFFF)


def read_dword(client: SilaClient, no: int) -> int:
    """2 words as signed 32-bit (low = no, high = no + 1)."""
    low = read_word(client, no)
    high = read_word(client, no + 1)
    u = (high << 16) | low
    return u - 0x100000000 if u >= 0x80000000 else u


def write_dword(client: SilaClient, no: int, value: int) -> None:
    """Write a signed 32-bit value as low = no, high = no + 1."""
    u = int(value) & 0xFFFFFFFF
    write_word(client, no, u & 0xFFFF)
    write_word(client, no + 1, (u >> 16) & 0xFFFF)


def read_bit(client: SilaClient, word_no: int, bit: int) -> int:
    return (read_word(client, word_no) >> bit) & 1


def write_bit(client: SilaClient, word_no: int, bit: int, on: bool) -> None:
    """Read the whole word, change only the target bit, write it back."""
    w = read_word(client, word_no)
    if on:
        w |= (1 << bit)
    else:
        w &= ~(1 << bit)
    write_word(client, word_no, w)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="server host")
    parser.add_argument("--port", type=int, default=50052, help="server port")
    args = parser.parse_args()

    # Connect without encryption (matches `--insecure` on the server).
    client = SilaClient(args.host, args.port, insecure=True)

    print("Implemented features:")
    for feature in sorted(client.SiLAService.ImplementedFeatures.get()):
        print(f"  - {feature}")

    try:
        # GetMode also confirms the PLC is reachable (replaces mgr.GetMode).
        mode = client.ConnectionService.GetMode().Mode
        print(f"\nConnected. Operating mode: {mode}")
        print("=" * 50)

        # --- 1) confirm operable state (move-complete signal ON) ---
        cur = read_dword(client, ADDR_CUR_POS)
        done = read_bit(client, ADDR_DONE_WORD, BIT_MOVE_DONE)
        moving = read_bit(client, ADDR_DONE_WORD, BIT_MOVING)
        print("Current position : %d mm" % cur)
        print("Move-complete D6000.0 :", done, " / Moving D6000.1 :", moving)
        if done != 1 or moving == 1:
            print("-> Not in an operable state (complete ON, moving OFF). Aborting.")
            return

        # --- compute target and range-check ---
        target = cur + TRAVEL_MM
        if not (RANGE_MIN_MM <= target <= RANGE_MAX_MM):
            target = cur - TRAVEL_MM
            if not (RANGE_MIN_MM <= target <= RANGE_MAX_MM):
                print(
                    "-> %+dmm is out of the travel range %d..%d in both directions. Aborting."
                    % (TRAVEL_MM, RANGE_MIN_MM, RANGE_MAX_MM)
                )
                return
        print("Target position : %d mm (move %+d mm)" % (target, target - cur))
        print("Speed : %d mm/s  Accel : %d mm/s/ms" % (SPEED_MMPS, ACCEL))
        print("-" * 50)

        # --- 2) write target / speed / accel (2 words each) ----------------
        # WRITE: state-changing. Uncomment to actually set the move parameters.
        # write_dword(client, ADDR_TGT_POS, target)
        # write_dword(client, ADDR_SPEED, SPEED_MMPS)
        # write_dword(client, ADDR_ACCEL, ACCEL)
        # print("Readback target D5010 =", read_dword(client, ADDR_TGT_POS),
        #       " speed D5020 =", read_dword(client, ADDR_SPEED),
        #       " accel D5030 =", read_dword(client, ADDR_ACCEL))

        # --- 3) turn the move-request signal ON (start) --------------------
        # WRITE: this starts the carriage moving. Uncomment to run the move.
        # write_bit(client, ADDR_REQ_WORD, BIT_MOVE_REQ, True)
        # print("Move-request D5000.0 = ON  -> start")
        # print("-" * 50)

        # --- 4) poll for completion (read-only) ----------------------------
        # NOTE: with the writes above commented out, no move was started, so
        # the carriage is at rest (complete ON, moving OFF) and this loop exits
        # on the first iteration. Once the move-request write is enabled, this
        # same loop tracks the move to completion. The `finally` clause turns
        # the request bit back OFF (step 6) — also commented out for safety.
        t0 = time.time()
        try:
            while True:
                done = read_bit(client, ADDR_DONE_WORD, BIT_MOVE_DONE)
                moving = read_bit(client, ADDR_DONE_WORD, BIT_MOVING)
                pos = read_dword(client, ADDR_CUR_POS)
                cur_spd = read_dword(client, ADDR_CUR_SPEED)
                print(
                    "  pos %5d mm  speed %4d mm/s  moving=%d complete=%d"
                    % (pos, cur_spd, moving, done)
                )
                if moving == 0 and done == 1:
                    print("-> Move complete (or at rest).")
                    break
                if time.time() - t0 > TIMEOUT_SEC:
                    print("-> Timeout (%ds): complete signal did not return." % TIMEOUT_SEC)
                    break
                time.sleep(POLL_SEC)
        finally:
            # --- 5/6) turn the move-request signal back OFF (always) -------
            # WRITE: uncomment together with step 3 so the request bit is
            # always cleared after the move.
            # write_bit(client, ADDR_REQ_WORD, BIT_MOVE_REQ, False)
            # print("Move-request D5000.0 = OFF")
            pass

        final = read_dword(client, ADDR_CUR_POS)
        print("-" * 50)
        print("Final position : %d mm" % final)

    except DefinedExecutionError as e:
        # ConnectionError (PLC unreachable / COM error) or DeviceAccessError.
        print(f"\nCommand failed [{e.identifier}]: {e.message}")


if __name__ == "__main__":
    main()
