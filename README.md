[English](README_EN.md)

# TOKENBANK

Claude Code & Codex 本地 Token 用量统计面板。

## 截图

> 轻量级桌面应用，基于 pywebview 原生窗口渲染，无需浏览器。

## 功能

- **概览** — 总量卡片、每日趋势图、费用分布、模型/应用排行、最近消息、缓存率
- **使用报告** — 按日/小时聚合，支持 1D/7D/14D/30D/全部 时间范围
- **项目** — 按项目统计用量
- **会话** — 按会话统计用量排行，含摘要信息
- **对比** — 任意两个时间段对比，支持今天/昨天预设，显示差异指标
- **模型计费** — 自定义模型单价，支持新增/删除模型

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python app.py
```

## 打包为 EXE

```bash
# 方式一：Python 脚本
python build.py

# 方式二：批处理
build.bat
```

打包完成后在 `dist/` 目录生成 `TOKENBANK.exe`，单文件免安装，可直接拷贝到任意 Windows 电脑运行。

## 数据源

自动读取以下路径的会话数据：

| 应用 | 路径 |
|------|------|
| Claude Code | `~/.claude/projects/*/sessions/*.jsonl` |
| Codex | `~/.codex/sessions/*.jsonl` |

数据仅在本地读取和处理，不会上传到任何服务器。

## 技术栈

- **Python** — 数据读取与聚合
- **pywebview** — 原生桌面窗口（Windows 上使用 WebView2）
- **Chart.js** — 图表渲染
- **pystray** — 系统托盘图标

## 系统要求

- Windows 10+（需 WebView2 运行时，Win10 21H2+ 已内置）
- Python 3.9+
