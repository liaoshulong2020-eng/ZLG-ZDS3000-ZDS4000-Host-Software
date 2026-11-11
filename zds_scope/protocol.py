"""
ZLG ZDS3000 / ZDS4000 Plus 系列示波器通信协议与 WFM 文件格式解析器
依据官方编程手册规范实现
"""

import struct
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict, Any
import numpy as np


# ==============================================================================
# WFM 文件结构常量与格式定义
# ==============================================================================
# wfm_head_info: 424 bytes
# <4s 64s 128s 40s i d d d i i d i i 64s i 17i
WFM_HEAD_FORMAT = "<4s64s128s40sidddiidii64si17i"
WFM_HEAD_SIZE = struct.calcsize(WFM_HEAD_FORMAT)

# wfm_item_info: 120 bytes
# <6i 3d 2i 16i
WFM_ITEM_FORMAT = "<6i3d2i16i"
WFM_ITEM_SIZE = struct.calcsize(WFM_ITEM_FORMAT)


@dataclass
class WfmHeadInfo:
    file_type: str = "WFM"
    dev_name: str = "ZDS3024 Plus"
    firmware_version: str = "V1.00"
    data_format: str = "V1.00"
    rev0: int = 0
    hrzt_division: float = 1e-6       # 水平档位，单位秒 (s/div)
    hrzt_offset: float = 0.0          # 水平偏移，单位秒
    start_time: float = 0.0           # 起始时间，单位秒
    acq_mode: int = 0                 # 采样模式：0 标准，1 峰值，2 平均，3 高分辨率
    rev1: int = 0
    sample_rate: float = 1e9          # 采样率，单位Hz
    trig_mode: int = 0                # 触发模式：0 自动，1 普通
    trig_source: int = 0              # 触发源：0 通道1，1 通道2，2 通道3，3 通道4
    trig_type: str = "EDGE"           # 触发类型
    item_num: int = 1                 # 数据项(通道)个数
    rev_arr: List[int] = field(default_factory=lambda: [0] * 17)


@dataclass
class WfmItemInfo:
    channel: int = 0                  # 0: CH1, 1: CH2, 2: CH3, 3: CH4
    couple_mode: int = 0              # 0: 直流, 1: 交流, 2: 接地
    bw_limit: int = 0                 # 0: 关闭, 1: 20MHz
    probe_type: int = 0               # 0: 电压, 1: 电流
    reversed: int = 0                 # 0: 正常, 1: 反相
    rev0: int = 0
    probe_att: float = 1.0            # 探头衰减比 (1, 10, 100...)
    vert_division: float = 1.0        # 垂直档位，单位伏 (V/div)
    vert_offset: float = 0.0          # 垂直偏置，单位伏 (V)
    data_length: int = 0              # 原始数据长度(字节数或采样点数)
    data_offset: int = 0              # 数据相对于文件开头的偏移量
    rev1_arr: List[int] = field(default_factory=lambda: [0] * 16)


def raw_code_to_voltage(
    raw_array: np.ndarray,
    vert_division: float,
    vert_offset: float,
    probe_att: float = 1.0,
    reversed_ch: int = 0
) -> np.ndarray:
    """
    原始码兼容换算。中心码与每格量化步数仍需按固件及采集模式真机校准。
    
    参数说明:
    - raw_array: 示波器原生 8 位采样码 (uint8, 0~255)
    - vert_division: 垂直灵敏度档位 (V/div)
    - vert_offset: 垂直偏置电压 (V)
    - probe_att: 探头衰减倍率 (1, 10, 100...)，示波器返回的 vert_division 已标定探头倍率
    - reversed_ch: 是否反相 (0: 正常, 1: 反相)
    
    换算关系:
      - 当前兼容假设：中心码 128.0，每格 25.0 台阶；PEAK 等模式尚未验证
      - 派生公式: V = ((code - 128.0) / 25.0) * vert_division - vert_offset
    - 若反相使能: V = -V
    """
    code_f = raw_array.astype(np.float64)
    v = ((code_f - 128.0) / 25.0) * vert_division - vert_offset
    if reversed_ch:
        v = -v
    return v


def voltage_to_raw_code(
    voltage: np.ndarray,
    vert_division: float,
    vert_offset: float,
    reversed_ch: int = 0
) -> np.ndarray:
    """物理电压反推回 8 位 ADC 原生码值 (0~255 uint8)"""
    v = -voltage if reversed_ch else voltage
    code = (v + vert_offset) * (25.0 / vert_division) + 128.0
    return np.clip(np.round(code), 0, 255).astype(np.uint8)


