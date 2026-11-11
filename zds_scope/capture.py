"""Fresh memory acquisition shared by GUI and API; no cached waveform fallback."""
import os
import tempfile
import time
from datetime import datetime
from contextlib import nullcontext
from typing import Optional, List, Dict, Any

from .storage import ScopeStorage
from .memory_depth import expected_depth_m


def capture_archive(
    driver,
    channels=None,
    session_name="capture",
    base_dir="data/captures",
    export_options: Optional[Dict[str, bool]] = None,
    progress_callback: Optional[Any] = None,
    cancel_token: Optional[Any] = None,
    test_context: Optional[Dict[str, Any]] = None,
    stage_callback: Optional[Any] = None
):
    channels = [1, 2, 3, 4] if channels is None else list(channels)
    if not channels or any(ch not in (1, 2, 3, 4) for ch in channels):
        raise ValueError("请选择 CH1–CH4 中的至少一个通道")

    opts = export_options or {}
    def stage(message):
        if stage_callback:
            stage_callback(message)
        if cancel_token and getattr(cancel_token, "is_cancelled", False):
            from .driver import ScopeTransferCancelled
            raise ScopeTransferCancelled("已取消采集")

    wfm = None

    with getattr(driver, "io_lock", nullcontext()):
        started = datetime.now().astimezone().isoformat()
        previous_state = driver.get_run_state()
        errors = {}

        stage("读取仪器当前设置（保留实际触发源）")
        actual_config = {}
        if hasattr(driver, "get_scope_actual_configuration"):
            try:
                actual_config = driver.get_scope_actual_configuration()
            except Exception as e:
                errors["actual_config_query"] = str(e)

        # 2. 预期点数评估
        expected_points = None
        raw_mdep = actual_config.get("mdepth_raw")
        if raw_mdep and str(raw_mdep).isdigit():
            expected_points = int(raw_mdep)
        # Channel-combination capacity is not evidence of the current record length.


        # 3. 磁盘空间预估检查
        est = ScopeStorage.estimate_storage_requirements(
            num_channels=len(channels),
            points_per_channel=expected_points or 10000,
            options=opts,
            target_dir=base_dir
        )
        if not est["has_sufficient_space"]:
            raise RuntimeError(f"磁盘空间不足: 预计需要 {est['estimated_total_str']}，剩余空间仅 {est['free_str']}")

        temp_wfm_path = None
        observed_trigger_state = actual_config.get("extended_settings", {}).get(":TRIG:STATE")
        try:
            if opts.get("wait_for_trigger"):
                deadline = time.monotonic() + float(opts.get("trigger_timeout_s", 60))
                while True:
                    stage("等待当前采集触发完成（不重发 SINGLE，不改变触发源）")
                    observed_trigger_state = driver.query(":TRIG:STATE?").strip()
                    if observed_trigger_state.upper() in ("TRIG", "STOP"):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeError("等待触发超时：没有下载新记录，仪器触发设置保持不变")
                    time.sleep(.2)
            stage("停止并确认已有记录，准备下载；不会重新触发")
            if hasattr(driver, "stop_and_wait"):
                stopped_state = driver.stop_and_wait()
            else:
                driver.stop()
                stopped_state = driver.get_run_state()
            if "stop" not in stopped_state.lower():
                raise RuntimeError(f"无法确认示波器已停止: {stopped_state}")

            # 4. 流式或直接读取波形
            stage("正在下载 MEMOry 原始数据")
            if hasattr(driver, "read_waveform_to_file"):
                with tempfile.NamedTemporaryFile(suffix=".wfm", delete=False) as tf:
                    temp_wfm_path = tf.name
                _, wfm = driver.read_waveform_to_file(
                    channels=channels,
                    target_path=temp_wfm_path,
                    mode="MEMOry",
                    progress_callback=progress_callback,
                    cancel_token=cancel_token
                )
            else:
                wfm = driver.read_waveform(channels=channels, mode="MEMOry")

            missing = sorted(set(channels) - set(wfm.channels))
            if missing:
                raise RuntimeError(f"设备未返回请求通道: {missing}，本次不生成完整归档")

            measurements = {}
            for ch in channels:
                try:
                    measurements[ch] = driver.get_measurements(ch)
                except Exception as exc:
                    errors[f"measurements_CH{ch}"] = str(exc)

            screenshot = None
            if opts.get("capture_screenshot", True):
                try:
                    screenshot = driver.capture_screen(fmt="PNG")
                except Exception as exc:
                    errors["screenshot"] = str(exc)

            # 5. 完整性核对结果 ("预期点数／收到点数／完整性结果")
            received_points = {str(ch): len(data.raw_data) for ch, data in wfm.channels.items()}
            channels_match = set(channels) == set(wfm.channels.keys())
            enabled_scope_channels = actual_config.get("enabled_channels", channels)

            if expected_points and any(pts < expected_points for pts in received_points.values()):
                integrity_status = "WARNING_DEPTH_MISMATCH"
                integrity_desc = f"设置深度 {expected_points} 点，实际收到 {min(received_points.values())} 原始码；需核对有效记录长度和编码，不能据此认定丢包"
            elif not channels_match:
                integrity_status = "MISMATCH"
                integrity_desc = f"通道不一致: 请求通道 {channels}，实际返回 {list(wfm.channels.keys())}"
            elif expected_points is None:
                integrity_status = "UNVERIFIED_RECORD_LENGTH"
                integrity_desc = "设备未返回确定的当前点数；已保留全部返回字节，不能证明读满存储深度"
            elif any(pts != expected_points for pts in received_points.values()):
                integrity_status = "UNVERIFIED_ENCODING_OR_LENGTH"
                integrity_desc = "原始码数量与深度不一致，需核对采集模式、编码宽度和实际记录长度"
            else:
                integrity_status = "PASS"
                integrity_desc = f"完整性核验通过: 全部 {len(channels)} 个通道均足额返回 {next(iter(received_points.values())) if received_points else 0} 点"

            integrity_info = {
                "instrument_actual_channels": enabled_scope_channels,
                "requested_channels": channels,
                "channels_match": channels_match,
                "instrument_mdepth_setting": raw_mdep or "AUTO",
                "expected_points_per_channel": expected_points,
                "received_points_per_channel": received_points,
                "integrity_status": integrity_status,
                "description": integrity_desc
                , "length_comparison_basis": "uint8 raw-code count, pending encoding validation",
                "transport_received_bytes": wfm.capture_metadata.get("total_bytes"),
                "capture_mode": wfm.head.acq_mode,
                "trigger_source_header": wfm.head.trig_source + 1,
                "decoding_status": "UNVERIFIED_WITH_REAL_CALIBRATION",
                "acquisition_completed_before_download": "not_proven_by_stop_command",
                "header_start_time_s": wfm.head.start_time,
                "estimated_trigger_index": (-wfm.head.start_time * wfm.head.sample_rate)
                    if wfm.head.sample_rate > 0 and 0 <= -wfm.head.start_time * wfm.head.sample_rate < min(received_points.values()) else None,
                "trigger_index_status": "estimated_from_header; timing/peak_encoding_unverified",
                "observed_trigger_state_before_stop": observed_trigger_state,
                "trigger_wait_requested": bool(opts.get("wait_for_trigger")),
            }

            wfm.capture_metadata = {
                "started_at": started,
                "finished_at": datetime.now().astimezone().isoformat(),
                "mode": "MEMOry",
                "requested_channels": channels,
                "channel_combination_depth_m": expected_depth_m(channels),
                "depth_rule_source": "user_supplied_channel_combination",
                "depth_rule_basis": "requested_channels; actual enabled channels must match",
                "returned_points": received_points,
                "device_id": getattr(driver, "device_info", "unknown"),
                "host": getattr(driver, "host", "unknown"),
                "port": getattr(driver, "port", 5025),
                "driver": type(driver).__name__,
                "previous_run_state": previous_state,
                "stopped_state": stopped_state,
                "optional_read_errors": errors,
                "measurement_raw_responses": getattr(driver, "last_measurement_responses", {}),
                "loop_data": "not_requested_separate_test",
                "integrity_verification": integrity_info,
                "hardware_configuration": actual_config
                , "test_context": test_context or {},
                "record_source": "existing_memory_buffer; no new SINGLE command issued",
            }
        finally:
            if previous_state.strip().lower() == "run" and driver.is_connected:
                driver.run()

    # 6. 保存快照并执行按需导出
    stage("正在保存原始记录、配置、校验与报告")
    path = ScopeStorage.save_snapshot(
        base_dir=base_dir,
        session_name=session_name,
        wfm=wfm,
        screenshot_bytes=screenshot,
        measurements=measurements,
        export_options=opts,
        integrity_info=integrity_info,
        cancel_check=lambda: bool(cancel_token and getattr(cancel_token, "is_cancelled", False)),
        analysis_progress=lambda done, total: stage(f"正在扫描全记录异常候选 {done:,}/{total:,} 原始码")
    )
    if temp_wfm_path:
        from .protocol import parse_wfm_from_file
        metadata = wfm.capture_metadata
        wfm.close()
        wfm = parse_wfm_from_file(os.path.join(path, "waveform.wfm"), lazy=True)
        wfm.capture_metadata = metadata
        if os.path.exists(temp_wfm_path):
            os.remove(temp_wfm_path)
    return path, wfm, measurements, screenshot

