# SPDX-License-Identifier: GPL-2.0
"""
ch341.py - Python port of the Linux kernel CH341 USB serial driver

Original C driver authors:
  Copyright 2007, Frank A Kingswood <frank@kingswood-consulting.co.uk>
  Copyright 2007, Werner Cornelius <werner@cornelius-consult.de>
  Copyright 2009, Boris Hajduk <boris@hajduk.org>

Python port: translated from drivers/usb/serial/ch341.c in the Linux kernel.
Kernel calls are replaced with calls to usb_compat (layer 1).
TTY/port infrastructure is removed; this module exposes direct configuration
and I/O functions for use by a caller (layer 3).

ch341.c implements a serial port driver for the Winchiphead CH341.

The CH341 device can be used to implement an RS232 asynchronous
serial port, an IEEE-1284 parallel printer port or a memory-like
interface. In all cases the CH341 supports an I2C interface as well.
This driver only supports the asynchronous serial interface.
"""

import atexit
import logging
import signal
import sys

from usb_compat import (
    GFP_KERNEL,
    USB_DIR_IN,
    USB_DIR_OUT,
    USB_RECIP_DEVICE,
    USB_TYPE_VENDOR,
    EPIPE,
    usb_control_msg,
    usb_control_msg_recv,
    usb_sndctrlpipe,
    usb_find_device,
    usb_claim_interface,
    usb_release_interface,
    usb_bulk_read,
    usb_bulk_write,
    usb_interrupt_read,
    usb_get_max_packet_size,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Device identification
# ---------------------------------------------------------------------------
CH341_VENDOR_ID  = 0x1A86
CH341_PRODUCT_ID = 0x7523   # CH340/CH341 USB-serial

DEFAULT_BAUD_RATE = 115200
DEFAULT_TIMEOUT   = 1000

# ---------------------------------------------------------------------------
# BIT helper — replaces the kernel BIT(n) macro
# ---------------------------------------------------------------------------
def BIT(n: int) -> int:
    return 1 << n

# ---------------------------------------------------------------------------
# flags for IO-Bits
# ---------------------------------------------------------------------------
CH341_BIT_RTS = BIT(6)
CH341_BIT_DTR = BIT(5)

# ---------------------------------------------------------------------------
# interrupt pipe definitions
# ---------------------------------------------------------------------------
# always 4 interrupt bytes
# first irq byte normally 0x08
# second irq byte base 0x7d + below
# third irq byte base 0x94 + below
# fourth irq byte normally 0xee

# second interrupt byte
CH341_MULT_STAT = 0x04  # multiple status since last interrupt event

# status returned in third interrupt answer byte, inverted in data from irq
CH341_BIT_CTS = 0x01
CH341_BIT_DSR = 0x02
CH341_BIT_RI  = 0x04
CH341_BIT_DCD = 0x08
CH341_BITS_MODEM_STAT = 0x0F  # all bits

# ---------------------------------------------------------------------------
# Break support — the information used to implement this was gleaned from
# the Net/FreeBSD uchcom.c driver by Takanori Watanabe.  Domo arigato.
# ---------------------------------------------------------------------------
CH341_REQ_READ_VERSION = 0x5F
CH341_REQ_WRITE_REG    = 0x9A
CH341_REQ_READ_REG     = 0x95
CH341_REQ_SERIAL_INIT  = 0xA1
CH341_REQ_MODEM_CTRL   = 0xA4

CH341_REG_BREAK     = 0x05
CH341_REG_PRESCALER = 0x12
CH341_REG_DIVISOR   = 0x13
CH341_REG_LCR       = 0x18
CH341_REG_LCR2      = 0x25
CH341_REG_FLOW_CTL  = 0x27

CH341_NBREAK_BITS = 0x01

CH341_LCR_ENABLE_RX   = 0x80
CH341_LCR_ENABLE_TX   = 0x40
CH341_LCR_MARK_SPACE  = 0x20
CH341_LCR_PAR_EVEN    = 0x10
CH341_LCR_ENABLE_PAR  = 0x08
CH341_LCR_STOP_BITS_2 = 0x04
CH341_LCR_CS8         = 0x03
CH341_LCR_CS7         = 0x02
CH341_LCR_CS6         = 0x01
CH341_LCR_CS5         = 0x00

CH341_FLOW_CTL_NONE   = 0x00
CH341_FLOW_CTL_RTSCTS = 0x01

CH341_QUIRK_LIMITED_PRESCALER = BIT(0)
CH341_QUIRK_SIMULATE_BREAK    = BIT(1)

# ---------------------------------------------------------------------------
# Clock / baud-rate constants
# ---------------------------------------------------------------------------
CH341_CLKRATE = 48_000_000

def CH341_CLK_DIV(ps: int, fact: int) -> int:
    return 1 << (12 - 3 * ps - fact)

def CH341_MIN_RATE(ps: int) -> int:
    return CH341_CLKRATE // (CH341_CLK_DIV(ps, 1) * 512)

ch341_min_rates = [
    CH341_MIN_RATE(0),
    CH341_MIN_RATE(1),
    CH341_MIN_RATE(2),
    CH341_MIN_RATE(3),
]

def _div_round_up(a: int, b: int) -> int:
    return (a + b - 1) // b

# Supported range is 46 to 3000000 bps.
CH341_MIN_BPS = _div_round_up(CH341_CLKRATE, CH341_CLK_DIV(0, 0) * 256)
CH341_MAX_BPS = CH341_CLKRATE // (CH341_CLK_DIV(3, 0) * 2)

# ---------------------------------------------------------------------------
# Endpoint addresses (standard for CH340/CH341)
# ---------------------------------------------------------------------------
CH341_BULK_IN_EP  = 0x82   # bulk IN
CH341_BULK_OUT_EP = 0x02   # bulk OUT
CH341_INT_IN_EP   = 0x81   # interrupt IN (modem status)

# ---------------------------------------------------------------------------
# Private state — replaces struct ch341_private
# ---------------------------------------------------------------------------
class CH341Private:
    def __init__(self):
        self.baud_rate: int  = DEFAULT_BAUD_RATE
        self.mcr: int        = 0          # modem control register shadow
        self.msr: int        = 0          # modem status register shadow
        self.lcr: int        = 0          # line control register shadow
        self.quirks: int     = 0
        self.version: int    = 0
        self.break_end: int  = 0          # monotonic ns timestamp

# ---------------------------------------------------------------------------
# Top-level device handle exposed to layer 3
# ---------------------------------------------------------------------------
class CH341Device:
    """
    Wraps a usb.core.Device together with CH341-specific private state.

    Instantiate via ch341_open(); release via ch341_close().
    """
    def __init__(self, udev, priv: CH341Private):
        self.dev  = udev          # underlying usb.core.Device
        self.priv = priv

# ---------------------------------------------------------------------------
# Low-level control helpers
# (keep fn names and USB_ naming exactly as in kernel driver)
# ---------------------------------------------------------------------------

def ch341_control_out(dev, request: int, value: int, index: int) -> int:
    r = usb_control_msg(
        dev,
        usb_sndctrlpipe(dev, 0),
        request,
        USB_TYPE_VENDOR | USB_RECIP_DEVICE | USB_DIR_OUT,
        value,
        index,
        None,
        0,
        DEFAULT_TIMEOUT,
    )
    if r < 0:
        log.error("failed to send control message: %d", r)
    return r


def ch341_control_in(dev, request: int, value: int, index: int,
                     buf: bytearray, bufsize: int) -> int:
    r = usb_control_msg_recv(
        dev,
        0,
        request,
        USB_TYPE_VENDOR | USB_RECIP_DEVICE | USB_DIR_IN,
        value,
        index,
        buf,
        bufsize,
        DEFAULT_TIMEOUT,
        GFP_KERNEL,
    )
    if r:
        log.error("failed to receive control message: %d", r)
        return r
    return 0

# ---------------------------------------------------------------------------
# Baud-rate / divisor calculation
#
# The device line speed is given by the following equation:
#
#   baudrate = 48000000 / (2^(12 - 3 * ps - fact) * div), where
#
#       0 <= ps   <= 3,
#       0 <= fact <= 1,
#       2 <= div  <= 256 if fact = 0, or
#       9 <= div  <= 256 if fact = 1
# ---------------------------------------------------------------------------

def ch341_get_divisor(priv: CH341Private, speed: int) -> int:
    """
    Return the packed divisor register value for *speed*, or negative errno.
    """
    #
    # Clamp to supported range, this makes the (ps < 0) and (div < 2)
    # sanity checks below redundant.
    #
    speed = max(CH341_MIN_BPS, min(speed, CH341_MAX_BPS))

    #
    # Start with highest possible base clock (fact = 1) that will give a
    # divisor strictly less than 512.
    #
    fact = 1
    ps = 3
    while ps >= 0:
        if speed > ch341_min_rates[ps]:
            break
        ps -= 1

    if ps < 0:
        return -22  # -EINVAL

    # Determine corresponding divisor, rounding down.
    clk_div = CH341_CLK_DIV(ps, fact)
    div = CH341_CLKRATE // (clk_div * speed)

    # Some devices require a lower base clock if ps < 3.
    force_fact0 = False
    if ps < 3 and (priv.quirks & CH341_QUIRK_LIMITED_PRESCALER):
        force_fact0 = True

    # Halve base clock (fact = 0) if required.
    if div < 9 or div > 255 or force_fact0:
        div //= 2
        clk_div *= 2
        fact = 0

    if div < 2:
        return -22  # -EINVAL

    #
    # Pick next divisor if resulting rate is closer to the requested one,
    # scale up to avoid rounding errors on low rates.
    #
    if (16 * CH341_CLKRATE // (clk_div * div) - 16 * speed >=
            16 * speed - 16 * CH341_CLKRATE // (clk_div * (div + 1))):
        div += 1

    #
    # Prefer lower base clock (fact = 0) if even divisor.
    #
    # Note that this makes the receiver more tolerant to errors.
    #
    if fact == 1 and div % 2 == 0:
        div //= 2
        fact = 0

    return (0x100 - div) << 8 | fact << 2 | ps


def ch341_set_baudrate_lcr(dev, priv: CH341Private,
                           baud_rate: int, lcr: int) -> int:
    if not baud_rate:
        return -22  # -EINVAL

    val = ch341_get_divisor(priv, baud_rate)
    if val < 0:
        return -22  # -EINVAL

    #
    # CH341A buffers data until a full endpoint-size packet (32 bytes)
    # has been received unless bit 7 is set.
    #
    # At least one device with version 0x27 appears to have this bit
    # inverted.
    #
    if priv.version > 0x27:
        val |= BIT(7)

    r = ch341_control_out(
        dev,
        CH341_REQ_WRITE_REG,
        CH341_REG_DIVISOR << 8 | CH341_REG_PRESCALER,
        val,
    )
    if r:
        return r

    #
    # Chip versions before version 0x30 as read using
    # CH341_REQ_READ_VERSION used separate registers for line control
    # (stop bits, parity and word length). Version 0x30 and above use
    # CH341_REG_LCR only and CH341_REG_LCR2 is always set to zero.
    #
    if priv.version < 0x30:
        return 0

    r = ch341_control_out(
        dev,
        CH341_REQ_WRITE_REG,
        CH341_REG_LCR2 << 8 | CH341_REG_LCR,
        lcr,
    )
    return r


def ch341_set_handshake(dev, control: int) -> int:
    return ch341_control_out(dev, CH341_REQ_MODEM_CTRL, (~control) & 0xFF, 0)


def ch341_get_status(dev, priv: CH341Private) -> int:
    size = 2
    buffer = bytearray(size)
    r = ch341_control_in(dev, CH341_REQ_READ_REG, 0x0706, 0, buffer, size)
    if r:
        return r
    priv.msr = (~buffer[0]) & CH341_BITS_MODEM_STAT
    return 0

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def ch341_configure(dev, priv: CH341Private) -> int:
    size = 2
    buffer = bytearray(size)

    # expect two bytes 0x27 0x00
    r = ch341_control_in(dev, CH341_REQ_READ_VERSION, 0, 0, buffer, size)
    if r:
        return r

    priv.version = buffer[0]
    log.debug("Chip version: 0x%02x", priv.version)

    r = ch341_control_out(dev, CH341_REQ_SERIAL_INIT, 0, 0)
    if r < 0:
        return r

    r = ch341_set_baudrate_lcr(dev, priv, priv.baud_rate, priv.lcr)
    if r < 0:
        return r

    r = ch341_set_handshake(dev, priv.mcr)
    if r < 0:
        return r

    return 0


def ch341_detect_quirks(dev, priv: CH341Private) -> int:
    """
    A subset of CH34x devices does not support all features. The
    prescaler is limited and there is no support for sending a RS232
    break condition. A read failure when trying to set up the latter is
    used to detect these devices.
    """
    size = 2
    buffer = bytearray(size)
    quirks = 0

    r = usb_control_msg_recv(
        dev,
        0,
        CH341_REQ_READ_REG,
        USB_TYPE_VENDOR | USB_RECIP_DEVICE | USB_DIR_IN,
        CH341_REG_BREAK,
        0,
        buffer,
        size,
        DEFAULT_TIMEOUT,
        GFP_KERNEL,
    )

    if r == EPIPE:
        log.info("break control not supported, using simulated break")
        quirks = CH341_QUIRK_LIMITED_PRESCALER | CH341_QUIRK_SIMULATE_BREAK
        r = 0
    elif r:
        log.error("failed to read break control: %d", r)

    if quirks:
        log.debug("enabling quirk flags: 0x%02x", quirks)
        priv.quirks |= quirks

    return r

# ---------------------------------------------------------------------------
# Carrier / handshake helpers (kept from kernel driver, useful to layer 3)
# ---------------------------------------------------------------------------

def ch341_carrier_raised(priv: CH341Private) -> bool:
    """Return True if DCD (carrier detect) is asserted."""
    return bool(priv.msr & CH341_BIT_DCD)


def ch341_dtr_rts(dev, priv: CH341Private, on: bool) -> None:
    """Assert or de-assert DTR and RTS lines."""
    # drop DTR and RTS
    if on:
        priv.mcr |= CH341_BIT_RTS | CH341_BIT_DTR
    else:
        priv.mcr &= ~(CH341_BIT_RTS | CH341_BIT_DTR)
    ch341_set_handshake(dev, priv.mcr)

# ---------------------------------------------------------------------------
# Interrupt / modem-status update
#
# In the kernel this is driven by an async URB callback (ch341_read_int_callback).
# Here it is called synchronously by ch341_read_interrupt() which layer 3
# can poll as needed.
# ---------------------------------------------------------------------------

def ch341_update_status(priv: CH341Private, data: bytes) -> dict:
    """
    Parse a 4-byte interrupt packet and update the modem status shadow.

    Returns a dict of changed signal names (for layer 3 inspection).
    Mirrors ch341_update_status() from the kernel driver.
    """
    if len(data) < 4:
        return {}

    status = (~data[2]) & CH341_BITS_MODEM_STAT

    delta = status ^ priv.msr
    priv.msr = status

    if data[1] & CH341_MULT_STAT:
        log.debug("ch341_update_status - multiple status change")

    if not delta:
        return {}

    changes = {}
    if delta & CH341_BIT_CTS:
        changes["cts"] = bool(status & CH341_BIT_CTS)
    if delta & CH341_BIT_DSR:
        changes["dsr"] = bool(status & CH341_BIT_DSR)
    if delta & CH341_BIT_RI:
        changes["ri"]  = bool(status & CH341_BIT_RI)
    if delta & CH341_BIT_DCD:
        changes["dcd"] = bool(status & CH341_BIT_DCD)

    return changes

# ---------------------------------------------------------------------------
# Public I/O functions (new in Python port — no TTY layer)
# ---------------------------------------------------------------------------

def ch341_read(handle: CH341Device, size: int = 64,
               timeout: int = DEFAULT_TIMEOUT) -> bytes:
    """
    Read up to *size* bytes from the CH341 bulk IN endpoint.

    The read size is clamped to at least wMaxPacketSize for the bulk IN
    endpoint — passing a smaller value causes EOVERFLOW on some hosts.

    Returns bytes on success, raises OSError on failure.
    """
    mps = getattr(handle, "bulk_in_max_packet_size", 64)
    if mps > 0 and size < mps:
        log.debug("ch341_read: clamping size %d -> %d (wMaxPacketSize)", size, mps)
        size = mps
    result = usb_bulk_read(handle.dev, CH341_BULK_IN_EP, size, timeout)
    if isinstance(result, int):  # negative errno
        raise OSError(-result, f"ch341_read failed (errno {result})")
    return result


def ch341_write(handle: CH341Device, data: bytes,
                timeout: int = DEFAULT_TIMEOUT) -> int:
    """
    Write *data* to the CH341 bulk OUT endpoint.

    Returns number of bytes written, raises OSError on failure.
    """
    n = usb_bulk_write(handle.dev, CH341_BULK_OUT_EP, data, timeout)
    if isinstance(n, int) and n < 0:
        raise OSError(-n, f"ch341_write failed (errno {n})")
    return n


def ch341_read_interrupt(handle: CH341Device,
                         timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    Perform a single synchronous read of the interrupt IN endpoint and
    update the modem-status shadow in *handle.priv*.

    Returns a dict of changed modem signals (may be empty).
    Replaces the async ch341_read_int_callback() from the kernel driver.
    """
    result = usb_interrupt_read(handle.dev, CH341_INT_IN_EP, 4, timeout)
    if isinstance(result, int):
        log.debug("ch341_read_interrupt: no data (errno %d)", result)
        return {}
    return ch341_update_status(handle.priv, result)

# ---------------------------------------------------------------------------
# Device open / close (replaces ch341_port_probe / ch341_port_remove)
# ---------------------------------------------------------------------------

def ch341_open(vendor_id: int = CH341_VENDOR_ID,
               product_id: int = CH341_PRODUCT_ID,
               baud_rate: int = DEFAULT_BAUD_RATE) -> CH341Device:
    """
    Find the CH341 device, claim the interface, and configure it.

    Returns a CH341Device handle on success.
    Raises OSError on any failure.

    Replaces ch341_port_probe() from the kernel driver.
    """
    udev = usb_find_device(vendor_id, product_id)
    if udev is None:
        raise OSError(19, "CH341 device not found")  # ENODEV

    r = usb_claim_interface(udev, 0)
    if r < 0:
        raise OSError(-r, f"failed to claim interface: {r}")

    priv = CH341Private()
    priv.baud_rate = baud_rate
    #
    # Some CH340 devices appear unable to change the initial LCR
    # settings, so set a sane 8N1 default.
    #
    priv.lcr = CH341_LCR_ENABLE_RX | CH341_LCR_ENABLE_TX | CH341_LCR_CS8

    r = ch341_configure(udev, priv)
    if r < 0:
        usb_release_interface(udev, 0)
        raise OSError(-r, f"ch341_configure failed: {r}")

    handle = CH341Device(udev, priv)

    r = ch341_detect_quirks(udev, priv)
    if r < 0:
        usb_release_interface(udev, 0)
        raise OSError(-r, f"ch341_detect_quirks failed: {r}")

    # Register cleanup on normal interpreter exit and on SIGINT / SIGTERM
    atexit.register(_cleanup_handle, handle)
    signal.signal(signal.SIGINT,  _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    mps = usb_get_max_packet_size(udev, CH341_BULK_IN_EP)
    log.info("CH341 device opened (version 0x%02x, baud %d, bulk IN max packet size %d)",
             priv.version, priv.baud_rate, mps)
    handle.bulk_in_max_packet_size = mps
    return handle


def ch341_close(handle: CH341Device) -> None:
    """
    Release the USB interface and clean up.

    Safe to call more than once.
    Replaces ch341_port_remove() from the kernel driver.
    """
    if handle.dev is None:
        return
    try:
        ch341_dtr_rts(handle.dev, handle.priv, False)
    except Exception:
        pass
    usb_release_interface(handle.dev, 0)
    handle.dev = None
    log.info("CH341 device closed")

# ---------------------------------------------------------------------------
# Reconfigure helpers (for layer 3 runtime changes)
# ---------------------------------------------------------------------------

def ch341_set_baud_rate(handle: CH341Device, baud_rate: int) -> int:
    """Change baud rate on an already-open device."""
    handle.priv.baud_rate = baud_rate
    return ch341_set_baudrate_lcr(handle.dev, handle.priv,
                                  baud_rate, handle.priv.lcr)


def ch341_set_line_control(handle: CH341Device, lcr: int) -> int:
    """Change LCR (parity, stop bits, word length) on an already-open device."""
    handle.priv.lcr = lcr
    return ch341_set_baudrate_lcr(handle.dev, handle.priv,
                                  handle.priv.baud_rate, lcr)


def ch341_reset_resume(handle: CH341Device) -> int:
    """
    Reconfigure the CH341 after a USB bus reset.

    Replaces ch341_reset_resume() from the kernel driver.
    Returns 0 on success, negative errno on failure.
    """
    r = ch341_configure(handle.dev, handle.priv)
    if r:
        return r
    r = ch341_get_status(handle.dev, handle.priv)
    if r < 0:
        log.error("failed to read modem status: %d", r)
    return r

# ---------------------------------------------------------------------------
# Interrupt / signal handlers — ensure device is released on exit
# ---------------------------------------------------------------------------

_open_handles: list = []   # weak registry so atexit can clean up

def _cleanup_handle(handle: CH341Device) -> None:
    ch341_close(handle)


def _signal_handler(signum, frame):
    log.info("Signal %d received — closing CH341 device(s)", signum)
    # atexit handlers fire on sys.exit()
    sys.exit(0)