class ChannelWaveformData:
    """通道波形数据对象（包含物理刻度的时间与电压数组，支持零拷贝与按需流式访问）"""

    def __init__(
        self,
        channel_id: int,
        info: WfmItemInfo,
        raw_data: np.ndarray,
        time_array: Optional[np.ndarray] = None,
        voltage_array: Optional[np.ndarray] = None,
        start_time: float = 0.0,
        dt: float = 1e-9
    ):
        self.channel_id = channel_id
        self.info = info
        self.raw_data = raw_data
        self.start_time = start_time
        self.dt = dt
        self._time_array = time_array
        self._voltage_array = voltage_array

    @property
    def time_array(self) -> np.ndarray:
        """按需生成完整时间数组；对超大数组建议使用 get_downsampled 或 stream_blocks"""
        if self._time_array is None:
            n = len(self.raw_data)
            self._time_array = self.start_time + np.arange(n) * self.dt
        return self._time_array

    @time_array.setter
    def time_array(self, arr: np.ndarray):
        self._time_array = arr

    @property
    def voltage_array(self) -> np.ndarray:
        """按需生成完整物理电压数组；对超大数组建议使用 get_downsampled 或 stream_blocks"""
        if self._voltage_array is None:
            self._voltage_array = raw_code_to_voltage(
                self.raw_data,
                self.info.vert_division,
                self.info.vert_offset,
                self.info.probe_att,
                self.info.reversed
            )
        return self._voltage_array

    @voltage_array.setter
    def voltage_array(self, arr: np.ndarray):
        self._voltage_array = arr

    def get_downsampled(self, max_points: int = 5000) -> Tuple[np.ndarray, np.ndarray]:
        """
        零内存爆炸按需抽取降采样点（专为 UI 绘制与测量摘要设计，内存占用 < 100KB）
        """
        n = len(self.raw_data)
        if n <= max_points:
            return self.time_array, self.voltage_array
        step = max(1, int(np.ceil(n / max_points)))
        idx = np.arange(0, n, step)
        sub_raw = self.raw_data[idx]
        t = self._time_array[idx] if self._time_array is not None else self.start_time + idx * self.dt
        v = self._voltage_array[idx] if self._voltage_array is not None else raw_code_to_voltage(
            sub_raw,
            self.info.vert_division,
            self.info.vert_offset,
            self.info.probe_att,
            self.info.reversed
        )
        return t, v

    def stream_blocks(self, block_size: int = 1048576):
        """
        流式分块生成器：每次产出一块数据，内存占用仅一个 block_size (如 1MB)，不占巨额内存
        yields (start_idx, t_chunk, v_chunk, raw_chunk)
        """
        n = len(self.raw_data)
        for s in range(0, n, block_size):
            e = min(s + block_size, n)
            raw_c = self.raw_data[s:e]
            t_c = self._time_array[s:e] if self._time_array is not None else self.start_time + np.arange(s, e) * self.dt
            v_c = self._voltage_array[s:e] if self._voltage_array is not None else raw_code_to_voltage(
                raw_c,
                self.info.vert_division,
                self.info.vert_offset,
                self.info.probe_att,
                self.info.reversed
            )
            yield s, t_c, v_c, raw_c

    @property
    def vpp(self) -> float:
        if self._voltage_array is not None and len(self._voltage_array) > 0:
            return float(np.max(self._voltage_array) - np.min(self._voltage_array))
        if len(self.raw_data) == 0:
            return 0.0
        r_min = float(np.min(self.raw_data))
        r_max = float(np.max(self.raw_data))
        v1 = float(raw_code_to_voltage(np.array([r_min]), self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)[0])
        v2 = float(raw_code_to_voltage(np.array([r_max]), self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)[0])
        return abs(v2 - v1)

    @property
    def vmax(self) -> float:
        if self._voltage_array is not None and len(self._voltage_array) > 0:
            return float(np.max(self._voltage_array))
        if len(self.raw_data) == 0:
            return 0.0
        r = float(np.min(self.raw_data) if self.info.reversed else np.max(self.raw_data))
        return float(raw_code_to_voltage(np.array([r]), self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)[0])

    @property
    def vmin(self) -> float:
        if self._voltage_array is not None and len(self._voltage_array) > 0:
            return float(np.min(self._voltage_array))
        if len(self.raw_data) == 0:
            return 0.0
        r = float(np.max(self.raw_data) if self.info.reversed else np.min(self.raw_data))
        return float(raw_code_to_voltage(np.array([r]), self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)[0])

    @property
    def vavg(self) -> float:
        if self._voltage_array is not None and len(self._voltage_array) > 0:
            return float(np.mean(self._voltage_array))
        if len(self.raw_data) == 0:
            return 0.0
        sample = self.raw_data[:min(len(self.raw_data), 100000)]
        v_sample = raw_code_to_voltage(sample, self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)
        return float(np.mean(v_sample))

    @property
    def vrms(self) -> float:
        if self._voltage_array is not None and len(self._voltage_array) > 0:
            return float(np.sqrt(np.mean(self._voltage_array ** 2)))
        if len(self.raw_data) == 0:
            return 0.0
        sample = self.raw_data[:min(len(self.raw_data), 100000)]
        v_sample = raw_code_to_voltage(sample, self.info.vert_division, self.info.vert_offset, self.info.probe_att, self.info.reversed)
        return float(np.sqrt(np.mean(v_sample ** 2)))

    def close(self):
        """释放底层 memmap 文件句柄 (Windows 平台必须显式关闭)"""
        if hasattr(self.raw_data, '_mmap') and self.raw_data._mmap is not None:
            try:
                self.raw_data._mmap.close()
            except Exception:
                pass


