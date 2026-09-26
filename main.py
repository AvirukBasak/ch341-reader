#!/bin/env python3
"""
ch341_serial.py - Layer 3: interactive CLI serial monitor for the CH341

Usage:
    python ch341_serial.py [--baud RATE] [--vid VID] [--pid PID] [--hex]

Reads data continuously from the CH341 device and prints it to the terminal.
Ctrl-C cleanly releases the device before exit.

Depends on:
    ch341.py      (layer 2 — driver logic)
    usb_compat.py (layer 1 — libusb/PyUSB compat)
"""

import argparse
import logging
import sys

from ch341 import (
    CH341_VENDOR_ID,
    CH341_PRODUCT_ID,
    CH341_DEFAULT_BAUDRATE,
    ch341_open,
    ch341_close,
    ch341_read,
    ch341_carrier_raised,
)

# ---------------------------------------------------------------------------
# Logging — show INFO and above on stderr so stdout stays clean data output
# ---------------------------------------------------------------------------
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ch341_serial")


# ---------------------------------------------------------------------------
# Read loop
# ---------------------------------------------------------------------------

def run(handle, hex_mode: bool, read_size: int, timeout: int) -> None:
    """
    Block-read from the device forever, printing to stdout.

    hex_mode=True  →  "de ad be ef  " style hex dump per line
    hex_mode=False →  raw bytes written directly (pass-through)
    """
    log.info("Streaming — press Ctrl-C to exit")

    # Flush stdout so piping works correctly
    out = sys.stdout.buffer if not hex_mode else None

    while True:
        try:
            data = ch341_read(handle, size=read_size, timeout=timeout)
        except OSError as e:
            # A timeout on an empty line is normal for interrupt-driven devices;
            # errno 110 = ETIMEDOUT, errno 60 = same on macOS
            if e.errno in (110, 60):
                continue
            log.error("read error: %s", e)
            break

        if not data:
            continue

        if hex_mode:
            hex_str = " ".join(f"{b:02x}" for b in data)
            print(hex_str, flush=True)
        elif out:
            out.write(data)
            out.flush()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="CH341 USB serial monitor — streams device output to stdout",
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
        default=64,
        help="Max bytes per read call (default: 64)",
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
        handle = ch341_open(
            vendor_id=args.vid,
            product_id=args.pid,
            baud_rate=args.baud,
        )
    except OSError as e:
        log.error("Could not open device: %s", e)
        sys.exit(1)

    # -- Report initial modem status -----------------------------------------
    if ch341_carrier_raised(handle):
        log.info("Carrier detected (DCD asserted)")
    else:
        log.info("No carrier (DCD not asserted)")

    # -- Stream --------------------------------------------------------------
    try:
        run(handle, args.hex_mode, args.read_size, args.timeout)
    except KeyboardInterrupt:
        # Ctrl-C: fall through to clean shutdown below
        print(file=sys.stderr)   # newline after ^C on terminal
        log.info("Interrupted")
    finally:
        # Always release the device, even if an exception propagates
        log.info("Releasing device...")
        ch341_close(handle)
        log.info("Done")


if __name__ == "__main__":
    main()
