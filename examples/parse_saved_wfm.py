"""
离线解析已有 .wfm 波形文件并导出为 CSV / 绘制波形示例
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import matplotlib.pyplot as plt
from zds_scope.protocol import parse_wfm
from zds_scope.storage import ScopeStorage


def main():
    parser = argparse.ArgumentParser(description="ZLG WFM File Parser & Exporter")
    parser.add_argument("wfm_file", nargs="?", help="WFM 文件路径")
    parser.add_argument("--to-csv", help="导出的 CSV 目标路径")
    parser.add_argument("--plot", action="store_true", help="是否绘制波形预览")
    args = parser.parse_args()

    # 如果没有指定文件，则查找最近保存的一个
    wfm_path = args.wfm_file
    if not wfm_path:
        search_dir = "data/captures"
        if os.path.exists(search_dir):
            for root, dirs, files in os.walk(search_dir):
                for f in files:
                    if f.endswith(".wfm"):
                        wfm_path = os.path.join(root, f)
                        break
                if wfm_path:
                    break

    if not wfm_path or not os.path.exists(wfm_path):
        print("未找到可解析的 .wfm 文件。请传入文件路径，例如: python examples/parse_saved_wfm.py path/to/waveform.wfm")
        return

    print(f"正在读取并解析: {wfm_path}")
    with open(wfm_path, "rb") as f:
        data = f.read()

    wfm = parse_wfm(data)
    print("=== WFM 解析成功 ===")
    print(f"设备名称    : {wfm.head.dev_name}")
    print(f"固件版本    : {wfm.head.firmware_version}")
    print(f"采样率      : {wfm.head.sample_rate:.2e} Hz")
    print(f"水平档位    : {wfm.head.hrzt_division:.2e} s/div")
    print(f"包含通道    : {list(wfm.channels.keys())}")

    for ch_id, ch in wfm.channels.items():
        print(f"\n[CH{ch_id}] 采样点数: {len(ch.voltage_array)}, Vpp: {ch.vpp:.3f} V, Vrms: {ch.vrms:.3f} V, Vavg: {ch.vavg:.3f} V")

    # 导出 CSV
    if args.to_csv:
        csv_path = ScopeStorage.save_csv(wfm, args.to_csv)
        print(f"\n已成功导出 CSV 至: {csv_path}")

    # 绘图
    if args.plot:
        plt.figure(figsize=(10, 5))
        for ch_id, ch in wfm.channels.items():
            t_us = ch.time_array * 1e6
            plt.plot(t_us, ch.voltage_array, label=f"CH{ch_id}")
        plt.title(f"Waveform: {os.path.basename(wfm_path)}")
        plt.xlabel("Time (us)")
        plt.ylabel("Voltage (V)")
        plt.grid(True, linestyle="--", alpha=0.6)
        plt.legend()
        plt.tight_layout()
        plt.show()


if __name__ == "__main__":
    main()