@dataclass
class WfmFile:
    """完整波形文件对象"""
    head: WfmHeadInfo
    channels: Dict[int, ChannelWaveformData] = field(default_factory=dict)
    original_bytes: Optional[bytes] = field(default=None, repr=False)
    file_path: Optional[str] = field(default=None, repr=False)
    capture_metadata: Dict[str, Any] = field(default_factory=dict)

    def close(self):
        """关闭所有通道的 memmap 映射"""
        for ch in self.channels.values():
            ch.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def parse_wfm(data: bytes, eager_arrays: bool = True) -> WfmFile:
    """
    解析 ZLG WFM 二进制波形数据
    文件格式: Head (424B) + Item[n] (120B * n) + Data[n]
    :param data: WFM 文件的全部原始字节
    :param eager_arrays: 若为 True 且数据量适中，立即生成全量 time_array 与 voltage_array
    """
    if len(data) < WFM_HEAD_SIZE:
        raise ValueError(f"WFM 数据过短 ({len(data)} 字节)，小于头部大小 {WFM_HEAD_SIZE}")

    head_tuple = struct.unpack(WFM_HEAD_FORMAT, data[:WFM_HEAD_SIZE])
    cFileType = head_tuple[0].decode('ascii', errors='ignore').strip('\x00')
    cDevName = head_tuple[1].decode('gbk', errors='ignore').strip('\x00')
    cFirmware = head_tuple[2].decode('ascii', errors='ignore').strip('\x00')
    cDataFormat = head_tuple[3].decode('ascii', errors='ignore').strip('\x00')
    cTrigType = head_tuple[13].decode('ascii', errors='ignore').strip('\x00')

    head = WfmHeadInfo(
        file_type=cFileType,
        dev_name=cDevName,
        firmware_version=cFirmware,
        data_format=cDataFormat,
        rev0=head_tuple[4],
        hrzt_division=head_tuple[5],
        hrzt_offset=head_tuple[6],
        start_time=head_tuple[7],
        acq_mode=head_tuple[8],
        rev1=head_tuple[9],
        sample_rate=head_tuple[10],
        trig_mode=head_tuple[11],
        trig_source=head_tuple[12],
        trig_type=cTrigType,
        item_num=head_tuple[14],
        rev_arr=list(head_tuple[15:])
    )

    if head.file_type != "WFM":
        raise ValueError("无效的 WFM 文件标识")
    if not 1 <= head.item_num <= 4:
        raise ValueError("WFM 通道数量无效")
    table_end = WFM_HEAD_SIZE + head.item_num * WFM_ITEM_SIZE
    if table_end > len(data):
        raise ValueError("WFM 通道头被截断")
    wfm = WfmFile(head=head, original_bytes=bytes(data))
    item_offset = WFM_HEAD_SIZE

    for i in range(head.item_num):
        if item_offset + WFM_ITEM_SIZE > len(data):
            break
        item_bytes = data[item_offset:item_offset + WFM_ITEM_SIZE]
        it = struct.unpack(WFM_ITEM_FORMAT, item_bytes)

        item = WfmItemInfo(
            channel=it[0],
            couple_mode=it[1],
            bw_limit=it[2],
            probe_type=it[3],
            reversed=it[4],
            rev0=it[5],
            probe_att=it[6],
            vert_division=it[7],
            vert_offset=it[8],
            data_length=it[9],
            data_offset=it[10],
            rev1_arr=list(it[11:])
        )
        item_offset += WFM_ITEM_SIZE

        # 提取该通道原始二进制数据
        raw_start = item.data_offset
        raw_end = raw_start + item.data_length
        if item.data_length <= 0 or raw_start < table_end or raw_end > len(data):
            raise ValueError(f"WFM 通道 {item.channel + 1} 数据不完整或偏移无效")
        if item.channel + 1 in wfm.channels:
            raise ValueError("WFM 通道重复")
        raw_bytes = data[raw_start:raw_end]

        raw_len = len(raw_bytes)
        if raw_len == 0:
            continue

        raw_array = np.frombuffer(raw_bytes, dtype=np.uint8)
        n_points = len(raw_array)
        dt = 1.0 / head.sample_rate if head.sample_rate > 0 else (head.hrzt_division * 14.0 / n_points if n_points > 0 else 1e-6)

        # 针对中小波形立即生成数组兼容旧代码；对于特大波形 (> 500,000 点) 采用延迟生成避免占用 8GB+ 内存
        if eager_arrays and n_points <= 500_000:
            time_array = head.start_time + np.arange(n_points) * dt
            voltage_array = raw_code_to_voltage(
                raw_array,
                item.vert_division,
                item.vert_offset,
                item.probe_att,
                item.reversed
            )
        else:
            time_array = None
            voltage_array = None

        ch_num = item.channel + 1
        wfm.channels[ch_num] = ChannelWaveformData(
            channel_id=ch_num,
            info=item,
            raw_data=raw_array,
            time_array=time_array,
            voltage_array=voltage_array,
            start_time=head.start_time,
            dt=dt
        )

    return wfm


