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

## Usage - `read_to_stdout.py`

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

## Usage - `read_to_file.py`

Reads data continuously from the device via CH341 and writes to hourly files. Automatically switches to a new file at the top of each hour.

```shell
# Basic - write raw bytes to timestamped files
python tools/read_to_file.py

# With prefix and suffix
python tools/read_to_file.py -p mydevice -s raw

# With extension
python tools/read_to_file.py -p gps -e bin

# All three - produces e.g. gps-2026.10.11-02.48.41-raw.bin
python tools/read_to_file.py -p gps -s raw -e bin

# Hex dump mode
python tools/read_to_file.py -p gps -e hex --hex

# Different baud rate
python tools/read_to_file.py --baud 9600

# Non-default VID/PID, with debug logging
python tools/read_to_file.py --vid 0x1a86 --pid 0x7522 --debug

# Stop gracefully (flushes and closes current file)
kill -SIGTERM <pid> # You can also use Ctrl+C or SIGINT
```

## Interactive Shell

Launch the shell with:

```shell
sudo ./shell.py
```

- On launch you get a `>>` prompt, meaning no device is open.
- Run `open` to open a device, which gives a `[vid]:[pid]>` prompt.
- Anything starting with `[INFO]` or `[ERROR]` is from the program.
- Any `>` prompt is to give a command to the program.

### Device Commands

#### `open`

```
open [vid:pid]
```

- Opens a USB device. Default: `VID=0x1a86 PID=0x7523`, baud `115200`.
- If no `vid:pid` is given, the default is used.
- If a device is already open, it is closed first and then reopened.

#### `close`

```
close
```

- Releases the USB device.
- If no device is open, does nothing.

### Configuration Commands

#### `set baud`

```
set baud <value>
```

- Sets the baud rate. Default is `115200`.

#### `set read-mode`

```
set read-mode <txt|hex>
```

- Sets the default read mode.
- `txt` streams output as text.
- `hex` outputs one line of space-separated hex bytes per read (e.g. `de ad be ef`).
- Used by both `read` and the automatic read loop on `write`.
- Default is `hex`. Useful for byte-level inspection.

#### `set read-size`

```
set read-size <bytes>
```

- Sets the read buffer size in bytes.
- On open, this is automatically queried from the device over USB.
- Too small and the device buffer will overflow; too large and reads may time out.
- Best left untouched.

### Reading

#### `read`

```
read
read <read-size> <read-mode>
```

- Reads from the device.
- Uses the current `read-size` and `read-mode` defaults unless overridden inline.
- Press Ctrl+C to stop reading and return to the prompt.

### Writing

#### `write`

```
write <payload> [write-mode=txt] [write-template=raw]
```

| Argument         | Options                       | Default |
|------------------|-------------------------------|---------|
| `write-mode`     | `txt`, `hex`                  | `txt`   |
| `write-template` | `raw` or a loaded driver name | `raw`   |

Sends `payload` to the device. After writing, a read loop starts automatically. Press Ctrl+C to stop it and return to the prompt. ACK / NACK / Device Response will show up after you press Ctrl+C (if the loaded driver supports it).

`write-mode` controls how the payload is interpreted: as text (with escape sequences supported) or as hex bytes. Underscores in hex payloads are ignored and allowed for readability.

Spaces are not supported in the payload (hex or txt). For hex, use `_` as a separator instead. To include a space character in a txt payload, use `\x20`.

`write-template` controls framing. `raw` sends the payload as-is with no framing. A driver name wraps the payload in that driver's protocol frame before sending.

**Send a text string (escape sequences supported):**

```shell
write hello\n
```

**Send raw bytes with no framing:**

```shell
write 0a_0b_0c_0d_0e_0f hex raw
```

**Send a command to the SkyTraQ PX1125S-01A (reset):**

```shell
write 01 hex phoenix
```

See [`docs/drivers/PX1125S-01A.md`](docs/drivers/PX1125S-01A.md) for the full list of supported commands.

### Driver Commands

#### `load`

```
load <file>
```
Loads a driver from a `.py` file. Drivers provide write framing and optional ACK/NACK handling.

#### `lsdrv`

```
lsdrv
```

Lists all currently loaded drivers. By default the following drivers will be loaded:
- `drivers/raw.py`
- `drivers/PX1125S01A.py`

To disable a stock driver, edit the function `load_stock_drivers()` in [`shell.py`](shell.py).

### Exiting

- Press Ctrl+C or Ctrl+D at the `>>` or `[vid]:[pid]>` prompt to exit.
- Ctrl+C inside a `read` or `write` loop stops the loop and returns to the prompt; it does not exit the shell.

### Drivers System

Write templates are handled by driver modules. All drivers are loaded from `.py` files.

Stock drivers ship in `drivers/` and some of these may be auto-loaded on start. To disable a stock driver, edit the function `load_stock_drivers()` in `shell.py`.

Presently, the following drivers are present:

| Driver file             | `CH341RDR_NAME` | Device                                                           |
|-------------------------|-----------------|------------------------------------------------------------------|
| `drivers/raw.py`        | `raw`           | Pass-through, no framing                                         |
| `drivers/PX1125S01A.py` | `phoenix`       | [SkyTraQ PX1125S-01A GNSS receiver](docs/drivers/PX1125S-01A.md) |

To write a custom driver, see `drivers/raw.py` for the required exports (`CH341RDR_NAME`, `ch341rdr_write`, `ch341rdr_onwrite`).
