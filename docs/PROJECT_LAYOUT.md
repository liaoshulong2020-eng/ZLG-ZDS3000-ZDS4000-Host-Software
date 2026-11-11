# 项目整理说明



整理日期：2026-11-11。



| 位置 | 内容 | 提交源码仓库 |

| --- | --- | --- |

| zds_scope/ | 驱动、协议、存储、UI 与内置图标资源 | 是 |

| run_app.py、run_api_server.py | 程序入口 | 是 |

| test_*.py、capture_test_fixture.py、examples/ | 回归检查、实机辅助脚本、示例 | 是 |

| requirements*.txt、ZLG_Scope.spec、build_exe.ps1 | 依赖与构建配置 | 是 |

| 网络配置脚本 | 本机连接维护工具 | 是，使用前检查目标网卡与地址 |

| docs/、README.md、FEATURES.md | 当前文档和历史说明 | 是 |

| build/、tmp/、日志 | 构建输出与验证产物 | 否，已忽略 |

| data/、WFM 文件 | 采集数据与运行状态 | 否，已忽略 |

| ZDS_USB_DRV/、官方 PDF、manual_settings_reference.txt | 本地厂家参考资料 | 否，已忽略 |

| scope_network_before_*.json | 电脑网络配置备份 | 否，已忽略 |



测试文件暂留根目录，保持现有导入和运行命令兼容。构建文件位于 build/，默认发布目录为 dist/；这些目录不提交源码仓库。

## 发布方式

源码通过 GitHub 仓库获取，可运行程序通过 Releases 下载。版本附件提供 EXE 和 SHA256 校验文件。用户采集数据、本机配置及运行日志不属于源码发布内容。
