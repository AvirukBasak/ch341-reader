import logging
import os
import sys


logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(os.path.basename(__file__))


CH341RDR_NAME = "phoenix"


def __ch341rdr_phoenix_parse_ack(frame: bytes) -> tuple[bool, str | None]:
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


# -------------------------------------------------------------------------------------
# Following functions are exports and hence must have exact formal parameters as shown
# -------------------------------------------------------------------------------------


def ch341rdr_write(parsed_userdata: bytes) -> bytes:
    """
    Converts bytes taken from user (same endian as user input)
    and optionally contructs device specific frames / payload.
    """
    # A0 A1 [size:2B] [payload:size] [cs:1B] 0D 0A
    # [size] unit is in bytes, stored big endian
    # [cs] is just xor of everything, likely a simple xor checksum
    # payload contains actual command, see datasheet
    cs = 0
    for b in parsed_userdata: cs ^= b
    size = len(parsed_userdata)
    return (
        bytes([0xA0, 0xA1])
        + size.to_bytes(2, "big")
        + parsed_userdata
        + bytes([cs, 0x0D, 0x0A])
    )


def ch341rdr_onwrite(stream_fn, read_fn):
    """
    Do some work after write success, e.g. parse returned ACKs
    or show some status. The shell framework provides 2 functions
    to read any ACKed data.
    
    The stream_fn(...) -> None writes data to stdout.
    Provides a callback to parse that data.
    ```
    stream_fn(
      hex_mode: bool = state, - Hex mode, default value is from `set`
      read_size: int = state, - Read size, default value is from `set`
      timeout: int   = state, - Timeout, default value is from `set`
      process_instream = lambda x: None - Parse data when it arrives,
                                          coz stream auto-write to stdout
    ) -> None
    ```

    The read_fn(...) -> str | bytes | None reads once and returns it.
    ```
    read_fn(
      hex_mode: bool = state, - Hex mode, default value is from `set`
      read_size: int = state, - Read size, default value is from `set`
      timeout: int   = state, - Timeout, default value is from `set`
    ) -> str | bytes | None
    ```
    """

    # attempt get an ACK, may need to read through some bytes for this

    stats = {
        "parsing-success": False,
        "max-parse-retry": 10,
        "retry-count":     0,
        "ack-result":      None,
    }

    def try_parsing(resp):
        # this fn gets called for every stream loop, and once ACK/NACK found, this needs not run
        if stats["parsing-success"]: return
        ok, val = __ch341rdr_phoenix_parse_ack(resp)
        if not ok:
            # parse failed, return but increment the counter
            stats["retry-count"] += 1
            if stats["retry-count"] > stats["max-parse-retry"]:
                # if count exceeds threshold, raise to have stream function return (simulate a Ctrl+C)
                raise KeyboardInterrupt()
            # else let stream continue reading data
            return
        else:
            # on success: these will be used outside stream
            stats["parsing-success"] = True
            stats["ack-result"] = val

    stream_fn(process_instream=try_parsing)

    if not stats["parsing-success"]:
        log.error("no valid ACK / NACK received")
    elif stats["ack-result"] is not None:
        log.info("ACK payload: %s", stats["ack-result"])
    else:
        log.error("NACK: command rejected")


__all__ = [
    "CH341RDR_NAME",
    "ch341rdr_write",
    "ch341rdr_onwrite"
]
