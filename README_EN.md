[中文](README.md)

<div align="center">

# TOKENBANK

**Local token usage dashboard and cost management panel for AI coding assistants**

100% Local · Zero Upload · Ready to Use

</div>

---

## Quick Start

**No install needed — just run:**

```bash
# Double-click
TOKENBANK.bat

# Or from command line
python app.py
```

**Build single-file EXE (no Python required):**

```bash
python build.py    # outputs dist/TOKENBANK.exe
```

---

## Key Features

| Feature | Description |
|:---|:---|
| **100% Local** | All data read and processed locally, never sent to any server |
| **Ready to Use** | Single `app.py` is the complete app, copy and run |
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

## Built-in Model Pricing

| Model | Input | Output | Cache Read |
|:---|---:|---:|---:|
| claude-opus-4-7 | $5.00 | $25.00 | $0.50 |
| claude-sonnet-4-6 | $3.00 | $15.00 | $0.30 |
| claude-haiku-4-5 | $1.00 | $5.00 | $0.10 |
| mimo-v2-pro | $1.00 | $3.00 | $0.00 |
| mimo-v2.5-pro | $0.00 | $0.00 | $0.00 |
| gpt-5.4 | $2.50 | $15.00 | $0.25 |
| gpt-5.5 | $5.00 | $30.00 | $0.50 |

> Prices are $/1M tokens. Fully customizable in Settings. Unmatched models use default pricing.

---

## Tech Stack

Python + pywebview (WebView2) + Chart.js + pystray + Pillow + PyInstaller

## Requirements

Windows 10+ · Python 3.9+
