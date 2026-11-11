"""SCPI settings verified against the local ZDS programming manual, pp.24–46,169–202."""
import json
import math
import re
from pathlib import Path

TRIGGER_QUERIES = json.loads(Path(__file__).with_name("trigger_queries.json").read_text())
GLOBAL = {
    ":ACQuire:TYPE": "采集模式", ":ACQuire:AVERages": "平均次数",
    ":ACQuire:MAREa": "存储区域", ":TRIGger:MODE": "触发类型",
    ":TRIGger:SWEep": "触发方式", ":TRIGger:HOLDoff": "触发释抑(s)",
    ":TRIGger:COUPling": "触发耦合", ":TRIG:STATE": "触发状态（只读）",
}
CHANNEL = {"UNITs": "探头类型/单位", "PROBe": "探头倍率", "TERMination": "输入阻抗",
           "INVert": "反相", "DELAy": "延迟校正(s)"}


def read_extended(driver):
    values = {command: driver.query(command + "?") for command in GLOBAL}
    mode = values[":TRIGger:MODE"].upper()
    for command in TRIGGER_QUERIES.get(mode, []):
        # LINE triggering has no level setting according to the manual.
        source = values.get(":TRIGger:EDGE:SOURce", "").upper()
        if command == ":TRIGger:EDGE:LEVel" and source == "LINE":
            continue
        values[command] = driver.query(command + "?")
    for channel in range(1, 5):
        for suffix in CHANNEL:
            command = f":CHANnel{channel}:{suffix}"
            values[command] = driver.query(command + "?")
    return values


def validate_write(command, value, current):
    """Only explicit edits to known settings; never synthesize a trigger source."""
    if command not in current or command == ":TRIG:STATE":
        raise ValueError("该设置不支持写入或尚未从设备读取")
    value = str(value).strip()
    upper = value.upper()
    allowed = {
        ":ACQUIRE:TYPE": {"NORMAL", "PEAK", "AVERAGES", "HRESOLUTION"},
        ":ACQUIRE:MAREA": {"AUTO", "FIXED"},
        ":TRIGGER:MODE": set(TRIGGER_QUERIES),
        ":TRIGGER:SWEEP": {"AUTO", "NORMAL"},
        ":TRIGGER:COUPLING": {"DC", "AC", "LFREJECT", "HFREJECT"},
    }
    key = command.upper()
    if key in allowed:
        if upper not in allowed[key]:
            raise ValueError("可选值: " + ", ".join(sorted(allowed[key])))
        return upper
    if key.endswith(":UNITS"):
        if upper not in {"VOLTAGE", "AMPERE"}:
            raise ValueError("探头类型应为 VOLTage 或 AMPere")
        return upper
    if key.endswith(":TERMINATION"):
        if upper not in {"1M", "50"}:
            raise ValueError("输入阻抗应为 1M 或 50")
        return upper
    if key.endswith(":INVERT"):
        if upper not in {"0", "1", "OFF", "ON"}:
            raise ValueError("反相应为 0/1/OFF/ON")
        return upper
    if key.endswith((":SOURCE", ":ASRC", ":BSRC", ":DSRC", ":CSRC")):
        if upper not in {"CHANNEL1", "CHANNEL2", "CHANNEL3", "CHANNEL4", "LINE", "EXTERNAL"}:
            raise ValueError("触发源应为 CHANnel1–4、LINE 或 EXTernal")
        return upper
    numeric = key.endswith((":PROBE", ":DELAY", ":HOLDOFF", ":AVERAGES", ":LEVEL", ":HLEVEL", ":LLEVEL",
                            ":UWIDTH", ":LWIDTH", ":TUPPER", ":TLOWER", ":TIME", ":STIME", ":HTIME",
                            ":ATHRES", ":BTHRES", ":EDGENUM", ":IDLE", ":LINE"))
    if numeric:
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("必须输入有限数值")
        if key.endswith(":AVERAGES") and (not number.is_integer() or not 2 <= number <= 65536 or int(number) & (int(number) - 1)):
            raise ValueError("平均次数必须为 2–65536 的 2 的整数次幂")
        if key.endswith(":HOLDOFF") and not 0 <= number <= 16:
            raise ValueError("释抑时间范围为 0–16 秒")
        if re.match(r":CHANNEL\d:DELAY$", key) and abs(number) > 100e-9:
            raise ValueError("延迟校正范围为 ±100ns（输入单位为秒）")
        if key.endswith(":PROBE"):
            unit = current.get(command.rsplit(":", 1)[0] + ":UNITs", "VOLTage").upper()
            choices = {10, 5, 2, 1, .5, .2, .1, .05, .02, .01, .005, .002, .001} if unit == "AMPERE" else {.1, .2, .5, 1, 2, 5, 10, 20, 50, 100, 200, 500, 1000}
            if number not in choices:
                raise ValueError("该探头倍率不在设备支持的离散值中")
        return f"{number:.15g}"
    if not re.fullmatch(r"[A-Za-z0-9_+.-]+", value):
        raise ValueError("参数中不能包含命令分隔符或控制字符")
    return value
