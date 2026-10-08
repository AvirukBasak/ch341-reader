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

load [path/to/driver.py] - Load a template driver
lsdrv                    - Lists loaded drivers
open vid:pid             - Open a non-default device
close                    - Release device to OS
cls                      - CLear screen

Supported Read and Write Modes:
- read-mode:  txt - Standard text output
- read-mode:  hex - Display space separated hex bytes as data arrives
- write-mode: txt - Parse as Python bytes string, spaces unsupported
- read-mode:  hex - Parse as hex number, same endian as input

Driver Modules:
- A driver handles payloads for the system behind the C341
- Stock drivers defined in "drivers/*.py"
- User of this program can define their own anywhere
- A driver definition requires CH341RDR_NAME = "..." (see drivers/raw.py)
- Also requires several functions (also see drivers/raw.py)
- write-template takes CH341RDR_NAME and calls the right functions
"""

import ast
from enum import Enum
import logging
import sys
import os
import re
import importlib.util

from modules.ch341 import (
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

class WriteTemplate:
    RAW = "raw"

WRITE_TEMPLATES = [WriteTemplate.RAW]


class TemplateDriverFields:
    VAR_NAMESTR = "CH341RDR_NAME"
    FN_WRITE    = "ch341rdr_write"
    FN_ONWRITE  = "ch341rdr_onwrite"

TEMPLATE_DRIVER_REQURIED_FIELDS = [
    TemplateDriverFields.VAR_NAMESTR,
    TemplateDriverFields.FN_WRITE,
    TemplateDriverFields.FN_ONWRITE
]


class DataMode:
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
    "drivers":   {}
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

def read_once(handle, hex_mode: bool, read_size: int, timeout: int) -> str | bytes | None:
    try:
        data = ch341_read(handle, size=read_size, timeout=timeout)
    except OSError as e:
        log.error("read error: %s", e)
        data = None
    if not data:
        # data empty? return None
        return None
    if hex_mode:
        data = " ".join(f'{b:02x}' for b in data)
    return data


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


def cmd_load(args) -> bool:
    if not args:
        log.error("missing args, usage: load [path/to/driver.py], no spaces")
        return False

    path = args[0]
    spec = importlib.util.spec_from_file_location("_ch341_driver", path)

    if spec is None:
        log.error("failed to load '%s', `spec` is none", path)
        return False
    if spec.loader is None:
        log.error("failed to load '%s', `spec.loader` is none", path)
        return False

    # load driver.py python module
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
    except Exception as e:
        log.error("failed to load '%s', %s", path, e)
        return False

    # check for missing fields
    for field in TEMPLATE_DRIVER_REQURIED_FIELDS:
        if not hasattr(mod, field):
            log.error("failed to load '%s', missing required '%s'", path, field)
            return False

    # get driver name
    name = getattr(mod, TemplateDriverFields.VAR_NAMESTR, None)
    if not name or not isinstance(name, str):
        log.error("failed to load '%s', missing '%s'",
                  path, TemplateDriverFields.VAR_NAMESTR)
        return False

    if name in state["drivers"]:
        state["drivers"][name] = mod
        log.info("reloaded '%s', driver name = '%s'", path, name)
    else:
        state["drivers"][name] = mod
        log.info("loaded '%s', driver name = '%s'", path, name)
    return True


def cmd_lsdrv(_args):
    if not state["drivers"]:
        log.info("no drivers loaded")
        return
    for name, mod in state["drivers"].items():
        has_write   = hasattr(mod, TemplateDriverFields.FN_WRITE)
        has_onwrite = hasattr(mod, TemplateDriverFields.FN_ONWRITE)
        log.info("%-20s write=%-5s onwrite=%-5s path=%s",
                 name, has_write, has_onwrite, getattr(mod, "__file__", "?"))


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

    hex_mode = read_mode == DataMode.HEX
    read_streaming(state["handle"], hex_mode, read_size, state["timeout"])


def cmd_write(args):
    if state["handle"] is None:
        log.error("no device open")
        return

    if not args:
        log.error("missing write payload")
        return

    try:
        write_mode     = SET_PARSERS["write-mode"] (args[1]) if len(args) > 1 and args[1] != NONE else DataMode.TXT
        write_template = args[2]                             if len(args) > 2 and args[2] != NONE else WriteTemplate.RAW
    except ValueError as e:
        log.error(f"{e}")
        return

    if write_mode == DataMode.HEX:
        try:
            parsed_userdata = bytes.fromhex(args[0].replace("_", ""))
        except ValueError as e:
            log.error(f"invalid hex: {e}")
            return
    else: # txt
        parsed_userdata = ast.literal_eval(f'b"{args[0]}"')

    driver = state["drivers"].get(write_template)
    if driver is None:
        log.error("unknown template '%s', did you load its driver?", write_template)
        return
    try:
        wire_data = driver.ch341rdr_write(parsed_userdata)
        ch341_write(state["handle"], wire_data)
        log.debug("wrote %d bytes", len(wire_data))
    except Exception as e:
        log.error("driver-side write error: %s", e)
        return

    def read_fn(
        hex_mode: bool = state["read-mode"] == DataMode.HEX,
        read_size: int = state["read-size"],
        timeout: int = state["timeout"]
    ):
        return read_once(state["handle"], hex_mode, read_size, timeout)

    def stream_fn(
        hex_mode: bool = state["read-mode"] == DataMode.HEX,
        read_size: int = state["read-size"],
        timeout: int = state["timeout"],
        process_instream = lambda r: None
    ):
        return read_streaming(state["handle"], hex_mode, read_size, timeout, process_instream)

    try:
        driver.ch341rdr_onwrite(stream_fn, read_fn)
    except Exception as e:
        log.error("driver-side onwrite error: %s", e)


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
    "load":  cmd_load,
    "lsdrv": cmd_lsdrv,
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

def load_stock_drivers() -> bool:
    stat_raw = cmd_load(["drivers/raw.py"])
    stat_px = cmd_load(["drivers/PX1125S01A.py"])
    return stat_raw and stat_px


def main() -> None:
    stat = load_stock_drivers()
    if not stat:
        log.error("failed to load stock drivers, exiting")
        return

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
