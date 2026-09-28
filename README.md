# README

```
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
```
A0 A1 [size:2B] [payload:size] [cs:1B] 0D 0A
[size] unit is in bytes, stored big endian
[cs] is just xor of everything, likely a simple xor checksum
payload contains actual command, see datasheet
```

To use write, you only need the `[payload]`, rest is set automatically.
