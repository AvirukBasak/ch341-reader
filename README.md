# README

```shell
# Basic - stream raw bytes at default 115200
python main.py

# Different baud rate
python main.py --baud 9600

# Hex dump mode (one line of "de ad be ef" per read)
python main.py --hex

# Non-default VID/PID, with debug logging
python main.py --vid 0x1a86 --pid 0x7522 --debug

# Pipe raw output to a file
python main.py > output.bin
```

Alternatively you can use the `shell.py` script for an interactive version. You can use the `write` command to send payloads:

## Interactive Shell

Use `shell.py` for an interactive session:

```shell
> open
> write 01 hex sktrq-px
.........
^C
[INFO] shell.py: Interrupted
[INFO] ACK: command id: 0x1
```

### Write Command

```shell
write [payload] [write-mode=txt] [write-template=raw]
```

| Argument | Options | Default |
|---|---|---|
| `write-mode` | `txt`, `hex` | `txt` |
| `write-template` | `raw`, `sktrq-px` | `raw` |

**Examples:**

```shell
# Reset SkyTraq Phoenix chip (hex payload, sktrq-px framing)
write 01 hex sktrq-px

# Send a raw text string (escape sequences supported)
write hello\n
```

The `sktrq-px` template automatically wraps your `[payload]` in the SkyTraq binary frame:
```
A0 A1 [size:2B] [payload] [cs:1B] 0D 0A
```
Checksum (simple XOR) `cs` and size are auto-computed.

## Other Commands

```shell
open [vid:pid] # Default: CH341 VID/PID
close
read [read-size] [read-mode]
set baud 9600
set read-mode hex
help
```
