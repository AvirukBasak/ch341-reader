# SPDX-License-Identifier: GPL-2.0
"""
usb_compat.py - Kernel USB API compatibility layer over PyUSB/libusb

Provides the same function signatures and naming scheme as the Linux kernel
USB subsystem, translating calls to PyUSB (usb.core / libusb backend).

Kernel errno values are mapped from USBError exceptions so callers can
do the same  `if r < 0: return r`  idiom as the original C code.
"""

import errno as _errno
import logging
from typing import Any, Generator
import usb.core
import usb.util

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Kernel-style errno constants (negative, as returned by kernel functions)
# ---------------------------------------------------------------------------
EPIPE    = -_errno.EPIPE
EINVAL   = -_errno.EINVAL
ENOMEM   = -_errno.ENOMEM
ENODEV   = -_errno.ENODEV
ENOENT   = -_errno.ENOENT
ECONNRESET = -_errno.ECONNRESET
ESHUTDOWN  = -_errno.ESHUTDOWN
EIO        = -_errno.EIO

# ---------------------------------------------------------------------------
# bmRequestType bit fields (mirrors kernel USB constants)
# ---------------------------------------------------------------------------
USB_DIR_OUT        = 0x00
USB_DIR_IN         = 0x80
USB_TYPE_VENDOR    = 0x40
USB_RECIP_DEVICE   = 0x00

# ---------------------------------------------------------------------------
# GFP flags - meaningless in userspace, kept so call sites compile unchanged
# ---------------------------------------------------------------------------
GFP_KERNEL = 0
GFP_ATOMIC = 0
GFP_NOIO   = 0


# ---------------------------------------------------------------------------
# Internal helper: translate PyUSB USBError → negative errno integer
# ---------------------------------------------------------------------------
def _usb_errno(exc: usb.core.USBError) -> int:
    """Return a negative errno integer from a USBError."""
    # PyUSB exposes .errno (a positive POSIX errno) on most backends
    if exc.errno is not None:
        return -abs(exc.errno)
    # Fall back to inspecting the backend error string
    msg = str(exc).lower()
    if "pipe" in msg or "stall" in msg:
        return EPIPE
    if "no device" in msg:
        return ENODEV
    return EIO


# ---------------------------------------------------------------------------
# usb_control_msg  (fire-and-forget, OUT direction)
#
# Kernel prototype:
#   int usb_control_msg(struct usb_device *dev, unsigned int pipe,
#                       __u8 request, __u8 requesttype,
#                       __u16 value, __u16 index,
#                       void *data, __u16 size, int timeout)
#
# Here *dev* is a usb.core.Device object.
# *pipe* is ignored (direction is encoded in requesttype).
# Returns 0 on success, negative errno on failure.
# ---------------------------------------------------------------------------
def usb_control_msg(dev, pipe, request, requesttype,
                    value, index, data, size, timeout):
    try:
        ret = dev.ctrl_transfer(
            requesttype,          # bmRequestType
            request,              # bRequest
            value,                # wValue
            index,                # wIndex
            data if size else None,  # data_or_wLength
            timeout,
        )
        # ctrl_transfer returns bytes transferred for OUT; treat as success
        return ret if ret is not None else 0
    except usb.core.USBError as e:
        log.error("usb_control_msg failed: %s", e)
        return _usb_errno(e)


# ---------------------------------------------------------------------------
# usb_control_msg_recv  (IN direction, receives data into buf)
#
# Kernel prototype:
#   int usb_control_msg_recv(struct usb_device *dev, u8 ifnum,
#                            u8 request, u8 requesttype,
#                            u16 value, u16 index,
#                            void *driver_data, u16 size,
#                            int timeout, gfp_t memflags)
#
# Writes received bytes into *buf* (a bytearray or list).
# Returns 0 on success, negative errno on failure.
# ---------------------------------------------------------------------------
def usb_control_msg_recv(dev, ifnum, request, requesttype,
                         value, index, buf, size, timeout, memflags=0):
    try:
        result = dev.ctrl_transfer(
            requesttype,
            request,
            value,
            index,
            size,      # wLength — how many bytes to read
            timeout,
        )
        # Copy received bytes into the caller's buffer
        if buf is not None:
            for i, b in enumerate(result):
                if i >= len(buf):
                    break
                buf[i] = b
        return 0
    except usb.core.USBError as e:
        log.error("usb_control_msg_recv failed: %s", e)
        return _usb_errno(e)


# ---------------------------------------------------------------------------
# Pipe constructors — no-ops in userspace; direction lives in requesttype
# ---------------------------------------------------------------------------
def usb_sndctrlpipe(dev, endpoint):
    """Kernel macro stub — direction is encoded in bmRequestType."""
    return 0  # ignored by usb_control_msg above


