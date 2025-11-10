import sys
import re
import json
import time
import os
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QGroupBox, QTableWidget,
    QTableWidgetItem, QMessageBox, QFileDialog
)

try:
    import serial
    import serial.tools.list_ports as list_ports
except Exception as e:
    serial = None
    list_ports = None


SUMMARY_MARK = "SELFTEST SUMMARY:"
CONFIG_FILE = Path("factory_gui_config.json")  # 配置文件路径

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
    ign_tested: bool = False
    ign_pass: bool = False

    @property
    def overall(self) -> bool:
        return all([
            self.eg915_ok,
            self.motion_ok,
            self.rs485_pass,
            self.can_pass,
            self.gnss_ok,
            self.battery_ok,
            self.ign_pass,  # 添加IGN到overall判断
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

    def write_text(self, text: str):
        if not self.ser:
            return False
        try:
            self.ser.write(text.encode('utf-8', errors='ignore'))
            self.ser.flush()
            return True
        except Exception:
            return False


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

        # 双串口管理：控制口用于命令，刷机口用于读取数据
        self.ctrl_serial = SerialClient()  # 控制口（治具，发送 !BOOT/!RUN）
        self.flash_serial = SerialClient()  # 刷机口（DUT，读取自测结果）
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)  # 20Hz 轮询串口
        self.timer.timeout.connect(self.on_tick)

        # 状态：是否在等待一次自测 JSON（用于"烧录并等待"功能）
        self._awaiting_result = False
        self._await_deadline_ms = 0

        # 加载配置
        self.config = self._load_config()

        self._build_ui()
        self._refresh_ports()
        
        # 启动时自动连接（如果找到了 COM24 和 COM6）
        QTimer.singleShot(500, self._auto_connect)

    def _load_config(self):
        """加载配置文件"""
        if CONFIG_FILE.exists():
            try:
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"[CONFIG] 加载配置失败: {e}")
        return {
            "last_flash_args": None,
            "last_ctrl_port": None,
            "last_flash_port": None
        }

    def _save_config(self):
        """保存配置文件"""
        try:
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            print(f"[CONFIG] 配置已保存: {self.config}")
        except Exception as e:
            print(f"[CONFIG] 保存配置失败: {e}")

    def _auto_connect(self):
        """启动时自动连接控制口和烧录口"""
        ctrl_port = self.ctrl_combo.currentText()
        flash_port = self.flash_combo.currentText()
        
        # 检查是否找到了有效端口
        if ctrl_port and not ctrl_port.startswith("<") and flash_port and not flash_port.startswith("<"):
            self._append_log(f"[AUTO] 自动连接: 控制口={ctrl_port}, 烧录口={flash_port}\n")
            self._on_connect()
        else:
            self._append_log("[AUTO] 未找到有效端口，请手动选择并连接\n")

    def _set_ui_busy(self, busy: bool):
        # 仅在主线程调用
        # 双口模型：同时控制两个下拉
        self.ctrl_combo.setEnabled(not busy)
        self.flash_combo.setEnabled(not busy)
        self.refresh_btn.setEnabled(not busy)
        self.connect_btn.setEnabled(not busy and self.connect_btn.isEnabled())
        # 断开按钮在忙时不可点击，避免打断流程
        self.disconnect_btn.setEnabled(False if busy else self.disconnect_btn.isEnabled())
        self.simulate_btn.setEnabled(not busy)
        self.flash_btn.setEnabled(not busy)
        # 新增按钮的忙碌态控制
        if hasattr(self, 'boot_btn'):
            self.boot_btn.setEnabled(not busy)
        if hasattr(self, 'run_btn'):
            self.run_btn.setEnabled(not busy)
        if hasattr(self, 'flash_only_btn'):
            self.flash_only_btn.setEnabled(not busy)
        if hasattr(self, 'chipid_btn'):
            self.chipid_btn.setEnabled(not busy)

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 顶部：串口控制
        top = QHBoxLayout()
        self.ctrl_combo = QComboBox()
        self.flash_combo = QComboBox()
        self.refresh_btn = QPushButton("刷新串口")
        self.connect_btn = QPushButton("连接")
        self.disconnect_btn = QPushButton("断开")
        self.flash_btn = QPushButton("🔥 烧录并等待")
        
        # 设置主按钮样式
        self.flash_btn.setStyleSheet("QPushButton { font-size: 14pt; font-weight: bold; padding: 10px; background-color: #4CAF50; color: white; }")
        
        # 新增：分步诊断/操作按钮
        self.simulate_btn = QPushButton("模拟JSON")
        self.boot_btn = QPushButton("进下载(!BOOT)")
        self.run_btn = QPushButton("运行(!RUN)")
        self.flash_only_btn = QPushButton("仅刷写(esptool)")
        self.chipid_btn = QPushButton("esptool chip_id")
        self.disconnect_btn.setEnabled(False)
        
        top.addWidget(QLabel("控制口(治具):"))
        top.addWidget(self.ctrl_combo, 1)
        top.addWidget(QLabel("刷机口(DUT):"))
        top.addWidget(self.flash_combo, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        root.addLayout(top)
        
        # 烧录文件显示栏
        flash_file_layout = QHBoxLayout()
        flash_file_layout.addWidget(QLabel("烧录文件:"))
        self.flash_file_label = QLabel("(未选择)")
        self.flash_file_label.setStyleSheet("QLabel { padding: 5px; background-color: #f0f0f0; border: 1px solid #ccc; }")
        self.change_file_btn = QPushButton("更换文件")
        self.change_file_btn.clicked.connect(self._on_change_flash_file)
        flash_file_layout.addWidget(self.flash_file_label, 1)
        flash_file_layout.addWidget(self.change_file_btn)
        root.addLayout(flash_file_layout)
        
        # 启动时加载上次的文件路径
        last_path = self.config.get("last_flash_args")
        if last_path and Path(last_path).exists():
            self.flash_file_label.setText(last_path)
            self.flash_file_label.setStyleSheet("QLabel { padding: 5px; background-color: #e8f5e9; border: 1px solid #4CAF50; color: #2e7d32; }")
        
        # 主功能按钮（大按钮）
        main_btn_layout = QHBoxLayout()
        main_btn_layout.addWidget(self.flash_btn)
        root.addLayout(main_btn_layout)
        
        # 高级功能区（可折叠）- 默认隐藏
        self.show_advanced_btn = QPushButton("▼ 显示高级功能")
        self.show_advanced_btn.setCheckable(True)
        self.show_advanced_btn.clicked.connect(self._toggle_advanced)
        root.addWidget(self.show_advanced_btn)
        
        self.advanced_widget = QWidget()
        advanced_layout = QHBoxLayout(self.advanced_widget)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(self.simulate_btn)
        advanced_layout.addWidget(self.boot_btn)
        advanced_layout.addWidget(self.run_btn)
        advanced_layout.addWidget(self.flash_only_btn)
        advanced_layout.addWidget(self.chipid_btn)
        root.addWidget(self.advanced_widget)
        self.advanced_widget.hide()  # 默认隐藏

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

        self.lbl_ign = StatusLabel("-")
        panel.addWidget(QLabel("IGN Opto"), r, 0)
        panel.addWidget(self.lbl_ign, r, 1)
        r += 1

        # 右侧/下方：8路电压显示
        vbox2 = QVBoxLayout()
        voltage_group = QGroupBox("8路电压采集 (K10-3U8)")
        voltage_layout = QVBoxLayout()
        self.volt_table = QTableWidget(8, 3)
        self.volt_table.setHorizontalHeaderLabels(["通道", "电压(V)", "状态"])
        self.volt_table.setColumnWidth(0, 80)
        self.volt_table.setColumnWidth(1, 100)
        self.volt_table.setColumnWidth(2, 80)
        for i in range(8):
            self.volt_table.setItem(i, 0, QTableWidgetItem(f"AI{i+1}"))
            self.volt_table.setItem(i, 1, QTableWidgetItem("-"))
            self.volt_table.setItem(i, 2, QTableWidgetItem("-"))
        voltage_layout.addWidget(self.volt_table)
        voltage_group.setLayout(voltage_layout)
        root.addWidget(voltage_group)

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
        self.flash_btn.clicked.connect(self._on_flash_and_wait)
        self.boot_btn.clicked.connect(self._on_boot_only)
        self.run_btn.clicked.connect(self._on_run_only)
        self.flash_only_btn.clicked.connect(self._on_flash_only)
        self.chipid_btn.clicked.connect(self._on_chip_id)

    def _refresh_ports(self):
        for combo in (self.ctrl_combo, self.flash_combo):
            combo.clear()
        ports = SerialClient.list_ports()
        
        # 从配置获取上次使用的端口
        last_ctrl = self.config.get("last_ctrl_port")
        last_flash = self.config.get("last_flash_port")
        
        # 智能选择优先级：1. 配置记忆 2. COM24/COM6 默认值
        ctrl_default = None
        flash_default = None
        
        for p in ports:
            self.ctrl_combo.addItem(p)
            self.flash_combo.addItem(p)
            
            # 优先使用配置中记忆的端口
            if last_ctrl and last_ctrl in p:
                ctrl_default = p
            elif not ctrl_default and "COM24" in p:
                ctrl_default = p
            
            if last_flash and last_flash in p:
                flash_default = p
            elif not flash_default and "COM6" in p:
                flash_default = p
        
        if not ports:
            self.ctrl_combo.addItem("<无串口>")
            self.flash_combo.addItem("<无串口>")
        else:
            # 自动选择默认端口
            if ctrl_default:
                idx = self.ctrl_combo.findText(ctrl_default)
                if idx >= 0:
                    self.ctrl_combo.setCurrentIndex(idx)
                    self._append_log(f"[AUTO] 自动选择控制口: {ctrl_default}\n")
            
            if flash_default:
                idx = self.flash_combo.findText(flash_default)
                if idx >= 0:
                    self.flash_combo.setCurrentIndex(idx)
                    self._append_log(f"[AUTO] 自动选择烧录口: {flash_default}\n")

    def _toggle_advanced(self):
        """切换高级功能显示/隐藏"""
        if self.advanced_widget.isVisible():
            self.advanced_widget.hide()
            self.show_advanced_btn.setText("▼ 显示高级功能")
        else:
            self.advanced_widget.show()
            self.show_advanced_btn.setText("▲ 隐藏高级功能")

    def _on_change_flash_file(self):
        """更换烧录文件"""
        caption = "选择 flash_project_args (ESP-IDF 构建生成)"
        last_path = self.config.get("last_flash_args")
        default_dir = str(Path(last_path).parent) if last_path and Path(last_path).exists() else os.getcwd()
        
        path, _ = QFileDialog.getOpenFileName(self, caption, default_dir, "flash_project_args;*.*")
        
        if not path:
            return
            
        p = Path(path)
        if p.name != "flash_project_args":
            QMessageBox.warning(self, "文件不匹配", "请选择 ESP-IDF 构建目录下的 flash_project_args 文件。")
            return
        
        # 保存到配置并更新显示
        abs_path = str(p.resolve())
        self.config["last_flash_args"] = abs_path
        self._save_config()
        self.flash_file_label.setText(abs_path)
        self.flash_file_label.setStyleSheet("QLabel { padding: 5px; background-color: #e8f5e9; border: 1px solid #4CAF50; color: #2e7d32; }")
        self._append_log(f"[CONFIG] 已更新烧录文件: {abs_path}\n")

    def _on_connect(self):
        # 同时连接控制口和刷机口
        ctrl_port = self.ctrl_combo.currentText()
        flash_port = self.flash_combo.currentText()
        
        if not ctrl_port or ctrl_port.startswith("<"):
            QMessageBox.warning(self, "连接失败", "请选择控制口")
            return
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "连接失败", "请选择刷机口")
            return
            
        # 打开控制口
        ok1 = self.ctrl_serial.open(ctrl_port, 115200)
        if not ok1:
            QMessageBox.critical(self, "连接失败", f"无法打开控制口: {ctrl_port}")
            return
            
        # 打开刷机口
        ok2 = self.flash_serial.open(flash_port, 115200)
        if not ok2:
            self.ctrl_serial.close()
            QMessageBox.critical(self, "连接失败", f"无法打开刷机口: {flash_port}")
            return
        
        # 启动定时器读取刷机口数据
        self.timer.start()
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self._append_log(f"[INFO] 已连接控制口 {ctrl_port} + 刷机口 {flash_port}\n")
        
        # 保存端口配置
        self.config["last_ctrl_port"] = ctrl_port
        self.config["last_flash_port"] = flash_port
        self._save_config()
        self._append_log(f"[CONFIG] 已记忆端口配置\n")

    def _on_connect_flash(self):
        # 连接刷机口（CH340），用于读取 DUT 输出的自测结果
        port = self.flash_combo.currentText()
        if not port or port.startswith("<"):
            QMessageBox.warning(self, "连接失败", "未发现可用串口")
            return
        ok = self.flash_serial.open(port, 115200)
        if not ok:
            QMessageBox.critical(self, "连接失败", f"无法打开串口: {port}")
            return
        self.timer.start()
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self._append_log(f"[INFO] Connected 刷机口 {port} @115200（将读取 DUT 自测输出）\n")

    def _on_disconnect(self):
        # 同时断开两个串口
        self.timer.stop()
        self.ctrl_serial.close()
        self.flash_serial.close()
        self.connect_btn.setEnabled(True)
        self.disconnect_btn.setEnabled(False)
        self._append_log("[INFO] 已断开所有串口\n")

    def _append_log(self, text: str):
        self.log.moveCursor(QTextCursor.End)
        self.log.insertPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    # 仅发送 !BOOT（进入下载）
    def _on_boot_only(self):
        ctrl_port = self.ctrl_combo.currentText()
        if not ctrl_port or ctrl_port.startswith("<"):
            QMessageBox.warning(self, "无控制口", "请选择控制口(USB-Serial-JTAG)")
            return
        try:
            self._append_log("[CTRL] 开始进入下载模式: !BOOT\n")
            used_existing = False
            # 如果 GUI 已连接控制口，复用现有句柄，避免 Windows 占口冲突
            if self.disconnect_btn.isEnabled() and getattr(self.ctrl_serial, 'ser', None):
                try:
                    self.timer.stop()
                    self.ctrl_serial.ser.reset_input_buffer()
                    self.ctrl_serial.ser.write(b"!BOOT\n")
                    self.ctrl_serial.ser.flush()
                    used_existing = True
                    t0 = time.time(); boot_ok = False
                    while time.time() - t0 < 2.0:
                        line = self.ctrl_serial.ser.readline().decode(errors='ignore')
                        if line:
                            self._append_log(line)
                            if "JIG: BOOT OK" in line:
                                boot_ok = True
                                break
                    if not boot_ok:
                        self._append_log("[CTRL] 未收到 JIG: BOOT OK（DUT 可能也已进入下载，继续下一步验证）\n")
                finally:
                    # 恢复轮询
                    self.timer.start()
            else:
                # 未连接，则临时打开一次
                tmp = serial.Serial(port=ctrl_port, baudrate=115200, timeout=0.2)
                try:
                    tmp.reset_input_buffer()
                    tmp.write(b"!BOOT\n")
                    tmp.flush()
                    t0 = time.time(); boot_ok = False
                    while time.time() - t0 < 2.0:
                        line = tmp.readline().decode(errors='ignore')
                        if line:
                            self._append_log(line)
                            if "JIG: BOOT OK" in line:
                                boot_ok = True
                                break
                    if not boot_ok:
                        self._append_log("[CTRL] 未收到 JIG: BOOT OK（DUT 可能也已进入下载，继续下一步验证）\n")
                finally:
                    try:
                        tmp.close()
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.critical(self, "失败", f"发送 !BOOT 失败: {e}")
            self._append_log(f"[CTRL] 发送 !BOOT 失败: {e}\n")

    # 仅发送 !RUN（正常启动）
    def _on_run_only(self):
        ctrl_port = self.ctrl_combo.currentText()
        if not ctrl_port or ctrl_port.startswith("<"):
            QMessageBox.warning(self, "无控制口", "请选择控制口(USB-Serial-JTAG)")
            return
        try:
            self._append_log("[CTRL] 发送运行命令: !RUN\n")
            if self.disconnect_btn.isEnabled() and getattr(self.ctrl_serial, 'ser', None):
                try:
                    self.timer.stop()
                    self.ctrl_serial.ser.reset_input_buffer()
                    self.ctrl_serial.ser.write(b"!RUN\n")
                    self.ctrl_serial.ser.flush()
                    t0 = time.time()
                    while time.time() - t0 < 0.8:
                        line = self.ctrl_serial.ser.readline().decode(errors='ignore')
                        if line:
                            self._append_log(line)
                finally:
                    self.timer.start()
            else:
                tmp = serial.Serial(port=ctrl_port, baudrate=115200, timeout=0.2)
                try:
                    tmp.reset_input_buffer()
                    tmp.write(b"!RUN\n")
                    tmp.flush()
                    t0 = time.time()
                    while time.time() - t0 < 0.8:
                        line = tmp.readline().decode(errors='ignore')
                        if line:
                            self._append_log(line)
                finally:
                    try:
                        tmp.close()
                    except Exception:
                        pass
        except Exception as e:
            QMessageBox.critical(self, "失败", f"发送 !RUN 失败: {e}")
            self._append_log(f"[CTRL] 发送 !RUN 失败: {e}\n")

    # 仅运行 esptool write_flash（不发送 !BOOT/!RUN）— 同步执行版本
    def _on_flash_only(self):
        flash_port = self.flash_combo.currentText()
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "无刷机口", "请选择刷机口(CH340)")
            return
        
        # 先检查是否已进入下载模式
        reply = QMessageBox.question(
            self, 
            "准备刷写", 
            '请确认已点击"进下载(!BOOT)"让 DUT 进入下载模式。\n\n是否继续刷写？',
            QMessageBox.Yes | QMessageBox.No
        )
        if reply != QMessageBox.Yes:
            return
            
        arg_file = self._pick_flash_args_file()
        if not arg_file:
            return
        self._append_log(f"[FLASH-ONLY] 仅刷写，端口 {flash_port}, args: {arg_file}\n")
        
        # 直接在主线程同步执行，避免跨线程问题
        try:
            abs_args = str(arg_file.resolve())
            # 打印参数文件头几行帮助核对
            try:
                with open(abs_args, 'r', encoding='utf-8', errors='ignore') as f:
                    head = ''.join([next(f) for _ in range(5)])
                self._append_log("[FLASH-ONLY] flash_project_args head:\n" + head + "\n")
            except Exception:
                pass
            
            cmd = [
                sys.executable, "-u", "-m", "esptool",
                "--chip", "esp32s3",
                "--port", flash_port,
                "-b", "921600",  # 直连 DUT，可以用高速了
                "--before", "no_reset",
                "--after", "no_reset",
                "write_flash",
                "@" + abs_args,
            ]
            self._append_log("[FLASH-ONLY] cmd: " + " ".join(cmd) + "\n")
            self._append_log("[FLASH-ONLY] 运行中（可能需要20秒-3分钟，期间界面会暂停响应）...\n")
            QApplication.processEvents()  # 让 UI 更新
            # 在 build 目录下运行，确保 @flash_project_args 中的相对路径可解析
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=arg_file.parent)
            out, _ = proc.communicate(timeout=300)  # 最多5分钟
            self._append_log(out)
            ret = proc.returncode
            self._append_log(f"[FLASH-ONLY] 退出码: {ret}\n")
            if ret == 0:
                QMessageBox.information(self, "完成", "仅刷写成功")
            else:
                QMessageBox.critical(self, "烧录失败", f"esptool 返回码 {ret}")
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                out = proc.communicate()[0]
                self._append_log(out)
            except:
                pass
            self._append_log("[FLASH-ONLY] 超时（5分钟）\n")
            QMessageBox.critical(self, "超时", "烧录超时（5分钟）")
        except Exception as e:
            self._append_log(f"[FLASH-ONLY] 异常: {e}\n")
            QMessageBox.critical(self, "异常", str(e))

    # 仅运行 esptool chip_id（不刷写）— 简化版，直接同步执行
    def _on_chip_id(self):
        flash_port = self.flash_combo.currentText()
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "无刷机口", "请选择刷机口(CH340)")
            return
        self._append_log(f"[CHIP_ID] 开始，端口 {flash_port}\n")
        # 直接在主线程同步执行，避免所有跨线程问题
        try:
            cmd = [
                sys.executable, "-u", "-m", "esptool",
                "--chip", "esp32s3",
                "--port", flash_port,
                "-b", "115200",
                "--before", "no_reset",
                "--after", "no_reset",
                "chip_id",
            ]
            self._append_log("[CHIP_ID] cmd: " + " ".join(cmd) + "\n")
            self._append_log("[CHIP_ID] 运行中（可能需要10-20秒，期间界面会暂停响应）...\n")
            QApplication.processEvents()  # 让 UI 更新
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            out, _ = proc.communicate(timeout=30)
            self._append_log(out)
            ret = proc.returncode
            self._append_log(f"[CHIP_ID] 退出码: {ret}\n")
            if ret == 0:
                QMessageBox.information(self, "成功", "chip_id 读取成功")
            else:
                QMessageBox.critical(self, "失败", f"chip_id 失败，返回码 {ret}")
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                out = proc.communicate()[0]
                self._append_log(out)
            except:
                pass
            self._append_log("[CHIP_ID] 超时（30秒）\n")
            QMessageBox.critical(self, "超时", "chip_id 超时（30秒）")
        except Exception as e:
            self._append_log(f"[CHIP_ID] 异常: {e}\n")
            QMessageBox.critical(self, "异常", str(e))

    def _parse_voltage_adc(self, text: str):
        """解析电压 ADC JSON 数据
        格式: VOLTAGE_ADC: {"ch1":4620,"ch2":0,...,"ch8":0}
        """
        pattern = r'VOLTAGE_ADC:\s*(\{[^}]+\})'
        match = re.search(pattern, text)
        if not match:
            return
        
        try:
            json_str = match.group(1)
            data = json.loads(json_str)
            
            # 更新电压表格
            for i in range(8):
                ch_key = f"ch{i+1}"
                if ch_key in data:
                    voltage_mv = int(data[ch_key])
                    voltage_v = voltage_mv / 1000.0
                    
                    # 更新电压值
                    self.volt_table.item(i, 1).setText(f"{voltage_v:.3f}")
                    
                    # 更新状态（根据电压范围判断）
                    status = "OK"
                    color = QColor(144, 238, 144)  # 浅绿色
                    
                    if voltage_mv > 10000:  # 超过 10V
                        status = "过压"
                        color = QColor(255, 182, 193)  # 浅红色
                    elif voltage_mv < 0:
                        status = "异常"
                        color = QColor(255, 182, 193)  # 浅红色
                    elif voltage_mv == 0:
                        status = "未接"
                        color = QColor(211, 211, 211)  # 浅灰色
                    
                    self.volt_table.item(i, 2).setText(status)
                    self.volt_table.item(i, 2).setBackground(color)
                    
        except json.JSONDecodeError as e:
            print(f"[VOLTAGE] JSON 解析失败: {e}")
        except Exception as e:
            print(f"[VOLTAGE] 更新电压表失败: {e}")

    def on_tick(self):
        # 从控制口读取治具输出（电压数据从这里来）
        ctrl_chunk = self.ctrl_serial.read_lines()
        if ctrl_chunk:
            self._append_log(ctrl_chunk)
            # 解析电压 ADC 数据 (VOLTAGE_ADC: {...})
            self._parse_voltage_adc(ctrl_chunk)
        
        # 从刷机口读取 DUT 输出（自测结果从这里来）
        chunk = self.flash_serial.read_lines()
        if not chunk:
            # 减少调试输出：只在每 100 次打印一次
            if not hasattr(self, '_tick_counter'):
                self._tick_counter = 0
            self._tick_counter += 1
            if self._tick_counter % 100 == 0:
                print(f"[TICK][DBG] on_tick #{self._tick_counter}, flash_serial.ser={self.flash_serial.ser is not None}")
            return
            # 添加定期心跳日志，确认 on_tick 在运行
            if hasattr(self, '_tick_counter'):
                self._tick_counter += 1
                if self._tick_counter % 100 == 0:  # 每100次（约10秒）打印一次
                    self._append_log(f"[TICK] on_tick alive ({self._tick_counter} ticks, buffer={len(self.flash_serial.buffer)} bytes)\n")
            else:
                self._tick_counter = 0
            return
        self._append_log(chunk)
        self.flash_serial.buffer += chunk
        
        # 查找并解析 JSON 摘要
        # 使用括号平衡算法抽取完整 JSON 块，避免正则在嵌套时截断
        blocks = extract_json_blocks(self.flash_serial.buffer)
        if blocks:
            self._append_log(f"[TICK] Found {len(blocks)} JSON block(s)\n")
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
            self.flash_serial.buffer = self.flash_serial.buffer[last_end:]
        elif len(self.flash_serial.buffer) > 100_000:
            self.flash_serial.buffer = self.flash_serial.buffer[-50_000:]

        # 若正在等待一次结果，检测是否超时
        if self._awaiting_result and self._await_deadline_ms:
            if int(time.time() * 1000) > self._await_deadline_ms:
                self._awaiting_result = False
                QMessageBox.warning(self, "等待超时", "烧录完成后未在超时时间内收到自测结果。")

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
        
        # 解析 IGN 光耦测试结果
        ign = data.get("ign", {}) or {}
        res.ign_tested = bool(ign.get("tested", False))
        res.ign_pass = bool(ign.get("pass", False))

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
        self.lbl_ign.set_state(res.ign_pass)  # 更新IGN光耦测试状态

        # 如果正处于“烧录并等待”的等待阶段，第一帧解析成功即完成
        if self._awaiting_result:
            self._awaiting_result = False
            QMessageBox.information(self, "自测完成", "已收到 DUT 自测结果。")

    def _on_simulate(self):
        demo = (
            "SELFTEST SUMMARY:\n"
            "{\n"
            "  \"eg915_ok\": true,\n"
            "  \"motion\": { \"ok\": true, \"mag\": 1.237 },\n"
            "  \"rs485\": { \"inited\": true, \"written\": 8, \"rx_bytes\": 8, \"pass\": true },\n"
            "  \"can\": { \"inited\": true, \"started\": true, \"state\": 1, \"pass\": true },\n"
            "  \"gnss\": { \"uart_ok\": true, \"bytes\": 64 },\n"
            "  \"battery\": { \"ok\": true, \"voltage\": 3.97 },\n"
            "  \"ign\": { \"tested\": true, \"pass\": true }\n"
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

    # 选择 flash_project_args 文件（推荐）
    def _pick_flash_args_file(self) -> Optional[Path]:
        """获取烧录参数文件，使用配置中记忆的路径"""
        last_path = self.config.get("last_flash_args")
        
        # 如果有记忆且文件存在，直接使用
        if last_path and Path(last_path).exists():
            return Path(last_path)
        
        # 否则提示选择文件
        QMessageBox.warning(
            self,
            "未选择烧录文件",
            "请先点击「更换文件」按钮选择 flash_project_args 文件"
        )
        return None

    def _reconnect_and_wait(self, flash_port: str, ctrl_port: str = None):
        """在主线程中重连刷机口并启动定时器等待自测结果"""
        print(f"[RECONNECT][DBG] _reconnect_and_wait called, flash_port={flash_port}, ctrl_port={ctrl_port}")
        self._append_log(f"[RECONNECT] 尝试重新打开刷机口 {flash_port}...\n")
        
        # 如果提供了控制口且之前有打开，也重连控制口
        if ctrl_port:
            print(f"[RECONNECT][DBG] attempting to open ctrl_port {ctrl_port}")
            ctrl_ok = self.ctrl_serial.open(ctrl_port, 115200)
            if ctrl_ok:
                self._append_log(f"[RECONNECT] 控制口 {ctrl_port} 已重连\n")
                print(f"[RECONNECT][DBG] ctrl_port opened successfully")
            else:
                self._append_log(f"[RECONNECT] 控制口 {ctrl_port} 重连失败（不影响读取结果）\n")
                print(f"[RECONNECT][DBG] ctrl_port open failed")
        
        # 重新连接刷机口（Windows 可能需要时间释放端口，加入重试）
        print(f"[RECONNECT][DBG] attempting to open flash_port {flash_port}")
        ok = False
        for i in range(20):  # 最长约 5 秒
            ok = self.flash_serial.open(flash_port, 115200)
            if ok:
                self._append_log(f"[RECONNECT] 刷机口打开成功（尝试 {i+1} 次）\n")
                print(f"[RECONNECT][DBG] flash_port opened successfully on attempt {i+1}")
                break
            time.sleep(0.25)
        if ok:
            # 开始轮询
            self._append_log("[RECONNECT] 启动定时器，开始读取数据...\n")
            print(f"[RECONNECT][DBG] starting timer, interval={self.timer.interval()}ms")
            self.timer.start()
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setEnabled(True)
            # 启动等待窗口/超时（例如 40 秒）
            self._awaiting_result = True
            self._await_deadline_ms = int(time.time() * 1000) + 40_000
            self._append_log("[RECONNECT] 等待 DUT 打印 SELFTEST SUMMARY JSON...\n")
            print(f"[RECONNECT][DBG] awaiting result, deadline in 40s")
        else:
            self._append_log(f"[RECONNECT] 重新打开刷机口失败: {flash_port}\n")
            print(f"[RECONNECT][DBG] flash_port open failed after 20 retries")
            QMessageBox.critical(self, "连接失败", f"无法重新打开串口: {flash_port}")

    def _on_flash_and_wait(self):
        """极简版：COM24保持打开，只断开/重连COM6"""
        flash_port = self.flash_combo.currentText()
        ctrl_port = self.ctrl_combo.currentText()
        
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "无刷机口", "请选择刷机口(CH340)")
            return

        arg_file = self._pick_flash_args_file()
        if not arg_file:
            return

        self._append_log(f"[FLASH] 开始烧录: {flash_port}\n")
        
        # 在后台线程中执行
        def run_flash():
            try:
                # 1. 发送 !BOOT (复用控制口)
                if ctrl_port and self.ctrl_serial.ser:
                    print("[FLASH][DBG] sending !BOOT")
                    self.ctrl_serial.ser.write(b"!BOOT\n")
                    self.ctrl_serial.ser.flush()
                    self._append_log_async("[FLASH] !BOOT 已发送\n")
                    time.sleep(0.3)

                # 2. 关闭烧录口
                print("[FLASH][DBG] closing flash port")
                if self.flash_serial.ser:
                    self.flash_serial.ser.close()
                    self.flash_serial.ser = None
                time.sleep(0.5)
                
                # 3. 烧录
                abs_args = str(arg_file.resolve())
                cmd = [
                    sys.executable, "-m", "esptool",
                    "--chip", "esp32s3",
                    "--port", flash_port,
                    "-b", "921600",
                    "--before", "no_reset",
                    "--after", "no_reset",
                    "write_flash",
                    "@" + abs_args,
                ]
                
                print("[FLASH][DBG] starting esptool")
                print(f"[FLASH][DBG] cmd: {' '.join(cmd)}")
                self._append_log_async("[FLASH] 正在烧录...\n")
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                      text=True, cwd=arg_file.parent)
                
                # 实时读取输出（避免缓冲区满导致阻塞）
                output_lines = []
                while True:
                    line = proc.stdout.readline()
                    if not line and proc.poll() is not None:
                        break
                    if line:
                        output_lines.append(line)
                        print(line.rstrip())  # 实时打印到终端
                
                ret = proc.returncode
                out = ''.join(output_lines)
                self._append_log_async(out)
                self._append_log_async(f"[FLASH] 烧录完成，返回码: {ret}\n")
                print(f"[FLASH][DBG] esptool ret={ret}")
                
                # 4. 发送 !RUN (复用控制口)
                if ret == 0 and ctrl_port and self.ctrl_serial.ser:
                    print("[FLASH][DBG] sending !RUN")
                    self.ctrl_serial.ser.write(b"!RUN\n")
                    self.ctrl_serial.ser.flush()
                    self._append_log_async("[FLASH] !RUN 已发送\n")
                    time.sleep(1.5)  # 等待 DUT 复位完成
                
                # 5. 重新打开烧录口（直接在线程里重连，不用 QTimer）
                if ret == 0:
                    print(f"[FLASH][DBG] reopening {flash_port}")
                    self._append_log_async("[FLASH] 重新打开烧录口...\n")
                    
                    # 添加重试逻辑（Windows 可能需要时间释放端口）
                    ok = False
                    for i in range(20):  # 最多重试 20 次，共 5 秒
                        ok = self.flash_serial.open(flash_port, 115200)
                        if ok:
                            self._append_log_async(f"[FLASH] 烧录口已重新打开（尝试 {i+1} 次），等待自测结果...\n")
                            print(f"[FLASH][DBG] flash port reopened successfully on attempt {i+1}")
                            break
                        time.sleep(0.25)
                    
                    if not ok:
                        self._append_log_async("[FLASH] 重新打开失败，请手动重新连接！\n")
                        print("[FLASH][DBG] flash port reopen failed after 20 retries")
                else:
                    self._append_log_async(f"[FLASH] 烧录失败，返回码 {ret}\n")
                    
            except Exception as e:
                self._append_log_async(f"[FLASH] 异常: {e}\n")
                print(f"[FLASH][DBG] exception: {e}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=run_flash, daemon=True).start()


    def _append_log_async(self, text: str):
        QTimer.singleShot(0, lambda: self._append_log(text))


def main():
    app = QApplication(sys.argv)
    w = FactoryGUI()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
