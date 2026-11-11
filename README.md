# ZLG ZDS3000/ZDS4000 Host Software



项目 / GitHub 仓库名称：`ZLG-ZDS3000-ZDS4000-Host-Software`。中文名称：致远 ZDS3000/ZDS4000 示波器上位机。



适用于致远 ZDS3000 / ZDS4000 Plus 系列的 Windows 上位机。使用 Python、PySide6 和 PyQtGraph，通过 TCP/IP 连接仪器。



## 下载 Windows EXE



在 [最新发布版本](https://github.com/liaoshulong2020-eng/ZLG-ZDS3000-ZDS4000-Host-Software/releases/latest) 的 **Assets** 中下载 `ZLG-Scope-Windows-x64.exe`，不要下载 Source code 当作安装程序。



将 EXE 放到可写目录后双击运行，无需安装 Python。Windows 64 位版本；建议 Windows 10/11。程序未进行商业代码签名。`SHA256SUMS.txt` 提供下载文件校验值。公开仓库，无需登录即可下载。



首次使用：



1. 用网线连接仪器，确保电脑和仪器 IP 可达。

2. 在右侧“设备”菜单填写仪器 IP 和端口（默认 5025），点击连接。

3. 点击“刷新波形”读取数据；点击通道卡片设置参数。

4. “导出 WFM”保存原始波形；保存成功后可打开保存文件夹。全量 CSV 在“数据导出”中操作。



更新版本前关闭旧程序，保留原应用目录的 `data/`，再替换 EXE。EXE 附件与源码分别发布，仓库不包含个人采集文件。



## 运行与目录



- 源码：下载或克隆本仓库，项目根目录包含 `run_app.py`。
- 可运行程序：从 Releases 下载 `ZLG-Scope-Windows-x64.exe`，放到你选择的目录。
- EXE 无需安装 Python。运行数据保存在应用目录的 `data/`。

- 源码运行：安装 `requirements.txt` 后执行 `python run_app.py`。



## 当前功能



- 设备菜单中配置 IP 和端口。连接后读取仪器当前设置，不用上位机默认值覆盖仪器。

- RUN / STOP / SINGLE / AUTO 控制，空闲时后台同步仪器运行状态；波形通过“刷新波形”手动读取。

- 各通道独立显示档位、名称与位置；本地居中默认关闭，通道设置中可单独开启。

- 左侧通道基准标记、双光标、可拖动收起的底部工具区。概览保留分块极值，放大后细化采样点，原始 WFM 不抽样。

- 原生 WFM 保存、最近保存目录记忆、保存时间戳和打开保存文件夹；数据导出包含全量 CSV 等选项。

- 原生截图、自动命名、反色、剪贴板、保存和双击预览。

- 硬件测量、FFT 仪器画面、电源环路工具。

- Math：加减乘除、微分、积分、信源和反相。点击应用后写入仪器，结果通过仪器截图查看。

- Decode：只读仪器当前解码事件表，支持复制、CSV 导出；协议、信源和阈值在仪器上配置。列按原始字段展示，最多预览 2000 块，复制和导出保留全部返回内容。



示例仪器地址为 `192.168.1.100:5025`，请改成实际仪器 IP。网络路由需能直接到达仪器；大记录读取期间状态查询会让路。



## 开发及打包



```powershell

python -m pip install -r requirements-dev.txt

python run_app.py

powershell -ExecutionPolicy Bypass -File build_exe.ps1 -Python python

```



打包脚本生成 `build/release/` 中的 EXE 并执行启动自检。可用 `-OutputDirectory` 指定发布目录，未指定时发布到项目内的 `dist/`。通过 `-Python` 指定 Python 解释器。



不连接仪器的回归检查示例：



```powershell

python -m unittest test_connection_ui test_run_state_sync test_plot_interaction test_instrument_analysis test_display_envelope

```



实机脚本需要仪器连接，不要将所有 `test_*.py` 默认当作离线单元测试运行。Math / Decode 已通过模拟驱动测试，仍需按仪器固件验证。PEAK 等模式的物理量标定与传输完整性是不同检查，原始码保留在 WFM。



## 源码管理



目录分类、GitHub 核查结果与提交范围见 [项目整理说明](docs/PROJECT_LAYOUT.md)。历史说明见 [FEATURES.md](FEATURES.md)。

