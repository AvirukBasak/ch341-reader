#!/bin/env python3

help = """
Commands Help Text:

get baud
get read-size
get read-mode
get timeout
get debug

set baud      [int]
set read-size [int]
set read-mode [hex|txt]
set timeout   [int]
set debug     [0|1]

read  [read-size=None] [read-mode=None]
write [payload]        [write-mode=txt] [write-template=raw]

list
open vid:pid
close

Supported Read and Write Modes:
- read-mode:  txt - Standard text output
- read-mode:  hex - Display space separated hex bytes as data arrives
- write-mode: txt - Parse as Python bytes string, spaces unsupported
- read-mode:  hex - Parse as hex number, same endian as input

Supported Write Templates:
- raw      - Exact bytes
- sktrq-px - SkyTraq Phoenix GNSS receiver
"""

import ast
from enum import Enum
import logging
import sys
import os
import re

from ch341 import (
    CH341_VENDOR_ID,
    CH341_PRODUCT_ID,
    CH341_DEFAULT_BAUDRATE,
    CH341_DEFAULT_BULK_IN_SIZE,
    ch341_open,
    ch341_close,
    ch341_read,
    ch341_write,
    ch341_carrier_raised,
)

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(os.path.basename(__file__))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

NONE = "None"

class WriteTemplate(str, Enum):
    RAW      = "raw"
    SKTRQ_PX = "sktrq-px"

WRITE_TEMPLATES = [WriteTemplate.RAW, WriteTemplate.SKTRQ_PX]


class DataMode(str, Enum):
    HEX = "hex"
    TXT = "txt"

DATA_MODES = [DataMode.HEX, DataMode.TXT]


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

state = {
    "baud":      CH341_DEFAULT_BAUDRATE,
    "read-size": CH341_DEFAULT_BULK_IN_SIZE,
    "read-mode": DataMode.TXT,
    "timeout":   1000,
    "debug":     False,
    "handle":    None,
    "vid":       CH341_VENDOR_ID,
    "pid":       CH341_PRODUCT_ID,
}

GETTABLE = {"baud", "read-size", "read-mode", "timeout", "debug"}

SET_PARSERS = {
    "baud":           int,
    "read-size":      int,
    "read-mode":      lambda v: v if v in DATA_MODES      else (_ for _ in ()).throw(ValueError(f"invalid read-mode: {v}")),
    "write-mode":     lambda v: v if v in DATA_MODES      else (_ for _ in ()).throw(ValueError(f"invalid write-mode: {v}")),
    "write-template": lambda v: v if v in WRITE_TEMPLATES else (_ for _ in ()).throw(ValueError(f"invalid write-template: {v}")),
    "timeout":        int,
    "debug":          lambda v: bool(int(v)),
}

# ---------------------------------------------------------------------------
# Read / stream Loop
# ---------------------------------------------------------------------------

def stream(handle, hex_mode: bool, read_size: int, timeout: int, process_instream = lambda resp: None) -> None:
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
                print(" ".join(f'{b:02x}' for b in data), flush=True)
            elif out:
                out.write(data)
                out.flush()

    except KeyboardInterrupt as e:
        if isinstance(e, KeyboardInterrupt):
            print(file=sys.stderr)
            log.info("Interrupted")


# ---------------------------------------------------------------------------
# Command handlers  (each receives the remaining argv tokens)
# ---------------------------------------------------------------------------

def cmd_get(args):
    if not args or args[0] not in GETTABLE:
        log.error(f"get requires one of: {', '.join(sorted(GETTABLE))}")
        return
    key = args[0]
    log.info(f"{key} = {state[key]}")


def cmd_set(args):
    if len(args) < 2:
        log.error("set requires <key> <value>")
        return
    key, raw = args[0], args[1]
    if key not in SET_PARSERS:
        log.error(f"unknown setting '{key}'")
        return
    try:
        state[key] = SET_PARSERS[key](raw)
    except (ValueError, TypeError) as e:
        log.error(f"{e}")
        return
    if key == "debug":
        logging.getLogger().setLevel(logging.DEBUG if state["debug"] else logging.INFO)
    log.info(f"{key} = {state[key]}")


def cmd_read(args):
    if state["handle"] is None:
        log.error("no device open")
        return

    try:
        read_size = SET_PARSERS["read-size"](args[0]) if args          and args[0] != NONE else state["read-size"]
        read_mode = SET_PARSERS["read-mode"](args[1]) if len(args) > 1 and args[1] != NONE else state["read-mode"]
    except ValueError as e:
        log.error(f"{e}")
        return

    hex_mode  = read_mode == DataMode.HEX
    stream(state["handle"], hex_mode, read_size, state["timeout"])


def parse_skytraq_px_ack(frame: bytes) -> tuple[bool, str | None]:
    """
    Returns a tuple. First member if true implies success else
    second member contains parsing error string. On success, 2nd
    member if None implies NACK else successful ACK.
    """
    # Scan for start sequence in case there's leading NMEA noise
    idx = frame.find(b'\xA0\xA1')
    if idx == -1:
        return False, f"start sequence xA0 xA1 not found"

     # a0 a1 [size:2B] [type:1B] [payload?] [cs:1B] 0d 0a (assume minimum payload of 1B)
    if len(frame) < 9:
        return False, f"response too short: {frame.hex()}"

    ack_payload_size = int.from_bytes(frame[2:4], 'big')
    ack_type         = frame[4] # 0x83 = ACK, 0x84 = NACK
    ack_payload      = frame[5 : 5 + ack_payload_size - 1] # size includes ack_type, so -1

    if ack_type == 0x83: # ACK
        return True, ' '.join(f'{b:02x}' for b in ack_payload)
    elif ack_type == 0x84: # NACK
        return True, None
    else:
        return False, f"unexpected response: {frame.hex()}"


