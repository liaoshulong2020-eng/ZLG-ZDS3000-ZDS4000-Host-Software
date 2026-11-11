"""
致远示波器本地控制 RESTful API 接口服务
基于 FastAPI 构建，提供自动化脚本控制与数据接口
"""

import os
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
import numpy as np

from .protocol import WfmFile
from .driver import ZDScopeDriver
from .storage import ScopeStorage
from .capture import capture_archive

app = FastAPI(
    title="ZLG ZDS Scope Control API",
    description="致远示波器自动化控制与数据接口",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局驱动实例状态
class ScopeState:
    driver: Any = None
    last_wfm: Optional[WfmFile] = None
    last_loop: Optional[Dict[str, Any]] = None

state = ScopeState()


# 请求模型定义
class ConnectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str = "192.168.1.100"
    port: int = 5025
    timeout: float = 5.0


class ControlRequest(BaseModel):
    action: str  # run, stop, single, auto_setup, clear


class ChannelConfigRequest(BaseModel):
    channel: int
    display: Optional[bool] = None
    scale: Optional[float] = None
    offset: Optional[float] = None
    coupling: Optional[str] = None
    bw_limit_20m: Optional[bool] = None


class TimebaseConfigRequest(BaseModel):
    scale: Optional[float] = None
    offset: Optional[float] = None


class SaveSnapshotRequest(BaseModel):
    session_name: str = "capture"
    channels: List[int] = [1, 2]
    export_csv: bool = False
    export_npz: bool = False
    create_zip: bool = True
    test_context: Dict[str, Any] = Field(default_factory=dict)
    analyze_events: bool = False


class SettingsWriteRequest(BaseModel):
    command: str
    value: str


class RecordAnalysisRequest(BaseModel):
    test_context: Dict[str, Any] = Field(default_factory=dict)


class RegionExportRequest(RecordAnalysisRequest):
    channels: List[int]
    start_s: float
    end_s: float
    directory: str



def get_driver():
    if not state.driver or not state.driver.is_connected:
        raise HTTPException(status_code=400, detail="示波器尚未连接，请先调用 POST /api/scope/connect")
    return state.driver


@app.get("/")
def root():
    return {
        "message": "致远 ZDS 示波器本地控制 API 服务已就绪",
        "docs_url": "/docs",
        "is_connected": state.driver.is_connected if state.driver else False
    }


@app.post("/api/scope/connect")
def connect_scope(req: ConnectRequest):
    """连接到真实致远示波器"""
    try:
        if state.driver:
            state.driver.disconnect()
        state.last_wfm = None
        state.last_loop = None
        state.driver = ZDScopeDriver(host=req.host, port=req.port, timeout=req.timeout)
        state.driver.connect()
        idn = state.driver.device_info
        return {
            "status": "success",
            "message": f"成功连接至示波器: {idn}",
            "device_info": idn
        }
    except Exception as e:
        if state.driver:
            state.driver.disconnect()
        state.driver = None
        raise HTTPException(status_code=500, detail=f"连接失败: {str(e)}")


@app.post("/api/scope/disconnect")
def disconnect_scope():
    """断开示波器连接"""
    if state.driver:
        state.driver.disconnect()
    return {"status": "success", "message": "示波器已断开连接"}


@app.get("/api/scope/status")
def get_status():
    """获取示波器连接与运行状态"""
    if not state.driver or not state.driver.is_connected:
        return {"connected": False, "run_state": "Disconnected"}
    return {
        "connected": True,
        "device_info": state.driver.get_idn(),
        "run_state": state.driver.get_run_state()
    }


@app.post("/api/scope/control")
def control_scope(req: ControlRequest):
    """控制示波器运行状态: run, stop, single, auto_setup, clear"""
    drv = get_driver()
    act = req.action.lower()
    if act == "run":
        drv.run()
    elif act == "stop":
        drv.stop()
    elif act == "single":
        drv.single()
    elif act == "auto_setup":
        drv.auto_setup()
    elif act == "clear":
        drv.clear()
    else:
        raise HTTPException(status_code=400, detail=f"不支持的控制动作: {req.action}")

    return {"status": "success", "action": req.action, "current_state": drv.get_run_state()}


@app.post("/api/scope/config/channel")
def configure_channel(req: ChannelConfigRequest):
    """配置示波器通道参数"""
    drv = get_driver()
    drv.set_channel(
        ch=req.channel,
        display=req.display,
        scale=req.scale,
        offset=req.offset,
        coupling=req.coupling,
        bw_limit_20m=req.bw_limit_20m
    )
    return {"status": "success", "channel": req.channel}


@app.post("/api/scope/config/timebase")
def configure_timebase(req: TimebaseConfigRequest):
    """配置示波器水平时基"""
    drv = get_driver()
    drv.set_timebase(scale=req.scale, offset=req.offset)
    return {"status": "success"}


@app.get("/api/scope/measurements")
def get_measurements(channel: int = Query(1, ge=1, le=4)):
    """获取指定通道的硬件测量值 (Vpp, Vrms, Vmax, Vmin, Freq 等)"""
    drv = get_driver()
    return drv.get_measurements(channel)


@app.get("/api/scope/waveform")
def get_waveform(
    channels: str = Query("1", description="逗号分隔通道，例如 '1' 或 '1,2'"),
    mode: str = Query("SCREen", description="SCREen 或 MEMOry"),
    max_points: int = Query(500, description="API传输降采样点数，-1 表示全采样点")
):
    """
    获取校准后的波形数据 (时间与电压浮点数组)
    支持自动自适应降采样，防止传输超大体积数据阻塞数据接口
    """
    drv = get_driver()
    ch_list = [int(c.strip()) for c in channels.split(",") if c.strip().isdigit()]
    wfm = drv.read_waveform(channels=ch_list, mode=mode)
    state.last_wfm = wfm

    result = {
        "dev_name": wfm.head.dev_name,
        "sample_rate": wfm.head.sample_rate,
        "hrzt_division": wfm.head.hrzt_division,
        "channels": {}
    }

    for ch_id, ch_data in wfm.channels.items():
        n = len(ch_data.voltage_array)
        if max_points > 0 and n > max_points:
            step = max(1, n // max_points)
            t_sub = ch_data.time_array[::step].tolist()
            v_sub = ch_data.voltage_array[::step].tolist()
        else:
            t_sub = ch_data.time_array.tolist()
            v_sub = ch_data.voltage_array.tolist()

        result["channels"][ch_id] = {
            "vpp": ch_data.vpp,
            "vrms": ch_data.vrms,
            "vavg": ch_data.vavg,
            "vmax": ch_data.vmax,
            "vmin": ch_data.vmin,
            "total_points": n,
            "sampled_points": len(v_sub),
            "time": t_sub,
            "voltage": v_sub
        }

    return result


@app.get("/api/scope/loop_analysis")
def get_loop_analysis():
    """获取电源环路 Bode 分析结果 (PM, GM, 扫频报表)"""
    drv = get_driver()
    loop_data = drv.get_loop_analysis()
    state.last_loop = loop_data
    return loop_data


@app.get("/api/scope/screenshot")
def get_screenshot():
    """获取示波器屏幕截图 PNG 图像"""
    drv = get_driver()
    png_bytes = drv.capture_screen(fmt="PNG")
    return Response(content=png_bytes, media_type="image/png")






@app.post("/api/scope/save_snapshot")
def save_snapshot(req: SaveSnapshotRequest):
    """保存当前所有数据至独立快照目录"""
    drv = get_driver()
    try:
        export_opts = {
            "export_full_csv": req.export_csv,
            "export_channel_csv": req.export_csv,
            "export_npz": req.export_npz,
            "create_zip": req.create_zip,
            "legacy_full_bundle": req.create_zip and req.export_npz,
            "analyze_events": req.analyze_events,
        }
        saved_dir, wfm, meas, screenshot = capture_archive(
            drv,
            channels=req.channels,
            session_name=req.session_name,
            export_options=export_opts,
            test_context=req.test_context,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    state.last_wfm = wfm
    return {
        "status": "success",
        "saved_path": os.path.abspath(saved_dir),
        "zip_path": os.path.abspath(saved_dir + ".zip") if (req.create_zip or os.path.exists(saved_dir + ".zip")) else None,
        "mode": "MEMOry",
        "points": {str(ch): len(data.time_array) for ch, data in wfm.channels.items()},
        "optional_read_errors": wfm.capture_metadata["optional_read_errors"],
    }


@app.get("/api/scope/settings")
def read_scope_settings():
    return get_driver().get_scope_actual_configuration()


@app.patch("/api/scope/settings")
def write_scope_setting(req: SettingsWriteRequest):
    driver = get_driver()
    try:
        return driver.write_extended_setting(req.command, req.value)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


def get_record():
    if state.last_wfm is None:
        raise HTTPException(status_code=400, detail="尚未读取或归档波形记录")
    return state.last_wfm


@app.post("/api/record/analyze")
def analyze_saved_record(req: RecordAnalysisRequest):
    from .records import scan_record
    try:
        return scan_record(get_record(), req.test_context)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error))


@app.post("/api/record/export_region")
def export_saved_region(req: RegionExportRequest):
    from .records import export_region
    try:
        return {"directory": export_region(get_record(), req.directory, req.channels, req.start_s, req.end_s, req.test_context)}
    except (ValueError, FileExistsError) as error:
        raise HTTPException(status_code=422, detail=str(error))


@app.get("/api/record/integrity")
def record_integrity():
    return get_record().capture_metadata.get("integrity_verification", {"integrity_status": "NOT_VERIFIED"})
