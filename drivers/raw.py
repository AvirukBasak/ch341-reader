import logging
import os
import sys


logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(os.path.basename(__file__))


CH341RDR_NAME = "raw"


def __ch341rdr_raw_foo_bar(parsed_userdata):
    # a function private to this module only
    # must be prefixed with __ch341rdr_{CH341RDR_NAME}_
    return parsed_userdata


# -------------------------------------------------------------------------------------
# Following functions are exports and hence must have exact formal parameters as shown
# -------------------------------------------------------------------------------------


def ch341rdr_write(parsed_userdata: bytes) -> bytes:
    """
    Converts bytes taken from user (same endian as user input)
    and optionally contructs device specific frames / payload.
    """
    data = __ch341rdr_raw_foo_bar(parsed_userdata)
    return data


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
    return


__all__ = [
    "CH341RDR_NAME",
    "ch341rdr_write",
    "ch341rdr_onwrite"
]