def parse_wfm_from_file(filepath: str, lazy: bool = True) -> WfmFile:
    """
    直接从磁盘解析 WFM 文件（np.memmap 按需读取，避免预先创建全量浮点数组）
    """
    import os
    file_size = os.path.getsize(filepath)
    if file_size < WFM_HEAD_SIZE:
        raise ValueError(f"WFM 文件过短 ({file_size} 字节)")

    with open(filepath, "rb") as f:
        head_bytes = f.read(WFM_HEAD_SIZE)
    head_tuple = struct.unpack(WFM_HEAD_FORMAT, head_bytes)
    cFileType = head_tuple[0].decode('ascii', errors='ignore').strip('\x00')
    cDevName = head_tuple[1].decode('gbk', errors='ignore').strip('\x00')
    cFirmware = head_tuple[2].decode('ascii', errors='ignore').strip('\x00')
    cDataFormat = head_tuple[3].decode('ascii', errors='ignore').strip('\x00')
    cTrigType = head_tuple[13].decode('ascii', errors='ignore').strip('\x00')

    head = WfmHeadInfo(
        file_type=cFileType,
        dev_name=cDevName,
        firmware_version=cFirmware,
        data_format=cDataFormat,
        rev0=head_tuple[4],
        hrzt_division=head_tuple[5],
        hrzt_offset=head_tuple[6],
        start_time=head_tuple[7],
        acq_mode=head_tuple[8],
        rev1=head_tuple[9],
        sample_rate=head_tuple[10],
        trig_mode=head_tuple[11],
        trig_source=head_tuple[12],
        trig_type=cTrigType,
        item_num=head_tuple[14],
        rev_arr=list(head_tuple[15:])
    )

    if head.file_type != "WFM":
        raise ValueError("无效的 WFM 文件标识")
    if not 1 <= head.item_num <= 4:
        raise ValueError("WFM 通道数量无效")

    table_end = WFM_HEAD_SIZE + head.item_num * WFM_ITEM_SIZE
    if table_end > file_size:
        raise ValueError("WFM 通道头被截断")

    wfm = WfmFile(head=head, file_path=filepath)
    with open(filepath, "rb") as f:
        f.seek(WFM_HEAD_SIZE)
        items_bytes = f.read(head.item_num * WFM_ITEM_SIZE)

    for i in range(head.item_num):
        chunk = items_bytes[i * WFM_ITEM_SIZE:(i + 1) * WFM_ITEM_SIZE]
        it = struct.unpack(WFM_ITEM_FORMAT, chunk)
        item = WfmItemInfo(
            channel=it[0],
            couple_mode=it[1],
            bw_limit=it[2],
            probe_type=it[3],
            reversed=it[4],
            rev0=it[5],
            probe_att=it[6],
            vert_division=it[7],
            vert_offset=it[8],
            data_length=it[9],
            data_offset=it[10],
            rev1_arr=list(it[11:])
        )
        raw_start = item.data_offset
        raw_end = raw_start + item.data_length
        if item.data_length <= 0 or raw_start < table_end or raw_end > file_size:
            raise ValueError(f"WFM 通道 {item.channel + 1} 范围无效")

        # 使用 np.memmap 零拷贝直接映射磁盘数据
        raw_memmap = np.memmap(filepath, dtype=np.uint8, mode='r', offset=raw_start, shape=(item.data_length,))
        dt = 1.0 / head.sample_rate if head.sample_rate > 0 else (head.hrzt_division * 14.0 / len(raw_memmap) if len(raw_memmap) > 0 else 1e-6)

        time_arr = None
        volt_arr = None
        if not lazy and len(raw_memmap) <= 500_000:
            time_arr = head.start_time + np.arange(len(raw_memmap)) * dt
            volt_arr = raw_code_to_voltage(raw_memmap, item.vert_division, item.vert_offset, item.probe_att, item.reversed)

        ch_num = item.channel + 1
        wfm.channels[ch_num] = ChannelWaveformData(
            channel_id=ch_num,
            info=item,
            raw_data=raw_memmap,
            time_array=time_arr,
            voltage_array=volt_arr,
            start_time=head.start_time,
            dt=dt
        )

    return wfm


