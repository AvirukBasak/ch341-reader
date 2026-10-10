#!/bin/env python3
"""
ch341_serial.py - Layer 3: interactive CLI serial monitor for the CH341

Usage:
    python ch341_serial.py [--baud RATE] [--vid VID] [--pid PID] [--hex]

Reads data continuously from the CH341 device and prints it to the terminal.
Ctrl-C cleanly releases the device before exit.

Depends on:
    ch341.py      (layer 2 - driver logic)
    usb_compat.py (layer 1 - libusb/PyUSB compat)
"""

import argparse
import logging
import signal
import sys
import os
from typing import Any

from modules.ch341 import (
    CH341_VENDOR_ID,
    CH341_PRODUCT_ID,
    CH341_DEFAULT_BAUDRATE,
    CH341_DEFAULT_BULK_IN_SIZE,
    ch341_open,
    ch341_close,
    ch341_read,
    ch341_carrier_raised,
)


# ---------------------------------------------------------------------------
# Logging - show INFO and above on stderr so stdout stays clean data output
# ---------------------------------------------------------------------------

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(os.path.basename(__file__))


# ---------------------------------------------------------------------------
# Read / stream Loop
# ---------------------------------------------------------------------------

def read_streaming(handle, hex_mode: bool, read_size: int, timeout: int, process_instream = lambda resp: None) -> None:
    log.info("Streaming - press Ctrl-C to exit")
    out = sys.stdout.buffer if not hex_mode else None
    try:
        while True:
            try:
                data = ch341_read(handle, size=read_size, timeout=timeout)
                process_instream(data)
            except OSError as e:
                if e.errno in (110, 60): # ETIMEDOUT
                    # try again
                    continue
                log.error("read error: %s", e)
                break

            if not data:
                # data empty? try again
                continue

            if hex_mode:
                print(" ".join(hex(b) for b in data), flush=True)
            elif out:
                out.write(data)
                out.flush()

    except KeyboardInterrupt as e:
        if isinstance(e, KeyboardInterrupt):
            print(file=sys.stderr)
            log.info("Interrupted")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

state: dict[str, Any] = { "handle": None }

SIGNALS_MUST_CONTINUE = [
    signal.SIGHUP,   # Terminal close / daemon reload attempts
]

def continue_signal_handler(signum, frame):
    print(file=sys.stdout)
    log.info("Signal %d received, continuing execution", signum)


SIGNALS_MUST_STOP = [
    signal.SIGTERM,  # Terminate
    signal.SIGQUIT,  # Apparently used for debugging
    signal.SIGPIPE,  # Broken pipe
]

def stop_signal_handler(signum, frame):
    print(file=sys.stdout)
    log.info("Signal %d received, releasing device...", signum)
    ch341_close(state["handle"])
    log.info("Done")


def register_signal_handlers():
    for sig in SIGNALS_MUST_CONTINUE:
        signal.signal(sig, continue_signal_handler)
    for sig in SIGNALS_MUST_STOP:
        signal.signal(sig, stop_signal_handler)


def main() -> None:
    register_signal_handlers()

    parser = argparse.ArgumentParser(
        description="CH341 USB serial monitor - streams device output to stdout",
    )
    parser.add_argument(
        "--baud", "-b",
        type=int,
        default=CH341_DEFAULT_BAUDRATE,
        help=f"Baud rate (default: {CH341_DEFAULT_BAUDRATE})",
    )
    parser.add_argument(
        "--vid",
        type=lambda x: int(x, 0),   # accept 0x1a86 or 6790
        default=CH341_VENDOR_ID,
        help=f"USB vendor ID (default: 0x{CH341_VENDOR_ID:04x})",
    )
    parser.add_argument(
        "--pid",
        type=lambda x: int(x, 0),
        default=CH341_PRODUCT_ID,
        help=f"USB product ID (default: 0x{CH341_PRODUCT_ID:04x})",
    )
    parser.add_argument(
        "--hex",
        action="store_true",
        dest="hex_mode",
        help="Print received bytes as hex instead of raw output",
    )
    parser.add_argument(
        "--read-size",
        type=int,
        default=CH341_DEFAULT_BULK_IN_SIZE,
        help=f"Max bytes per read call (default: {CH341_DEFAULT_BULK_IN_SIZE})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1000,
        help="Read timeout in milliseconds (default: 1000)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    # -- Open device ---------------------------------------------------------
    log.info(
        "Opening CH341 at VID=0x%04x PID=0x%04x baud=%d",
        args.vid, args.pid, args.baud,
    )
    try:
        state["handle"] = ch341_open(
            vendor_id=args.vid,
            product_id=args.pid,
            baud_rate=args.baud,
        )
    except OSError as e:
        log.error("Could not open device: %s", e)
        sys.exit(1)

    # -- Report initial modem status -----------------------------------------
    if ch341_carrier_raised(state["handle"]):
        log.info("Carrier detected (DCD asserted)")
    else:
        log.info("No carrier (DCD not asserted)")

    # -- Stream --------------------------------------------------------------
    read_streaming(state["handle"], args.hex_mode, args.read_size, args.timeout)

    log.info("Releasing device...")
    ch341_close(state["handle"])
    log.info("Done")


if __name__ == "__main__":
    main()
