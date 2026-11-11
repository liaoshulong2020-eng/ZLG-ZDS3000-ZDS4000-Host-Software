"""Test-only generated waveform fixture. Not part of the application or public SDK."""
import time
import io
import numpy as np
from typing import Optional, List, Dict, Any, Tuple
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from zds_scope.protocol import (
    WfmHeadInfo,
    WfmItemInfo,
    WfmFile,
    ChannelWaveformData,
    pack_wfm,
    parse_wfm
)


class CaptureFixtureDriver:
    """
    虚拟示波器驱动，模拟致远 ZDS4054 Plus 示波器
    内置开关电源 (Buck/Boost 开关节点振铃、输出纹波、电感电流、环路 Bode 测试) 仿真模型
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 5025, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._is_connected = False
        self.device_info = "ZLG,ZDS4054 Plus,SN-SIM2026,V2.30"
        self.run_state = "Run"

        # 模拟配置
        self.timebase_scale = 1e-6    # 1 us / div (总宽 14 div = 14 us)
        self.timebase_offset = 0.0
        self.ch_scales = {1: 5.0, 2: 0.05, 3: 0.5, 4: 5.0}  # V/div
        self.ch_offsets = {1: -12.0, 2: -5.0, 3: -1.0, 4: -5.0}
        self.ch_display = {1: True, 2: True, 3: False, 4: False}

        # 电源仿真参数
        self.vin = 24.0               # 输入电压 24V
        self.vout = 5.0               # 输出电压 5V
        self.fsw = 200e3              # 开关频率 200 kHz (周期 5 us)
        self.duty = self.vout / self.vin  # 约 0.208
        self.f_ring = 35e6            # 开关节点振铃频率 35 MHz
        self.overshoot_ratio = 0.35   # 35% 开关过冲

    def connect(self) -> bool:
        time.sleep(0.1)
        self._is_connected = True
        return True

    def disconnect(self):
        self._is_connected = False

    @property
    def is_connected(self) -> bool:
        return self._is_connected

    def send(self, cmd: str):
        pass

    def query(self, cmd: str) -> str:
        if "*IDN?" in cmd:
            return self.device_info
        elif ":GLOBal:RUN:STATe?" in cmd:
            return self.run_state
        elif ":ACQuire:MDEPth?" in cmd:
            return self.get_memory_depth()
        elif ":ACQuire:SRATe?" in cmd:
            return f"{self.get_sample_rate():.6e}"
        elif ":TIMebase:SCALe?" in cmd:
            return f"{self.timebase_scale:.6e}"
        elif ":TIMebase:OFFSet?" in cmd:
            return f"{self.timebase_offset:.6e}"

        for ch in [1, 2, 3, 4]:
            if f":CHANnel{ch}:DISPlay?" in cmd:
                return "1" if self.ch_display.get(ch, False) else "0"
            if f":CHANnel{ch}:SCALe?" in cmd:
                return f"{self.ch_scales.get(ch, 1.0):.6e}"
            if f":CHANnel{ch}:OFFSet?" in cmd:
                return f"{self.ch_offsets.get(ch, 0.0):.6e}"
            if f":CHANnel{ch}:PROBe?" in cmd:
                return "1.0"
        return "0"

    def get_scope_actual_configuration(self) -> Dict[str, Any]:
        """返回虚拟示波器硬件实际配置"""
        return {
            "device_id": self.device_info,
            "enabled_channels": [c for c, d in self.ch_display.items() if d],
            "channels": {
                c: {
                    "enabled": self.ch_display.get(c, False),
                    "scale": self.ch_scales.get(c, 1.0),
                    "offset": self.ch_offsets.get(c, 0.0),
                    "probe": 1.0
                }
                for c in [1, 2, 3, 4]
            },
            "mdepth_raw": "2000",
            "sample_rate_hz": self.get_sample_rate(),
            "timebase_scale_s": self.timebase_scale,
            "timebase_offset_s": self.timebase_offset,
        }


    def get_memory_depth(self) -> str:
        """
        致远示波器硬件交织架构:
        - 仅使用 CH1, CH3 (独立 Bank A/B) 或 CH2, CH4: 存储深度可达 512M (512,000,000)
        - 使用 CH1+CH2 (同Bank冲突) 或 3~4 通道并发: 存储深度降为 256M (256,000,000)
        """
        if getattr(self, "_user_mdep", None) and self._user_mdep != "AUTO":
            return self._user_mdep
        active = [c for c, d in self.ch_display.items() if d]
        s = set(active)
        if s.issubset({1, 3}) or s.issubset({2, 4}):
            return "512000000"
        else:
            return "256000000"

    def set_memory_depth(self, depth: str):
        self._user_mdep = depth

    def get_sample_rate(self) -> float:
        active = [c for c, d in self.ch_display.items() if d]
        s = set(active)
        if s.issubset({1, 3}) or s.issubset({2, 4}):
            return 2.0e9
        else:
            return 1.0e9

    def get_idn(self) -> str:
        return self.device_info

    def get_run_state(self) -> str:
        return self.run_state

    def run(self):
        self.run_state = "Run"

    def stop(self):
        self.run_state = "Stop"

    def single(self):
        self.run_state = "Single"

    def auto_setup(self):
        pass

    def clear(self):
        pass

    def set_channel(self, ch: int, display: Optional[bool] = None,
                    scale: Optional[float] = None, offset: Optional[float] = None,
                    coupling: Optional[str] = None, bw_limit_20m: Optional[bool] = None):
        if display is not None:
            self.ch_display[ch] = display
        if scale is not None:
            self.ch_scales[ch] = scale
        if offset is not None:
            self.ch_offsets[ch] = offset

    def set_timebase(self, scale: Optional[float] = None, offset: Optional[float] = None):
        if scale is not None:
            self.timebase_scale = scale
        if offset is not None:
            self.timebase_offset = offset

    def get_measurements(self, ch: int) -> Dict[str, float]:
        """返回高保真仿真物理量"""
        if ch == 1:
            # 开关管 SW 节点 (24V 输入，过冲可达 32V)
            vmax = self.vin * (1.0 + self.overshoot_ratio)
            vmin = -1.2
            return {
                "vpp": float(vmax - vmin),
                "vrms": float(self.vin * np.sqrt(self.duty)),
                "vavg": float(self.vout),
                "vmax": float(vmax),
                "vmin": float(vmin),
                "freq": float(self.fsw),
                "period": float(1.0 / self.fsw),
                "rise_time": 18e-9,
                "fall_time": 15e-9,
                "duty": float(self.duty * 100),
                "overshoot": float(self.overshoot_ratio * 100),
            }
        elif ch == 2:
            # 输出纹波 (5V 典型，35mV 纹波)
            return {
                "vpp": 0.038,
                "vrms": 5.002,
                "vavg": 5.000,
                "vmax": 5.022,
                "vmin": 4.981,
                "freq": float(self.fsw),
                "period": float(1.0 / self.fsw),
                "rise_time": 120e-9,
                "fall_time": 110e-9,
                "duty": 50.0,
                "overshoot": 2.1,
            }
        else:
            return {
                "vpp": 1.0,
                "vrms": 0.707,
                "vavg": 0.0,
                "vmax": 0.5,
                "vmin": -0.5,
                "freq": 1e5,
                "period": 1e-5,
                "rise_time": 20e-9,
                "fall_time": 20e-9,
                "duty": 50.0,
                "overshoot": 0.0,
            }

    def read_waveform(self, channels: List[int] = [1], mode: str = "SCREen") -> WfmFile:
        """
        生成逼真的仿真 WFM 波形文件
        包含开关节点高频振铃与输出电容 ESR 脉冲纹波
        """
        sample_rate = 500e6  # 500 MSa/s
        n_points = 2000
        dt = 1.0 / sample_rate
        total_time = n_points * dt
        t = np.linspace(0, total_time, n_points)

        head = WfmHeadInfo(
            file_type="WFM",
            dev_name=self.device_info,
            firmware_version="V2.30",
            data_format="V1.00",
            hrzt_division=self.timebase_scale,
            hrzt_offset=self.timebase_offset,
            start_time=0.0,
            sample_rate=sample_rate,
            item_num=len(channels)
        )

        channels_packed = []

        for ch in channels:
            v_scale = self.ch_scales.get(ch, 1.0)
            v_offset = self.ch_offsets.get(ch, 0.0)

            if ch == 1:
                # 开关管 SW 节点波形: PWM + 开通关断振铃
                phase = (t * self.fsw) % 1.0
                sw_base = np.where(phase < self.duty, self.vin, 0.0)

                # 添加关断和开通时的阻尼高频振铃
                # 振铃频率 ~ 35MHz
                tau = 80e-9  # 衰减时间常数 80ns
                turn_off_time = (phase - self.duty) / self.fsw
                turn_off_mask = (turn_off_time >= 0) & (turn_off_time < 500e-9)
                ring_off = np.zeros_like(t)
                ring_off[turn_off_mask] = (
                    - 1.2
                    * np.exp(-turn_off_time[turn_off_mask] / tau)
                    * np.sin(2 * np.pi * self.f_ring * turn_off_time[turn_off_mask])
                )

                turn_on_time = phase / self.fsw
                turn_on_mask = (turn_on_time >= 0) & (turn_on_time < 500e-9)
                ring_on = np.zeros_like(t)
                ring_on[turn_on_mask] = (
                    self.vin * self.overshoot_ratio
                    * np.exp(-turn_on_time[turn_on_mask] / tau)
                    * np.sin(2 * np.pi * self.f_ring * turn_on_time[turn_on_mask])
                )

                # 叠加微弱白噪声
                noise = np.random.normal(0, 0.15, n_points)
                v_signal = sw_base + ring_on + ring_off + noise

            elif ch == 2:
                # 输出纹波: 三角波积分纹波 + ESR 开关毛刺
                phase = (t * self.fsw) % 1.0
                # 三角波
                tri = 0.020 * (np.abs(phase - 0.5) - 0.25) * 4
                # ESR 毛刺
                esr_spike = np.zeros_like(t)
                edge_on = np.exp(-((phase) / 0.02) ** 2) * 0.018
                edge_off = np.exp(-((phase - self.duty) / 0.02) ** 2) * -0.015
                noise = np.random.normal(0, 0.002, n_points)
                v_signal = self.vout + tri + edge_on + edge_off + noise

            elif ch == 3:
                # 电感电流: 带有斜率的三角波 (CCM 模式)
                phase = (t * self.fsw) % 1.0
                i_base = np.where(
                    phase < self.duty,
                    1.0 + 0.3 * (phase / self.duty - 0.5),
                    1.0 + 0.3 * (0.5 - (phase - self.duty) / (1 - self.duty))
                )
                v_signal = i_base + np.random.normal(0, 0.02, n_points)
            else:
                v_signal = 2.5 * np.sin(2 * np.pi * 50e3 * t)

            # 转换为 8-bit ADC 原始码值
            # V = ((raw - 128) / 25.0) * v_scale - v_offset
            # raw = 128 + 25.0 * (V + v_offset) / v_scale
            raw_floats = 128.0 + 25.0 * (v_signal + v_offset) / v_scale
            raw_bytes = np.clip(raw_floats, 0, 255).astype(np.uint8)

            item = WfmItemInfo(
                channel=ch - 1,
                couple_mode=0,
                bw_limit=0,
                probe_type=0,
                reversed=0,
                probe_att=1.0,
                vert_division=v_scale,
                vert_offset=v_offset,
                data_length=len(raw_bytes),
                data_offset=0
            )
            channels_packed.append((item, raw_bytes))

        # 打包并重新通过真实解码器解析，保证解析流程完全一致
        wfm_binary = pack_wfm(head, channels_packed)
        return parse_wfm(wfm_binary)

    def read_waveform_to_file(
        self,
        channels: List[int],
        target_path: str,
        mode: str = "MEMOry",
        chunk_size: int = 1024 * 1024,
        progress_callback: Optional[Any] = None,
        cancel_token: Optional[Any] = None
    ) -> Tuple[int, WfmFile]:
        """虚拟仿真驱动的磁盘直写流实现"""
        import os
        from zds_scope.protocol import parse_wfm_from_file

        os.makedirs(os.path.dirname(os.path.abspath(target_path)), exist_ok=True)
        wfm = self.read_waveform(channels=channels, mode=mode)
        raw_bytes = wfm.original_bytes
        total_len = len(raw_bytes)

        part_path = target_path + ".downloading"
        try:
            with open(part_path, "wb") as f:
                bytes_recvd = 0
                while bytes_recvd < total_len:
                    if cancel_token and getattr(cancel_token, "is_cancelled", False):
                        from zds_scope.driver import ScopeTransferCancelled
                        raise ScopeTransferCancelled("用户取消传输")
                    step = min(chunk_size, total_len - bytes_recvd)
                    chunk = raw_bytes[bytes_recvd:bytes_recvd + step]
                    f.write(chunk)
                    bytes_recvd += len(chunk)
                    if progress_callback:
                        progress_callback(bytes_recvd, total_len, 48.5, 0.05, 0.0)

            if os.path.exists(target_path):
                os.remove(target_path)
            os.rename(part_path, target_path)
        except ScopeTransferCancelled:
            cancelled_path = target_path + ".cancelled"
            if os.path.exists(part_path):
                if os.path.exists(cancelled_path):
                    os.remove(cancelled_path)
                os.rename(part_path, cancelled_path)
            raise

        parsed_wfm = parse_wfm_from_file(target_path, lazy=True)
        parsed_wfm.capture_metadata = {
            "mode": mode,
            "requested_channels": list(channels),
            "received_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "total_bytes": total_len,
            "transfer_duration_s": 0.05
        }
        return total_len, parsed_wfm


    def capture_screen(self, fmt: str = "PNG") -> bytes:
        """生成逼真的虚拟示波器屏幕截图 PNG"""
        fig, ax = plt.subplots(figsize=(8, 4.5), dpi=100)
        fig.patch.set_facecolor('#111118')
        ax.set_facecolor('#1a1a24')

        # 绘制网格
        ax.grid(True, color='#2c2d3d', linestyle='--', linewidth=0.7)
        ax.tick_params(colors='#888899', labelsize=8)
        for spine in ax.spines.values():
            spine.set_color('#3d3e52')

        # 绘制示波器模拟信号
        wfm = self.read_waveform(channels=[1, 2])
        if 1 in wfm.channels:
            ch1 = wfm.channels[1]
            t_us = ch1.time_array * 1e6
            ax.plot(t_us, ch1.voltage_array, color='#ffe600', lw=1.2, label='CH1: Switch Node (SW)')
        if 2 in wfm.channels:
            ch2 = wfm.channels[2]
            t_us = ch2.time_array * 1e6
            ax.plot(t_us, ch2.voltage_array, color='#00d9ff', lw=1.2, label='CH2: Vout Ripple')

        ax.set_title("ZLG ZDS4054 Plus - Virtual Scope Screen", color='#ffffff', fontsize=10, pad=8)
        ax.set_xlabel("Time (us)", color='#aaaaaa', fontsize=8)
        ax.set_ylabel("Voltage (V)", color='#aaaaaa', fontsize=8)
        ax.legend(facecolor='#222230', edgecolor='#444455', labelcolor='#ffffff', fontsize=8, loc='upper right')

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', facecolor=fig.get_facecolor(), edgecolor='none')
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()

    def get_loop_analysis(self) -> Dict[str, Any]:
        """生成逼真的电源环路测试 (Bode 图) 数据"""
        # 典型开关电源补偿环路: 带宽 22 kHz, 相位裕度 48.6°, 增益裕度 14.2 dB
        freqs = np.logspace(2, 6, 60)  # 100 Hz 到 1 MHz
        table = []

        fc = 22e3  # 截止频率 22 kHz
        for idx, f in enumerate(freqs, start=1):
            # 增益拟合: 低频大增益 (积分器)，交越点 0dB，高频滚降
            gain = 20 * np.log10(1 + (fc / f) ** 1.1) - 6.0
            # 相位拟合: 低频 -90°，在交越频率附近有零点提升 (+45°)，高频极点拉低
            phase = -90.0 + 55.0 * np.exp(-((np.log10(f) - np.log10(fc)) ** 2) / 1.5) - 40.0 * (f / 3e5)

            table.append({
                "index": idx,
                "freq_hz": float(f),
                "freq_str": f"{f / 1000.0:.3f}kHz" if f >= 1000 else f"{f:.1f}Hz",
                "gain_db": float(gain),
                "gain_str": f"{gain:.2f}dB",
                "phase_deg": float(phase),
                "phase_str": f"{phase:.2f}°"
            })

        return {
            "pm": "48.6°",
            "gm": "14.2dB",
            "fc": "22.5kHz",
            "table": table
        }
