[中文](README.md)

# TOKENBANK

Local token usage dashboard for Claude Code & Codex.

## Screenshots

> Lightweight desktop app rendered in a native pywebview window — no browser needed.

## Features

- **Overview** — Summary cards, daily trend chart, cost distribution, model/app rankings, recent messages, cache rate
- **Usage Report** — Aggregated by day/hour, supports 1D/7D/14D/30D/All time ranges
- **Projects** — Per-project usage breakdown
- **Sessions** — Top sessions by usage with summary info
- **Compare** — Compare any two time periods side-by-side, with today/yesterday preset and diff indicators
- **Model Pricing** — Custom model unit prices, add/delete models

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

## Build EXE

```bash
# Option 1: Python script
python build.py

# Option 2: Batch file
build.bat
```

The output `dist/TOKENBANK.exe` is a single portable file that runs on any Windows PC without Python installed.

## Data Sources

Session data is automatically read from:

| Application | Path |
|-------------|------|
| Claude Code | `~/.claude/projects/*/sessions/*.jsonl` |
| Codex | `~/.codex/sessions/*.jsonl` |

All data is read and processed locally — nothing is uploaded to any server.

## Tech Stack

- **Python** — Data reading and aggregation
- **pywebview** — Native desktop window (WebView2 on Windows)
- **Chart.js** — Chart rendering
- **pystray** — System tray icon

## Requirements

- Windows 10+ (WebView2 runtime required, built-in since Win10 21H2+)
- Python 3.9+
