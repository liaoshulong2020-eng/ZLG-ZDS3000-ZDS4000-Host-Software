"""Bounded-memory record export, offline projects and heuristic event location."""
import csv
import hashlib
import json
import math
from datetime import datetime
from pathlib import Path

import numpy as np

from .protocol import parse_wfm_from_file


def unit_for(data):
    return "A" if data.info.probe_type == 1 else "V"


def record_context(wfm):
    return wfm.capture_metadata.get("test_context", {})


def bounds(data):
    n = len(data.raw_data)
    if not n:
        return (0.0, 0.0)
    if data._time_array is not None:
        return float(data._time_array[0]), float(data._time_array[-1])
    return data.start_time, data.start_time + (n - 1) * data.dt


def interval_indices(data, start, end):
    if not all(math.isfinite(v) for v in (start, end)) or end < start:
        raise ValueError("时间范围应为有限数值，且结束时间不能早于开始时间")
    n = len(data.raw_data)
    if data._time_array is not None:
        a = int(np.searchsorted(data._time_array, start, side="left"))
        b = int(np.searchsorted(data._time_array, end, side="right"))
    else:
        if data.dt <= 0:
            raise ValueError("采样时间间隔无效")
        a = max(0, min(n, math.ceil((start - data.start_time) / data.dt - 1e-8)))
        b = max(0, min(n, math.floor((end - data.start_time) / data.dt + 1e-8) + 1))
    return a, b


def blocks(data, start=0, end=None, size=262144):
    from .protocol import raw_code_to_voltage
    end = len(data.raw_data) if end is None else end
    for a in range(start, end, size):
        b = min(a + size, end)
        raw = data.raw_data[a:b]
        times = data._time_array[a:b] if data._time_array is not None else data.start_time + np.arange(a, b) * data.dt
        values = data._voltage_array[a:b] if data._voltage_array is not None else raw_code_to_voltage(
            raw, data.info.vert_division, data.info.vert_offset, data.info.probe_att, data.info.reversed)
        yield a, times, values, raw


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def save_json(path, data):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".part")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temp.replace(path)


def export_region(wfm, directory, channels, start, end, context=None):
    channels = list(dict.fromkeys(channels))
    if not channels or any(ch not in wfm.channels for ch in channels):
        raise ValueError("请选择记录中存在的通道")
    limits = {ch: interval_indices(wfm.channels[ch], start, end) for ch in channels}
    if not any(b > a for a, b in limits.values()):
        raise ValueError("所选时间范围中没有采样点")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    metadata = {"schema": 1, "start_s": start, "end_s": end, "downsampled": False,
                "capture": wfm.capture_metadata, "test_context": context or record_context(wfm),
                "conversion_status": "unverified_derived_values", "channels": {}}
    for ch in channels:
        data = wfm.channels[ch]
        a, b = limits[ch]
        path = root / f"CH{ch}_region.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["original_sample_index", "time_s", f"derived_value_{unit_for(data)}", "raw_code"])
            for offset, times, values, raw in blocks(data, a, b):
                writer.writerows((offset + i, format(float(t), ".17g"), format(float(v), ".17g"), int(r))
                                 for i, (t, v, r) in enumerate(zip(times, values, raw)))
        metadata["channels"][str(ch)] = {"first_index": a, "stop_index_exclusive": b, "points": max(0, b - a),
                                         "unit": unit_for(data), "sha256": digest(path)}
    save_json(root / "region_metadata.json", metadata)
    return str(root)


