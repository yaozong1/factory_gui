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
    - 控制口(治具): USB-Serial-JTAG
    - 刷机口(DUT): CH340 对应的 COM 口
3) Click "烧录并等待" and choose the ESP-IDF build file `flash_project_args` inside your DUT project's `build/` folder.
    - GUI sends `!BOOT` on 控制口 to enter download mode, runs `python -m esptool ... write_flash @flash_project_args` on 刷机口（在 `flash_project_args` 所在的 build 目录下执行，确保相对路径生效），然后发送 `!RUN` 并重连刷机口读取输出。
4) The GUI waits up to 40s for a `SELFTEST SUMMARY: { ... }` JSON and shows a popup once received.

Tips:
- DTR/RTS wiring is not required: the jig drives EN/IO0 via `!BOOT`/`!RUN` on 控制口.
- If your build directory changes, re-pick the latest `flash_project_args`.

## Standalone diagnostics (step-by-step)

Use these buttons to isolate issues:

1) 进下载(!BOOT): sends `!BOOT` on 控制口 so DUT enters bootloader (IO0=0 + EN pulse). Check for `JIG: BOOT OK`.
2) esptool chip_id: runs `python -m esptool -vv --trace --before no-reset --after no-reset chip_id` on 刷机口; verifies ROM handshake without flashing（带 25s 超时保护）。
3) 仅刷写(esptool): runs `write_flash @flash_project_args` on 刷机口 without toggling EN/IO0（在 build 目录运行，启用 stub）。
4) 运行(!RUN): sends `!RUN` on 控制口 (IO0=1 + EN pulse) to start application.

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

## Next steps

- Wire up 8-channel ADC (ADS1115x2 or MCP3208) into the jig and stream values to GUI
- Add a button/flow to automate flashing (call `idf.py -p COMx flash`) if desired
- Add CSV/JSON export for test records
- Optional: color rows in voltage table by thresholds
