# -*- coding: utf-8 -*-
import sys
import re
import json
import time
import os
print("="*80)
print("[INIT] Loading factory_gui.py - VERSION WITH JIG REQUEST FEATURE")
print("="*80)
import subprocess
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, QTimer, Signal, QObject
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QPushButton, QComboBox, QTextEdit, QGroupBox, QTableWidget,
    QTableWidgetItem, QMessageBox, QFileDialog, QProgressBar
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
    eg915_imei: str = ""
    eg915_iccid: str = ""
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
    ibl_mv: int = 0
    ibl_pass: bool = False
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
            self.ibl_pass,  # 添加IBL到overall判断
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
    
    @staticmethod
    def list_ports_with_desc():
        """返回端口列表，包含描述信息 [(device, description), ...]"""
        if list_ports is None:
            return []
        return [(p.device, p.description) for p in list_ports.comports()]

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


class ProgressSignals(QObject):
    """用于线程安全地更新进度条和按钮的信号"""
    update_progress = Signal(int, str)  # (value, format_text)
    reset_button = Signal()  # 重置按钮信号


class FactoryGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PE-Board Factory GUI (Preview)")
        # ?????????
        self.resize(980, 680)

        # 双串口管理：控制口用于命令，刷机口用于读取数据
        self.ctrl_serial = SerialClient()  # 控制口（治具，发送 !BOOT/!RUN）
        self.flash_serial = SerialClient()  # 刷机口（DUT，读取自测结果）
        
        self.timer = QTimer(self)
        self.timer.setInterval(50)  # 20Hz 轮询串口
        self.timer.timeout.connect(self.on_tick)


        # ACK 定时器:flash 后每 500ms 发送 ACK
        self.ack_timer = QTimer(self)
        self.ack_timer.setInterval(500)  # 500ms
        self.ack_timer.timeout.connect(self._send_ack)
        self._ack_running = False
        self._ack_started = False  # 记录是否已打印ACK开始日志
        # 状态：是否在等待一次自测 JSON（用于"烧录并等待"功能）
        self._awaiting_result = False
        self._await_deadline_ms = 0
        # 治具数据请求标志
        self._requesting_jig_data = False
        self._jig_request_deadline_ms = 0  # 治具请求超时时间
        self._jig_handshake_sent = False
        self._jig_payload_received = False
        
        # 进度条更新信号（线程安全）
        self.progress_signals = ProgressSignals()
        self.progress_signals.update_progress.connect(self._update_progress_bar)
        self.progress_signals.reset_button.connect(self._reset_test_button)
        
        # 测试运行状态标志（防止重复点击）
        self._test_running = False

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
                print(f"[CONFIG] Load config failed: {e}")
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
            print(f"[CONFIG] Config saved: {self.config}")
        except Exception as e:
            print(f"[CONFIG] Save config failed: {e}")

    def _auto_connect(self):
        """启动时自动连接控制口和烧录口"""
        ctrl_port = self.ctrl_combo.currentText()
        flash_port = self.flash_combo.currentText()
        
        # 检查是否找到了有效端口
        if ctrl_port and not ctrl_port.startswith("<") and flash_port and not flash_port.startswith("<"):
            self._append_log(f"[AUTO] Auto connect: Ctrl={ctrl_port}, Flash={flash_port}\n")
            self._on_connect()
        else:
            self._append_log("[AUTO] No valid port found, please select and connect manually\n")

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
        self.refresh_btn = QPushButton("Refresh Ports")
        self.connect_btn = QPushButton("Connect")
        self.disconnect_btn = QPushButton("Disconnect")
        self.flash_btn = QPushButton("Test")
        
        # 设置主按钮样式
        self.flash_btn.setStyleSheet("QPushButton { font-size: 14pt; font-weight: bold; padding: 10px; background-color: #4CAF50; color: white; }")
        
        # 新增：分步诊断/操作按钮
        self.simulate_btn = QPushButton("Simulate JSON")
        self.boot_btn = QPushButton("Enter Boot(!BOOT)")
        self.run_btn = QPushButton("Run(!RUN)")
        self.flash_only_btn = QPushButton("Flash Only(esptool)")
        self.chipid_btn = QPushButton("esptool chip_id")
        self.disconnect_btn.setEnabled(False)
        
        top.addWidget(QLabel("Control Port(Jig):"))
        top.addWidget(self.ctrl_combo, 1)
        top.addWidget(QLabel("Flash Port(DUT):"))
        top.addWidget(self.flash_combo, 1)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.connect_btn)
        top.addWidget(self.disconnect_btn)
        root.addLayout(top)
        
        # 烧录文件显示栏
        flash_file_layout = QHBoxLayout()
        flash_file_layout.addWidget(QLabel("Flash File:"))
        self.flash_file_label = QLabel("(Not Selected)")
        self.flash_file_label.setStyleSheet("QLabel { padding: 5px; background-color: #f0f0f0; border: 1px solid #ccc; }")
        self.change_file_btn = QPushButton("Change File")
        self.change_file_btn.clicked.connect(self._on_change_flash_file)
        flash_file_layout.addWidget(self.flash_file_label, 1)
        flash_file_layout.addWidget(self.change_file_btn)
        root.addLayout(flash_file_layout)
        
        # 启动时加载上次的文件路径
        last_path = self.config.get("last_flash_args")
        if last_path and Path(last_path).exists():
            self.flash_file_label.setText(last_path)
            self.flash_file_label.setStyleSheet("QLabel { padding: 5px; background-color: #e8f5e9; border: 1px solid #4CAF50; color: #2e7d32; }")
        
        # 进度条
        progress_layout = QHBoxLayout()
        progress_layout.addWidget(QLabel("Progress:"))
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFormat("%p% - Ready")
        # 设置进度条为绿色
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 2px solid grey;
                border-radius: 5px;
                text-align: center;
            }
            QProgressBar::chunk {
                background-color: #4CAF50;
                width: 10px;
                margin: 0.5px;
            }
        """)
        progress_layout.addWidget(self.progress_bar, 1)
        root.addLayout(progress_layout)
        
        # 主功能按钮（大按钮）
        main_btn_layout = QHBoxLayout()
        main_btn_layout.addWidget(self.flash_btn)
        root.addLayout(main_btn_layout)
        
        # 高级功能区（可折叠）- 默认隐藏
        self.show_advanced_btn = QPushButton("▼ Show Advanced")
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
        box = QGroupBox("Selftest Results")
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

        # IMEI and ICCID labels
        self.lbl_imei = QLabel("IMEI: -")
        self.lbl_imei.setStyleSheet("font-size: 9pt; color: #cfcfcf;")
        panel.addWidget(self.lbl_imei, r, 0, 1, 2)
        r += 1
        
        self.lbl_iccid = QLabel("ICCID: -")
        self.lbl_iccid.setStyleSheet("font-size: 9pt; color: #cfcfcf;")
        panel.addWidget(self.lbl_iccid, r, 0, 1, 2)
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
        panel.addWidget(QLabel("EBL"), r, 0)
        panel.addWidget(self.lbl_bat, r, 1)
        panel.addWidget(self.lbl_bat_v, r, 2)
        r += 1

        self.lbl_ign = StatusLabel("-")
        panel.addWidget(QLabel("IGN Opto"), r, 0)
        panel.addWidget(self.lbl_ign, r, 1)
        r += 1

        self.lbl_im = StatusLabel("-")
        panel.addWidget(QLabel("IM Opto"), r, 0)
        panel.addWidget(self.lbl_im, r, 1)
        r += 1

        # IBL (IO2) display: show numeric voltage and PASS/FAIL based on 2530mV ?5%
        self.lbl_ibl = StatusLabel("-")
        self.lbl_ibl_v = QLabel("V=N/A")
        panel.addWidget(QLabel("IBL"), r, 0)
        panel.addWidget(self.lbl_ibl, r, 1)
        panel.addWidget(self.lbl_ibl_v, r, 2)
        r += 1

        # ???????????Voltage ????Serial Log ???
        voltage_group = QGroupBox("7-CH Voltage (K10-3U8)")
        voltage_layout = QVBoxLayout()
        self.volt_table = QTableWidget(7, 3)  # Only 7 channels, ignore AI8(IBL)
        self.volt_table.setHorizontalHeaderLabels(["Channel", "Voltage(V)", "Status"])
        # ?????????????????????????
        # ??????????????30%??
        self.volt_table.setColumnWidth(0, 260)
        self.volt_table.setColumnWidth(1, 140)
        self.volt_table.setColumnWidth(2, 120)
        self.volt_table.setMinimumWidth(550)

        channel_names = [
            "AI1 (5V0_BUCK)",
            "AI2 (5V0)",
            "AI3 (3V3_AWS)",
            "AI4 (4V0)",
            "AI5 (3V3_LDO)",
            "AI6 (4V7_BOS)",
            "AI7 (3V3ANT)",
            "AI7 (3V3ANT)"
            # AI8 (IBL) - ignored
        ]
        self.voltage_specs = [5.0, 5.0, 3.3, 4.0, 3.3, 4.7, 3.2]  # AI7 changed to 3.2V
        for i in range(7):  # Only 7 channels
            self.volt_table.setItem(i, 0, QTableWidgetItem(channel_names[i]))
            self.volt_table.setItem(i, 1, QTableWidgetItem("-"))
            self.volt_table.setItem(i, 2, QTableWidgetItem("-"))
        # ?????????????????????7????????????
        self.volt_table.verticalHeader().setDefaultSectionSize(32)  # ?????????
        # ?????????? + 7??? + ????
        try:
            row_h = self.volt_table.verticalHeader().defaultSectionSize()
            header_h = self.volt_table.horizontalHeader().height() or 24
        except Exception:
            row_h = 32
            header_h = 24
        extra = 2  # Minimal padding to avoid cutting off bottom border
        total_h = header_h + row_h * 7 + extra  # 7 channels
        self.volt_table.setFixedHeight(total_h)
        # ???????????????
        self.volt_table.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        voltage_layout.addWidget(self.volt_table)
        voltage_group.setLayout(voltage_layout)
        root.addWidget(voltage_group)

        # ????????Voltage????????
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        root.addWidget(QLabel("Serial Log"))
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
        ports_with_desc = SerialClient.list_ports_with_desc()
        
        # 从配置获取上次使用的端口
        last_ctrl = self.config.get("last_ctrl_port")
        last_flash = self.config.get("last_flash_port")
        
        # 智能选择优先级：1. 配置记忆 2. 描述匹配 3. 固定默认值
        ctrl_default = None
        flash_default = None
        
        for device, desc in ports_with_desc:
            self.ctrl_combo.addItem(device)
            self.flash_combo.addItem(device)
            
            # Control Port 选择逻辑
            if last_ctrl and last_ctrl in device:
                # 优先：配置中记忆的端口
                ctrl_default = device
            elif not ctrl_default:
                # 次优：根据描述自动匹配（USB Serial, JTAG）
                desc_lower = desc.lower()
                if any(keyword in desc_lower for keyword in ["usb serial", "jtag", "usb-serial-jtag"]):
                    ctrl_default = device
                    self._append_log(f"[AUTO] Detected control port by description: {device} ({desc})\n")
            
            # Flash Port 选择逻辑
            if last_flash and last_flash in device:
                # 优先：配置中记忆的端口
                flash_default = device
            elif not flash_default:
                # 次优：根据描述自动匹配（CH340, CH343, USB-to-Serial）
                desc_lower = desc.lower()
                if any(keyword in desc_lower for keyword in ["ch340", "ch343", "ch9102", "usb-serial chip"]):
                    flash_default = device
                    self._append_log(f"[AUTO] Detected flash port by description: {device} ({desc})\n")
        
        if not ports_with_desc:
            self.ctrl_combo.addItem("<No Port>")
            self.flash_combo.addItem("<No Port>")
        else:
            # 自动选择默认端口
            if ctrl_default:
                idx = self.ctrl_combo.findText(ctrl_default)
                if idx >= 0:
                    self.ctrl_combo.setCurrentIndex(idx)
                    if last_ctrl and last_ctrl in ctrl_default:
                        self._append_log(f"[AUTO] Auto-select control port (saved): {ctrl_default}\n")
            
            if flash_default:
                idx = self.flash_combo.findText(flash_default)
                if idx >= 0:
                    self.flash_combo.setCurrentIndex(idx)
                    if last_flash and last_flash in flash_default:
                        self._append_log(f"[AUTO] Auto-select flash port (saved): {flash_default}\n")

    def _toggle_advanced(self):
        """切换高级功能显示/隐藏"""
        if self.advanced_widget.isVisible():
            self.advanced_widget.hide()
            self.show_advanced_btn.setText("▼ Show Advanced")
        else:
            self.advanced_widget.show()
            self.show_advanced_btn.setText("▲ Hide Advanced")

    def _on_change_flash_file(self):
        """更换烧录文件"""
        caption = "Select flash_project_args (ESP-IDF build output)"
        last_path = self.config.get("last_flash_args")
        default_dir = str(Path(last_path).parent) if last_path and Path(last_path).exists() else os.getcwd()
        
        path, _ = QFileDialog.getOpenFileName(self, caption, default_dir, "flash_project_args;*.*")
        
        if not path:
            return
            
        p = Path(path)
        if p.name != "flash_project_args":
            QMessageBox.warning(self, "File Mismatch", "Please select 'flash_project_args' from ESP-IDF build directory.")
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
            QMessageBox.warning(self, "Connection Failed", "Please select control port")
            return
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "Connection Failed", "Please select flash port")
            return
            
        # 打开控制口
        ok1 = self.ctrl_serial.open(ctrl_port, 115200)
        if not ok1:
            QMessageBox.critical(self, "Connection Failed", f"Cannot open control port: {ctrl_port}")
            return
            
        # 打开刷机口
        ok2 = self.flash_serial.open(flash_port, 115200)
        if not ok2:
            self.ctrl_serial.close()
            QMessageBox.critical(self, "Connection Failed", f"Cannot open flash port: {flash_port}")
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
            QMessageBox.warning(self, "Connection Failed", "No available port detected")
            return
        ok = self.flash_serial.open(port, 115200)
        if not ok:
            QMessageBox.critical(self, "Connection Failed", f"Cannot open port: {port}")
            return
        self.timer.start()
        self.connect_btn.setEnabled(False)
        self.disconnect_btn.setEnabled(True)
        self._append_log(f"[INFO] Connected 刷机口 {port} @115200（将读取 DUT 自测输出）\n")

    def _on_disconnect(self):
        # 同时断开两个串口
        self.timer.stop()
        self._stop_ack_timer()
        self._requesting_jig_data = False
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
            QMessageBox.warning(self, "No Control Port", "Please select control port (USB-Serial-JTAG)")
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
            QMessageBox.critical(self, "Failed", f"Send !BOOT failed: {e}")
            self._append_log(f"[CTRL] Send !BOOT failed: {e}\n")

    # 仅发送 !RUN（正常启动）
    def _on_run_only(self):
        ctrl_port = self.ctrl_combo.currentText()
        if not ctrl_port or ctrl_port.startswith("<"):
            QMessageBox.warning(self, "No Control Port", "Please select control port (USB-Serial-JTAG)")
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
            QMessageBox.critical(self, "Failed", f"Send !RUN failed: {e}")
            self._append_log(f"[CTRL] Send !RUN failed: {e}\n")

    # 仅运行 esptool write_flash（不发送 !BOOT/!RUN）— 同步执行版本
    def _on_flash_only(self):
        flash_port = self.flash_combo.currentText()
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "No Flash Port", "Please select flash port (CH340)")
            return
        
        # 先检查是否已进入下载模式
        reply = QMessageBox.question(
            self, 
            "Ready to Flash", 
            'Please confirm DUT entered boot mode via "Enter Boot(!BOOT)".\n\nContinue flashing?',
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
            self._append_log(f"[CHIP_ID] Exit code: {ret}\n")
            if ret == 0:
                QMessageBox.information(self, "Success", "chip_id read successfully")
            else:
                QMessageBox.critical(self, "Failed", f"chip_id failed, exit code {ret}")
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                out = proc.communicate()[0]
                self._append_log(out)
            except:
                pass
            self._append_log("[CHIP_ID] Timeout (30s)\n")
            QMessageBox.critical(self, "Timeout", "chip_id timeout (30s)")
        except Exception as e:
            self._append_log(f"[CHIP_ID] Exception: {e}\n")
            QMessageBox.critical(self, "Exception", str(e))

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
            
            # 更新电压表格 (只处理前7个通道，忽略ch8)
            for i in range(7):  # Only process first 7 channels, ignore ch8 (IBL)
                ch_key = f"ch{i+1}"
                if ch_key in data:
                    voltage_mv = int(data[ch_key])
                    voltage_v = voltage_mv / 1000.0
                    
                    # 更新电压值
                    self.volt_table.item(i, 1).setText(f"{voltage_v:.3f}")
                    
                    # 更新状态（根据电压规格判断±5%）
                    expected_v = self.voltage_specs[i]
                    tolerance = 0.05  # ±5%
                    lower_limit = expected_v * (1 - tolerance)
                    upper_limit = expected_v * (1 + tolerance)
                    
                    status = "OK"
                    color = QColor(144, 238, 144)  # 浅绿色
                    
                    if voltage_mv == 0:
                        status = "N/A"
                        color = QColor(211, 211, 211)  # 浅灰色
                    elif voltage_mv < 0:
                        status = "Error"
                        color = QColor(255, 182, 193)  # 浅红色
                    elif voltage_v < lower_limit or voltage_v > upper_limit:
                        status = "Out of Range"
                        color = QColor(255, 182, 193)  # 浅红色
                    
                    self.volt_table.item(i, 2).setText(status)
                    self.volt_table.item(i, 2).setBackground(color)

            # If K10 payload includes explicit ibl_mv (mV), use it to set AI8 (IBL) status
            if "ibl_mv" in data:
                try:
                    ibl_mv = int(data.get("ibl_mv", 0))
                    ibl_v = ibl_mv / 1000.0
                    # update displayed voltage (override ch8 if needed)
                    self.volt_table.item(7, 1).setText(f"{ibl_v:.3f}")

                    # IBL 电压应在 2.2V - 2.5V 范围内 (2200mV - 2500mV)
                    lower_m = 2200
                    upper_m = 2500
                    if ibl_mv == 0:
                        status = "N/A"
                        color = QColor(211, 211, 211)
                    elif lower_m <= ibl_mv <= upper_m:
                        status = "OK"
                        color = QColor(144, 238, 144)
                    else:
                        status = "Out of Range"
                        color = QColor(255, 182, 193)

                    self.volt_table.item(7, 2).setText(status)
                    self.volt_table.item(7, 2).setBackground(color)
                except Exception:
                    pass
            
            # 解析 IM 测试结果
            if "im_tested" in data and "im_pass" in data:
                im_tested = bool(data.get("im_tested", False))
                im_pass = bool(data.get("im_pass", False))
                if im_tested:
                    self.lbl_im.set_state(im_pass)
            
            # 收到治具的完整payload后，停止发送 !GUI_REQUEST_DATA
            if self._requesting_jig_data:
                self._requesting_jig_data = False
                self._jig_request_deadline_ms = 0  # 重置治具请求deadline
                print("[JIG][DBG] Received payload, stopped sending !GUI_REQUEST_DATA")
                self._append_log("[JIG] Payload received, stopped requesting\n")
                    
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

        # 如果在等待自测结果,每 500ms 发送 ACK
        if self._awaiting_result and self.flash_serial.ser and self.flash_serial.ser.is_open:
            if not hasattr(self, "_last_ack_ms"):
                self._last_ack_ms = 0
            now_ms = int(time.time() * 1000)
            if now_ms - self._last_ack_ms >= 500:
                try:
                    # 只在第一次发送时打印开始日志
                    if not self._ack_started:
                        self._append_log("[ACK] Start sending !GUI_SELFTEST_ACK (every 500ms)\n")
                        self._ack_started = True
                    
                    self.flash_serial.ser.write(b"!GUI_SELFTEST_ACK\n")
                    self.flash_serial.ser.flush()
                    self._last_ack_ms = now_ms
                except Exception as e:
                    self._append_log(f"[ACK] Send failed: {e}\n")

        # ??????????????? 500ms ??????ACK???????
        if self._requesting_jig_data and self.ctrl_serial.ser and self.ctrl_serial.ser.is_open:
            if not hasattr(self, "_last_jig_req_ms"):
                self._last_jig_req_ms = 0
            now_ms = int(time.time() * 1000)
            if now_ms - self._last_jig_req_ms >= 500:
                try:
                    self.ctrl_serial.ser.write(b"!GUI_REQUEST_DATA\n")
                    self.ctrl_serial.ser.flush()
                    self._append_log("[JIG] Sent: !GUI_REQUEST_DATA\n")
                    print("[JIG][DBG] Sent !GUI_REQUEST_DATA to ctrl_serial")  # ?????????
                    self._last_jig_req_ms = now_ms
                except Exception as e:
                    self._append_log(f"[JIG] Request failed: {e}\n")
                    print(f"[JIG][DBG] Send failed: {e}")  # ?????????
        elif self._requesting_jig_data:
            # ??????????
            if not hasattr(self, '_jig_nosend_counter'):
                self._jig_nosend_counter = 0
            self._jig_nosend_counter += 1
            if self._jig_nosend_counter == 1:  # ??????
                ctrl_open = self.ctrl_serial.ser is not None and self.ctrl_serial.ser.is_open if self.ctrl_serial.ser else False
                print(f"[JIG][DBG] Not sending: ctrl_serial.ser={self.ctrl_serial.ser is not None}, is_open={ctrl_open}")

        # 超时检测（必须在return之前执行）
        # 若正在等待一次结果，检测是否超时
        if self._awaiting_result and self._await_deadline_ms:
            if int(time.time() * 1000) > self._await_deadline_ms:
                self._awaiting_result = False
                self._await_deadline_ms = 0  # 重置deadline
                self._requesting_jig_data = False  # 停止请求治具数据
                self._jig_request_deadline_ms = 0  # 重置治具请求deadline
                if self._ack_started:
                    self._append_log('[ACK] Done sending ACK (timeout)\n')
                    self._ack_started = False
                # 更新进度条并恢复Test按钮状态（超时）
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("%p% - TIMEOUT")
                self._reset_test_button()
                QMessageBox.warning(self, "Wait Timeout", "No selftest result received after flashing timeout.")
                return
        
        # 检测治具请求超时
        if self._requesting_jig_data and self._jig_request_deadline_ms:
            if int(time.time() * 1000) > self._jig_request_deadline_ms:
                self._requesting_jig_data = False
                self._jig_request_deadline_ms = 0
                self._awaiting_result = False
                self._await_deadline_ms = 0
                if self._ack_started:
                    self._append_log('[ACK] Done sending ACK (jig timeout)\n')
                    self._ack_started = False
                # 更新进度条并恢复Test按钮状态（治具超时）
                self.progress_bar.setValue(0)
                self.progress_bar.setFormat("%p% - JIG TIMEOUT")
                self._reset_test_button()
                QMessageBox.warning(self, "Jig Timeout", "No response from test jig. Please check jig connection and firmware.")
                return

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

    def _apply_summary(self, data: dict):
        # 更新进度: 90%
        self.progress_bar.setValue(90)
        self.progress_bar.setFormat("%p% - Parsing results...")
        
        # ????payload??????
        self._jig_payload_received = True
        self._requesting_jig_data = False
        # 容错解析
        res = SelftestResult()
        res.eg915_ok = bool(data.get("eg915_ok", False))
        res.eg915_imei = str(data.get("eg915_imei", ""))
        res.eg915_iccid = str(data.get("eg915_iccid", ""))
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
        
        # 解析 IBL 测试结果
        if "ibl" in data and isinstance(data["ibl"], dict):
            # 新格式: "ibl": { "pass": true/false, "mv": 2364 }
            ibl = data.get("ibl", {})
            res.ibl_pass = bool(ibl.get("pass", False))
            res.ibl_mv = int(ibl.get("mv", 0))
        elif "ibl_mv" in data:
            # 旧格式: "ibl_mv": 2364（只有数值，需要自行判断）
            res.ibl_mv = int(data.get("ibl_mv", 0))
            # IBL 电压应在 2.2V - 2.5V 范围内 (2200mV - 2500mV)
            res.ibl_pass = 2200 <= res.ibl_mv <= 2500

        # 更新界面
        # self.lbl_overall.set_state(res.overall)  # Will be set after voltage check
        self.lbl_eg915.set_state(res.eg915_ok)
        
        # Update IMEI and ICCID labels
        if res.eg915_imei:
            self.lbl_imei.setText(f"IMEI: {res.eg915_imei}")
        else:
            self.lbl_imei.setText("IMEI: -")
        
        if res.eg915_iccid:
            self.lbl_iccid.setText(f"ICCID: {res.eg915_iccid}")
        else:
            self.lbl_iccid.setText("ICCID: -")
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

        # 更新 IBL 测试状态
        self.lbl_ibl.set_state(res.ibl_pass)
        if res.ibl_mv > 0:
            ibl_v = res.ibl_mv / 1000.0
            self.lbl_ibl_v.setText(f"V={ibl_v:.3f}V")
        else:
            self.lbl_ibl_v.setText("V=N/A")

        # IBL (IO2) from selftest payload (mV). PASS if within 2530mV ?5%.
        # 保留旧的逻辑作为备用（兼容性）
        try:
            ibl_mv = data.get("ibl_mv", None)
            if ibl_mv is not None and res.ibl_mv == 0:
                # 如果新逻辑没有解析到，使用旧逻辑
                ibl_mv = int(ibl_mv)
                ibl_v = ibl_mv / 1000.0
                self.lbl_ibl_v.setText(f"V={ibl_v:.3f}V")
                # IBL 电压应在 2.2V - 2.5V 范围内 (2200mV - 2500mV)
                lower = 2200
                upper = 2500
                if ibl_mv == 0:
                    self.lbl_ibl.setText("N/A")
                    self.lbl_ibl.setStyleSheet("color: gray; font-weight: bold;")
                elif lower <= ibl_mv <= upper:
                    self.lbl_ibl.set_state(True)
                else:
                    self.lbl_ibl.set_state(False)
        except Exception:
            pass


        # 验证 7-CH Voltage 表格判断 OVERALL (忽略第8通道IBL)
        voltage_all_ok = True
        for i in range(7):  # Only check first 7 channels, ignore AI8(IBL)
            status_item = self.volt_table.item(i, 2)
            if status_item:
                status_text = status_item.text()
                if status_text != "OK":
                    voltage_all_ok = False
                    break
            else:
                voltage_all_ok = False
                break
        
        # 最终 OVERALL = selftest OVERALL AND 7-CH Voltage OVERALL
        final_overall = res.overall and voltage_all_ok
        self.lbl_overall.set_state(final_overall)
        
        if not final_overall:
            reasons = []
            if not res.overall:
                reasons.append("Selftest FAIL")
            if not voltage_all_ok:
                reasons.append("Voltage NOT all OK")
            reason_str = ", ".join(reasons)
            self._append_log(f"[OVERALL] FAIL - Reason: {reason_str}\n")
        else:
            self._append_log("[OVERALL] PASS - All tests passed\n")

        # 停止 ACK 定时器
        self._stop_ack_timer()

        # 如果正处于“烧录并等待”的等待阶段，第一帧解析成功即完成
        if self._awaiting_result:
            self._awaiting_result = False
            self._await_deadline_ms = 0  # 重置deadline
            self._jig_request_deadline_ms = 0  # 重置治具请求deadline
            self._append_log("[INFO] Done testing\n")
            # 更新进度: 100%
            self.progress_bar.setValue(100)
            if final_overall:
                self.progress_bar.setFormat("%p% - PASS")
            else:
                self.progress_bar.setFormat("%p% - FAIL")
            # 恢复Test按钮状态（测试完成）
            self._reset_test_button()
            # 保存CSV
            self._save_test_result_to_csv(res)

    def _save_test_result_to_csv(self, res: SelftestResult):
        """Save parsed test results to CSV file"""
        import csv
        from datetime import datetime
        
        try:
            # Read parsed results from GUI widgets
            # Selftest Results section
            overall_text = self.lbl_overall.text()
            eg915_text = self.lbl_eg915.text()
            imei_text = self.lbl_imei.text().replace("IMEI: ", "")
            iccid_text = self.lbl_iccid.text().replace("ICCID: ", "")
            motion_text = self.lbl_motion.text()
            motion_mag_text = self.lbl_motion_mag.text().replace("mag=", "")
            rs485_text = self.lbl_rs485.text()
            rs485_info_text = self.lbl_rs485_info.text()
            can_text = self.lbl_can.text()
            can_info_text = self.lbl_can_info.text()
            gnss_text = self.lbl_gnss.text()
            gnss_info_text = self.lbl_gnss_info.text()
            battery_text = self.lbl_bat.text()
            battery_v_text = self.lbl_bat_v.text().replace("V=", "").replace("V", "")
            ign_text = self.lbl_ign.text()
            im_text = self.lbl_im.text()
            
            # 7-CH Voltage section: read parsed data from table (ignore AI8/IBL)
            channel_names = ["5V_BUCK", "5V0", "3V3_AWS", "4V0", "3V3_LDO", "4V7_BOS", "3V3ANT"]  # 7 channels only
            
            # Data row
            row = [
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                imei_text,
                iccid_text,
                overall_text,
                eg915_text,
                motion_text,
                motion_mag_text,
                rs485_text,
                rs485_info_text,
                can_text,
                can_info_text,
                gnss_text,
                gnss_info_text,
                battery_text,
                battery_v_text,
                ign_text,
                im_text
            ]
            
            # Add 7-channel ADC voltage and status (ignore AI8/IBL)
            for i in range(7):  # Only 7 channels
                v_item = self.volt_table.item(i, 1)
                s_item = self.volt_table.item(i, 2)
                v_text = v_item.text() if v_item else "-"
                s_text = s_item.text() if s_item else "-"
                row.append(v_text)
                row.append(s_text)
            
            # CSV file path
            csv_path = os.path.join(os.path.dirname(__file__), 'test_results.csv')
            write_header = not os.path.exists(csv_path)
            
            with open(csv_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                if write_header:
                    # Header
                    header = [
                        'Time', 'IMEI', 'ICCID', 'Overall',
                        'EG915', 'Motion', 'Motion_Mag',
                        'RS485', 'RS485_Info', 'CAN', 'CAN_Info',
                        'GNSS', 'GNSS_Info', 'Battery', 'Battery_V',
                        'IGN_Opto', 'IM_Opto'
                    ]
                    #  8路ADC头路?起
                    for ch in channel_names:
                        header.append(f'{ch}_V')
                        header.append(f'{ch}_Status')
                    writer.writerow(header)
                
                writer.writerow(row)
            
            self._append_log(f"[CSV] Test result saved to {csv_path}\n")
            print(f"[CSV] Test result saved: {len(row)} columns")
            
        except Exception as e:
            self._append_log(f"[CSV] Save failed: {e}\n")
            print(f"[CSV] Save failed: {e}")
            import traceback
            traceback.print_exc()


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
            "No Flash File Selected",
            "Please click 'Change File' to select flash_project_args file"
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
            # 启动等待窗口/超时（20秒）
            self._awaiting_result = True
            self._await_deadline_ms = int(time.time() * 1000) + 20_000
            self._append_log("[RECONNECT] 等待 DUT 打印 SELFTEST SUMMARY JSON...\n")
            print(f"[RECONNECT][DBG] awaiting result, deadline in 20s")
        else:
            self._append_log(f"[RECONNECT] 重新打开刷机口失败: {flash_port}\n")
            print(f"[RECONNECT][DBG] flash_port open failed after 20 retries")
            QMessageBox.critical(self, "连接失败", f"无法重新打开串口: {flash_port}")

    def _on_flash_and_wait(self):
        """极简版：COM24保持打开，只断开/重连COM6"""
        # 防止重复点击：检查是否已有测试在运行
        if self._test_running:
            QMessageBox.warning(self, "测试进行中", "当前已有测试正在运行，请等待完成后再试")
            self._append_log("[FLASH] Ignored: Test already running\n")
            return
        
        # 清空上一轮测试的标志和状态（新一轮测试开始）
        self._awaiting_result = False
        self._ack_started = False  # 重置ACK开始标志
        self._requesting_jig_data = False
        if hasattr(self, '_last_ack_ms'):
            delattr(self, '_last_ack_ms')
        if hasattr(self, '_last_jig_req_ms'):
            delattr(self, '_last_jig_req_ms')
        if hasattr(self, '_jig_nosend_counter'):
            delattr(self, '_jig_nosend_counter')
        
        # 清空界面上的 Selftest Results 显示
        self.lbl_overall.setText("-")
        self.lbl_overall.setStyleSheet("")
        self.lbl_eg915.setText("-")
        self.lbl_eg915.setStyleSheet("")
        self.lbl_imei.setText("IMEI: -")
        self.lbl_iccid.setText("ICCID: -")
        self.lbl_motion.setText("-")
        self.lbl_motion.setStyleSheet("")
        self.lbl_motion_mag.setText("mag=0.000")
        self.lbl_rs485.setText("-")
        self.lbl_rs485.setStyleSheet("")
        self.lbl_rs485_info.setText("-")
        self.lbl_can.setText("-")
        self.lbl_can.setStyleSheet("")
        self.lbl_can_info.setText("-")
        self.lbl_gnss.setText("-")
        self.lbl_gnss.setStyleSheet("")
        self.lbl_gnss_info.setText("-")
        self.lbl_bat.setText("-")
        self.lbl_bat.setStyleSheet("")
        self.lbl_bat_v.setText("V=0.00V")
        self.lbl_ign.setText("-")
        self.lbl_ign.setStyleSheet("")
        self.lbl_im.setText("-")
        self.lbl_im.setStyleSheet("")
        self.lbl_ibl.setText("-")
        self.lbl_ibl.setStyleSheet("")
        self.lbl_ibl_v.setText("V=N/A")
        
        # 清空 7-CH Voltage 表格 (忽略AI8)
        for i in range(7):  # Only 7 channels, ignore AI8(IBL)
            self.volt_table.item(i, 1).setText("-")  # Voltage列
            # 重新创建 Status 列的项，恢复默认背景色（和 Voltage 列一样的深灰色）
            self.volt_table.setItem(i, 2, QTableWidgetItem("-"))
        
        print("[FLASH][DBG] Cleared previous test results and flags for new round")
        self._append_log("[FLASH] === New test round started ===\n")
        
        # 重置进度条
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p% - Ready")
        
        flash_port = self.flash_combo.currentText()
        ctrl_port = self.ctrl_combo.currentText()
        
        if not flash_port or flash_port.startswith("<"):
            QMessageBox.warning(self, "No Flash Port", "Please select flash port (CH340)")
            return

        arg_file = self._pick_flash_args_file()
        if not arg_file:
            return

        # 设置测试运行标志并禁用Test按钮
        self._test_running = True
        self.flash_btn.setEnabled(False)
        self.flash_btn.setText("Testing...")
        
        self._append_log(f"[FLASH] 开始烧录: {flash_port}\n")
        
        # 在后台线程中执行
        def run_flash():
            try:
                # 1. 发送 !BOOT (复用控制口)
                if ctrl_port and self.ctrl_serial.ser:
                    try:
                        print("[FLASH][DBG] sending !BOOT")
                        self.ctrl_serial.ser.write(b"!BOOT\n")
                        self.ctrl_serial.ser.flush()
                        self._append_log_async("[FLASH] !BOOT 已发送\n")
                        # 更新进度: 10%
                        self.progress_signals.update_progress.emit(10, "%p% - Sent !BOOT")
                        time.sleep(0.3)
                    except Exception as e:
                        self._append_log_async(f"[FLASH] Failed to send !BOOT: {e}\n")
                        print(f"[FLASH][DBG] Failed to send !BOOT: {e}")
                        self.progress_signals.update_progress.emit(0, "%p% - !BOOT Failed")
                        self.progress_signals.reset_button.emit()
                        return

                # 2. 关闭烧录口
                print("[FLASH][DBG] closing flash port")
                try:
                    if self.flash_serial.ser:
                        self.flash_serial.ser.close()
                        self.flash_serial.ser = None
                    time.sleep(0.5)
                except Exception as e:
                    self._append_log_async(f"[FLASH] Failed to close flash port: {e}\n")
                    print(f"[FLASH][DBG] Failed to close flash port: {e}")
                    # 继续执行，因为可能端口已经关闭了
                
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
                # 更新进度: 20%
                self.progress_signals.update_progress.emit(20, "%p% - Flashing...")
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
                self._append_log_async(f"[FLASH] Flash complete, exit code: {ret}\n")
                print(f"[FLASH][DBG] esptool ret={ret}")
                
                # 检查烧录结果
                if ret == 0:
                    # 更新进度: 50%
                    self.progress_signals.update_progress.emit(50, "%p% - Flash OK")
                else:
                    # 烧录失败，更新进度条并恢复按钮
                    self.progress_signals.update_progress.emit(0, "%p% - Flash FAILED")
                    if ret is None:
                        self._append_log_async(f"[FLASH] Flash process did not complete properly\n")
                    else:
                        self._append_log_async(f"[FLASH] Flash failed, exit code: {ret}\n")
                    print(f"[FLASH][DBG] Flash failed (ret={ret}), restoring button")
                    self.progress_signals.reset_button.emit()
                    return  # 提前退出，不继续后续步骤
                
                # 4. 发送 !RUN (复用控制口)
                if ret == 0 and ctrl_port and self.ctrl_serial.ser:
                    print("[FLASH][DBG] sending !RUN")
                    self.ctrl_serial.ser.write(b"!RUN\n")
                    self.ctrl_serial.ser.flush()
                    self._append_log_async("[FLASH] !RUN sent\n")
                    # 更新进度: 60%
                    self.progress_signals.update_progress.emit(60, "%p% - Sent !RUN")
                    time.sleep(1.5)  # 等待 DUT 复位完成
                
                # 5. 重新打开烧录口（直接在线程里重连，不用 QTimer）
                if ret == 0:
                    print(f"[FLASH][DBG] reopening {flash_port}")
                    self._append_log_async("[FLASH] Reopening flash port...\n")
                    
                    # 添加重试逻辑（Windows 可能需要时间释放端口）
                    ok = False
                    for i in range(20):  # 最多重试 20 次，共 5 秒
                        ok = self.flash_serial.open(flash_port, 115200)
                        if ok:
                            self._append_log_async(f"[FLASH] Flash port reopened (attempt {i+1}), waiting for selftest result...\n")
                            print(f"[FLASH][DBG] flash port reopened successfully on attempt {i+1}")
                            # 更新进度: 70%
                            self.progress_signals.update_progress.emit(70, "%p% - Waiting selftest...")
                            # 设置等待标志,触发 on_tick 中的 ACK 发送
                            self._awaiting_result = True
                            self._await_deadline_ms = int(time.time() * 1000) + 20_000
                            print("[FLASH][DBG] set _awaiting_result=True, ACK will be sent every 500ms")
                            # 同时向治具发送请求,让治具发送测试 payload
                            self._requesting_jig_data = True
                            self._jig_request_deadline_ms = int(time.time() * 1000) + 20_000  # 治具请求20秒超时
                            print("[FLASH][DBG] set _requesting_jig_data=True, will request jig data every 500ms")
                            break
                        time.sleep(0.25)
                    
                    if not ok:
                        self._append_log_async("[FLASH] Reopen failed, please reconnect manually!\n")
                        print("[FLASH][DBG] flash port reopen failed after 20 retries")
                        # 更新进度条并恢复按钮状态（重连失败）
                        self.progress_signals.update_progress.emit(0, "%p% - Reopen FAILED")
                        self.progress_signals.reset_button.emit()
                    
            except Exception as e:
                self._append_log_async(f"[FLASH] Exception: {e}\n")
                print(f"[FLASH][DBG] exception: {e}")
                import traceback
                traceback.print_exc()
                # 更新进度条并恢复按钮状态（异常）
                self.progress_signals.update_progress.emit(0, "%p% - ERROR")
                self.progress_signals.reset_button.emit()

        threading.Thread(target=run_flash, daemon=True).start()


    def _start_ack_timer(self):
        """启动ACK定时器"""
        if not self._ack_running:
            self._ack_running = True
            self.ack_timer.start()
            self._append_log('[ACK] ACK timer started (500ms interval)\n')
            print('[ACK][DBG] ACK timer started')

    def _stop_ack_timer(self):
        """停止ACK定时器"""
        if self._ack_running:
            self.ack_timer.stop()
            self._ack_running = False
            if self._ack_started:
                self._append_log('[ACK] Done sending ACK\n')
                self._ack_started = False
            print('[ACK][DBG] ACK timer stopped')

    def _send_ack(self):
        """发送ACK到DUT"""
        if not self.flash_serial.ser or not self.flash_serial.ser.is_open:
            self._append_log('[ACK] Flash port closed, stopping ACK timer\n')
            self.ack_timer.stop()
            self._ack_running = False
            return
        
        try:
            self.flash_serial.ser.write(b'!GUI_SELFTEST_ACK\n')
            self.flash_serial.ser.flush()
            self._append_log('[ACK] Sent: !GUI_SELFTEST_ACK\n')
        except Exception as e:
            self._append_log(f'[ACK] Send failed: {e}\n')
            self.ack_timer.stop()
            self._ack_running = False

    def _append_log_async(self, text: str):
        QTimer.singleShot(0, lambda: self._append_log(text))

    def _update_progress_bar(self, value: int, format_text: str):
        """线程安全的进度条更新槽函数（在主线程中执行）"""
        self.progress_bar.setValue(value)
        self.progress_bar.setFormat(format_text)

    def _reset_test_button(self):
        """重置Test按钮状态（在主线程调用）"""
        print("[FLASH][DBG] _reset_test_button() called")
        self._test_running = False
        self.flash_btn.setEnabled(True)
        self.flash_btn.setText("Test")
        self._append_log("[FLASH] Test button restored\n")


def main():
    app = QApplication(sys.argv)
    w = FactoryGUI()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
