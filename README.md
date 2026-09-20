# README

```
# Basic — stream raw bytes at default 115200
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
