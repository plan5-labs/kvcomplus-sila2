"""Travel-carriage MOVE test for the KV COM+ SiLA2 server.

★★ WARNING: THIS SCRIPT PHYSICALLY MOVES THE TRAVEL CARRIAGE. ★★

This is the move-enabled counterpart of ``samples/client_example.py``. That
sample is read-only (every state-changing WriteDevice is commented out); this one
has those writes ENABLED so the carriage actually moves. The two are kept
separate on purpose: ``client_example.py`` stays safe to run against a live PLC,
and this file is the explicit "I want it to move" entry point.

It mirrors ``external/kv-scripts/kv_move_test.py``: read/write single words over
SiLA's ``DeviceService``; bit and 2-word (32-bit, signed) logic lives client-side.

Move sequence (per the Ardea signal proposal):
  1. confirm the move-complete signal D6000.0 is ON (operable state)
  2. write target D5010 / speed D5020 / accel D5030 (2 words each)   [WRITE]
  3. turn the move-request signal D5000.0 ON (start)                 [WRITE]
  4. poll the moving signal D6000.1 / current position D6010
  5/6. turn the move-request signal D5000.0 back OFF (in finally)    [WRITE]

Move distance defaults to TRAVEL_MM = 500 mm (matching kv_move_test.py); lower it
for a more cautious first move.

Safety measures built in:
  - an explicit "yes" confirmation is required before the move starts
    (skip with --yes; note that under `uv run` stdin may be non-interactive,
    so --yes is the reliable way to run it)
  - the move-request bit is always cleared in the ``finally`` block

Start the server first, e.g.:

    uv run python -m kvcomplus_sila2 --config config.toml --insecure

then run this script (VERIFY THE AREA IS CLEAR FIRST):

    uv run python samples/move_test.py --host 127.0.0.1 --port 50052
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
                                  # (matches external/kv-scripts/kv_move_test.py)
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


def read_done_moving(client: SilaClient) -> tuple[int, int]:
    """Read D6000 once and return (move_complete, moving).

    Both status bits live in the same word, so reading it once (instead of two
    ReadDevice calls) halves the per-poll COM round-trips over the bridge.
    """
    w = read_word(client, ADDR_DONE_WORD)
    return (w >> BIT_MOVE_DONE) & 1, (w >> BIT_MOVING) & 1


def write_bit(client: SilaClient, word_no: int, bit: int, on: bool) -> None:
    """Read the whole word, change only the target bit, write it back."""
    w = read_word(client, word_no)
    if on:
        w |= (1 << bit)
    else:
        w &= ~(1 << bit)
    write_word(client, word_no, w)


def main() -> None:
    parser = argparse.ArgumentParser(description="Travel-carriage MOVE test (moves the carriage).")
    parser.add_argument("--host", default="127.0.0.1", help="server host")
    parser.add_argument("--port", type=int, default=50052, help="server port")
    parser.add_argument(
        "--yes", action="store_true",
        help="skip the confirmation prompt (only if you are sure the area is clear)",
    )
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

        # --- confirmation gate (the carriage is about to move) ---
        if not args.yes:
            print("★ The travel carriage WILL physically move to %d mm." % target)
            try:
                ans = input("  Type 'yes' to proceed (anything else aborts): ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans != "yes":
                print("-> Aborted. No move was started.")
                return
            print("-" * 50)

        # --- 2) write target / speed / accel (2 words each) ----------------
        write_dword(client, ADDR_TGT_POS, target)
        write_dword(client, ADDR_SPEED, SPEED_MMPS)
        write_dword(client, ADDR_ACCEL, ACCEL)
        print("Readback target D5010 =", read_dword(client, ADDR_TGT_POS),
              " speed D5020 =", read_dword(client, ADDR_SPEED),
              " accel D5030 =", read_dword(client, ADDR_ACCEL))

        # --- 3) turn the move-request signal ON (start) --------------------
        write_bit(client, ADDR_REQ_WORD, BIT_MOVE_REQ, True)
        print("Move-request D5000.0 = ON  -> start")
        print("-" * 50)

        # --- 4) wait for the move to START, then for completion -------------
        # The move-request bit is now ON, but the PLC needs a moment to react.
        # Until it does, D6000.0 (complete) is still 1 and D6000.1 (moving) is
        # still 0 — i.e. *identical* to the "finished/at rest" reading. If we
        # checked for completion right away we would mistake "not started yet"
        # for "done" and then clear the request bit in `finally`, which CANCELS a
        # move that never ran (this is why a 500 mm move appeared not to move).
        # So phase A waits for the PLC to acknowledge the start (complete -> 0 or
        # moving -> 1); only then does phase B wait for completion.
        START_TIMEOUT_SEC = 10        # allow for slow per-word bridge round-trips
        try:
            # phase A: wait for the start to be acknowledged
            started = False
            t0 = time.time()
            while time.time() - t0 <= START_TIMEOUT_SEC:
                done, moving = read_done_moving(client)
                print("  [waiting for start]  moving=%d complete=%d" % (moving, done))
                if moving == 1 or done == 0:
                    started = True
                    break
                time.sleep(POLL_SEC)
            if not started:
                print(
                    "-> The PLC did not acknowledge the move within %ds "
                    "(complete stayed ON, moving stayed OFF). Aborting; check the "
                    "move-request wiring/interlocks." % START_TIMEOUT_SEC
                )
                return

            # phase B: wait for completion (moving OFF and complete ON again)
            t1 = time.time()
            while True:
                done, moving = read_done_moving(client)
                pos = read_dword(client, ADDR_CUR_POS)
                print("  pos %5d mm  moving=%d complete=%d" % (pos, moving, done))
                if moving == 0 and done == 1:
                    print("-> Move complete.")
                    break
                if time.time() - t1 > TIMEOUT_SEC:
                    print("-> Timeout (%ds): complete signal did not return." % TIMEOUT_SEC)
                    break
                time.sleep(POLL_SEC)
        finally:
            # --- 5/6) turn the move-request signal back OFF (always) -------
            write_bit(client, ADDR_REQ_WORD, BIT_MOVE_REQ, False)
            print("Move-request D5000.0 = OFF")

        final = read_dword(client, ADDR_CUR_POS)
        print("-" * 50)
        print("Final position : %d mm (start %d mm)" % (final, cur))

    except DefinedExecutionError as e:
        # ConnectionError (PLC unreachable / COM error) or DeviceAccessError.
        print(f"\nCommand failed [{e.identifier}]: {e.message}")


if __name__ == "__main__":
    main()
