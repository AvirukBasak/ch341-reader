#!/bin/env python3

"""
Usage:
    python read_to_file.py -p [prefix] -s [suffix] -e [ext]

Reads data continuously from the SkyTraQ PX1125S-01A via CH341
and appends it to a file. Automatically switches to a new file
at the end of the hour. File name is of the format:

[prefix]-[YYYY.MM.DD]-[HH.MM.SS]-[suffix].[ext]

Any of [prefix], [suffix] or [ext] can be empty. Delimiters will
not be added if empty.
"""

import argparse
import logging
import queue
import signal
import sys
import os
import threading
from datetime import datetime
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
# File naming
# ---------------------------------------------------------------------------

def make_filename(prefix: str, suffix: str, ext: str) -> str:
    now = datetime.now()
    timestamp = now.strftime("%Y.%m.%d-%H.%M.%S")
    parts = [p for p in [prefix, timestamp, suffix] if p]
    name = "-".join(parts)
    if ext:
        name = f"{name}.{ext}"
    return name


# ---------------------------------------------------------------------------
# Global consants and states
# ---------------------------------------------------------------------------

# Queue size limit: 64 MB worth of bulk-in reads
QUEUE_MAX_BYTES = 64 * 1024 * 1024


state: dict[str, Any] = {
    "handle":        None,
    "write_q":       None,
    "writer_thread": None,
    "stop_event":    None,
    "ch341_read_size": CH341_DEFAULT_BULK_IN_SIZE,
}

# ---------------------------------------------------------------------------
# Writer thread
# ---------------------------------------------------------------------------

# writer exits abruptly once stop event is set (queue may not be traversed)
# writer thread exits only on sentinel encountered (queue is traversed)
WRITER_THREAD_SENTINEL = object()


def open_new_file(old_f, prefix, suffix, ext, hex_mode) -> tuple:
    if old_f is not None:
        old_f.flush()
        old_f.close()
    fname = make_filename(prefix, suffix, ext)
    log.info("Writing to file: %s", fname)
    mode = "w" if hex_mode else "wb"
    return open(fname, mode), datetime.now().hour


def writer_thread_loop(q: queue.Queue, prefix: str, suffix: str, ext: str, hex_mode: bool) -> None:
    working_hour: int | None = None
    f = None

    while True:
        # if thread was asked to stop by main thread
        if state["stop_event"].is_set():
            if f is not None:
                f.flush()
                f.close()
            break

        try:
            item = q.get(timeout=1)
        except queue.Empty:
            # nothing to write, so try again
            # waiting for 1s here is OK coz the queue cannot fill in 1s
            continue

        # final item, close file, stop writing
        if item is WRITER_THREAD_SENTINEL:
            if f is not None:
                f.flush()
                f.close()
            break

        # thread not asked to stop, open new file if none open or new hour
        if f is None or datetime.now().hour != working_hour:
            f, working_hour = open_new_file(f, prefix, suffix, ext, hex_mode)

        # write and flush
        if hex_mode:
            f.write(" ".join(hex(b) for b in item) + "\n")
            f.flush()
        else:
            f.write(item)
            f.flush()


# ---------------------------------------------------------------------------
# Read / stream loop
# ---------------------------------------------------------------------------

