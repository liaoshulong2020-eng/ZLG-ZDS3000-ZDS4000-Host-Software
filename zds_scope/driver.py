"""
ZLG ZDS3000 / ZDS4000 Plus 系列示波器 TCP/IP SCPI 驱动程序
基于致远官方以太网通信规范 (默认端口 5025)
"""

import socket
import re
import threading
from functools import wraps
from datetime import datetime
import time
import logging
from typing import Optional, List, Dict, Any, Tuple
import numpy as np

from .protocol import (
    ScopeCommands,
    WfmFile,
    parse_wfm,
    parse_wfm_from_file,
    unpack_screen_image
)
from .instrument_settings import read_extended, validate_write

logger = logging.getLogger("zds_scope.driver")


class ScopeCommunicationError(Exception):
    """示波器通信异常"""
    pass


class ScopeTransferCancelled(ScopeCommunicationError):
    """用户主动取消波形传输"""
    pass


class ScopeTransferInterrupted(ScopeCommunicationError):
    """以太网传输意外中断（网络超时/断线/半包）"""
    pass


def serialized(method):
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self.io_lock:
            return method(self, *args, **kwargs)
    return wrapped


class ZDScopeDriver:
    """
    致远示波器以太网硬件驱动
    通过 TCP Socket (默认端口 5025) 直接通信，无需安装庞大的 VISA 运行库
    同时也兼容各种操作系统的纯 Python 环境
    """

    def __init__(self, host: str = "192.168.1.100", port: int = 5025, timeout: float = 5.0):
        self.io_lock = threading.RLock()
        self.host = host
        self.port = port
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._is_connected = False
        self.device_info: str = ""
        self.last_error: str = ""
        self.last_measurement_responses = {}

    @serialized
    def connect(self) -> bool:
        """建立 TCP 连接并查询设备标识"""
        try:
            for attempt in range(3):
                self.disconnect()
                self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.sock.settimeout(self.timeout)
                logger.info(f"正在连接示波器 {self.host}:{self.port}，尝试 {attempt + 1}/3 ...")
                try:
                    self.sock.connect((self.host, self.port))
                    break
                except (TimeoutError, ConnectionRefusedError):
                    self.disconnect()
                    if attempt == 2:
                        raise
                    time.sleep(1.0)
            self._is_connected = True

            # 查询设备标识符
            self.device_info = self.query(ScopeCommands.IDN).strip()
            self.last_error = ""
            logger.info(f"示波器连接成功: {self.device_info}")
            return True
        except Exception as e:
            self.disconnect()
            logger.error(f"示波器连接失败: {e}")
            raise ScopeCommunicationError(f"无法连接到示波器 {self.host}:{self.port} - {e}")

    @serialized
    def disconnect(self):
        """断开连接"""
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected and self.sock is not None

    @serialized
    def send(self, cmd: str):
        """发送 SCPI 命令"""
        if not self.is_connected:
            raise ScopeCommunicationError("示波器未连接")
        try:
            if not cmd.endswith("\n"):
                cmd += "\n"
            self.sock.sendall(cmd.encode("ascii"))
        except Exception as e:
            self.last_error = f"发送命令 {cmd.strip()} 失败: {e}"
            self.disconnect()
            raise ScopeCommunicationError(f"发送命令失败: {e}")

    @serialized
    def query(self, cmd: str, buf_size: int = 4096) -> str:
        """发送查询命令并获取 ASCII 字符串响应"""
        self.send(cmd)
        try:
            for _ in range(16):
                resp = self._read_line()
                if not resp:
                    continue
                if self._is_status_line(resp):
                    continue
                return resp
            raise ScopeCommunicationError("响应中存在过多状态行，无法确定查询结果")
        except Exception as e:
            self.last_error = f"查询 {cmd.strip()} 失败: {e}"
            self.disconnect()
            raise ScopeCommunicationError(self.last_error)

    def _read_line(self, prefix=b""):
        response = bytearray(prefix)
        while not response.endswith(b"\n"):
            response.extend(self.read_exact_bytes(1))
            if len(response) > 1024 * 1024:
                raise ScopeCommunicationError("ASCII 响应超出长度限制")
        return response.decode("gbk", errors="replace").strip()

    def _is_status_line(self, response):
        status = re.fullmatch(r'([+-]?\d+)\s*,\s*"([^"\r\n]*)"', response)
        if status is None:
            return False
        if int(status[1]) != 0:
            raise ScopeCommunicationError("设备返回错误: " + response)
        return True

    def _read_waveform_length(self):
        for _ in range(16):
            header = self.read_exact_bytes(4)
            if header.startswith((b'0,"', b'0, "')):
                line = self._read_line(header)
                if self._is_status_line(line):
                    continue
                raise ScopeCommunicationError("波形包头前收到非二进制响应: " + line)
            length = int.from_bytes(header, "little")
            if length == 0:
                raise ScopeCommunicationError("示波器返回空波形：当前记录尚未就绪，请等待触发完成后再读取；连接成功不代表已有有效波形。")
            if length < 424:
                raise ScopeCommunicationError(f"波形长度无效: {length}")
            return length
        raise ScopeCommunicationError("波形包头前状态行过多")

    @serialized
    def stop_and_wait(self, timeout=5.0):
        self.stop()
        deadline = time.monotonic() + timeout
        while True:
            state = self.get_run_state()
            if state.strip().lower() == "stop":
                return state
            if time.monotonic() >= deadline:
                raise ScopeCommunicationError(f"停止采集未确认，最后状态: {state}")
            time.sleep(0.05)

    @serialized
    def read_exact_bytes(self, num_bytes: int) -> bytes:
        """精确读取指定字节数（异常时主动断开连接，防止半包残留污染后续指令）"""
        chunks = []
        bytes_left = num_bytes
        try:
            while bytes_left > 0:
                chunk = self.sock.recv(min(bytes_left, 65536))
                if not chunk:
                    self.disconnect()
                    raise ScopeCommunicationError(f"网络流中断，期望 {num_bytes} 字节，剩余 {bytes_left} 字节未收到")
                chunks.append(chunk)
                bytes_left -= len(chunk)
            return b"".join(chunks)
        except Exception as e:
            self.disconnect()
            self.last_error = f"以太网流读取失败: {e}"
            raise ScopeCommunicationError(f"以太网流读取失败: {e}")

    # --------------------------------------------------------------------------
    # 状态与运行控制
    # --------------------------------------------------------------------------
    def get_idn(self) -> str:
        self.device_info = self.query(ScopeCommands.IDN)
        return self.device_info

    @serialized
    def get_run_state(self) -> str:
        """返回 Run, Single, Stop"""
        return self.query(ScopeCommands.RUN_STATE)

    @serialized
    def run(self):
        self.send(ScopeCommands.RUN)

    @serialized
    def stop(self):
        self.send(ScopeCommands.STOP)

    @serialized
    def single(self):
        self.send(ScopeCommands.SINGLE)

    @serialized
    def auto_setup(self):
        self.send(ScopeCommands.AUTO_SETUP)

    @serialized
    def clear(self):
        self.send(ScopeCommands.CLEAR)

    # --------------------------------------------------------------------------
    # 通道与时基配置
    # --------------------------------------------------------------------------
    @serialized
    def set_channel(self, ch: int, display: Optional[bool] = None,
                    scale: Optional[float] = None, offset: Optional[float] = None,
                    coupling: Optional[str] = None, bw_limit_20m: Optional[bool] = None):
        """配置模拟输入通道"""
        if display is not None:
            self.send(ScopeCommands.set_channel_display(ch, display))
        if scale is not None:
            self.send(ScopeCommands.set_channel_scale(ch, scale))
        if offset is not None:
            self.send(ScopeCommands.set_channel_offset(ch, offset))
        if coupling is not None:
            self.send(ScopeCommands.set_channel_coupling(ch, coupling))
        if bw_limit_20m is not None:
            self.send(ScopeCommands.set_channel_bwlimit(ch, bw_limit_20m))

    @serialized
    def set_timebase(self, scale: Optional[float] = None, offset: Optional[float] = None):
        if scale is not None:
            self.send(ScopeCommands.set_timebase_scale(scale))
        if offset is not None:
            self.send(ScopeCommands.set_timebase_offset(offset))

    # --------------------------------------------------------------------------
    # 存储深度与采样率 (512M / 256M Bank 交织机制)
    # --------------------------------------------------------------------------
    def get_memory_depth(self) -> str:
        """查询示波器当前存储深度 (如 512000000, 256000000, AUTO)"""
        return self.query(ScopeCommands.MEMORY_DEPTH_QUERY)

    def set_memory_depth(self, depth: str):
        """设置存储深度 (例如 512000000 或 AUTO)"""
        self.send(ScopeCommands.set_memory_depth(depth))

    def get_sample_rate(self) -> float:
        """查询当前采样率 (Hz)"""
        try:
            return float(self.query(ScopeCommands.SAMPLE_RATE_QUERY))
        except Exception:
            return 0.0

    # --------------------------------------------------------------------------
    # 硬件测量参数读取
    # --------------------------------------------------------------------------
    @serialized
    def get_measurements(self, ch: int, wait_seconds: float = 2.0, activate_missing: bool = False) -> Dict[str, float]:
        """
        读取示波器硬件 DSP 实时计算的精准测量值
        """
        meas = {}
        queries = {
            "vpp": ScopeCommands.query_measure_vpp(ch),
            "vrms": ScopeCommands.query_measure_vrms(ch),
            "vavg": ScopeCommands.query_measure_vavg(ch),
            "vmax": ScopeCommands.query_measure_vmax(ch),
            "vmin": ScopeCommands.query_measure_vmin(ch),
            "freq": ScopeCommands.query_measure_freq(ch),
            "period": ScopeCommands.query_measure_period(ch),
            "rise_time": ScopeCommands.query_measure_rise(ch),
            "fall_time": ScopeCommands.query_measure_fall(ch),
            "duty": ScopeCommands.query_measure_duty(ch),
            "overshoot": ScopeCommands.query_measure_overshoot(ch),
        }

        details = {}
        self.last_measurement_responses[ch] = details

        def read_value(name, cmd):
            resp = self.query(cmd)
            try:
                number = float(resp)
                valid = bool(np.isfinite(number) and abs(number) < 1e30)
            except (ValueError, TypeError):
                number, valid = None, False
            meas[name] = number if valid else None
            details.setdefault(name, {}).update({"response": resp, "valid": valid,
                "reason": None if valid else "instrument_invalid_or_not_ready"})

        for name, cmd in queries.items():
            read_value(name, cmd)
        pending = [name for name in queries if meas[name] is None]
        if not activate_missing:
            return meas
        # The official measurement example enables an item before reading it.
        # Only missing items are enabled; do not clear other items or retrigger.
        for name in pending:
            cmd = queries[name].replace(":CURRent?", "")
            self.send(cmd)
            details[name]["enabled_for_read"] = True
        deadline = time.monotonic() + max(0.0, wait_seconds)
        while pending:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
            for name in list(pending):
                read_value(name, queries[name])
                if meas[name] is not None:
                    pending.remove(name)
            if time.monotonic() >= deadline:
                break
        for name in pending:
            status = self.query(queries[name].replace(":CURRent?", ":STATe?")).strip()
            details[name]["status"] = status
            details[name]["reason"] = "instrument_invalid_or_not_ready"

        return meas

    @serialized
    def get_scope_actual_configuration(self) -> Dict[str, Any]:
        """Read current settings only. Never send setters, RUN/STOP or AUTO."""
        def number(cmd):
            value = float(self.query(cmd))
            if not np.isfinite(value):
                raise ValueError(f"设置响应不是有限数值: {cmd.strip()}")
            return value

        config = {
            "device_id": self.device_info or self.get_idn(),
            "run_state": self.get_run_state(),
            "enabled_channels": [],
            "channels": {},
            "mdepth_raw": self.get_memory_depth(),
            "sample_rate_hz": number(ScopeCommands.SAMPLE_RATE_QUERY),
            "timebase_scale_s": number(ScopeCommands.get_timebase_scale()),
            "timebase_offset_s": number(":TIMebase:OFFSet?"),
        }
        for ch in (1, 2, 3, 4):
            display = self.query(ScopeCommands.get_channel_display(ch)).strip().upper()
            if display not in ("1", "0", "ON", "OFF"):
                raise ValueError(f"CH{ch} 通道开关响应无效: {display}")
            enabled = display in ("1", "ON")
            coupling = self.query(f":CHANnel{ch}:COUPling?").strip().upper()
            bandwidth = self.query(f":CHANnel{ch}:BWLimit?").strip().upper()
            if coupling not in ("DC", "AC", "GND") or bandwidth not in ("OFF", "20M"):
                raise ValueError(f"CH{ch} 耦合/带宽响应无效: {coupling}, {bandwidth}")
            config["channels"][ch] = {
                "enabled": enabled,
                "scale": number(ScopeCommands.get_channel_scale(ch)),
                "offset": number(ScopeCommands.get_channel_offset(ch)),
                "probe": number(f":CHANnel{ch}:PROBe?"),
                "coupling": coupling,
                "bw_limit": bandwidth == "20M",
            }
            if enabled:
                config["enabled_channels"].append(ch)
        config["extended_settings"] = read_extended(self)
        self.current_configuration = config
        return config

    @serialized
    def write_extended_setting(self, command, value):
        current = getattr(self, "current_configuration", {}).get("extended_settings", {})
        value = validate_write(command, value, current)
        self.send(f"{command} {value}")
        # Read back without replaying other setters.
        return self.get_scope_actual_configuration()

    # --------------------------------------------------------------------------
    # 波形数据读取与磁盘直写流 (Direct-to-Disk Streamer)
    # --------------------------------------------------------------------------
    @serialized
    def read_waveform_to_file(
        self,
        channels: List[int],
        target_path: str,
        mode: str = "MEMOry",
        chunk_size: int = 1024 * 1024,
        progress_callback: Optional[Any] = None,
        cancel_token: Optional[Any] = None
    ) -> Tuple[int, WfmFile]:
        """
        直接将以太网接收的波形流写入磁盘，内存占用仅一个 chunk_size (1MB)，彻底解决 512M 点 (8.2GB+) 内存溢出。
        若传输中断或被取消，保留已写入的未完成数据，并立即关闭底层连接防止残留半包。
        """
        import os
        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        part_path = target_path + ".downloading"

        ch_names = [f"CHANnel{c}" for c in channels]
        cmd = ScopeCommands.read_multi_waveform(ch_names, mode=mode)
        self.send(cmd)

        total_len = self._read_waveform_length()
        logger.info(f"示波器返回波形数据流长度: {total_len} 字节，分块流式写入: {part_path}")

        bytes_recvd = 0
        t_start = time.time()
        last_report_t = t_start

        try:
            with open(part_path, "wb") as f:
                prefix = self.read_exact_bytes(4)
                if not prefix.startswith(b"WFM"):
                    raise ScopeCommunicationError(f"设备返回的不是 WFM 文件: {prefix!r}")
                f.write(prefix)
                bytes_recvd = len(prefix)
                while bytes_recvd < total_len:
                    if cancel_token and getattr(cancel_token, "is_cancelled", False):
                        raise ScopeTransferCancelled("用户主动取消波形传输")

                    needed = min(chunk_size, total_len - bytes_recvd)
                    chunk = self.sock.recv(min(needed, 65536))
                    if not chunk:
                        raise ScopeTransferInterrupted(
                            f"以太网连接断开: 期望 {total_len} 字节，实际接收 {bytes_recvd} 字节"
                        )
                    f.write(chunk)
                    bytes_recvd += len(chunk)

                    now = time.time()
                    if progress_callback and (now - last_report_t >= 0.1 or bytes_recvd >= total_len):
                        elapsed = max(0.001, now - t_start)
                        speed = (bytes_recvd / (1024 * 1024)) / elapsed
                        rem_bytes = max(0, total_len - bytes_recvd)
                        eta = (rem_bytes / (1024 * 1024)) / speed if speed > 0 else 0.0
                        progress_callback(bytes_recvd, total_len, speed, elapsed, eta)
                        last_report_t = now

            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(part_path, target_path)

            wfm = parse_wfm_from_file(target_path, lazy=True)
            wfm.capture_metadata = {
                "mode": mode,
                "requested_channels": list(channels),
                "received_at": datetime.now().astimezone().isoformat(),
                "total_bytes": total_len,
                "transfer_duration_s": round(time.time() - t_start, 2)
            }
            return total_len, wfm

        except ScopeTransferCancelled:
            self.disconnect()
            cancelled_path = target_path + ".cancelled"
            if os.path.exists(part_path):
                if os.path.exists(cancelled_path):
                    os.remove(cancelled_path)
                os.rename(part_path, cancelled_path)
            logger.warning(f"波形传输已取消，已保留已接收部分数据: {cancelled_path}")
            raise

        except Exception as exc:
            self.disconnect()
            incomplete_path = target_path + ".incomplete"
            if os.path.exists(part_path):
                if os.path.exists(incomplete_path):
                    os.remove(incomplete_path)
                os.rename(part_path, incomplete_path)
            logger.error(f"波形传输异常中断: {exc}，已保存已接收部分数据: {incomplete_path}")
            raise ScopeTransferInterrupted(f"波形传输中断 ({bytes_recvd}/{total_len} 字节): {exc}")

    @serialized
    def read_waveform(self, channels: List[int] = [1], mode: str = "SCREen") -> WfmFile:
        """
        读取示波器通道波形数据
        :param channels: 通道列表，例如 [1] 或 [1, 2]
        :param mode: "SCREen" (屏幕波形) 或 "MEMOry" (深存储波形)
        :return: 解析后的 WfmFile 对象
        """
        if mode == "MEMOry":
            import tempfile
            with tempfile.NamedTemporaryFile(suffix=".wfm", delete=False) as tmp:
                tmp_path = tmp.name
            try:
                _, wfm = self.read_waveform_to_file(channels=channels, target_path=tmp_path, mode="MEMOry")
                return wfm
            finally:
                if os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

        ch_names = [f"CHANnel{c}" for c in channels]
        cmd = ScopeCommands.read_multi_waveform(ch_names, mode=mode)
        self.send(cmd)

        total_len = self._read_waveform_length()
        if total_len > 64 * 1024 * 1024:
            self.last_error = f"屏幕范围返回 {total_len:,} 字节，超过实时内存读取上限；请关闭 Live，使用单次读取分块保存。"
            self.disconnect()
            raise ScopeCommunicationError(self.last_error)
        logger.info(f"示波器返回波形数据流长度: {total_len} 字节")

        wfm_raw = self.read_exact_bytes(total_len)
        wfm = parse_wfm(wfm_raw)
        wfm.capture_metadata = {"mode": mode, "requested_channels": list(channels),
                                "received_at": datetime.now().astimezone().isoformat()}
        return wfm


    # --------------------------------------------------------------------------
    # 示波器屏幕截图
    # --------------------------------------------------------------------------
    @serialized
    def capture_screen(self, fmt: str = "PNG") -> bytes:
        """
        获取示波器实时屏幕截图
        :param fmt: "PNG" 或 "BMP"
        :return: 图像文件的原生二进制字节 (可直接存为 .png 或 .bmp)
        """
        cmd = ScopeCommands.SCREEN_PNG if fmt.upper() == "PNG" else ScopeCommands.SCREEN_BMP
        self.send(cmd)

        # 截图头解析：致远仪器以 # 开头
        # #<len_digits><data_length><bytes>
        # 首先读取前 2 字节，如 "#9"
        hdr = self.read_exact_bytes(2)
        for _ in range(16):
            if hdr != b"0,":
                break
            if not self._is_status_line(self._read_line(hdr)):
                raise ScopeCommunicationError("截图前出现未知状态响应")
            hdr = self.read_exact_bytes(2)
        if hdr.startswith(b'#'):
            num_digits = int(chr(hdr[1]), 16)
            len_str = self.read_exact_bytes(num_digits).decode("ascii")
            img_len = int(len_str)
            img_bytes = self.read_exact_bytes(img_len)
            return img_bytes
        else:
            # 兼容非标准包头，持续接收直至超时
            raw_buf = bytearray(hdr)
            self.sock.settimeout(1.5)
            try:
                while True:
                    b = self.sock.recv(65536)
                    if not b:
                        break
                    raw_buf.extend(b)
            except Exception:
                pass
            finally:
                self.sock.settimeout(self.timeout)
            return unpack_screen_image(bytes(raw_buf))

    # --------------------------------------------------------------------------
    # 电源环路分析 (Loop Test / Bode 图)
    # --------------------------------------------------------------------------
    @serialized
    def get_loop_analysis(self) -> Dict[str, Any]:
        """
        读取电源环路分析数据（相位裕度 PM、增益裕度 GM 以及各频点响应表）
        """
        result = {
            "pm": "---",
            "gm": "---",
            "table": []  # List of dict(index, freq_hz, gain_db, phase_deg)
        }

        # 查询 PM 和 GM
        try:
            result["pm"] = self.query(ScopeCommands.LOOP_GET_PM)
        except Exception:
            pass

        try:
            result["gm"] = self.query(ScopeCommands.LOOP_GET_GM)
        except Exception:
            pass

        # 查询报表总行数
        try:
            size_resp = self.query(ScopeCommands.LOOP_REPORT_SIZE)
            total_lines = int(size_resp.strip())
            for i in range(1, total_lines + 1):
                line_str = self.query(ScopeCommands.query_loop_line(i))
                # 格式: 序号;频率;增益;相位，例如 "1;10.000kHz;-100.0kdB;-96.12°"
                parts = line_str.split(";")
                if len(parts) >= 4:
                    result["table"].append({
                        "index": int(parts[0]),
                        "freq_str": parts[1],
                        "gain_str": parts[2],
                        "phase_str": parts[3]
                    })
        except Exception as e:
            logger.warning(f"获取环路报表部分行失败: {e}")

        return result
