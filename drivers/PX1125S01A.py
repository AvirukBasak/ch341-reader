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


def __ch341rdr_phoenix_output_parser(payload: bytes) -> list[str]:

    hex_str = ' '.join(
        f'{b:02x}' for b in payload
    )

    # Generic ACK -------------------------------------------------------------------------

    if (
        len(payload) >= 1 and
        payload[0] == 0x83
    ):
        return [ f"ACK (83): {hex_str}" ]

    # Generic NACK ------------------------------------------------------------------------

    if (
        len(payload) >= 1 and
        payload[0] == 0x84
    ):
        return [ f"NACK (84): {hex_str}" ]

    # Contellations used in solution ------------------------------------------------------

    if (
        # Reference: Page 80-81 of "AN0037 - Binary Messages of SkyTraQ Phoenix GNSS Receiver"
        # E.g. 64 8c 00 1d
        len(payload) == 4  and
        payload[0] == 0x64 and
        payload[1] == 0x8C
    ):
        fields = {
            # "ID":    payload[0],
            # "SubID": payload[1],
            "B2":    payload[2],
            "B3":    payload[3],
        }

        # Combine B2 and B3 into a 16-bit big-endian value
        constls_bitmask = (fields["B2"] << 8) | fields["B3"]

        constls = {
             "GP": bool(constls_bitmask & (1 << 0)),  # GPS
             "GL": bool(constls_bitmask & (1 << 1)),  # GLONASS
             "GA": bool(constls_bitmask & (1 << 2)),  # Galileo
             "GB": bool(constls_bitmask & (1 << 3)),  # BeiDou
             "GI": bool(constls_bitmask & (1 << 4)),  # NavIC
        }

        output_str = ""
        for k, v in constls.items():
            output_str += f"{k}:{v} "
        output_str = output_str.strip()

        return [
            f"Constellations (Bool): {output_str}",
            f"      Payload (Bytes): {hex_str}"
        ]

    # Sentences active and interval -------------------------------------------------------

    if (
        # Reference: Page 68-69 of "AN0037 - Binary Messages of SkyTraQ Phoenix GNSS Receiver"
        # E.g. 64 81 01 01 01 01 01 01 01 00 00 00 00 01
        len(payload) == 14 and # 14 byte payload
        payload[0] == 0x64 and # ID    = 0x64
        payload[1] == 0x81     # SubID = 0x81
    ):
        fields = {
            # "ID":    payload[0],
            # "SubID": payload[1],
            "GGA":   payload[2],
            "GSA":   payload[3],
            "GSV":   payload[4],
            "GLL":   payload[5],
            "RMC":   payload[6],
            "VTG":   payload[7],
            "ZDA":   payload[8],
            "GNS":   payload[9],
            "GBS":   payload[10],
            "GSR":   payload[11],
            "DTM":   payload[12],
            "GST":   payload[13],
        }

        output_str = ""
        for k, v in fields.items():
            output_str += f"{k}:{v} "
        output_str = output_str.strip()

        return [
            f"Sentence Intervals (Sec): {output_str}",
            f"         Payload (Bytes): {hex_str}"
        ]

    # Cannot parse, generic O/P -----------------------------------------------------------

    return [ f"O/P: {hex_str}" ]


def __ch341rdr_phoenix_parse_output(response: bytes) -> list[str]:
    results: list[str] = []
    cursor = 0

    while True:
        # Extract a frame ------------------------------------------------------

        # Scan for start sequence in case there's leading NMEA noise
        start_idx = response.find(b'\xA0\xA1', cursor)
        # Start sequence missing -> all frames handled, exit
        if start_idx == -1: break
    
        # Also find the end sequence after the start sequence
        end_idx = response.find(b'\x0D\x0A', start_idx)
        # End sequence missing -> all frames that could end handled, ignore incomplete
        if end_idx == -1: break

        # Extract full frame from the response
        frame = response[start_idx : end_idx + 2]

        # Set cursor for the next iteration
        # cursor = end_idx + 2
        cursor = start_idx + len(frame)

        # Parse current frame ---------------------------------------------------

        # a0 a1 [size:2B] [payload?] [cs:1B] 0d 0a (assume minimum payload of 0B)
        if len(frame) < 7:
            continue

        payload_size = int.from_bytes(frame[2:4], 'big')
        payload      = frame[4 : 4 + payload_size]

        payload_parsed = __ch341rdr_phoenix_output_parser(payload)
        results.extend(payload_parsed)

    return results


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

    stats = {
        "parsing-success": False,
        "max-parse-retry": 10,
        "retry-count":     0,
        "outputs":         [],
    }

    def try_parsing(resp):
        # this fn gets called for every stream loop, and once outputs found, this needs not run
        if stats["parsing-success"]: return
        results = __ch341rdr_phoenix_parse_output(resp)

        if len(results) == 0:
            # no outputs found
            stats["retry-count"] += 1
            if stats["retry-count"] > stats["max-parse-retry"]:
                # if count exceeds threshold, raise to have stream function return (simulate a Ctrl+C)
                raise KeyboardInterrupt()
            return

        else:
            # Dont't raise KeyboardInterrupt, let the user keep reading
            stats["parsing-success"] = True
            stats["outputs"].extend(results)


    stream_fn(process_instream=try_parsing)

    if len(stats["outputs"]) > 0:
        for output_str in stats["outputs"]:
            log.info(output_str)
    else:
        log.error("no output found")


__all__ = [
    "CH341RDR_NAME",
    "ch341rdr_write",
    "ch341rdr_onwrite"
]