def main_thread_read_loop(
    handle,
    hex_mode: bool,
    read_size: int,
    timeout: int,
    prefix: str,
    suffix: str,
    ext: str,
) -> bool:
    """
    Returns True  → caller should restart (KeyboardInterrupt / SIGHUP)
    Returns False → caller should stop (unrecoverable error)
    """
    log.warning("Deleting write files will not any cause error, but the file(s) will become irrecoverable")
    log.info("Streaming to files - Send Ctrl+C or SIGTERM to stop. Please don't send SIGKILL manually.")

    # make ONE queue
    if state["write_q"] is None:
        max_size = max(1, QUEUE_MAX_BYTES // state["ch341_read_size"])
        state["write_q"] = queue.Queue(maxsize=max_size)

    # make ONE thread
    if state["writer_thread"] is None:
        state["writer_thread"] = threading.Thread(
            target=writer_thread_loop,
            args=(state["write_q"], prefix, suffix, ext, hex_mode),
            daemon=True,
            name="writer",
        )

    # make ONE stopping event
    # no need to clear this or re-assign this
    # this is fine coz main thread eventually calls sys.exit(0) after setting this
    if state["stop_event"] is None:
        state["stop_event"] = threading.Event()

    # start the thread if not running
    if not state["writer_thread"].is_alive():
        state["writer_thread"].start()

    try:
        while True:
            try:
                data = ch341_read(handle, size=read_size, timeout=timeout)
            except OSError as e:
                if e.errno in (110, 60): # ETIMEDOUT, try again
                    continue
                # a fatal error, the beginning of the end for the main and writer threads
                log.error("Read error: %s", e)
                # ask writer thread to eventually stop
                # this is blocking but that is fine coz we won't read anymore
                state["write_q"].put(WRITER_THREAD_SENTINEL)
                state["writer_thread"].join()
                # fatal error, so ask caller fn to stop reading
                return False

            if not data:
                # data is None, try again
                continue

            try:
                # rapidly push data, don't wait for consumer, drop if full
                # this is coz we need to read from device continuously
                state["write_q"].put_nowait(data)
            except queue.Full:
                log.warning("Write queue full, dropping chunk")

    except KeyboardInterrupt:
        print(file=sys.stdout)
        log.info("Interrupted")
        # Ctrl+C, stop reading, ask thread to eventually stop
        state["write_q"].put(WRITER_THREAD_SENTINEL)
        state["writer_thread"].join()
        return False

    # This never hits
    return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

SIGNALS_MUST_CONTINUE = [
    signal.SIGHUP,
]

def continue_signal_handler(signum, frame):
    print(file=sys.stderr)
    log.info("Signal %d received, continuing execution", signum)


SIGNALS_MUST_STOP = [
    signal.SIGTERM,
    signal.SIGQUIT,
    signal.SIGPIPE,
]

def stop_signal_handler(signum, frame):
    print(file=sys.stderr)
    log.info("Signal %d received, stopping execution", signum)

    log.info("Releasing device...")
    ch341_close(state["handle"])

    # you can do this in peace coz the main thread has already jumped
    # from the read loop to this point. so the queue is no longer
    # being pushed to
    log.info("Closing writer thread...")
    if state.get("write_q") is not None:
        # forcing abrupt stop coz you don't know the queue backlog
        # and after this signal, process maybe actually killed
        state["stop_event"].set()
        # wait some seconds. doesn't join in time? nuke it anyway (python kills it if main thread exits)
        # note: thread can take <= 1s to notice the stop_event if blocked by queue.get(), so 10s is sufficient
        state["writer_thread"].join(timeout=10)
        if state["writer_thread"].is_alive():
            log.warning("Writer thread did not stop in time, file may be incomplete")

    log.info("Done")
    sys.exit(0)


def register_signal_handlers():
    for sig in SIGNALS_MUST_CONTINUE:
        signal.signal(sig, continue_signal_handler)
    for sig in SIGNALS_MUST_STOP:
        signal.signal(sig, stop_signal_handler)


def main() -> None:
    register_signal_handlers()

    parser = argparse.ArgumentParser(
        description="Reads data continuously from the SkyTraQ PX1125S-01A via CH341 and appends it to a file",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=CH341_DEFAULT_BAUDRATE,
        help=f"Baud rate (default: {CH341_DEFAULT_BAUDRATE})",
    )
    parser.add_argument(
        "--vid",
        type=lambda x: int(x, 0),
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
    parser.add_argument(
        "-p", "--prefix",
        default="",
        help="Filename prefix (default: none)",
    )
    parser.add_argument(
        "-s", "--suffix",
        default="",
        help="Filename suffix (default: none)",
    )
    parser.add_argument(
        "-e", "--extension",
        default="",
        help="Filename extension without dot (default: none)",
    )
    args = parser.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    log.info(
        "Opening CH341 at VID=0x%04x PID=0x%04x baud=%d",
        args.vid, args.pid, args.baud,
    )

    # used for the queue size
    state["ch341_read_size"] = args.read_size

    try:
        state["handle"] = ch341_open(
            vendor_id=args.vid,
            product_id=args.pid,
            baud_rate=args.baud,
        )
    except OSError as e:
        log.error("Could not open device: %s", e)
        sys.exit(1)

    if ch341_carrier_raised(state["handle"]):
        log.info("Carrier detected (DCD asserted)")
    else:
        log.info("No carrier (DCD not asserted)")

    while True:
        should_restart = main_thread_read_loop(
            state["handle"],
            args.hex_mode,
            args.read_size,
            args.timeout,
            args.prefix,
            args.suffix,
            args.extension,
        )
        if not should_restart:
            break
        log.info("Restarting stream...")

    log.info("Releasing device...")
    ch341_close(state["handle"])
    log.info("Done")


if __name__ == "__main__":
    main()
