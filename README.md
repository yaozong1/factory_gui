# PE Board Factory GUI (Preview)

Minimal Python GUI to monitor DUT self-test results over a serial port and preview 8-channel voltage placeholders.

## Features

- Serial port selection (Windows COMx)
- Connect/Disconnect and live log view
- Parses `SELFTEST SUMMARY: { ... }` JSON from DUT and updates PASS/FAIL indicators
- 8-channel voltage table (placeholder; to be wired to your jig ADC later)
- Simulate button to demo UI without hardware

## Requirements

- Python 3.10+
- Install dependencies:

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run

```powershell
python .\factory_gui.py
```

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
