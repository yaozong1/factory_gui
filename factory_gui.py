import sys
import re
import json
import time
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QGroupBox, QTableWidget,
    QTableWidgetItem, QMessageBox
)

try:
    import serial
    import serial.tools.list_ports as list_ports
except Exception as e:
    serial = None
    list_ports = None


SUMMARY_MARK = "SELFTEST SUMMARY:"

def extract_json_blocks(buf: str):
    blocks = []
    i = 0
    n = len(buf)
    while i < n:
        k = buf.find(SUMMARY_MARK, i)
        if k < 0:
            break
        # 找到 '{'
        j = buf.find('{', k)
        if j < 0:
            break
        depth = 0
        in_str = False
        esc = False
        end = -1
        for t in range(j, n):
            ch = buf[t]
            if in_str:
                if esc:
                    esc = False
                elif ch == '\\':
                    esc = True
                elif ch == '"':
                    in_str = False
                # 字符串内不计括号
            else:
                if ch == '"':
                    in_str = True
                elif ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        end = t + 1
                        break
        if end > 0:
            blocks.append((k, end, buf[j:end]))
            i = end
        else:
            # 未闭合，保留剩余等待下次补全
            break
    return blocks


@dataclass
class SelftestResult:
    eg915_ok: bool = False
    motion_ok: bool = False
    motion_mag: float = 0.0
    rs485_pass: bool = False
    rs485_written: int = -1
    rs485_rx_bytes: int = 0
    can_pass: bool = False
    can_state: int = -1
    gnss_ok: bool = False
    gnss_bytes: int = 0
    battery_ok: bool = False
    battery_v: float = 0.0

    @property
    def overall(self) -> bool:
        return all([
            self.eg915_ok,
            self.motion_ok,
            self.rs485_pass,
            self.can_pass,
            self.gnss_ok,
            self.battery_ok,
        ])


class SerialClient:
    def __init__(self):
        # 串口对象（运行时赋值）
        self.ser = None
        self.buffer = ""

    @staticmethod
    def list_ports():
        if list_ports is None:
            return []
        return [p.device for p in list_ports.comports()]

    def open(self, port: str, baud: int = 115200) -> bool:
        if serial is None:
            return False
        try:
            # 使用一个很小的超时，提升兼容性（部分 CDC/Windows 驱动下 in_waiting 表现异常）
            self.ser = serial.Serial(port=port, baudrate=baud, timeout=0.05)
            self.buffer = ""
            return True
        except Exception:
            self.ser = None
            return False

    def close(self):
        try:
            if self.ser:
                self.ser.close()
        finally:
            self.ser = None

    def read_lines(self) -> str:
        if not self.ser:
            return ""
        try:
            # 直接定长尝试读取，配合小超时，避免依赖 in_waiting 在某些驱动上的不一致行为
            data = self.ser.read(4096)
            if not data:
                return ""
            text = data.decode(errors='ignore')
            return text
        except Exception:
            return ""


class StatusLabel(QLabel):
    def set_state(self, ok: bool, text: Optional[str] = None):
        self.setText(text or ("PASS" if ok else "FAIL"))
        palette = self.palette()
        color = QColor(0, 160, 0) if ok else QColor(200, 0, 0)
        self.setStyleSheet(f"color: rgb({color.red()},{color.green()},{color.blue()}); font-weight: bold;")


class FactoryGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PE-Board Factory GUI (Preview)")
        self.resize(980, 680)

        self.serial = SerialClient()
        self.timer = QTimer(self)
        self.timer.setInterval(50)  # 20Hz 轮询串口
        self.timer.timeout.connect(self.on_tick)

        self._build_ui()
        self._refresh_ports()

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 顶部：串口控制
        top = QHBoxLayout()
        self.port_combo = QComboBox()
        self.refresh_btn = QPushButton("刷新串口")
        self.connect_btn = QPushButton("连接")
        self.disconnect_btn = QPushButton("断开")
        self.simulate_btn = QPushButton("模拟JSON")
        self.disconnect_btn.setEnabled(False)
        top.addWidget(QLabel("串口:"))
        top.addWidget(self.port_combo, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        top.addWidget(self.simulate_btn)
        root.addLayout(top)

        # 中部：结果面板
        panel = QGridLayout()
        box = QGroupBox("自测结果")
        box.setLayout(panel)
        root.addWidget(box)

        r = 0
        self.lbl_overall = StatusLabel("-")
        panel.addWidget(QLabel("OVERALL"), r, 0)
        panel.addWidget(self.lbl_overall, r, 1)
        r += 1

        self.lbl_eg915 = StatusLabel("-")
        panel.addWidget(QLabel("EG915"), r, 0)
        panel.addWidget(self.lbl_eg915, r, 1)
        r += 1

        self.lbl_motion = StatusLabel("-")
        self.lbl_motion_mag = QLabel("mag=0.000")
        panel.addWidget(QLabel("Motion"), r, 0)
        panel.addWidget(self.lbl_motion, r, 1)
        panel.addWidget(self.lbl_motion_mag, r, 2)
        r += 1

        self.lbl_rs485 = StatusLabel("-")
        self.lbl_rs485_info = QLabel("-")
        panel.addWidget(QLabel("RS485"), r, 0)
        panel.addWidget(self.lbl_rs485, r, 1)
        panel.addWidget(self.lbl_rs485_info, r, 2)
        r += 1

        self.lbl_can = StatusLabel("-")
        self.lbl_can_info = QLabel("-")
        panel.addWidget(QLabel("CAN"), r, 0)
        panel.addWidget(self.lbl_can, r, 1)
        panel.addWidget(self.lbl_can_info, r, 2)
        r += 1

        self.lbl_gnss = StatusLabel("-")
        self.lbl_gnss_info = QLabel("-")
        panel.addWidget(QLabel("GNSS UART"), r, 0)
        panel.addWidget(self.lbl_gnss, r, 1)
        panel.addWidget(self.lbl_gnss_info, r, 2)
        r += 1

        self.lbl_bat = StatusLabel("-")
        self.lbl_bat_v = QLabel("V=0.00V")
        panel.addWidget(QLabel("Battery/ADC"), r, 0)
        panel.addWidget(self.lbl_bat, r, 1)
        panel.addWidget(self.lbl_bat_v, r, 2)
        r += 1

        # 右侧/下方：8路电压占位
        vbox2 = QVBoxLayout()
        self.volt_table = QTableWidget(8, 3)
        self.volt_table.setHorizontalHeaderLabels(["Channel", "Voltage(V)", "Status"])
        for i in range(8):
            self.volt_table.setItem(i, 0, QTableWidgetItem(f"CH{i+1}"))
            self.volt_table.setItem(i, 1, QTableWidgetItem("-"))
            self.volt_table.setItem(i, 2, QTableWidgetItem("-"))
        vbox2.addWidget(QLabel("8x Voltage (placeholder)"))
        vbox2.addWidget(self.volt_table)
        root.addLayout(vbox2)

        # 底部：日志窗口
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(QLabel("串口日志"))
        root.addWidget(self.log, 1)

        # 绑定事件
        self.refresh_btn.clicked.connect(self._refresh_ports)
        self.connect_btn.clicked.connect(self._on_connect)
        self.disconnect_btn.clicked.connect(self._on_disconnect)
        self.simulate_btn.clicked.connect(self._on_simulate)

    def _refresh_ports(self):
        self.port_combo.clear()
        ports = self.serial.list_ports()
        for p in ports:
            self.port_combo.addItem(p)
        if not ports:
            self.port_combo.addItem("<无串口>")

    def _on_connect(self):
        port = self.port_combo.currentText()
        if not port or port.startswith("<"):
            QMessageBox.warning(self, "连接失败", "未发现可用串口")
            return
        ok = self.serial.open(port, 115200)
        if not ok:
            QMessageBox.critical(self, "连接失败", f"无法打开串口: {port}")
            return
        self.timer.start()
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self._append_log(f"[INFO] Connected {port} @115200\n")

    def _on_disconnect(self):
        self.timer.stop()
        self.serial.close()
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self._append_log("[INFO] Disconnected\n")

    def _append_log(self, text: str):
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    def on_tick(self):
        chunk = self.serial.read_lines()
        if not chunk:
            return
        self._append_log(chunk)
        self.serial.buffer += chunk
        # 查找并解析 JSON 摘要
        # 使用括号平衡算法抽取完整 JSON 块，避免正则在嵌套时截断
        blocks = extract_json_blocks(self.serial.buffer)
        for _, end_pos, json_text in blocks:
            # 清理不可见/非 ASCII 控制字符，避免解析失败
            cleaned = re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "", json_text)
            try:
                data = json.loads(cleaned)
                self._apply_summary(data)
                # 标注一次解析成功，便于用户确认
                self._append_log("[PARSED] SELFTEST JSON updated\n")
            except Exception:
                # 兼容性兜底：有些环境可能把双引号替换成了单引号/花引号，尝试规范化后再解析
                s = cleaned.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")
                if "'" in s:
                    s2 = s.replace("'", '"')
                    try:
                        data = json.loads(s2)
                        self._apply_summary(data)
                        self._append_log("[PARSED] SELFTEST JSON updated (normalized quotes)\n")
                    except Exception:
                        # 仍失败则忽略此片段
                        pass
        # 裁剪缓冲区：若已解析到最后一个 block 的结尾，丢弃其之前的数据，避免重复解析
        if blocks:
            last_end = blocks[-1][1]
            self.serial.buffer = self.serial.buffer[last_end:]
        elif len(self.serial.buffer) > 100_000:
            self.serial.buffer = self.serial.buffer[-50_000:]

    def _apply_summary(self, data: dict):
        # 容错解析
        res = SelftestResult()
        res.eg915_ok = bool(data.get("eg915_ok", False))
        m = data.get("motion", {}) or {}
        res.motion_ok = bool(m.get("ok", False))
        try:
            res.motion_mag = float(m.get("mag", 0.0))
        except Exception:
            res.motion_mag = 0.0
        r = data.get("rs485", {}) or {}
        res.rs485_pass = bool(r.get("pass", r.get("ok", False)))
        res.rs485_written = int(r.get("written", -1) or -1)
        res.rs485_rx_bytes = int(r.get("rx_bytes", 0) or 0)
        c = data.get("can", {}) or {}
        res.can_pass = bool(c.get("pass", c.get("ok", False)))
        try:
            res.can_state = int(c.get("state", -1) or -1)
        except Exception:
            res.can_state = -1
        g = data.get("gnss", {}) or {}
        res.gnss_ok = bool(g.get("uart_ok", g.get("ok", False)))
        try:
            res.gnss_bytes = int(g.get("bytes", 0) or 0)
        except Exception:
            res.gnss_bytes = 0
        b = data.get("battery", {}) or {}
        res.battery_ok = bool(b.get("ok", False))
        try:
            res.battery_v = float(b.get("voltage", 0.0) or 0.0)
        except Exception:
            res.battery_v = 0.0

        # 更新界面
        self.lbl_overall.set_state(res.overall)
        self.lbl_eg915.set_state(res.eg915_ok)
        self.lbl_motion.set_state(res.motion_ok)
        self.lbl_motion_mag.setText(f"mag={res.motion_mag:.3f}")
        self.lbl_rs485.set_state(res.rs485_pass)
        self.lbl_rs485_info.setText(f"written={res.rs485_written}, rx={res.rs485_rx_bytes}")
        self.lbl_can.set_state(res.can_pass)
        self.lbl_can_info.setText(f"state={res.can_state}")
        self.lbl_gnss.set_state(res.gnss_ok)
        self.lbl_gnss_info.setText(f"bytes={res.gnss_bytes}")
        self.lbl_bat.set_state(res.battery_ok)
        self.lbl_bat_v.setText(f"V={res.battery_v:.2f}V")

    def _on_simulate(self):
        demo = (
            "SELFTEST SUMMARY:\n"
            "{\n"
            "  \"eg915_ok\": true,\n"
            "  \"motion\": { \"ok\": true, \"mag\": 1.237 },\n"
            "  \"rs485\": { \"inited\": true, \"written\": 8, \"rx_bytes\": 8, \"pass\": true },\n"
            "  \"can\": { \"inited\": true, \"started\": true, \"state\": 1, \"pass\": true },\n"
            "  \"gnss\": { \"uart_ok\": true, \"bytes\": 64 },\n"
            "  \"battery\": { \"ok\": true, \"voltage\": 3.97 }\n"
            "}\n"
        )
        self._append_log(demo)
        blocks = extract_json_blocks(demo)
        if blocks:
            _, _, json_text = blocks[0]
            try:
                self._apply_summary(json.loads(json_text))
            except Exception:
                try:
                    cleaned = re.sub(r"[^\x09\x0a\x0d\x20-\x7e]", "", json_text)
                    self._apply_summary(json.loads(cleaned))
                except Exception:
                    pass


def main():
    app = QApplication(sys.argv)
    w = FactoryGUI()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
