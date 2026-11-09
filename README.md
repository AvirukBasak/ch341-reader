# CH341 Reader

A userspace program for directly interfacing with CH341 based modules over USB, bypassing kernel TTY drivers. Requires root access.

## What is CH341?

A CH341 generally refers to a series of UART-USB bridges manufactured by WCH (Nanjing Qinheng Microelectronics). It sits
between a UART speaking MCU/Chip/Device and a USB device (e.g. a PC) and translates b/w UART signals and USB packets.

Hence, the CH341 acts as the UART "host" and takes over the role of TX and RX instead. Since the UART device, which sits
"behind" the CH341 from the POV of a USB capable PC, is preprogrammed at a specific baud rate, the CH341 requires
configurations to determine at what rate to sample or generate UART signals for the UART capable device.

This and other configurations are handled by the userspace driver placed in this repo. Additional programs built on the userspace
driver handles things like reading data to stdout or files, and sending data to the UART capable device.

From now on, this doc will refer to the UART capable MCU/Chip/Device as a "device" and the CH341 will be referred to by its name.

## Why This Approach?

Generally we rely on a TTY node like `/dev/ttyUSB0` on Linux. Desktop linux ships with a `ch341` driver so when we
connect a UART device over CH341 to USB, it shows up as `/dev/ttyUSB*`. This hides the USB protcol and exposes a file
we can read or write to. Read acts like UART RX to the device, and write acts like UART TX. Hence, the driver has
abstracted away the interactions with the CH341 and we have direct access to the device.

Android doesn't have this driver (even on custom ROMs). Compiling a driver (ofc, rooted Android) is difficult because
of the exact kernel version header files needed (which IS available on desktop Linux).

This is where out userspace driver comes into play. It should work on all Linux systems and relies on PyUSB. However,
you need root access.

## Why is Root necessary?

Our userspace program (shell.py) takes control over USB (exclusive lock). Root is needed for this purpose. Closing this
program releases the USB device and immediately makes it available to other programs. PyUSB (libusb wrapper for Python)
is used for this. The CH341 implementation itself is ported into Python directly from the Linux kernel's source.

## Issue with `su` and `tsu` on Termux (rooted Android)

On Android, there are 3 ways to run as root, `su -c command`, `sudo command` and `tsu command`. Tsu is NOT sudo, even
though tsu claims to be sudo for compatibility. To install sudo, use `pkg install sudo`. Don't do `pkg install tsu`.
That installs tsu and makes sudo an alias to itself. Tsu does not work on new versions of magisk root. Sudo does.

While su works initially, on Ctrl+C, it returns to the shell instead of stopping the program or letting the program
handle the signal. If Ctrl+C is not received by the program, python will not raise `KeyboardInterrupt`, and the program
will keep printing messages and never release the lock even though user is returned to the shell. The only way to exit
that is to kill the terminal (close Termux). I have not yet understood why this happens.

## Setup

```shell
pip install pyusb   # the only dependency
```

---

## Usage: `read_to_stdout.py`

Streams UART data read from the UART device to `stdout`.
Enter Ctrl+C to stop logging.

### Flags

| Flag          | Short | Default   | Description                                            |
|---------------|-------|-----------|--------------------------------------------------------|
| `--baud`      | `-b`  | `115200`  | Baud rate                                              |
| `--vid`       | —     | `0x1a86`  | USB vendor ID                                          |
| `--pid`       | —     | `0x7523`  | USB product ID                                         |
| `--hex`       | —     | off       | Print bytes as hex (e.g. `de ad be ef`) instead of raw |
| `--read-size` | —     | `32`      | Max bytes per read call                                |
| `--timeout`   | —     | `1000` ms | Read timeout                                           |
| `--debug`     | —     | off       | Enable debug logging                                   |

**NOTE**: A longer `--read-size` occasionally causes timeouts. A shorter read size may not clear
the built-in buffer on the CH341, causing overwriting and data corruption. If the user-specified
`--read-size` is smaller than CH341's built-in configuration, it gets clamped to the built-in
configuration.

### Examples

