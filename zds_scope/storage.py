import os
import csv
import json
import time
import hashlib
import zipfile
import re
import shutil
from dataclasses import asdict
from datetime import datetime
from typing import Optional, List, Dict, Any
import numpy as np

from .protocol import (
    WfmFile,
    pack_wfm,
    raw_code_to_voltage,
    verify_waveform_conversion
)


class ScopeStorage:
    """数据存储与导出工具类"""

    @staticmethod
    def save_npz_stream(wfm, filepath):
        """Write standard NPZ members without materializing full float64 arrays."""
        from .records import blocks
        from numpy.lib.format import write_array_header_1_0, dtype_to_descr
        with zipfile.ZipFile(filepath, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for channel, data in sorted(wfm.channels.items()):
                for kind in ("raw", "time_s", "current_a" if data.info.probe_type == 1 else "voltage_v"):
                    array = data.raw_data if kind == "raw" else (data._time_array if kind == "time_s" else data._voltage_array)
                    length = len(array) if array is not None else len(data.raw_data)
                    dtype = data.raw_data.dtype if kind == "raw" else np.dtype("<f8")
                    with archive.open(f"ch{channel}_{kind}.npy", "w", force_zip64=True) as stream:
                        write_array_header_1_0(stream, {"descr": dtype_to_descr(dtype), "fortran_order": False, "shape": (length,)})
                        if kind == "raw":
                            for start in range(0, length, 262144):
                                stream.write(data.raw_data[start:start + 262144].tobytes())
                        else:
                            for _, times, values, _ in blocks(data, 0, length):
                                stream.write(np.asarray(times if kind == "time_s" else values, dtype=dtype).tobytes())

    @staticmethod
    def ensure_dir(path: str):
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)

    @staticmethod
    def estimate_storage_requirements(
        num_channels: int,
        points_per_channel: int,
        options: Optional[Dict[str, bool]] = None,
        target_dir: str = "data/captures"
    ) -> Dict[str, Any]:
        """
        估算导出数据所需的磁盘空间（专为 256M / 512M 大存储深度设计）
        """
        opts = options or {}
        raw_bytes = 424 + 120 * num_channels + num_channels * points_per_channel
        meta_bytes = 120 * 1024
        screen_bytes = 500 * 1024
        total = raw_bytes + meta_bytes + screen_bytes
        breakdown = {
            "原生 WFM 二进制 (.wfm)": raw_bytes,
            "元数据与验证报表 (JSON)": meta_bytes,
            "示波器屏幕截图 (PNG)": screen_bytes
        }

        if opts.get("export_full_csv") or opts.get("legacy_full_bundle"):
            csv_bytes = points_per_channel * (18 + 15 * num_channels)
            breakdown["全量总 CSV (waveform.csv)"] = csv_bytes
            total += csv_bytes

        if opts.get("export_channel_csv") or opts.get("legacy_full_bundle"):
            ch_csv_bytes = points_per_channel * num_channels * 28
            breakdown["分通道独立 CSV (CH*_full.csv)"] = ch_csv_bytes
            total += ch_csv_bytes

        if opts.get("export_npz") or opts.get("legacy_full_bundle"):
            npz_bytes = points_per_channel * num_channels * 16
            breakdown["NumPy 数组包 (.npz)"] = npz_bytes
            total += npz_bytes

        if opts.get("create_zip") or opts.get("legacy_full_bundle"):
            zip_est = int(total * 0.45)
            breakdown["ZIP 归档压缩包 (.zip)"] = zip_est
            total += zip_est

        try:
            abs_dir = os.path.abspath(target_dir)
            ScopeStorage.ensure_dir(abs_dir)
            free_bytes = shutil.disk_usage(abs_dir).free
        except Exception:
            free_bytes = 100 * 1024 * 1024 * 1024

        def fmt_size(b: int) -> str:
            if b >= 1024 ** 3:
                return f"{b / (1024 ** 3):.2f} GB"
            elif b >= 1024 ** 2:
                return f"{b / (1024 ** 2):.1f} MB"
            elif b >= 1024:
                return f"{b / 1024:.1f} KB"
            return f"{b} B"

        sufficient = free_bytes > total * 1.15
        return {
            "estimated_total_bytes": total,
            "estimated_total_str": fmt_size(total),
            "free_bytes": free_bytes,
            "free_str": fmt_size(free_bytes),
            "has_sufficient_space": sufficient,
            "breakdown": {k: fmt_size(v) for k, v in breakdown.items()},
            "warning": None if sufficient else f"磁盘剩余空间 ({fmt_size(free_bytes)}) 紧缺，预计需要 {fmt_size(total)}"
        }

    @staticmethod
    def save_wfm(wfm: WfmFile, filepath: str) -> str:
        """保存为标准的 ZLG .wfm 原生二进制文件"""
        ScopeStorage.ensure_dir(os.path.dirname(os.path.abspath(filepath)))
        if wfm.original_bytes is not None:
            with open(filepath, "wb") as f:
                f.write(wfm.original_bytes)
        elif wfm.file_path and os.path.exists(wfm.file_path) and os.path.abspath(wfm.file_path) != os.path.abspath(filepath):
            shutil.copyfile(wfm.file_path, filepath)
        else:
            channels_packed = []
            for ch_id, ch_data in sorted(wfm.channels.items()):
                channels_packed.append((ch_data.info, ch_data.raw_data))
            binary_data = pack_wfm(wfm.head, channels_packed)
            with open(filepath, "wb") as f:
                f.write(binary_data)
        return filepath

    @staticmethod
    def save_csv(wfm: WfmFile, filepath: str, progress_callback=None, cancel_check=None) -> str:
        """Write atomically, allow cancellation, and retain partial output on failure."""
        import tempfile
        folder = os.path.dirname(os.path.abspath(filepath))
        ScopeStorage.ensure_dir(folder)
        fd, partial = tempfile.mkstemp(prefix=os.path.basename(filepath) + ".", suffix=".partial", dir=folder)
        os.close(fd)
        try:
            ScopeStorage._save_csv_blocks(wfm, partial, progress_callback, cancel_check)
            if cancel_check and cancel_check():
                raise InterruptedError("用户取消导出")
            os.replace(partial, filepath)
            return filepath
        except InterruptedError as error:
            raise InterruptedError(f"已取消导出；原始记录不受影响，部分 CSV 保存在 {partial}") from error

    @staticmethod
    def _save_csv_blocks(wfm: WfmFile, filepath: str, progress_callback=None, cancel_check=None) -> str:
        """
        导出带有时间戳与通道校准物理电压的 CSV 文件
        采用 50,000 点块级流式输出，杜绝超大存储深度时内存溢出
        """
        ScopeStorage.ensure_dir(os.path.dirname(os.path.abspath(filepath)))
        if not wfm.channels:
            raise ValueError("波形数据中无可用通道")

        sorted_channels = sorted(wfm.channels)
        header = []
        for ch in sorted_channels:
            header.extend([f"CH{ch}_Time(s)", f"CH{ch}_Current(A)" if wfm.channels[ch].info.probe_type == 1 else f"CH{ch}_Voltage(V)"])

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(header)

            def get_ch_len(c):
                obj = wfm.channels[c]
                if obj._time_array is not None:
                    return len(obj._time_array)
                return len(obj.raw_data)

            max_len = max(get_ch_len(ch) for ch in sorted_channels)
            chunk_size = 8192
            last_progress = 0.0
            for start in range(0, max_len, chunk_size):
                if cancel_check and cancel_check():
                    raise InterruptedError("用户取消导出")
                end = min(start + chunk_size, max_len)
                cols = []
                for ch in sorted_channels:
                    ch_obj = wfm.channels[ch]
                    n_ch = get_ch_len(ch)
                    if start < n_ch:
                        s_ch = start
                        e_ch = min(end, n_ch)
                        if ch_obj._time_array is not None:
                            t_sub = ch_obj._time_array[s_ch:e_ch]
                        else:
                            t_sub = ch_obj.start_time + np.arange(s_ch, e_ch) * ch_obj.dt

                        if ch_obj._voltage_array is not None:
                            v_sub = ch_obj._voltage_array[s_ch:e_ch]
                        else:
                            v_sub = raw_code_to_voltage(
                                ch_obj.raw_data[s_ch:e_ch],
                                ch_obj.info.vert_division,
                                ch_obj.info.vert_offset,
                                ch_obj.info.probe_att,
                                ch_obj.info.reversed
                            )
                        cols.extend([t_sub, v_sub])
                    else:
                        cols.extend([None, None])

                for row_idx in range(end - start):
                    row = []
                    for c_idx in range(0, len(cols), 2):
                        t_col = cols[c_idx]
                        v_col = cols[c_idx + 1]
                        if (t_col is not None and v_col is not None
                                and row_idx < len(t_col) and row_idx < len(v_col)):
                            row.append(format(float(t_col[row_idx]), ".17g"))
                            row.append(format(float(v_col[row_idx]), ".17g"))
                        else:
                            row.append("")
                            row.append("")
                    writer.writerow(row)

                now = time.monotonic()
                if progress_callback and (now - last_progress >= 0.2 or end == max_len):
                    progress_callback(end, max_len)
                    last_progress = now
                # Give the GUI a regular turn even during long Python formatting loops.
                time.sleep(0.001)


        return filepath

    @staticmethod
    def save_loop_csv(loop_data: Dict[str, Any], filepath: str) -> str:
        """导出环路测试 (Bode 图) 数据为 CSV"""
        ScopeStorage.ensure_dir(os.path.dirname(os.path.abspath(filepath)))
        table = loop_data.get("table", [])

        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([f"# Power Loop Bode Analysis"])
            writer.writerow([f"# Phase Margin (PM): {loop_data.get('pm', '---')}"])
            writer.writerow([f"# Gain Margin (GM): {loop_data.get('gm', '---')}"])
            writer.writerow(["Index", "Frequency(Hz)", "Gain(dB)", "Phase(deg)"])

            for row in table:
                writer.writerow([
                    row.get("index", ""),
                    row.get("freq_hz", row.get("freq_str", "")),
                    row.get("gain_db", row.get("gain_str", "")),
                    row.get("phase_deg", row.get("phase_str", ""))
                ])

        return filepath

    @staticmethod
    def save_json(data: Dict[str, Any], filepath: str) -> str:
        """保存字典或元数据为 JSON"""
        ScopeStorage.ensure_dir(os.path.dirname(os.path.abspath(filepath)))
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return filepath

    @staticmethod
    def save_snapshot(
        base_dir: str = "data/captures",
        session_name: str = "capture",
        wfm: Optional[WfmFile] = None,
        screenshot_bytes: Optional[bytes] = None,
        measurements: Optional[Dict[str, Any]] = None,
        loop_data: Optional[Dict[str, Any]] = None,
        analysis_report_md: Optional[str] = None,
        export_options: Optional[Dict[str, bool]] = None,
        integrity_info: Optional[Dict[str, Any]] = None,
        cancel_check=None,
        analysis_progress=None
    ) -> str:
        """
        保存测试归档包
        默认策略：仅保存紧凑原生二进制 .wfm 与核心元数据，杜绝重复冗余；
        可选导出：CSV、NPZ、ZIP 按需生成，避免 512M 数据造成磁盘耗尽或长时间卡顿。
        """
        opts = export_options or {}
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", session_name)[:80].strip(" .") or "capture"
        folder_name = f"{timestamp_str}_{safe_name}"
        target_dir = os.path.join(base_dir, folder_name)
        ScopeStorage.ensure_dir(target_dir)

        # 1. 保存原生 WFM 二进制文件
        if wfm:
            ScopeStorage.save_wfm(wfm, os.path.join(target_dir, "waveform.wfm"))

        # 2. 按需保存合并总 CSV
        if wfm and (opts.get("export_full_csv") or opts.get("legacy_full_bundle")):
            ScopeStorage.save_csv(wfm, os.path.join(target_dir, "waveform.csv"))

        # 3. 保存截图
        if screenshot_bytes:
            with open(os.path.join(target_dir, "screen.png"), "wb") as f:
                f.write(screenshot_bytes)

        # 4. 保存测量参数 JSON
        if measurements:
            ScopeStorage.save_json(measurements, os.path.join(target_dir, "measurements.json"))

        # 5. 保存环路测试
        if loop_data and loop_data.get("table"):
            ScopeStorage.save_loop_csv(loop_data, os.path.join(target_dir, "bode_loop.csv"))
            ScopeStorage.save_json(loop_data, os.path.join(target_dir, "bode_loop.json"))

        # 6. 保存测量报告
        if analysis_report_md:
            with open(os.path.join(target_dir, "analysis_report.md"), "w", encoding="utf-8") as f:
                f.write(analysis_report_md)

        # 7. 元数据与核验结果 (含完整性与真机硬件测量对照)
        if wfm:
            conversion_verification = verify_waveform_conversion(wfm, measurements or {})
            metadata = {
                "schema_version": 2,
                "exported_at": datetime.now().astimezone().isoformat(),
                "capture": wfm.capture_metadata,
                "integrity_verification": integrity_info or wfm.capture_metadata.get("integrity_verification", {}),
                "voltage_conversion_verification": conversion_verification,
                "header": asdict(wfm.head),
                "channels": {},
                "export_options": opts
            }

            for ch, data in sorted(wfm.channels.items()):
                metadata["channels"][str(ch)] = {
                    "item": asdict(data.info),
                    "points": len(data.raw_data),
                    "raw_dtype": str(data.raw_data.dtype),
                    "raw_bytes": int(data.raw_data.nbytes),
                    "time_unit": "s",
                    "derived_value_unit": "A" if data.info.probe_type == 1 else "V",
                    "sample_rate_hz": wfm.head.sample_rate
                }

                # 按需流式导出分通道 CSV
                if opts.get("export_channel_csv") or opts.get("legacy_full_bundle"):
                    ch_csv_path = os.path.join(target_dir, f"CH{ch}_full.csv")
                    with open(ch_csv_path, "w", newline="", encoding="utf-8") as f:
                        writer = csv.writer(f)
                        writer.writerow(["sample_index", "time_s", "derived_current_a" if data.info.probe_type == 1 else "derived_voltage_v", "raw_code"])
                        for s_idx, t_blk, v_blk, r_blk in data.stream_blocks(block_size=50000):
                            for offset in range(len(r_blk)):
                                writer.writerow([
                                    s_idx + offset,
                                    format(float(t_blk[offset]), ".17g"),
                                    format(float(v_blk[offset]), ".17g"),
                                    int(r_blk[offset])
                                ])

            if opts.get("export_npz") or opts.get("legacy_full_bundle"):
                ScopeStorage.save_npz_stream(wfm, os.path.join(target_dir, "waveform_full.npz"))

            ScopeStorage.save_json(metadata, os.path.join(target_dir, "metadata.json"))
            from .records import scan_record, save_json
            save_json(os.path.join(target_dir, "test_context.json"), wfm.capture_metadata.get("test_context", {}))
            save_json(os.path.join(target_dir, "settings_snapshot.json"), wfm.capture_metadata.get("hardware_configuration", {}))
            if opts.get("analyze_events"):
                try:
                    report = scan_record(wfm, cancelled=cancel_check, progress=analysis_progress)
                except RuntimeError as error:
                    if cancel_check and cancel_check():
                        from .driver import ScopeTransferCancelled
                        save_json(os.path.join(target_dir, "analysis_status.json"), {"status": "cancelled", "raw_record_preserved": True})
                        raise ScopeTransferCancelled(f"分析已取消；已接收的完整原始记录保留于 {os.path.abspath(target_dir)}") from error
                    raise
                save_json(os.path.join(target_dir, "analysis_events.json"), report)
            with open(os.path.join(target_dir, "integrity_report.md"), "w", encoding="utf-8") as f:
                f.write("# 采集完整性报告\n\n" + json.dumps(metadata["integrity_verification"], ensure_ascii=False, indent=2)
                        + "\n\n原始字节完整接收、采样点数匹配、触发完成及物理量解码验证为不同检查项。\n"
                        "峰值/平均/高分辨率记录的数值和时间编码仍需真机标定；未验证时不得宣称满深度/校准通过。\n")
            with open(os.path.join(target_dir, "RECORD_README.md"), "w", encoding="utf-8") as f:
                f.write("# 数据记录说明\n\n请结合 test_context.json、settings_snapshot.json、metadata.json 与原始 waveform.wfm 分析。\n"
                        "analysis_events.json（如有）为逐块扫描整条记录的候选事件，不是确定故障；核对原始码、采集模式和探头单位。\n"
                        "CSV 可按需导出。区域 CSV 保留原始索引/时间和来源信息，原始全量 WFM 始终独立保留。\n"
                        "请检查触发源、阈值、采样率、记录长度、削顶、尖峰、丢脉冲、周期抖动，并引用具体采样索引和时间。\n"
                        "未验证编码、混叠风险、不同采样时刻的硬件测量与波形差异应明确说明。\n")

            # 生成规范说明文件
            with open(os.path.join(target_dir, "README_METADATA.md"), "w", encoding="utf-8") as f:
                f.write(f"""# 致远示波器数据归档包
- 归档时间: {metadata['exported_at']}
- 采样率: {wfm.head.sample_rate:.4e} Hz
- 通道数: {len(wfm.channels)}

## 完整性核对结果 (Integrity Verification)
```json
{json.dumps(metadata['integrity_verification'], ensure_ascii=False, indent=2)}
```

## 电压换算与硬件测量对照核验 (Conversion Verification)
```json
{json.dumps(conversion_verification, ensure_ascii=False, indent=2)}
```
""")

        # 8. 生成校验清单 (Manifest)
        manifest = {}
        for name in sorted(os.listdir(target_dir)):
            path = os.path.join(target_dir, name)
            if os.path.isfile(path):
                digest = hashlib.sha256()
                with open(path, "rb") as f:
                    for block in iter(lambda: f.read(1024 * 1024), b""):
                        digest.update(block)
                manifest[name] = {"bytes": os.path.getsize(path), "sha256": digest.hexdigest()}
        ScopeStorage.save_json(manifest, os.path.join(target_dir, "manifest.json"))

        # 9. 按需打包为 ZIP
        if opts.get("create_zip") or opts.get("legacy_full_bundle"):
            zip_target = target_dir + ".zip"
            with zipfile.ZipFile(zip_target, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                for name in sorted(os.listdir(target_dir)):
                    item_path = os.path.join(target_dir, name)
                    if os.path.isfile(item_path):
                        z.write(item_path, name)

        return target_dir

