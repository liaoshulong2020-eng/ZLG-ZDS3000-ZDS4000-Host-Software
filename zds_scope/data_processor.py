"""
示波器数据数字化处理与频域/谐波分析引擎 (Digital Data Processor & FFT Harmonics Engine)
将时域波形和频域谐波生成结构化测量报表
"""

import math
from typing import Dict, Any, Optional, List, Tuple
import numpy as np


class DigitalDataProcessor:
    """纯数学与信号处理引擎：负责将波形完全转化为高精度结构化文本数据"""

    @staticmethod
    def compute_fft_and_harmonics(
        time_arr: np.ndarray,
        volt_arr: np.ndarray,
        f0_hint: Optional[float] = None,
        max_harmonics: int = 15
    ) -> Dict[str, Any]:
        """
        计算高保真 FFT 频谱、提取基波及各次谐波参数，计算总谐波失真 (THD)
        :param time_arr: 时间点数组 (秒)
        :param volt_arr: 电压点数组 (伏)
        :param f0_hint: 提示基波频率 (若提供则优先在此频率附近搜索)
        :param max_harmonics: 分析的最高谐波次数 (默认前 15 次谐波)
        """
        n_points = len(volt_arr)
        if n_points < 32:
            return {
                "fundamental_freq": 0.0,
                "fundamental_amp": 0.0,
                "dc_val": float(np.mean(volt_arr)) if n_points else 0.0,
                "thd_pct": 0.0,
                "harmonics": [],
                "top_spurs": []
            }

        # 计算采样率
        dt = float(np.mean(np.diff(time_arr))) if n_points > 1 else 1e-9
        fs = 1.0 / dt if dt > 0 else 1e9

        # 去直流分量 (避免 0Hz 直流泄漏影响基波检测)
        dc_val = float(np.mean(volt_arr))
        ac_signal = volt_arr - dc_val

        # 加汉宁窗 (Hanning Window) 抑制频谱泄漏，并做幅度补偿系数 (x2)
        window = np.hanning(n_points)
        win_sum = np.sum(window)
        scale_factor = 2.0 / win_sum if win_sum > 0 else 1.0

        fft_raw = np.fft.rfft(ac_signal * window)
        spectrum_amp = np.abs(fft_raw) * scale_factor
        freqs = np.fft.rfftfreq(n_points, d=dt)

        # 1. 寻找基波频率 (Fundamental Frequency)
        if f0_hint and f0_hint > 0:
            # 在提示频率 ±25% 范围内寻找最大峰
            f_min, f_max = f0_hint * 0.75, f0_hint * 1.25
            mask = (freqs >= f_min) & (freqs <= f_max)
            if np.any(mask):
                f0_idx = np.where(mask)[0][np.argmax(spectrum_amp[mask])]
            else:
                f0_idx = np.argmax(spectrum_amp[1:]) + 1
        else:
            # 排除靠近 0Hz 的点，寻找最大峰
            f0_idx = np.argmax(spectrum_amp[1:]) + 1

        f0 = float(freqs[f0_idx])
        v1 = float(spectrum_amp[f0_idx])

        harmonics_list = []
        harmonic_powers = 0.0

        if f0 > 0 and v1 > 1e-6:
            harmonics_list.append({
                "order": 1,
                "name": "1次 (基波 Fundamental)",
                "freq_hz": f0,
                "amp_v": v1,
                "amp_dbv": 20.0 * math.log10(v1) if v1 > 0 else -100.0,
                "rel_dbc": 0.0
            })

            # 2. 搜索 2 ~ N 次谐波
            df = freqs[1] - freqs[0] if len(freqs) > 1 else 1.0
            search_window = max(2, int(f0 * 0.08 / df))  # 在谐波频点附近微窗搜索局部峰值

            for h in range(2, max_harmonics + 1):
                target_f = h * f0
                if target_f >= freqs[-1]:
                    break

                target_idx = np.argmin(np.abs(freqs - target_f))
                idx_start = max(1, target_idx - search_window)
                idx_end = min(len(freqs), target_idx + search_window + 1)

                h_local_idx = idx_start + np.argmax(spectrum_amp[idx_start:idx_end])
                h_freq = float(freqs[h_local_idx])
                h_amp = float(spectrum_amp[h_local_idx])

                rel_dbc = 20.0 * math.log10(h_amp / v1) if h_amp > 0 else -100.0
                harmonic_powers += (h_amp ** 2)

                harmonics_list.append({
                    "order": h,
                    "name": f"{h}次谐波",
                    "freq_hz": h_freq,
                    "amp_v": h_amp,
                    "amp_dbv": 20.0 * math.log10(h_amp) if h_amp > 0 else -100.0,
                    "rel_dbc": rel_dbc
                })

            thd_pct = (math.sqrt(harmonic_powers) / v1 * 100.0) if v1 > 0 else 0.0
        else:
            thd_pct = 0.0

        # 3. 搜索高频异常尖峰 / 杂散寄生振铃点 (Spurious / Ringing Peaks)
        top_spurs = []
        if f0 > 0:
            # 排除所有已知整数倍谐波邻域，寻找高频非整数倍突变峰 (如寄生 LC 振铃)
            spur_mask = np.ones(len(freqs), dtype=bool)
            spur_mask[0] = False  # 排除 DC
            for h_item in harmonics_list:
                h_idx = np.argmin(np.abs(freqs - h_item["freq_hz"]))
                w_exclude = max(3, int(f0 * 0.15 / (freqs[1] - freqs[0])))
                spur_mask[max(0, h_idx - w_exclude):min(len(freqs), h_idx + w_exclude)] = False

            if np.any(spur_mask):
                valid_indices = np.where(spur_mask)[0]
                sorted_by_amp = valid_indices[np.argsort(spectrum_amp[valid_indices])[::-1]]
                for idx in sorted_by_amp[:5]:
                    s_freq = float(freqs[idx])
                    s_amp = float(spectrum_amp[idx])
                    if s_amp > v1 * 0.01:  # 仅保留大于基波 1% 的尖峰
                        top_spurs.append({
                            "freq_hz": s_freq,
                            "amp_v": s_amp,
                            "rel_dbc": 20.0 * math.log10(s_amp / v1) if (v1 > 0 and s_amp > 0) else -100.0
                        })

        return {
            "dc_val": dc_val,
            "sample_rate_hz": fs,
            "fundamental_freq": f0,
            "fundamental_amp": v1,
            "thd_pct": thd_pct,
            "harmonics": harmonics_list,
            "top_spurs": top_spurs
        }

    @staticmethod
    def generate_summary_text(
        wfm,
        measurements: Dict[int, Dict[str, float]],
        loop_data: Optional[Dict[str, Any]] = None,
        active_ch: int = 1,
        downsample_points: int = 300,
        include_fft: bool = False
    ) -> str:
        """
        生成纯文本测量摘要和频谱报告
        不包含任何上位机自作主张的主观推测，全量输出精确的浮点数字与表格
        """
        lines = []
        lines.append("# 示波器实测波形与数字数据报表 (Digitized Measurement Dataset)")
        lines.append("> 测量摘要：包含抽样时域点、硬件测量值及已读取的环路数据；不是全量记录，不含仪器 FFT 频谱数组。\n")

        # 1. 采集元数据
        lines.append("## 一、数据采集元信息 (Acquisition Metadata)")
        if wfm and hasattr(wfm, "head"):
            lines.append(f"- **仪器型号**: `{wfm.head.dev_name}`")
            lines.append(f"- **硬件采样率 (Sample Rate)**: `{wfm.head.sample_rate:.4e} Hz` ({wfm.head.sample_rate/1e6:.2f} MSa/s)")
            lines.append(f"- **水平时基档位 (Timebase Scale)**: `{wfm.head.hrzt_division:.4e} s/div`")
            lines.append(f"- **通道数**: `{len(wfm.channels)}`")
            if hasattr(wfm, "capture_metadata") and wfm.capture_metadata:
                dep_m = wfm.capture_metadata.get("channel_combination_depth_m")
                if dep_m is not None:
                    lines.append(f"- **硬件存储深度 (Memory Depth)**: `{dep_m} Mpts`")
                iv = wfm.capture_metadata.get("integrity_verification")
                if iv:
                    lines.append(f"- **数据完整性结果**: `[{iv.get('integrity_status')}]` {iv.get('description')}")
                    lines.append(f"- **预期点数 vs 收到点数**: 预期 `{iv.get('expected_points_per_channel')}` 点/通道，收到 `{iv.get('received_points_per_channel')}`")
            elif hasattr(wfm, "channels") and wfm.channels:
                from .memory_depth import expected_depth_m
                dep_m = expected_depth_m(list(wfm.channels.keys()))
                if dep_m is not None:
                    lines.append(f"- **硬件存储深度 (Memory Depth)**: `{dep_m} Mpts`")
        lines.append("\n")

        # 2. 硬件测量参数表 (示波器硬件 FPGA 计算，IEEE 标准)
        lines.append("## 二、示波器硬件精确测量参数 (Hardware Measurements)")
        lines.append("| 测量指标 (Parameter) | CH1 | CH2 | CH3 | CH4 | 物理单位 |")
        lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

        keys_map = [
            ("峰峰值 (Peak-to-Peak, Vpp)", "vpp", "V"),
            ("真有效值 (True RMS, Vrms)", "vrms", "V"),
            ("直流平均值 (Mean/Average, Vavg)", "vavg", "V"),
            ("最大值 (Maximum, Vmax)", "vmax", "V"),
            ("最小值 (Minimum, Vmin)", "vmin", "V"),
            ("信号频率 (Frequency)", "freq", "Hz"),
            ("信号周期 (Period)", "period", "s"),
            ("正占空比 (Duty Cycle)", "duty", "%"),
            ("瞬态过冲 (Overshoot)", "overshoot", "%"),
            ("上升时间 (Rise Time, 10%-90%)", "rise_time", "s"),
            ("下降时间 (Fall Time, 90%-10%)", "fall_time", "s"),
        ]

        for label, k, unit in keys_map:
            row = [label]
            for ch in [1, 2, 3, 4]:
                ch_meas = measurements.get(ch, {})
                val = ch_meas.get(k, None)
                if val is not None:
                    if unit == "Hz":
                        row.append(f"{val/1e3:.3f} kHz" if val >= 1000 else f"{val:.2f} Hz")
                    elif unit == "s":
                        row.append(f"{val*1e9:.1f} ns" if val < 1e-6 else (f"{val*1e6:.2f} us" if val < 1e-3 else f"{val*1e3:.2f} ms"))
                    elif unit == "%":
                        row.append(f"{val:.2f} %")
                    else:
                        physical_unit = "A" if wfm and ch in wfm.channels and wfm.channels[ch].info.probe_type == 1 else "V"
                        row.append(f"{val:.4f} {physical_unit}")
                else:
                    row.append("---")
            row.append("见各通道" if unit == "V" else unit)
            lines.append("| " + " | ".join(row) + " |")
        lines.append("\n")

        # 3. FFT 频域与各次谐波量化分析
        if include_fft and wfm and active_ch in wfm.channels:
            ch_data = wfm.channels[active_ch]
            physical_unit = "A" if ch_data.info.probe_type == 1 else "V"
            f_hint = measurements.get(active_ch, {}).get("freq", None)
            from .records import blocks
            _, fft_time, fft_values, _ = next(blocks(ch_data, 0, min(len(ch_data.raw_data), 200000)))
            lines.append(f"> FFT 使用起始连续 {len(fft_values)} 个采样点；不是全记录频谱。\n")
            fft_res = DigitalDataProcessor.compute_fft_and_harmonics(
                fft_time,
                fft_values,
                f0_hint=f_hint,
                max_harmonics=12
            )

            lines.append(f"## 三、CH{active_ch} FFT 频谱与谐波分析数据 (Harmonics & Spectrum)")
            lines.append(f"- **基波频率 (f0)**: `{fft_res['fundamental_freq']/1e3:.3f} kHz`")
            lines.append(f"- **基波峰值幅值**: `{fft_res['fundamental_amp']:.4f} {physical_unit}`")
            lines.append(f"- **直流偏移分量 (DC Component)**: `{fft_res['dc_val']:.4f} {physical_unit}`")
            lines.append(f"- **总谐波失真 (THD)**: `{fft_res['thd_pct']:.2f} %`\n")

            lines.append("### 谐波分量表 (Harmonics Decomposition):")
            lines.append(f"| 次数 | 名称 | 实际频率 (kHz) | 峰值幅度 ({physical_unit}) | 幅度 (dB{physical_unit}) | 相对基波 (dBc) |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- |")

            for h in fft_res.get("harmonics", []):
                lines.append(
                    f"| {h['order']} | {h['name']} | `{h['freq_hz']/1e3:.3f}` | `{h['amp_v']:.4f}` | `{h['amp_dbv']:.2f}` | `{h['rel_dbc']:.2f}` |"
                )
            lines.append("\n")

            if fft_res.get("top_spurs"):
                lines.append("### 高频杂散 / 寄生振铃尖峰频点 (Top Spurious/Resonance Peaks):")
                lines.append(f"| 序号 | 寄生谐振频率 (MHz) | 尖峰幅值 ({physical_unit}) | 相对基波 (dBc) |")
                lines.append("| :--- | :--- | :--- | :--- |")
                for idx, sp in enumerate(fft_res["top_spurs"], start=1):
                    lines.append(f"| {idx} | `{sp['freq_hz']/1e6:.3f} MHz` | `{sp['amp_v']:.4f} {physical_unit}` | `{sp['rel_dbc']:.2f} dBc` |")
                lines.append("\n")

        # 4. 电源环路 Bode 分析扫频数据
        if loop_data and loop_data.get("table"):
            lines.append("## 四、电源环路响应测试数据 (Loop Bode Analysis)")
            lines.append(f"- **相位裕度 (Phase Margin, PM)**: `{loop_data.get('pm', '---')}`")
            lines.append(f"- **增益裕度 (Gain Margin, GM)**: `{loop_data.get('gm', '---')}`\n")
            lines.append("| 频点序号 | 频率 (Frequency) | 增益 (Gain dB) | 相位 (Phase deg) |")
            lines.append("| :--- | :--- | :--- | :--- |")
            # 抽样前 15 行或关键交越点行展示
            table = loop_data.get("table", [])
            step = max(1, len(table) // 15)
            for row in table[::step]:
                f_s = row.get("freq_str", f"{row.get('freq_hz', 0):.1f} Hz")
                g_s = row.get("gain_str", f"{row.get('gain_db', 0):.2f} dB")
                p_s = row.get("phase_str", f"{row.get('phase_deg', 0):.2f} °")
                lines.append(f"| {row.get('index', '')} | `{f_s}` | `{g_s}` | `{p_s}` |")
            lines.append("\n")

        # 5. 物理量电压标定与真机硬件对照核验
        if wfm:
            from .protocol import verify_waveform_conversion
            v_check = verify_waveform_conversion(wfm, measurements or {}) if sum(len(d.raw_data) for d in wfm.channels.values()) <= 200000 else {}
            lines.append("## 五、物理量电压标定与真机硬件对照核验 (Calibration Verification)")
            lines.append("> 提示: 针对示波器 ADC 原生码值换算进行真机测量值对照核验，防止幅值分析失真。\n")
            lines.append("| 通道 | ADC 原始码范围 | 垂直档位 | 垂直偏置 | 换算 Vpp | 硬件 DSP Vpp | 相对误差 | 核验状态 |")
            lines.append("| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |")
            for ch_id, vc in v_check.items():
                err_str = f"{vc['vpp_error_pct']:.2f}%" if vc['vpp_error_pct'] is not None else "---"
                physical_unit = "A" if wfm.channels[ch_id].info.probe_type == 1 else "V"
                disp_dsp = f"{vc['hardware_dsp_vpp']:.4f} {physical_unit}" if vc['hardware_dsp_vpp'] is not None else "---"
                status_icon = "Vpp 对比一致（非全项标定）" if vc['verification_status'] == 'MATCH' else ("未核验" if vc['verification_status'] == 'NOT_CHECKED' else "存在差异")
                lines.append(
                    f"| CH{ch_id} | `[{vc['raw_code_min']} .. {vc['raw_code_max']}]` (零点128) | "
                    f"`{vc['vert_division']} {physical_unit}/div` | `{vc['vert_offset']} {physical_unit}` | "
                    f"`{vc['calculated_vpp']:.4f} {physical_unit}` | `{disp_dsp}` | "
                    f"`{err_str}` | `{status_icon}` |"
                )
            lines.append("\n")

        # 6. 时域波形抽样点
        if wfm and active_ch in wfm.channels:
            ch_data = wfm.channels[active_ch]
            n_total = len(ch_data.raw_data)
            t_sub, v_sub = ch_data.get_downsampled(max_points=downsample_points)

            lines.append(f"## 六、CH{active_ch} 时域采样点数据 (Time-Domain Array, 抽样 {len(v_sub)} 点, 全量 {n_total} 点)")
            lines.append("```csv")
            physical_unit = "A" if ch_data.info.probe_type == 1 else "V"
            lines.append(f"Time(s),DerivedValue({physical_unit})")
            for t_val, v_val in zip(t_sub, v_sub):
                lines.append(f"{t_val:.6e},{v_val:.5f}")
            lines.append("```\n")

        lines.append("摘要只包含抽样点；完整数据请查看原始记录及全量 CSV。")

        return "\n".join(lines)