```shell
# Basic: stream raw bytes at default 115200 baud
python read_to_stdout.py

# Different baud rate
python read_to_stdout.py --baud 9600

# Hex dump mode, use only if output is binary
python read_to_stdout.py --hex

# Non-default VID/PID with debug logging
python read_to_stdout.py --vid 0x1a86 --pid 0x7522 --debug

# Larger read chunks, longer timeout
python read_to_stdout.py --read-size 128 --timeout 2000

# Pipe raw output to a file
python read_to_stdout.py > output.bin
```

---

## Usage: `read_to_file.py`

Reads data continuously from the device and writes to hourly files.
Automatically switches to a new file at the start of each hour.

Everything from [Usage: `read_to_stdout.py`](#usage-read_to_stdoutpy) applies.

**NOTES**:
- Send Ctrl+C or SIGTERM to stop reading.
- Please DO NOT send SIGKILL manually.
- Deleting a file being written to will not cause error and write will continue.
- But the file will become irrecoverable once the program closes it (classic Linux inodes).

### Flags

| Flag          | Short | Default   | Description                                            |
|---------------|-------|-----------|--------------------------------------------------------|
| `--baud`      | `-b`  | `115200`  | Baud rate                                              |
| `--vid`       | —     | `0x1a86`  | USB vendor ID                                          |
| `--pid`       | —     | `0x7523`  | USB product ID                                         |
| `--hex`       | —     | off       | Print bytes as hex (e.g. `de ad be ef`) instead of raw |
| `--read-size` | —     | `32`      | Max bytes per read call                                |
| `--timeout`   | —     | `1000` ms | Read timeout                                           |
| `--debug`     | —     | off       | Enable debug logging                                   |

### Additional Flags

| Flag          | Short | Default   | Description                                            |
|---------------|-------|-----------|--------------------------------------------------------|
| `--prefix`    | `-p`  | none      | Filename prefix                                        |
| `--suffix`    | `-s`  | none      | Filename suffix                                        |
| `--extension` | `-e`  | none      | Filename extension (without leading dot)               |

### Examples

```shell
# Basic: write raw bytes to timestamped files
python tools/read_to_file.py

# With prefix and suffix, produces file names like: gps-2026.10.11-02.48.41-raw
python tools/read_to_file.py -p gps -s raw

# With extension, produces file names like: gps-2026.10.11-02.48.41.bin
python tools/read_to_file.py -p gps -e bin

# All three, produces file names like: gps-2026.10.11-02.48.41-nmea.bin
python tools/read_to_file.py -p gps -s nmea -e bin

# Hex dump mode, use only if output is binary
python tools/read_to_file.py -p gps -e hex --hex

# Different baud rate
python tools/read_to_file.py --baud 9600

# Non-default VID/PID, with debug logging
python tools/read_to_file.py --vid 0x1a86 --pid 0x7522 --debug

# Stop gracefully (flushes and closes current file)
kill -SIGTERM <pid> # You can also use Ctrl+C or SIGINT
```

---

## Interactive Shell

The previous scripts only read from the UART device.
The interactive shell provides a prompt to read and additionally send (`write`) data to the device.
This is useful when the device accepts messages via UART TX for configurations or something else.

```shell
sudo ./shell.py
```

- On launch you get a `>>` prompt, meaning no CH341 connection is open.
- Run `open` to open a USB connection to a CH341, which gives a `[vid]:[pid]>` prompt.
- Anything starting with `[INFO]`, `[WARNING]` or `[ERROR]` is from the program.
- Any `>` prompt is to give a command to the program.

---

### Connection Commands

#### `open`

```
open [vid:pid]
```

- Opens a USB connection to a plugged in CH341.
- If no `vid:pid` is given, a default device is used: `VID=0x1a86 PID=0x7523`.
- If a connection is already open, it is closed first and then reopened.
- Default baud rate for the CH341 is `115200`.

#### `close`

```
close
```

- Releases the USB connection.
- If no connection is open, does nothing.

---

### Configuration Commands

#### `set baud`

Configures the shell and **passes the configuration to the CH341**.
This does NOT configure the UART device.

```
set baud <value>
```

Sets the baud rate. Default is `115200`.

#### `set read-mode`

Configures the **shell only**.

```
set read-mode <txt|hex>
```

- Sets the default read mode.
- Used by both `read` and the automatic read loop on `write`.
- `txt` streams output as text.
- `hex` outputs one line of space-separated hex bytes per read (e.g. `de ad be ef`).
- Default is `txt`. Useful for byte-level inspection.

#### `set read-size`

Configures the shell and **passes the configuration to the CH341**.
This does NOT configure the UART device.

```
set read-size <bytes>
```

- Sets the read buffer size in bytes.
- Too small and the CH341 buffer will overflow; too large and reads occasionally time out.
- On open, this is automatically queried from the CH341.
- If user-specified size is smaller than CH341's built-in configuration, it'll be clamped to the built-in configuration.

---

### Reading

#### `read`

```
read
read <read-size> <read-mode>
```

- Reads from the UART device in a continuous loop to `stdout`.
- Uses the configured `read-mode` and `read-size` unless overridden by inline args.
- Press Ctrl+C to stop reading and return to the prompt.

---

### Writing

#### `write`

```
write <payload> [write-mode=txt] [write-template=raw]
```

| Argument         | Options                       | Default |
|------------------|-------------------------------|---------|
| `write-mode`     | `txt`, `hex`                  | `txt`   |
| `write-template` | `raw` or a loaded driver name | `raw`   |

- Sends `payload` to the UART device.
- After writing, a read loop starts automatically.
- Press Ctrl+C to stop it and return to the prompt.
- ACK / NACK / Response will show up after you press Ctrl+C (depends on the `write-template` driver).

**Payload**:

- Spaces are not supported in the payload (hex or txt).
- For hex, use `_` as a separator instead.
- Underscores in hex payloads are ignored and allowed for readability.
- Python's escape sequences are supported in `txt` mode.
- To include a space character in `txt` mode, use `\x20`.

**Write Mode**:

-`write-mode` controls how the payload is interpreted: as text (with escape sequences supported) or as hex bytes.

**Write Template**:

- `write-template` controls framing by routing the payload through a "driver" module.
- `raw` sends the payload as-is with no framing.
- Using a driver wraps the payload in that driver's protocol frame before sending.
- Driver modules are used when the UART device itself supports a binary config protocol.

#### Examples

**Send a text string (escape sequences supported):**

```shell
write hello\n
```

**Send raw bytes with no framing:**

```shell
write 0a_0b_0c_0d_0e_0f hex raw
```

**Send command to restart a SkyTraQ PX1125S-01A device:**

```shell
write $restart txt phoenix
```

See [`docs/drivers/PX1125S-01A.md`](docs/drivers/PX1125S-01A.md) for the full list of supported commands.

---

### Driver Commands

#### `load`

```
load <path>
```
Loads a driver from a `.py` file. Drivers provide write framing and optional ACK/NACK/Output handling.

#### `lsdrv`

```
lsdrv
```

Lists all currently loaded drivers. By default the following drivers will be loaded:
- `drivers/raw.py`
- `drivers/PX1125S01A.py`

To disable a stock driver from autoloading, edit the function `load_stock_drivers()` in [`shell.py`](shell.py).

---

### Exiting

- Press Ctrl+C or Ctrl+D at the `>>` or `[vid]:[pid]>` prompt to exit.
- Ctrl+C inside a `read` or `write` loop stops the loop and returns to the prompt; it does not exit the shell.

---

### Drivers System

Write templates are handled by driver modules. All drivers are loaded from `.py` files.

Stock drivers ship in `drivers/` and some of these may be auto-loaded on start.
To disable a stock driver, edit the function `load_stock_drivers()` in `shell.py`.

Presently, the following drivers are bundled:

| Driver file             | `CH341RDR_NAME` | Device                                                           |
|-------------------------|-----------------|------------------------------------------------------------------|
| `drivers/raw.py`        | `raw`           | Pass-through, no framing                                         |
| `drivers/PX1125S01A.py` | `phoenix`       | [SkyTraQ PX1125S-01A GNSS receiver](docs/drivers/PX1125S-01A.md) |

To write a custom driver, see `drivers/raw.py` for the required exports (`CH341RDR_NAME`, `ch341rdr_write`, `ch341rdr_onwrite`).