def usb_rcvctrlpipe(dev, endpoint):
    """Kernel macro stub — direction is encoded in bmRequestType."""
    return 0


# ---------------------------------------------------------------------------
# usb_submit_urb / usb_kill_urb — synchronous stubs
#
# The ch341 driver uses the interrupt URB only to receive modem-status
# updates.  We model this as a synchronous bulk/interrupt read exposed
# through ch341_read_interrupt() in layer 2 rather than a live callback.
# These stubs satisfy any remaining call-sites.
# ---------------------------------------------------------------------------
def usb_submit_urb(urb, memflags=0):
    """No-op stub — interrupt reads are handled synchronously in layer 2."""
    log.debug("usb_submit_urb called (no-op in compat layer)")
    return 0


def usb_kill_urb(urb):
    """No-op stub."""
    log.debug("usb_kill_urb called (no-op in compat layer)")


# ---------------------------------------------------------------------------
# Device / interface lifecycle helpers
# ---------------------------------------------------------------------------
def usb_find_device(id_vendor: int, id_product: int) -> usb.core.Device | Generator[usb.core.Device, Any, None] | None:
    """
    Locate the first USB device matching *id_vendor*/*id_product*.

    Returns a usb.core.Device on success, or None.
    Equivalent to usb_get_dev() after iterating the device list.
    """
    dev = usb.core.find(idVendor=id_vendor, idProduct=id_product)
    if dev is None:
        log.error("Device %04x:%04x not found", id_vendor, id_product)
    return dev


def usb_claim_interface(dev, interface: int = 0) -> int:
    """
    Detach any kernel driver and claim *interface* for exclusive access.

    Returns 0 on success, negative errno on failure.
    """
    try:
        if dev.is_kernel_driver_active(interface):
            log.debug("Detaching kernel driver from interface %d", interface)
            dev.detach_kernel_driver(interface)
        usb.util.claim_interface(dev, interface)
        log.debug("Interface %d claimed", interface)
        return 0
    except usb.core.USBError as e:
        log.error("usb_claim_interface failed: %s", e)
        return _usb_errno(e)


def usb_release_interface(dev, interface: int = 0) -> int:
    """
    Release *interface* back to the OS.

    Returns 0 on success, negative errno on failure.
    """
    try:
        usb.util.release_interface(dev, interface)
        log.debug("Interface %d released", interface)
        return 0
    except usb.core.USBError as e:
        log.error("usb_release_interface failed: %s", e)
        return _usb_errno(e)


def usb_reset_device(dev) -> int:
    """
    Reset the USB device (bus reset).

    Returns 0 on success, negative errno on failure.
    """
    try:
        dev.reset()
        return 0
    except usb.core.USBError as e:
        log.error("usb_reset_device failed: %s", e)
        return _usb_errno(e)


# ---------------------------------------------------------------------------
# Bulk / interrupt endpoint I/O
# ---------------------------------------------------------------------------
def usb_bulk_read(dev, endpoint: int, size: int, timeout: int = 1000):
    """
    Read up to *size* bytes from a bulk IN *endpoint*.

    Returns a bytes object on success, or a negative errno int on failure.
    Timeouts (errno 110 / ETIMEDOUT) are logged at DEBUG level because they
    are normal during idle polling and should not clutter the terminal.
    """
    try:
        data = dev.read(endpoint, size, timeout)
        return bytes(data)
    except usb.core.USBError as e:
        err = _usb_errno(e)
        if abs(err) == _errno.ETIMEDOUT:
            log.debug("usb_bulk_read timed out (no data)")
        else:
            log.error("usb_bulk_read failed: %s", e)
        return err


def usb_interrupt_read(dev, endpoint: int, size: int, timeout: int = 1000):
    """
    Read up to *size* bytes from an interrupt IN *endpoint*.

    Same as usb_bulk_read — PyUSB uses the same dev.read() for both.
    Returns bytes on success, negative errno int on failure.
    """
    return usb_bulk_read(dev, endpoint, size, timeout)


def usb_get_max_packet_size(dev, endpoint: int) -> int:
    """
    Return the wMaxPacketSize for *endpoint* from the active configuration.
    Returns -1 if the endpoint is not found.
    """
    cfg = dev.get_active_configuration()
    for intf in cfg:
        for ep in intf:
            if ep.bEndpointAddress == endpoint:
                return ep.wMaxPacketSize
    return -1


def usb_bulk_write(dev, endpoint: int, data: bytes, timeout: int = 1000):
    """
    Write *data* to a bulk OUT *endpoint*.

    Returns number of bytes written, or negative errno int on failure.
    """
    try:
        n = dev.write(endpoint, data, timeout)
        return n
    except usb.core.USBError as e:
        log.error("usb_bulk_write failed: %s", e)
        return _usb_errno(e)
