# PE Board Factory GUI (Preview)

Minimal Python GUI to monitor DUT self-test results over a serial port and preview 8-channel voltage placeholders.

## Features

- Serial port selection (Windows COMx)
- Connect/Disconnect and live log view
- Parses `SELFTEST SUMMARY: { ... }` JSON from DUT and updates PASS/FAIL indicators
- 8-channel voltage table (placeholder; to be wired to your jig ADC later)
- Simulate button to demo UI without hardware
- One-click flash-and-wait: pick ESP-IDF build's `flash_project_args`, flash DUT via CDC COM, then auto-reconnect and wait for self-test JSON (uses esptool stub; no `--no-stub`)

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
    - USB-Serial-JTAG (ESP32-S3 default console) — control port for jig commands and logs
    - CH340 (external USB-UART) — flash port bridged via S3 UART to DUT U0TXD/U0RXD
2) In GUI, select:
    - Control Port (Jig): USB-Serial-JTAG
    - Flash Port (DUT): CH340 corresponding COM port
3) Click "Test" button and choose the ESP-IDF build file `flash_project_args` inside your DUT project's `build/` folder.
    - GUI sends `!BOOT` on control port to enter download mode, runs `python -m esptool ... write_flash @flash_project_args` on flash port (executed in the build directory where `flash_project_args` is located to ensure relative paths work), then sends `!RUN` and reconnects to flash port to read output.
4) The GUI waits up to 20s for a `SELFTEST SUMMARY: { ... }` JSON and shows a popup once received.

Tips:
- DTR/RTS wiring is not required: the jig drives EN/IO0 via `!BOOT`/`!RUN` on control port.
- If your build directory changes, re-pick the latest `flash_project_args`.

## Standalone diagnostics (step-by-step)

Use these buttons to isolate issues:

1) Enter Download (!BOOT): sends `!BOOT` on control port so DUT enters bootloader (IO0=0 + EN pulse). Check for `JIG: BOOT OK`.
2) esptool chip_id: runs `python -m esptool -vv --trace --before no-reset --after no-reset chip_id` on flash port; verifies ROM handshake without flashing (with 25s timeout protection).
3) Flash Only (esptool): runs `write_flash @flash_project_args` on flash port without toggling EN/IO0 (executed in build directory, stub enabled).
4) Run (!RUN): sends `!RUN` on control port (IO0=1 + EN pulse) to start application.

You can perform 1 → 2 → 3 → 4 to pinpoint whether the problem is entering bootloader, esptool connectivity, or flashing itself.

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
