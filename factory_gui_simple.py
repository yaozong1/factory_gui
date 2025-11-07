# 简化版_on_flash_and_wait函数 - 复制到主文件替换

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
                self.ctrl_serial.ser.write(b"!BOOT\n")
                self.ctrl_serial.ser.flush()
                self._append_log_async("[FLASH] !BOOT 已发送\n")
                time.sleep(0.3)

            # 2. 关闭烧录口
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
            
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, 
                                  text=True, cwd=arg_file.parent)
            out, _ = proc.communicate(timeout=300)
            self._append_log_async(out)
            ret = proc.returncode
            
            # 4. 发送 !RUN (复用控制口)
            if ret == 0 and ctrl_port and self.ctrl_serial.ser:
                self.ctrl_serial.ser.write(b"!RUN\n")
                self.ctrl_serial.ser.flush()
                self._append_log_async("[FLASH] !RUN 已发送\n")
                time.sleep(1.0)
            
            # 5. 重新打开烧录口 (在主线程)
            if ret == 0:
                def reopen():
                    self._append_log("[FLASH] 重新打开烧录口...\n")
                    if self.flash_serial.open(flash_port, 115200):
                        self._append_log("[FLASH] 烧录口已重新打开，等待自测结果...\n")
                    else:
                        self._append_log("[FLASH] 重新打开失败\n")
                
                QTimer.singleShot(0, reopen)
            else:
                self._append_log_async(f"[FLASH] 烧录失败，返回码 {ret}\n")
                
        except Exception as e:
            self._append_log_async(f"[FLASH] 异常: {e}\n")
            import traceback
            traceback.print_exc()

    threading.Thread(target=run_flash, daemon=True).start()
