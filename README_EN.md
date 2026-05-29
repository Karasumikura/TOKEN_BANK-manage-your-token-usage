[中文](README.md)

# TOKENBANK

**Local token usage dashboard and cost management panel for AI coding assistants**

## Highlights

- **100% Local, Zero Upload** — All data is read and processed locally, never sent to any external server. Your privacy is fully protected.
- **Ready to Use, Single File** — One `app.py` is the complete app, or pack it into a single `.exe` — no installation needed, just copy and run.
- **Multi-Platform Aggregation** — Supports both Claude Code and Codex session data, unified in a single dashboard.
- **Granular Cost Tracking** — Customizable model pricing down to Input / Output / Cache Read dimensions, with costs calculated automatically.
- **Period Comparison** — Compare any two date ranges side-by-side with one-click presets (Today vs Yesterday, This Week vs Last Week, etc.) and clear diff indicators.
- **Native Desktop Experience** — Powered by pywebview for native window rendering, with system tray support, dark/light themes, and Chinese/English localization.
- **Smart Budget Alerts** — Set daily/monthly spending limits with automatic warnings when approaching or exceeding thresholds.
- **Data Export & Reports** — One-click CSV export and auto-generated usage summary reports for easy sharing and analysis.
- **Desktop Mini View** — Compact always-on-top window for real-time monitoring from a desktop corner.

## Feature Details

### Overview

A global dashboard that gives you the full picture at a glance:

- **6 Key Metric Cards** — Total records, input tokens, output tokens, cache read, estimated cost, and cache hit rate. Each card has a colored top border for quick visual identification.
- **Daily Trend Chart** — Stacked bar chart showing the distribution of Input / Output / Cache Read / Cache Create across dates.
- **Cost Distribution** — Doughnut chart breaking down costs by model (auto-shown when multiple models have cost).
- **By Application Table** — Usage summary grouped by application (Claude Code, Codex, etc.) with color-coded badges.
- **By Model Table** — Usage and cost summary grouped by model.
- **Recent Messages** — Quick preview of the last 10 messages.
- **Efficiency Metrics** — Tokens per message, cost per message, cost per 1K tokens.
- **Cost Forecast** — Projected monthly cost based on the last 7 days' trend.

### Usage Report

Deep-dive into usage patterns over time:

- **Date Range Picker** — Custom start/end dates with quick presets: 1D / 7D / 14D / 30D / All.
- **Token Usage Line Chart** — Tracks Input, Output, and Cache Read trends over time.
- **Daily Cost Line Chart** — Visualizes cost trends over time.
- **Detail Table** — Daily aggregated data table.
- **Hourly Drill-Down** — Selecting a single day automatically switches to a 24-hour granular view, helping you pinpoint high-usage periods.
- **Usage Heatmap** — Weekday x hour heatmap to visualize peak usage periods at a glance.
- **Model Trends** — Line chart showing each model's usage share over time.

### Projects

Usage breakdown by project:

- Auto-decodes Claude Code's encoded project paths (e.g., `C--Users-foo-project` → `C:/Users/foo/project`).
- Shows session count, message count, input/output/cache read tokens, total tokens, and estimated cost per project.

### Sessions

Track consumption at the individual conversation level:

- **Top 100 Ranking** — Sessions sorted by total token usage in descending order, so you can quickly find the most token-hungry conversations.
- Displays application badge, model used, date, message count, input/output tokens, and cost.
- **Session Summary** — Auto-extracted topic summary for each session.
- **Session Detail** — Click any session row to expand and view the full message flow with per-message token usage.
- **Search & Filter** — Filter sessions by keyword, application, or model.

### Compare

Side-by-side comparison of any two time periods to spot usage trends:

- **One-Click Presets** — Today vs Yesterday, This Week vs Last Week, This Month vs Last Month. Click and results appear instantly.
- **Custom Ranges** — Manually select any two date ranges for comparison.
- **Diff Indicator Cards** — For total input, non-cache input, output, cache read, cost, and record count — each shows the delta with a directional arrow.
- **Per-Application Comparison Tables** — Detailed breakdown for each period.
- **Grouped Bar Chart** — Side-by-side bars for 5 core metrics across both periods.

### Settings

- **Language Toggle** — Chinese / English, instant full-UI switch with auto-persisted preference.
- **Theme Toggle** — Dark / Light themes with auto-persisted preference.
- **Number Format** — Chinese units (wan/yi) or Western units (K/M/B), auto-adapts to language, also manually switchable.
- **Model Pricing Editor** — Freely add/delete models, edit Input / Output / Cache Read prices ($/1M tokens) per model. Saved locally to `~/.tokenbank/pricing.json`. One-click reset to defaults.
- **Auto Refresh** — Automatically reload data from disk on a timer (default 30s, configurable: 15s/30s/60s/120s, toggleable).
- **Budget Alerts** — Set daily/monthly spending limits; warning appears at 80% and 100% usage.
- **Manual Reload** — Re-read all session data from disk and refresh the dashboard.

### Data Export

- **Export CSV** — One-click CSV export from Overview, Usage Report, Projects, Sessions, and Compare sections.
- **Usage Report** — Auto-generated text summary for the last 7/30 days or all time, including totals, daily averages, top models, and top projects. One-click copy to clipboard.

### Mini Mode

- **Desktop Mini View** — Click the Mini button to switch to a compact always-on-top window showing only core metrics (input/output/cost). Perfect for monitoring from a desktop corner.

## Screenshots

> Lightweight desktop app rendered in a native pywebview window — no browser needed. Supports system tray minimization; closing the window offers a choice between minimizing to tray or quitting.

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

You can also double-click `TOKENBANK.bat` to launch (automatically uses pythonw to hide the console window when available).

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

## Built-in Model Pricing

| Model | Input ($/1M tokens) | Output ($/1M tokens) | Cache Read ($/1M tokens) |
|-------|-----|------|------|
| claude-opus-4-7 | 5.00 | 25.00 | 0.50 |
| claude-opus-4-6 | 5.00 | 25.00 | 0.50 |
| claude-opus-4-5 | 5.00 | 25.00 | 0.50 |
| claude-sonnet-4-6 | 3.00 | 15.00 | 0.30 |
| claude-sonnet-4-5 | 3.00 | 15.00 | 0.30 |
| claude-haiku-4-5 | 1.00 | 5.00 | 0.10 |
| mimo-v2-pro | 1.00 | 3.00 | 0.00 |
| mimo-v2.5-pro | 0.00 | 0.00 | 0.00 |
| gpt-5.4 | 2.50 | 15.00 | 0.25 |
| gpt-5.5 | 5.00 | 30.00 | 0.50 |

Unmatched models fall back to default pricing: Input $3.00 / Output $15.00 / Cache Read $0.30. All prices are fully customizable in Settings.

## Tech Stack

- **Python** — Data reading and aggregation
- **pywebview** — Native desktop window (WebView2 on Windows)
- **Chart.js** — Chart rendering (bar, line, doughnut)
- **pystray** — System tray icon and menu
- **Pillow** — Programmatic tray icon generation
- **PyInstaller** — Single-file EXE packaging

## Requirements

- Windows 10+ (WebView2 runtime required, built-in since Win10 21H2+)
- Python 3.9+