def verify_waveform_conversion(
    wfm: WfmFile,
    measurements: Dict[int, Dict[str, float]]
) -> Dict[str, Any]:
    """
    用示波器硬件 DSP 测量值核对原始码与电压转换的准确性：
    核对编码、探头倍率、偏置和采样模式，防止使用错误的幅值
    """
    verification = {}
    for ch_id, ch_data in wfm.channels.items():
        ch_meas = measurements.get(ch_id, {})
        raw_min = int(np.min(ch_data.raw_data))
        raw_max = int(np.max(ch_data.raw_data))
        sample_len = min(100000, len(ch_data.raw_data))
        raw_mean = float(np.mean(ch_data.raw_data[:sample_len]))

        v_min_calc = float(raw_code_to_voltage(np.array([raw_min]), ch_data.info.vert_division, ch_data.info.vert_offset, reversed_ch=ch_data.info.reversed)[0])
        v_max_calc = float(raw_code_to_voltage(np.array([raw_max]), ch_data.info.vert_division, ch_data.info.vert_offset, reversed_ch=ch_data.info.reversed)[0])
        vpp_calc = abs(v_max_calc - v_min_calc)

        dsp_vpp = ch_meas.get("vpp")
        dsp_vmax = ch_meas.get("vmax")
        dsp_vmin = ch_meas.get("vmin")

        error_vpp_pct = None
        if dsp_vpp is not None and dsp_vpp > 0:
            error_vpp_pct = abs(vpp_calc - dsp_vpp) / dsp_vpp * 100.0

        status = "NOT_CHECKED" if error_vpp_pct is None else "MATCH"
        if error_vpp_pct is not None and error_vpp_pct > 20.0:
            status = "MISMATCH_WARNING"

        verification[ch_id] = {
            "channel": ch_id,
            "raw_code_min": raw_min,
            "raw_code_max": raw_max,
            "raw_code_mean": round(raw_mean, 2),
            "vert_division": ch_data.info.vert_division,
            "vert_offset": ch_data.info.vert_offset,
            "probe_att": ch_data.info.probe_att,
            "reversed": bool(ch_data.info.reversed),
            "formula": "V = ((raw_code - 128.0) / 25.0) * vert_division - vert_offset",
            "calculated_vpp": round(vpp_calc, 4),
            "calculated_vmax": round(v_max_calc, 4),
            "calculated_vmin": round(v_min_calc, 4),
            "hardware_dsp_vpp": dsp_vpp,
            "hardware_dsp_vmax": dsp_vmax,
            "hardware_dsp_vmin": dsp_vmin,
            "vpp_error_pct": round(error_vpp_pct, 2) if error_vpp_pct is not None else None,
            "verification_status": status
            , "verification_scope": "Vpp consistency only; encoding/offset/timing remain unverified",
        }
    return verification



