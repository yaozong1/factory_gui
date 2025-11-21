# Factory GUI Setup Guide

## Step 1: Install Python

1. Download Python 3.8 or higher from: https://www.python.org/downloads/
2. Run the installer
3. **Important**: Check "Add Python to PATH" during installation
4. Click "Install Now"
5. Verify installation:
   ```bash
   python --version
   ```
   Should output: `Python 3.x.x`

---

## Step 2: Install pip (Python Package Manager)

```bash
python -m ensurepip --upgrade
```

Verify pip installation:
```bash
pip --version
```
Should output: `pip x.x.x from ...`

---

## Step 3: Install Required Dependencies

```bash
pip install PySide6
pip install pyserial
pip install esptool
```

**Package details:**
- `PySide6` - Qt6 GUI framework for Python
- `pyserial` - Serial port communication library
- `esptool` - ESP32 flash programming tool

---

## Step 4: Verify Installation

Check if packages are installed correctly:

```bash
python -c "import PySide6; import serial; print('OK')"
esptool.py version
```

Should output: `OK` and esptool version information

---

## Step 5: Run the GUI

```bash
python .\tools\factory_gui\factory_gui.py
```
