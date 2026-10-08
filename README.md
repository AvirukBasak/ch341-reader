# CH340C Reader

A userspace program for directly interfacing with CH340C modules over USB, bypassing kernel TTY drivers.

## Why This Approach?

Desktop Linux ships with a `ch341` driver - when you connect a device with CH340C over USB, it shows up as `/dev/ttyUSB*`. This hides the USB protocol and exposes a file you can read or write to. Read acts like UART RX, write like TX.

However, Android doesn't have this driver (even on custom ROMs). This userspace program fills that gap: it works on both desktop Linux and rooted Android (via Termux), taking direct control over USB using PyUSB (a libusb wrapper for Python). The CH340C implementation itself is ported into Python directly from the Linux kernel's source.

## Why Root Is Necessary

`shell.py` takes control over USB (exclusive lock). Root is needed for this purpose. Closing the program releases the device and immediately makes it available.

On Android (Termux), there are 3 ways to run as root: `su -c command`, `sudo command`, and `tsu command`. **Tsu is NOT sudo**, even though tsu claims to be sudo for compatibility. To install sudo, use `pkg install sudo`. Don't do `pkg install tsu` - that installs tsu and makes sudo an alias to itself. Tsu does not work on new versions of Magisk root. Sudo does.

While `su` works initially, on Ctrl+C it returns to the shell instead of stopping the program or letting the program handle the signal. If Ctrl+C is not received by the program, Python will not raise `KeyboardInterrupt`, and the program will keep running and never release the lock even though the user is returned to the shell. The only way to exit that state is to kill the terminal (close Termux).

## Setup

```shell
pip install pyusb   # the only dependency
sudo ./shell.py     # needs root to directly access USB device
```

## Usage - `archived/reader.py`

NOTE: This is the old reader. Use the interactive reader (see below).

```shell
# Basic - stream raw bytes at default 115200
python archived/reader.py

# Different baud rate
python archived/reader.py --baud 9600

# Hex dump mode (one line of "de ad be ef" per read)
python archived/reader.py --hex

# Non-default VID/PID, with debug logging
python archived/reader.py --vid 0x1a86 --pid 0x7522 --debug

# Pipe raw output to a file
python archived/reader.py > output.bin
```

## Interactive Shell

Use `shell.py` for an interactive session. This command restarts the chip.

```shell
> open
> write 01 hex sktrq-px
.........
^C
[INFO] shell.py: Interrupted
[INFO] ACK: command id: 0x1
```

### Main Commands

```shell
open [vid:pid]               # Default: CH341 VID/PID
close                        # Release device
set read-mode hex            # Read as `hex` unless specified
read                         # Read, read-size and read-mode are `set`
read [read-size] [read-mode] # Read in chunks of `read-size`, as either `txt` or `hex`
set baud 115200              # Default baud, same as the SkyTraQ chip
help                         # List of commands
```

### Write Command

```shell
write [payload] [write-mode=txt] [write-template=raw]
```

| Argument         | Options                           | Default |
|------------------|-----------------------------------|---------|
| `write-mode`     | `txt`, `hex`                      | `txt`   |
| `write-template` | `raw` or user defined driver name | `raw`   |

**Examples:**

Send a text string (escape sequences supported)

```shell
write hello\n
```

Send bytes, no protocol, so `raw` (_ is ignored, allowed for readability)

```shell
write 0a_0b_0c_0d_0e_0f hex raw
```

Reset PX1125S-01A chip (hex payload, use `phoenix` for protocol frame). This chip
has stock support.

```shell
write 01 hex phoenix
```

### Reading the Output

- Anything starting with `[INFO]` or `[ERROR]` is from the program. Any `>` prompt is to give a command to the program.
- First run `open` after launching `sudo ./shell.py`. This gives a `[vid]:[pid]>` prompt.
- Run `read`. Press Ctrl+C to stop reading and return to the `[vid]:[pid]>` prompt.
- On `write`, a read loop starts automatically. ACK / NACK / failure information will show up after you press Ctrl+C.
- ACKs are dependent on the UART device and are implemented by user defined driver (see `drivers/*.py`).

### Driver System

Write templates are handled by driver modules. All drivers are loaded from `.py` files.

```shell
load drivers/phoenix.py   # load a driver
lsdrv                     # list loaded drivers
```

Stock drivers ship in `drivers/` and get auto-loaded on start:

| Driver file             | `CH341RDR_NAME` | Device                            |
|-------------------------|-----------------|-----------------------------------|
| `drivers/raw.py`        | `raw`           | Pass-through, no framing          |
| `drivers/PX1125S01A.py` | `phoenix`       | SkyTraQ PX1125S-01A GNSS receiver |

Stock drivers are loaded automatically on startup. To write a custom driver, see `drivers/raw.py`
for the required exports (`CH341RDR_NAME`, `ch341rdr_write`, `ch341rdr_onwrite`).

NOTE: To disable a stock driver, edit the function: `shell.py -> load_stock_drivers()`.

## Example: Enabling `$GNGST` Sentences

The GNSS receiver module we worked with has 3 layers in general:

1. **The Antenna** - the large ceramic patch antenna (possibly)
2. **SkyTraQ PX1125S-01A  MCU** - a.k.a "phoenix", processes the GNSS signals, produces NMEA sentences, communicates via UART.
3. **CH340C UART-USB** - takes NMEA from the MCU and exposes a USB interface for a PC

The MCU has its own binary protocol. You can send these commands over USB, going via the CH340C chip. This program handles the framing for you.

### Example

`$GNGST` reports error statistics directly from the GNSS receiver. Position error is generally CEP, measured in meters - a circle within which 50% of fixes land. The error is the circle's radius. A larger circle is worse.

```shell
# Ephemeral (lost on power cycle), last byte 00
write 64_02_01_01_03_01_01_01_01_00_00_00_00_01_00 hex phoenix

# Persistent (written to FLASH), last byte 01
write 64_02_01_01_03_01_01_01_01_00_00_00_00_01_01 hex phoenix
```

A successful ACK echoes back the message IDs of the command (e.g. `[INFO] shell.py: ACK payload: 64 02` for a `64 02` command).
