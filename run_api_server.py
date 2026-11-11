"""
致远 ZDS 示波器本地控制 RESTful 服务独立启动入口
可用于 Headless 无界面运行，为自动化测试脚本提供 HTTP API
"""

import argparse
import uvicorn
from zds_scope.server import app, state
from zds_scope.driver import ZDScopeDriver


def main():
    parser = argparse.ArgumentParser(description="ZLG ZDS Scope Control API Server")
    parser.add_argument("--host", default="127.0.0.1", help="API 服务器监听地址")
    parser.add_argument("--port", type=int, default=8000, help="API 服务器端口")
    parser.add_argument("--scope-ip", default="192.168.1.100", help="示波器以太网 IP 地址")
    parser.add_argument("--scope-port", type=int, default=5025, help="示波器 SCPI 端口 (默认 5025)")
    args = parser.parse_args()

    print(f"=== 致远 ZDS 示波器本地控制 RESTful 服务 ===")
    print(f"API 服务地址: http://{args.host}:{args.port}")
    print(f"API 在线文档: http://{args.host}:{args.port}/docs")
    print(f"示波器目标: {args.scope_ip}:{args.scope_port}")

    # 自动预连接
    try:
        state.driver = ZDScopeDriver(host=args.scope_ip, port=args.scope_port)
        state.driver.connect()
        print(f"示波器预连接成功: {state.driver.device_info}")
    except Exception as e:
        print(f"提示: 初始连接暂未建立 ({e})，可通过 API 接口随时连接。")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
