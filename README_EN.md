[中文](README.md)

<div align="center">

# TOKENBANK

**Local token usage dashboard and cost management panel for AI coding assistants**

100% Local · Zero Upload · Ready to Use

</div>

---

<div align="center">
<img src="PIC/屏幕截图%202026-05-29%20160646.png" width="90%" alt="Overview Dashboard">
<img src="PIC/屏幕截图%202026-05-29%20160653.png" width="90%" alt="Usage Report">
<img src="PIC/屏幕截图%202026-05-29%20161301.png" width="90%" alt="Mini View">
</div>

---

## Key Features

| Feature | Description |
|:---|:---|
| **100% Local** | All data read and processed locally, never sent to any server |
| **Ready to Use** | Single `app.py` is the complete app, or pack into a single `.exe` |
| **Multi-Platform** | Unified dashboard for Claude Code / Codex / Cline sessions |
| **Granular Pricing** | Custom model rates for Input / Output / Cache Read |
| **Period Compare** | One-click presets (Today vs Yesterday, This Week vs Last Week, etc.) |
| **Smart Budgets** | Daily/monthly limits, per-app and per-model with usage tracking |
| **Data Export** | One-click CSV export, auto-generated usage summary reports |
| **Mini View** | Compact always-on-top window for real-time monitoring |
| **Native Desktop** | pywebview window, system tray, dark/light themes, Chinese/English |

---

## Feature Details

### Overview Dashboard

- **6 Key Metrics** — Records, Input Tokens, Output Tokens, Cache Read, Cost, Cache Hit Rate
- **Daily Trend** — Stacked bar chart for Input / Output / Cache Read distribution
- **Cost Breakdown** — Doughnut chart by model (auto-shown for multi-model)
- **App & Model Tables** — Grouped summaries with color-coded badges
- **Efficiency Metrics** — Tokens/message, cost/message, cost/1K tokens
- **Cost Forecast** — Projected monthly cost from 7-day trend

### Usage Report

- **Date Range** — Custom picker with 1D / 7D / 14D / 30D / All presets
- **Trend Charts** — Token usage line chart + daily cost line chart
- **Hourly Drill-Down** — Select a single day for 24-hour granularity
- **Heatmap** — Short range: date × hour; long range: weekday × hour
- **Model Ranking** — Per-model per-period token comparison, sorted descending
- **Model Trends** — Usage share over time by model

### Projects / Sessions

- Project-level usage breakdown, auto-decodes Claude Code paths
- Session Top 100 ranking with search, filter, and expandable message detail

### Compare

- **One-Click Presets** — Today vs Yesterday, This Week vs Last Week, etc.
- **Diff Indicators** — Delta with directional arrows
- **Grouped Bar Chart** — Side-by-side period comparison

### Budget Management

- **Dual Mode** — Switch between Token budget and Cost budget
- **Global Limits** — Daily + monthly with ring progress visualization
- **Per-App/Model Limits** — Independent settings with usage rate + ratio
- **Alerts** — Automatic warnings at 80% / 100% thresholds

### Settings

- Chinese/English toggle · Dark/Light theme
- Model pricing editor (saved to `~/.tokenbank/pricing.json`)
- Auto-refresh (15s / 30s / 60s / 120s)

---

## Data Sources

| Application | Path |
|:---|:---|
| Claude Code | `~/.claude/projects/*/sessions/*.jsonl` |
| Codex | `~/.codex/sessions/*.jsonl` |

---

## Custom Model Pricing

Set per-model Input / Output / Cache Read prices ($/1M tokens). Built-in defaults for popular models, fully customizable in Settings — add new models, edit prices, or reset to defaults. Unmatched models fall back to default pricing.

---

## Quick Start

**Option 1: Use the pre-built EXE (no Python required)**

`dist/TOKENBANK.exe` is a single portable file. Copy it to any Windows PC and run directly.

**Option 2: Run from source**

```bash
pip install -r requirements.txt
python app.py
```

Or double-click `TOKENBANK.bat` (automatically uses pythonw to hide the console when available).

**Option 3: Build your own EXE**

```bash
python build.py    # outputs dist/TOKENBANK.exe
```

---

## Tech Stack

Python + pywebview (WebView2) + Chart.js + pystray + Pillow + PyInstaller

## Requirements

Windows 10+ · Python 3.9+