def scan_record(wfm, context=None, progress=None, cancelled=None, max_events=200):
    """Scan every returned code. Candidates are bounded; conclusions require review."""
    context = context or record_context(wfm)
    result = {"schema": 1, "method": "full_record_chunk_scan_v1", "test_context": context,
              "capture": wfm.capture_metadata, "channels": {}, "events": [],
              "warnings": ["自动定位结果为候选事件，不能直接认定为故障。", "派生物理量换算仍需真机标定。",
                           "脉冲/抖动检测基于上下分位值中点的上升沿；多电平、低信噪比和混叠可能误判。"]}
    if wfm.head.acq_mode != 0:
        result["warnings"].append("非标准采集模式：时间和数值解码尚未完成该模式的真机验证，事件时间与幅值仅供复核。")
    total = sum(len(data.raw_data) for data in wfm.channels.values())
    done = 0
    for ch, data in sorted(wfm.channels.items()):
        n = len(data.raw_data)
        if not n:
            continue
        cfg = context.get("channels", {}).get(str(ch), {})
        low_limit, high_limit = cfg.get("minimum"), cfg.get("maximum")
        expected_frequency = cfg.get("frequency_hz")
        if any(value is not None and not math.isfinite(value) for value in (low_limit, high_limit)):
            raise ValueError("期望上下限必须为有限数值")
        if low_limit is not None and high_limit is not None and low_limit > high_limit:
            raise ValueError("期望下限不能大于上限")
        if expected_frequency is not None and (not math.isfinite(expected_frequency) or expected_frequency <= 0):
            raise ValueError("预期频率必须为正的有限数值")
        first = next(blocks(data, 0, min(n, 100000)))
        sample = first[2]
        low, high = np.percentile(sample, [10, 90])
        threshold = float((low + high) / 2)
        diff = np.diff(sample)
        mad = float(np.median(np.abs(diff - np.median(diff)))) if len(diff) else 0.0
        quantum = abs(data.info.vert_division) / 25.0
        spike_threshold = max(8 * mad, 3 * quantum, np.finfo(float).eps)
        events, counts, last_group = [], {}, {}
        count, sum_v, sum_v2 = 0, 0.0, 0.0
        minimum, maximum = float("inf"), float("-inf")
        previous_tail_v = previous_tail_t = previous_tail_raw = None
        last_edge = None
        period_history = np.array([], dtype=float)

        def record(kind, indices, times, values, offset):
            if not len(indices):
                return
            absolute = indices + offset
            starts = np.r_[True, np.diff(absolute) > 1]
            if last_group.get(kind) == int(absolute[0]) - 1:
                starts[0] = False
            last_group[kind] = int(absolute[-1])
            positions = np.flatnonzero(starts)
            counts[kind] = counts.get(kind, 0) + len(positions)
            for position in positions[:max(0, max_events - len(events))]:
                index = int(indices[position])
                events.append({"channel": ch, "kind": kind, "sample_index": int(absolute[position]),
                               "time_s": float(times[index]), "derived_value": float(values[index]), "unit": unit_for(data)})

        for offset, times, values, raw in blocks(data):
            if cancelled and cancelled():
                raise RuntimeError("已取消分析；没有生成完成报告")
            count += len(values)
            sum_v += float(np.sum(values, dtype=np.float64))
            sum_v2 += float(np.dot(values, values))
            minimum, maximum = min(minimum, float(np.min(values))), max(maximum, float(np.max(values)))
            record("possible_clipping", np.flatnonzero((raw == 0) | (raw == 255)), times, values, offset)
            if low_limit is not None:
                record("below_specification", np.flatnonzero(values < low_limit), times, values, offset)
            if high_limit is not None:
                record("above_specification", np.flatnonzero(values > high_limit), times, values, offset)
            # Include two previous samples so single-sample spikes at chunk boundaries are visible.
            v = np.r_[previous_tail_v, values] if previous_tail_v is not None else values
            t = np.r_[previous_tail_t, times] if previous_tail_t is not None else times
            shift = len(v) - len(values)
            if len(v) >= 3:
                mask = (np.abs(v[1:-1] - v[:-2]) > spike_threshold) & (np.abs(v[1:-1] - v[2:]) > spike_threshold)
                mask &= np.abs(v[:-2] - v[2:]) < spike_threshold / 2
                index = np.flatnonzero(mask) + 1
                record("isolated_spike_candidate", index, t, v, offset - shift)
            if high - low > 2 * quantum:
                edge_idx = np.flatnonzero((v[:-1] < threshold) & (v[1:] >= threshold)) + 1
                edge_idx = edge_idx[edge_idx >= shift]
                edge_times = t[edge_idx]
                if len(edge_times):
                    all_times = np.r_[last_edge, edge_times] if last_edge is not None else edge_times
                    periods = np.diff(all_times)
                    corresponding = edge_idx[-len(periods):] if len(periods) else np.array([], dtype=int)
                    nominal = 1.0 / expected_frequency if expected_frequency else (float(np.median(period_history)) if len(period_history) >= 8 else None)
                    if nominal is not None and nominal > 0:
                        record("missing_pulse_candidate", corresponding[periods > nominal * 1.5], t, v, offset - shift)
                        record("period_jitter_candidate", corresponding[np.abs(periods - nominal) > nominal * .2], t, v, offset - shift)
                    period_history = np.r_[period_history, periods][-128:]
                    last_edge = float(edge_times[-1])
            previous_tail_v, previous_tail_t = values[-2:].copy(), times[-2:].copy()
            done += len(raw)
            if progress:
                progress(done, total)
        result["channels"][str(ch)] = {"scanned_points": count, "full_data_scanned": count == n,
            "unit": unit_for(data), "minimum": minimum, "maximum": maximum, "mean": sum_v / count,
            "rms": math.sqrt(sum_v2 / count), "candidate_counts": counts,
            "events_saved": len(events), "event_list_capped": sum(counts.values()) > len(events),
            "spike_threshold": spike_threshold, "edge_threshold": threshold}
        result["events"].extend(events)
    result["events"].sort(key=lambda event: event["time_s"])
    return result


def save_project(wfm, directory, state):
    from .storage import ScopeStorage
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=False)
    ScopeStorage.save_wfm(wfm, str(root / "waveform.wfm"))
    save_json(root / "project.json", {"schema": 1, "saved_at": datetime.now().astimezone().isoformat(),
                                     "capture": wfm.capture_metadata, "state": state})
    manifest = {name: {"bytes": (root / name).stat().st_size, "sha256": digest(root / name)}
                for name in ("waveform.wfm", "project.json")}
    save_json(root / "project_manifest.json", manifest)
    return str(root)


def load_project(directory):
    root = Path(directory)
    manifest = json.loads((root / "project_manifest.json").read_text(encoding="utf-8"))
    if not {"waveform.wfm", "project.json"}.issubset(manifest):
        raise ValueError("工程校验清单不完整")
    for name, expected in manifest.items():
        if name not in {"waveform.wfm", "project.json"}:
            raise ValueError("工程中存在未知路径")
        if (root / name).stat().st_size != expected["bytes"] or digest(root / name) != expected["sha256"]:
            raise ValueError(f"工程文件校验失败: {name}")
    document = json.loads((root / "project.json").read_text(encoding="utf-8"))
    if document.get("schema") != 1:
        raise ValueError("不支持的工程版本")
    wfm = parse_wfm_from_file(str(root / "waveform.wfm"), lazy=True)
    wfm.capture_metadata = document.get("capture", {})
    return wfm, document.get("state", {})