def pack_wfm(head: WfmHeadInfo, channels_data: List[Tuple[WfmItemInfo, np.ndarray]]) -> bytes:
    """
    将波形数据打包为标准的 ZLG WFM 二进制数据流
    """
    head.item_num = len(channels_data)
    head_bytes = struct.pack(
        WFM_HEAD_FORMAT,
        head.file_type.encode('ascii')[:4].ljust(4, b'\x00'),
        head.dev_name.encode('gbk')[:64].ljust(64, b'\x00'),
        head.firmware_version.encode('ascii')[:128].ljust(128, b'\x00'),
        head.data_format.encode('ascii')[:40].ljust(40, b'\x00'),
        head.rev0,
        head.hrzt_division,
        head.hrzt_offset,
        head.start_time,
        head.acq_mode,
        head.rev1,
        head.sample_rate,
        head.trig_mode,
        head.trig_source,
        head.trig_type.encode('ascii')[:64].ljust(64, b'\x00'),
        head.item_num,
        *head.rev_arr[:17]
    )

    items_bytes = bytearray()
    raw_data_bytes = bytearray()

    current_data_offset = WFM_HEAD_SIZE + len(channels_data) * WFM_ITEM_SIZE

    for item, raw_arr in channels_data:
        raw_bytes = raw_arr.tobytes()
        item.data_offset = current_data_offset + len(raw_data_bytes)
        item.data_length = len(raw_bytes)

        it_packed = struct.pack(
            WFM_ITEM_FORMAT,
            item.channel,
            item.couple_mode,
            item.bw_limit,
            item.probe_type,
            item.reversed,
            item.rev0,
            item.probe_att,
            item.vert_division,
            item.vert_offset,
            item.data_length,
            item.data_offset,
            *item.rev1_arr[:16]
        )
        items_bytes.extend(it_packed)
        raw_data_bytes.extend(raw_bytes)

    return head_bytes + items_bytes + raw_data_bytes


def unpack_screen_image(data: bytes) -> bytes:
    """
    解包示波器返回的屏幕截图数据块（:DISPlay:DATA? 或 :DISPlay:DATA:PNG?）
    数据格式: #<digit_len><data_len><binary_bytes>
    例如: #9001152054... 或 #70102400...
    """
    if not data:
        return b""
    
    # 查找起始 '#'
    start_idx = data.find(b'#')
    if start_idx == -1:
        # 若未找到'#'但以PNG头开头(\x89PNG)或BMP头(BM)
        if data.startswith(b'\x89PNG') or data.startswith(b'BM'):
            return data
        return b""

    try:
        # 获取表示长度位数的字符，例如 '9'
        digit_len = int(chr(data[start_idx + 1]), 16)
        len_str_start = start_idx + 2
        len_str_end = len_str_start + digit_len
        image_len = int(data[len_str_start:len_str_end].decode('ascii'))
        image_bytes = data[len_str_end:len_str_end + image_len]
        return image_bytes
    except Exception:
        # 兼容性回退
        if b'\x89PNG' in data:
            png_start = data.find(b'\x89PNG')
            return data[png_start:]
        elif b'BM' in data:
            bmp_start = data.find(b'BM')
            return data[bmp_start:]
        return data