def cmd_write(args):
    if state["handle"] is None:
        log.error("no device open")
        return

    if not args:
        log.error("missing write payload")
        return

    try:
        write_mode     = SET_PARSERS["write-mode"]    (args[1]) if len(args) > 1 and args[1] != NONE else DataMode.TXT
        write_template = SET_PARSERS["write-template"](args[2]) if len(args) > 2 and args[2] != NONE else WriteTemplate.RAW
    except ValueError as e:
        log.error(f"{e}")
        return

    if write_mode == DataMode.HEX:
        try:
            payload = bytes.fromhex(args[0].replace("_", ""))
        except ValueError as e:
            log.error(f"invalid hex: {e}")
            return
    else: # txt
        payload = ast.literal_eval(f'b"{args[0]}"')
    size = len(payload)

    if write_template == WriteTemplate.RAW:
        data = payload
    elif write_template == WriteTemplate.SKTRQ_PX:
        cs = 0
        for b in payload: cs ^= b
        # A0 A1 [size:2B] [payload:size] [cs:1B] 0D 0A
        # [size] unit is in bytes, stored big endian
        # [cs] is just xor of everything, likely a simple xor checksum
        # payload contains actual command, see datasheet
        data = (
            bytes([0xA0, 0xA1]) +
            size.to_bytes(2, 'big') +
            bytes(payload) +
            bytes([cs, 0x0D, 0x0A])
        )
    else:
        log.error(f"unknown template: {write_template}")
        return

    ch341_write(state["handle"], data)

    # attempt get an ACK, may need to read through some bytes for this
    success_stats = {
        "success":       False,
        "cmd_id_or_err": None,
        "fails_count":   0,
        "fails_max":     10
    }

    def process_instream(resp):
        if write_template == WriteTemplate.RAW:
            success_stats["success"] = True
            success_stats["cmd_id_or_err"] = "Unsupported Device"
            return
        # this fn gets called for every stream loop, if onces ACK/NACK found, this needs not run
        if success_stats["success"]: return
        # call parser once
        parse_success, cmd_id = parse_skytraq_px_ack(resp)
        # parse failed, return but increment the counter
        if not parse_success:
            success_stats["fails_count"] += 1
            # if count exceeds threshold, raise to have stream function return (simulate a Ctrl+C)
            if success_stats["fails_count"] > success_stats["fails_max"]:
                raise KeyboardInterrupt()
            # else let stream continue reading data
            return
        else:
            # on success: these will be used outside stream
            success_stats["success"] = parse_success
            success_stats["cmd_id_or_err"] = cmd_id

    hex_mode = state["read-mode"] == DataMode.HEX
    stream(state["handle"], hex_mode, state["read-size"], state["timeout"], process_instream)

    if not success_stats["success"]:
        log.error(f"parse failed: {success_stats["cmd_id_or_err"]}")
    elif success_stats["cmd_id_or_err"]:
        log.info(f"ACK payload: {success_stats["cmd_id_or_err"]}")
    else:
        log.error("NACK: command rejected")


def cmd_list(_args):
    log.error("error: list unimplemented")
    return
    devices = ch341_list()
    if not devices:
        log.error("no CH341 devices found")
        return
    for dev in devices:
        print(dev)


def cmd_open(args):
    if not args:
        vid = CH341_VENDOR_ID
        pid = CH341_PRODUCT_ID
    else:
        try:
            vid_s, pid_s = args[0].split(":")
            vid, pid = int(vid_s, 16), int(pid_s, 16)
        except ValueError:
            log.error("expected vid:pid in hex, e.g. 1a86:7523")
            return
    if state["handle"] is not None:
        log.warning("closing existing device first")
        ch341_close(state["handle"])
        state["handle"] = None
    try:
        state["handle"] = ch341_open(vendor_id=vid, product_id=pid, baud_rate=state["baud"])
        state["vid"], state["pid"] = vid, pid
        log.info("Opened VID=0x%04x PID=0x%04x baud=%d", vid, pid, state["baud"])
        if ch341_carrier_raised(state["handle"]):
            log.info("Carrier detected (DCD asserted)")
        else:
            log.info("No carrier (DCD not asserted)")
    except OSError as e:
        log.error("Could not open device: %s", e)


def cmd_close(_args):
    if state["handle"] is None:
        log.error("no device open")
        return
    ch341_close(state["handle"])
    state["handle"] = None
    log.info("Device closed")


def cmd_help(_args):
    print(help)


def cmd_cls(_args):
    import os
    os.system('cls' if os.name=='nt' else 'clear')


def cmd_exit(_args):
    raise EOFError()


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

COMMANDS = {
    "get":   cmd_get,
    "set":   cmd_set,
    "read":  cmd_read,
    "write": cmd_write,
    "list":  cmd_list,
    "open":  cmd_open,
    "close": cmd_close,
    "help":  cmd_help,
    "exit":  cmd_exit,
    "cls":   cmd_cls
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    while True:
        try:
            device = ">" if state["handle"] is None else f"{state["vid"]:02x}:{state["pid"]:02x}"
            cmd_input = input(f"\n{device}> ")
            cmd_input = re.sub(r" {2,}", " ", cmd_input)
            tokens = cmd_input.split()
        except (EOFError, KeyboardInterrupt):
            break
        if not tokens:
            continue
        verb, *rest = tokens
        handler = COMMANDS.get(verb)
        if handler:
            try: handler(rest)
            except EOFError: break
        else:
            log.error(f"unknown command '{verb}'")

    if state["handle"] is not None:
        print(file=sys.stdout)
        log.info("Releasing device...")
        ch341_close(state["handle"])
        log.info("Done")


if __name__ == "__main__":
    main()
