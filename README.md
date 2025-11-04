# PE Board Factory GUI (Preview)

Minimal Python GUI to monitor DUT self-test results over a serial port and preview 8-channel voltage placeholders.

## Features

- Serial port selection (Windows COMx)
- Connect/Disconnect and live log view
- Parses `SELFTEST SUMMARY: { ... }` JSON from DUT and updates PASS/FAIL indicators
- 8-channel voltage table (placeholder; to be wired to your jig ADC later)
- Simulate button to demo UI without hardware
- One-click flash-and-wait: pick ESP-IDF build's `flash_project_args`, flash DUT via CDC COM, then auto-reconnect and wait for self-test JSON

## Requirements

- Python 3.10+
- Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Note: `esptool` is used under the hood to flash DUT via the CDC bridge.

## Run

```powershell
python .\factory_gui.py
```

## Flash-and-wait workflow

1) Connect your jig so that Windows enumerates two COM ports:
    - USB-Serial-JTAG (ESP32-S3 default console) — for jig logs
    - TinyUSB CDC (your CDC↔UART bridge) — this is the port to talk to DUT U0TXD/U0RXD
2) In GUI, select the CDC COM and click "连接" if you want to monitor live output.
3) Click "烧录并等待" and choose the ESP-IDF build file `flash_project_args` inside your DUT project's `build/` folder.
    - The GUI will close the serial, run `python -m esptool ... write_flash @flash_project_args`, then reconnect.
4) The GUI waits up to 40s for a `SELFTEST SUMMARY: { ... }` JSON and shows a popup once received.

Tips:
- Ensure the CDC bridge maps DTR/RTS to DUT EN/IO0 so esptool can auto-enter bootloader.
- If your build directory changes, re-pick the latest `flash_project_args`.

## Expected DUT output shape

GUI expects lines containing:

```
SELFTEST SUMMARY:
{ "eg915_ok": true,
  "motion": { "ok": true, "mag": 1.23 },
  "rs485": { "inited": true, "written": 8, "rx_bytes": 8, "pass": true },
  "can":   { "inited": true, "started": true, "state": 1, "pass": true },
  "gnss":  { "uart_ok": true, "bytes": 64 },
  "battery": { "ok": true, "voltage": 3.95 }
}
```

The regex is lenient to whitespace/newlines and only requires `SELFTEST SUMMARY:` followed by a JSON object block.

## Next steps

- Wire up 8-channel ADC (ADS1115x2 or MCP3208) into the jig and stream values to GUI
- Add a button/flow to automate flashing (call `idf.py -p COMx flash`) if desired
- Add CSV/JSON export for test records
- Optional: color rows in voltage table by thresholds