# ==============================================================================
# SCPI 指令定义 (严格对照致远手册)
# ==============================================================================
class ScopeCommands:
    # 基础与通用
    IDN = "*IDN?\n"
    RUN_STATE = ":GLOBal:RUN:STATe?\n"
    RUN = ":RUN\n"
    STOP = ":STOP\n"
    SINGLE = ":SINGle\n"
    AUTO_SETUP = ":AUTosetup\n"
    CLEAR = ":CLEar\n"

    # 屏幕截图
    SCREEN_PNG = ":DISPlay:DATA:PNG?\n"
    SCREEN_BMP = ":DISPlay:DATA?\n"

    # 波形读取
    @staticmethod
    def read_waveform(source: str = "CHANnel1", mode: str = "SCREen") -> str:
        """mode: SCREen 或 MEMOry"""
        return f":GLOBal:MULTiwave? {mode},{source}\n"

    @staticmethod
    def read_multi_waveform(sources: List[str], mode: str = "SCREen") -> str:
        srcs = ",".join(sources)
        return f":GLOBal:MULTiwave? {mode},{srcs}\n"

    # 通道控制
    @staticmethod
    def set_channel_display(ch: int, on: bool) -> str:
        return f":CHANnel{ch}:DISPlay {1 if on else 0}\n"

    @staticmethod
    def get_channel_display(ch: int) -> str:
        return f":CHANnel{ch}:DISPlay?\n"

    @staticmethod
    def set_channel_scale(ch: int, scale_v: float) -> str:
        return f":CHANnel{ch}:SCALe {scale_v}\n"

    @staticmethod
    def get_channel_scale(ch: int) -> str:
        return f":CHANnel{ch}:SCALe?\n"

    @staticmethod
    def set_channel_offset(ch: int, offset_v: float) -> str:
        return f":CHANnel{ch}:OFFSet {offset_v}\n"

    @staticmethod
    def get_channel_offset(ch: int) -> str:
        return f":CHANnel{ch}:OFFSet?\n"

    @staticmethod
    def set_channel_coupling(ch: int, coupling: str) -> str:
        """coupling: DC, AC, GND"""
        return f":CHANnel{ch}:COUPling {coupling}\n"

    @staticmethod
    def set_channel_bwlimit(ch: int, limit_20m: bool) -> str:
        return f":CHANnel{ch}:BWLimit {'20M' if limit_20m else 'OFF'}\n"

    # 水平时基
    @staticmethod
    def set_timebase_scale(scale_s: float) -> str:
        return f":TIMebase:SCALe {scale_s}\n"

    @staticmethod
    def get_timebase_scale() -> str:
        return ":TIMebase:SCALe?\n"

    @staticmethod
    def set_timebase_offset(offset_s: float) -> str:
        return f":TIMebase:OFFSet {offset_s}\n"

    # 测量命令
    @staticmethod
    def query_measure_vpp(ch: int) -> str:
        return f":MEASure:VPP:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_vrms(ch: int) -> str:
        return f":MEASure:VRMS:CURRent? DC,DISPlay,CHANnel{ch}\n"

    @staticmethod
    def query_measure_vavg(ch: int) -> str:
        return f":MEASure:VAVG:CURRent? DISPlay,CHANnel{ch}\n"

    @staticmethod
    def query_measure_vmax(ch: int) -> str:
        return f":MEASure:VMAX:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_vmin(ch: int) -> str:
        return f":MEASure:VMIN:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_freq(ch: int) -> str:
        return f":MEASure:FREQuency:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_period(ch: int) -> str:
        return f":MEASure:PERiod:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_rise(ch: int) -> str:
        return f":MEASure:RISetime:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_fall(ch: int) -> str:
        return f":MEASure:FALLtime:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_duty(ch: int) -> str:
        return f":MEASure:PDUTy:CURRent? CHANnel{ch}\n"

    @staticmethod
    def query_measure_overshoot(ch: int) -> str:
        return f":MEASure:ROVErshoot:CURRent? CHANnel{ch}\n"

    # 存储深度与采样率命令
    @staticmethod
    def set_memory_depth(depth: str) -> str:
        return f":ACQuire:MDEPth {depth}\n"

    MEMORY_DEPTH_QUERY = ":ACQuire:MDEPth?\n"
    SAMPLE_RATE_QUERY = ":ACQuire:SRATe?\n"

    # 环路测试 (Power Loop Analysis / Bode 图)
    LOOP_ENABLE = ":LOOP:STATE 1\n"
    LOOP_DISABLE = ":LOOP:STATE 0\n"
    LOOP_GET_STATE = ":LOOP:STATE?\n"
    LOOP_GET_PM = ":LOOP:INFO:PM?\n"     # 相位裕度
    LOOP_GET_GM = ":LOOP:INFO:GM?\n"     # 增益裕度
    LOOP_REPORT_SIZE = ":LOOP:REPORT:SIZE?\n"

    @staticmethod
    def query_loop_line(line: int) -> str:
        return f":LOOP:REPORT:LINE? {line}\n"
