#!/usr/bin/env python3
"""TOKENBANK - Desktop Token Usage Dashboard"""

import json
import os
import re
import sys
import threading
import queue
import webview
import pystray
from PIL import Image, ImageDraw
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    import ctypes
    ctypes.windll.user32.ShowWindow(ctypes.windll.kernel32.GetConsoleWindow(), 0)

# ── Paths ────────────────────────────────────────────────────────────────────

HOME = Path.home()
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CLINE_BASE = HOME / "AppData" / "Roaming" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev"

# ── Model pricing (USD per 1M tokens) ────────────────────────────────────────

MODEL_PRICING = {
    "claude-opus-4-7":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5},
    "claude-opus-4-6":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5},
    "claude-opus-4-5":   {"input": 5.0,  "output": 25.0, "cache_read": 0.5},
    "claude-sonnet-4-6": {"input": 3.0,  "output": 15.0, "cache_read": 0.3},
    "claude-sonnet-4-5": {"input": 3.0,  "output": 15.0, "cache_read": 0.3},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_read": 0.1},
    "mimo-v2-pro":       {"input": 1.0,  "output": 3.0,  "cache_read": 0.0},
    "mimo-v2.5-pro":     {"input": 0.0,  "output": 0.0,  "cache_read": 0.0},
    "gpt-5.4":           {"input": 2.5,  "output": 15.0, "cache_read": 0.25},
    "gpt-5.5":           {"input": 5.0,  "output": 30.0, "cache_read": 0.5},
}
DEFAULT_PRICING = {"input": 3.0, "output": 15.0, "cache_read": 0.3}


CUSTOM_PRICING = {}

def load_custom_pricing():
    global CUSTOM_PRICING
    p = Path.home() / ".tokenbank" / "pricing.json"
    if p.exists():
        try:
            CUSTOM_PRICING = json.loads(p.read_text(encoding="utf-8"))
            for k, v in CUSTOM_PRICING.items():
                MODEL_PRICING[k] = v
        except (json.JSONDecodeError, OSError):
            pass

load_custom_pricing()

def get_pricing(model):
    for key, val in MODEL_PRICING.items():
        if key in (model or ""):
            return val
    return DEFAULT_PRICING


def calc_cost(model, inp, out, cache_read):
    p = get_pricing(model)
    return (inp * p["input"] + out * p["output"] + cache_read * p["cache_read"]) / 1_000_000


# ── Data loading ─────────────────────────────────────────────────────────────

def load_claude_sessions():
    records = []
    if not CLAUDE_PROJECTS.exists():
        return records
    for jsonl_path in CLAUDE_PROJECTS.rglob("*.jsonl"):
        if "subagents" in str(jsonl_path):
            continue
        rel = jsonl_path.relative_to(CLAUDE_PROJECTS)
        project = rel.parts[0] if rel.parts else "unknown"
        session_id = jsonl_path.stem
        summary = ""
        try:
            lines = []
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                        lines.append(obj)
                    except json.JSONDecodeError:
                        continue
            for obj in lines:
                if obj.get("type") == "system" and obj.get("subtype") == "away_summary":
                    raw = re.sub(r'^\s+|\s+$', '', obj.get("content", ""))
                    summary = re.sub(r'\s*\(disable recaps in /config\)\s*$', '', raw)
            for obj in lines:
                if obj.get("type") != "assistant":
                    continue
                usage = obj.get("message", {}).get("usage", {})
                inp = usage.get("input_tokens", 0)
                out = usage.get("output_tokens", 0)
                cr = usage.get("cache_read_input_tokens", 0)
                cc = usage.get("cache_creation_input_tokens", 0)
                if inp == 0 and out == 0:
                    continue
                model = obj.get("message", {}).get("model", "unknown")
                ts = obj.get("timestamp", "")
                local_dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone() if ts else None
                date_str = local_dt.strftime("%Y-%m-%d") if local_dt else ""
                ts_local = local_dt.strftime("%Y-%m-%dT%H:%M:%S") if local_dt else ts
                records.append({
                    "app": "claude", "project": project, "session": session_id,
                    "summary": summary, "model": model, "timestamp": ts_local,
                    "date": date_str,
                    "input": inp, "output": out, "cache_read": cr, "cache_create": cc,
                    "cost": calc_cost(model, inp, out, cr),
                })
        except (UnicodeDecodeError, OSError):
            continue
    return records


def load_codex_sessions():
    records = []
    if not CODEX_SESSIONS.exists():
        return records
    for jsonl_path in CODEX_SESSIONS.rglob("*.jsonl"):
        session_id = jsonl_path.stem
        session_date = session_id[8:18] if session_id.startswith("rollout-") and len(session_id) > 18 else ""
        model = None
        max_total = max_input = max_output = max_cached = 0
        last_ts = ""
        msg_count = 0
        try:
            with open(jsonl_path, encoding="utf-8") as f:
                for line in f:
                    try:
                        obj = json.loads(line.strip())
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "turn_context":
                        model = obj.get("payload", {}).get("model")
                    if obj.get("type") == "event_msg":
                        payload = obj.get("payload", {})
                        msg_type = payload.get("type", "")
                        if msg_type in ("user_message", "task_started", "task_complete", "error"):
                            msg_count += 1
                        if msg_type == "token_count":
                            info = payload.get("info")
                            if not info:
                                continue
                            for field in ("total_token_usage", "last_token_usage"):
                                u = info.get(field, {})
                                t = u.get("total_tokens", 0)
                                if t > max_total:
                                    max_total = t
                                    max_input = u.get("input_tokens", 0)
                                    max_output = u.get("output_tokens", 0)
                                    max_cached = u.get("cached_input_tokens", 0)
                                    last_ts = obj.get("timestamp", "")
        except (UnicodeDecodeError, OSError):
            continue
        if max_total == 0:
            continue
        if max_input == 0 and max_output == 0:
            max_output = int(max_total * 0.3)
            max_input = max_total - max_output
        non_cached = max_input - max_cached
        if msg_count == 0:
            msg_count = 1
        codex_local_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00")).astimezone() if last_ts else None
        codex_ts_local = codex_local_dt.strftime("%Y-%m-%dT%H:%M:%S") if codex_local_dt else last_ts
        codex_date = codex_local_dt.strftime("%Y-%m-%d") if codex_local_dt else ""
        records.append({
            "app": "codex", "project": "codex", "session": session_id,
            "summary": "", "model": model or "unknown", "timestamp": codex_ts_local,
            "date": session_date or codex_date,
            "input": non_cached, "output": max_output,
            "cache_read": max_cached, "cache_create": 0,
            "cost": calc_cost(model or "unknown", non_cached, max_output, max_cached),
            "count": msg_count,
        })
    return records


def load_cline_sessions():
    records = []
    history_path = CLINE_BASE / "state" / "taskHistory.json"
    if not history_path.exists():
        return records
    try:
        tasks = json.loads(history_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return records
    for t in tasks:
        task_id = t.get("id", "")
        ts_ms = t.get("ts", 0)
        tokens_in = t.get("tokensIn", 0)
        tokens_out = t.get("tokensOut", 0)
        cache_read = t.get("cacheReads", 0)
        cache_create = t.get("cacheWrites", 0)
        cost = t.get("totalCost", 0.0)
        task_text = t.get("task", "")
        summary = re.sub(r'^\s+|\s+$', '', task_text)[:80]
        dt = datetime.fromtimestamp(ts_ms / 1000) if ts_ms else None
        date_str = dt.strftime("%Y-%m-%d") if dt else ""
        ts_str = dt.strftime("%Y-%m-%dT%H:%M:%S") if dt else ""
        model = ""
        meta_path = CLINE_BASE / "tasks" / task_id / "task_metadata.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                usages = meta.get("model_usage", [])
                if usages:
                    model = usages[-1].get("model_id", "")
            except (json.JSONDecodeError, OSError):
                pass
        project = t.get("cwdOnTaskInitialization", "")
        if project:
            project = project.replace("\\", "/").split("/")[-1]
        else:
            project = "cline"
        if tokens_in == 0 and tokens_out == 0:
            continue
        records.append({
            "app": "cline", "project": project, "session": task_id,
            "summary": summary, "model": model or "unknown", "timestamp": ts_str,
            "date": date_str,
            "input": tokens_in, "output": tokens_out,
            "cache_read": cache_read, "cache_create": cache_create,
            "cost": cost,
        })
    return records


def decode_project(encoded):
    if encoded.startswith("C--"):
        return encoded.replace("--", ":/").replace("-", "/")
    return encoded


# ── System tray ──────────────────────────────────────────────────────────────

_dialog_queue = queue.Queue()
_app_window = None
_tray_icon = None


def create_tray_image():
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=(99, 102, 241))
    draw.text((14, 18), "TB", fill=(255, 255, 255))
    return img


def show_window(icon=None, item=None):
    global _app_window
    if _app_window:
        _app_window.show()
        _app_window.restore()


def quit_app(icon=None, item=None):
    global _app_window, _tray_icon
    if _tray_icon:
        _tray_icon.stop()
    if _app_window:
        _app_window.destroy()
    os._exit(0)


def on_closing(*args):
    _dialog_queue.put("close")
    return False


def dialog_worker():
    while True:
        msg = _dialog_queue.get()
        if msg == "close":
            try:
                import ctypes
                result = ctypes.windll.user32.MessageBoxW(
                    0,
                    "最小化到系统托盘还是退出？\n\n点击「是」最小化到托盘\n点击「否」退出程序",
                    "TOKENBANK",
                    3 | 0x40 | 0x1000,
                )
                if result == 6:
                    if _app_window:
                        _app_window.minimize()
                        _app_window.hide()
                else:
                    quit_app()
            except Exception:
                quit_app()


def setup_tray():
    global _tray_icon
    icon_img = create_tray_image()
    menu = pystray.Menu(
        pystray.MenuItem("显示窗口", show_window, default=True),
        pystray.MenuItem("退出", quit_app),
    )
    _tray_icon = pystray.Icon("TOKENBANK", icon_img, "TOKENBANK", menu)
    _tray_icon.run()


# ── API exposed to JS ────────────────────────────────────────────────────────

class Api:
    def __init__(self):
        self._records = None

    def load_data(self):
        if self._records is None:
            self._records = load_claude_sessions() + load_codex_sessions()
            # self._records += load_cline_sessions()  # TODO: fix Cline display bugs
        return self._records

    def get_all(self):
        return json.dumps(self._process(self.load_data()))

    def get_filtered(self, start_date, end_date):
        records = [r for r in self.load_data() if start_date <= r["date"] <= end_date]
        return json.dumps(self._process(records))

    def compare(self, s1, e1, s2, e2):
        r1 = [r for r in self.load_data() if s1 <= r["date"] <= e1]
        r2 = [r for r in self.load_data() if s2 <= r["date"] <= e2]
        return json.dumps({
            "period1": {"start": s1, "end": e1, **self._process(r1)},
            "period2": {"start": s2, "end": e2, **self._process(r2)},
        })

    def get_all_dates(self):
        dates = sorted(set(r["date"] for r in self.load_data() if r["date"]))
        return json.dumps(dates)

    def get_hourly(self, date_str):
        records = [r for r in self.load_data() if r.get("date") == date_str and r.get("timestamp")]
        by_hour = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            ts = r["timestamp"]
            hour = ts[11:13] if len(ts) > 13 else "00"
            h = by_hour[hour]
            h["input"] += r["input"]
            h["output"] += r["output"]
            h["cache_read"] += r["cache_read"]
            h["cache_create"] += r.get("cache_create", 0)
            h["cost"] += r["cost"]
            h["count"] += r.get("count", 1)
        hourly = [{"hour": h + ":00", **v, "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"]}
                  for h, v in sorted(by_hour.items())]
        return json.dumps({"date": date_str, "hourly": hourly})

    def reload(self):
        self._records = None
        return self.get_all()

    def get_pricing_data(self):
        return json.dumps({"models": MODEL_PRICING, "default": DEFAULT_PRICING, "custom": self._load_custom_pricing()})

    def set_model_pricing(self, model, inp, out, cache_read):
        custom = self._load_custom_pricing()
        custom[model] = {"input": float(inp), "output": float(out), "cache_read": float(cache_read)}
        self._save_custom_pricing(custom)
        MODEL_PRICING[model] = custom[model]
        self._records = None
        return json.dumps({"ok": True})

    def save_all_pricing(self, models_json):
        models = json.loads(models_json)
        self._save_custom_pricing(models)
        for k, v in models.items():
            MODEL_PRICING[k] = v
        self._records = None
        return json.dumps({"ok": True})

    def reset_pricing(self):
        custom_path = Path.home() / ".tokenbank" / "pricing.json"
        if custom_path.exists():
            custom_path.unlink()
        MODEL_PRICING.clear()
        MODEL_PRICING.update({
            "claude-opus-4-7": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
            "claude-opus-4-6": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
            "claude-opus-4-5": {"input": 5.0, "output": 25.0, "cache_read": 0.5},
            "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
            "claude-sonnet-4-5": {"input": 3.0, "output": 15.0, "cache_read": 0.3},
            "claude-haiku-4-5": {"input": 1.0, "output": 5.0, "cache_read": 0.1},
            "mimo-v2-pro": {"input": 1.0, "output": 3.0, "cache_read": 0.0},
            "mimo-v2.5-pro": {"input": 0.0, "output": 0.0, "cache_read": 0.0},
            "gpt-5.4": {"input": 2.5, "output": 15.0, "cache_read": 0.25},
            "gpt-5.5": {"input": 5.0, "output": 30.0, "cache_read": 0.5},
        })
        self._records = None
        return json.dumps({"ok": True})

    def _load_custom_pricing(self):
        p = Path.home() / ".tokenbank" / "pricing.json"
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save_custom_pricing(self, custom):
        d = Path.home() / ".tokenbank"
        d.mkdir(exist_ok=True)
        (d / "pricing.json").write_text(json.dumps(custom, indent=2), encoding="utf-8")

    def _process(self, records):
        by_app = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            a = by_app[r["app"]]
            a["input"] += r["input"]
            a["output"] += r["output"]
            a["cache_read"] += r["cache_read"]
            a["cache_create"] += r.get("cache_create", 0)
            a["cost"] += r["cost"]
            a["count"] += r.get("count", 1)

        by_model = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            m = by_model[r["model"]]
            m["input"] += r["input"]
            m["output"] += r["output"]
            m["cache_read"] += r["cache_read"]
            m["cache_create"] += r.get("cache_create", 0)
            m["cost"] += r["cost"]
            m["count"] += r.get("count", 1)

        by_date = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            if r["date"]:
                d = by_date[r["date"]]
                d["input"] += r["input"]
                d["output"] += r["output"]
                d["cache_read"] += r["cache_read"]
                d["cache_create"] += r.get("cache_create", 0)
                d["cost"] += r["cost"]
                d["count"] += r.get("count", 1)

        by_proj = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0, "sessions": set()})
        for r in records:
            p = by_proj[r["project"]]
            p["input"] += r["input"]
            p["output"] += r["output"]
            p["cache_read"] += r["cache_read"]
            p["cache_create"] += r.get("cache_create", 0)
            p["cost"] += r["cost"]
            p["count"] += r.get("count", 1)
            p["sessions"].add(r["session"])

        by_sess = defaultdict(lambda: {"app": "", "project": "", "model": "", "date": "", "summary": "", "input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            s = by_sess[r["session"]]
            s["app"] = r["app"]
            s["project"] = r["project"]
            s["model"] = r["model"]
            s["date"] = r["date"]
            if r.get("summary"):
                s["summary"] = r["summary"]
            s["input"] += r["input"]
            s["output"] += r["output"]
            s["cache_read"] += r["cache_read"]
            s["cache_create"] += r.get("cache_create", 0)
            s["cost"] += r["cost"]
            s["count"] += r.get("count", 1)

        total_input_full = sum(a["input"] + a["cache_read"] + a["cache_create"] for a in by_app.values())
        cache_denom = sum(a["input"] + a["cache_read"] + a["cache_create"] for a in by_app.values())
        cache_rate = (sum(a["cache_read"] for a in by_app.values()) / cache_denom * 100) if cache_denom else 0
        recent = sorted(records, key=lambda r: r.get("timestamp", ""), reverse=True)[:10]
        recent_out = [{"timestamp": r.get("timestamp", ""), "app": r["app"], "model": r["model"],
                        "session": r["session"], "summary": r.get("summary", ""),
                        "input": r["input"], "output": r["output"],
                        "cache_read": r["cache_read"], "cache_create": r.get("cache_create", 0),
                        "cost": r["cost"]} for r in recent]
        return {
            "summary": {
                "total_input": sum(a["input"] for a in by_app.values()),
                "total_input_full": total_input_full,
                "total_output": sum(a["output"] for a in by_app.values()),
                "total_cache_read": sum(a["cache_read"] for a in by_app.values()),
                "total_cache_create": sum(a["cache_create"] for a in by_app.values()),
                "total_cost": sum(a["cost"] for a in by_app.values()),
                "total_records": len(records),
                "cache_rate": round(cache_rate, 1),
            },
            "by_app": dict(by_app),
            "by_model": {k: dict(v) for k, v in sorted(by_model.items(), key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["output"], reverse=True)},
            "daily": [{"date": d, **v, "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"]} for d, v in sorted(by_date.items())],
            "projects": [{"name": decode_project(k), "sessions": len(v["sessions"]), "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"], **{x: v[x] for x in ("count", "input", "output", "cache_read", "cache_create", "cost")}} for k, v in sorted(by_proj.items(), key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["output"], reverse=True)],
            "sessions": [{"id": k, "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"], **{x: v[x] for x in ("app", "project", "model", "date", "summary", "count", "input", "output", "cache_read", "cache_create", "cost")}} for k, v in sorted(by_sess.items(), key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["output"], reverse=True)[:100]],
            "recent": recent_out,
        }


# ── HTML ─────────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>TOKENBANK</title>
<script>
(function(){
  try{
    var th=localStorage.getItem('tb-theme')||'light';
    document.documentElement.setAttribute('data-theme',th);
    var lg=localStorage.getItem('tb-lang')||'zh';
    document.documentElement.lang=lg==='zh'?'zh-CN':'en';
  }catch(e){}
})();
</script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#09090b;--surface:#18181b;--surface2:#27272a;--border:#3f3f46;
  --text:#fafafa;--dim:#a1a1aa;--muted:#71717a;
  --indigo:#818cf8;--cyan:#22d3ee;--green:#4ade80;--amber:#fbbf24;
  --red:#f87171;--purple:#c084fc;--pink:#f472b6;--blue:#60a5fa;
  --indigo-bg:rgba(129,140,248,.12);--cyan-bg:rgba(34,211,238,.12);
  --green-bg:rgba(74,222,128,.12);--amber-bg:rgba(251,191,36,.12);
  --red-bg:rgba(248,113,113,.12);--purple-bg:rgba(192,132,252,.12);
}
:root[data-theme="light"]{
  --bg:#ffffff;--surface:#f8f9fa;--surface2:#e9ecef;--border:#dee2e6;
  --text:#1a1a2e;--dim:#495057;--muted:#868e96;
  --indigo:#6366f1;--cyan:#06b6d4;--green:#16a34a;--amber:#d97706;
  --red:#dc2626;--purple:#9333ea;--pink:#db2777;--blue:#2563eb;
  --indigo-bg:rgba(99,102,241,.1);--cyan-bg:rgba(6,182,212,.1);
  --green-bg:rgba(22,163,74,.1);--amber-bg:rgba(217,119,6,.1);
  --red-bg:rgba(220,38,38,.1);--purple-bg:rgba(147,51,234,.1);
}
body{font-family:'Inter',system-ui,sans-serif;background:var(--bg);color:var(--text);overflow:hidden;height:100vh}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* Easing tokens */
:root{
  --ease-spring:cubic-bezier(.34,1.56,.64,1);
  --ease-out-back:cubic-bezier(.34,1.2,.64,1);
  --ease-out-expo:cubic-bezier(.16,1,.3,1);
  --ease-snap:cubic-bezier(.2,0,0,1);
}

/* Entrance animations */
@keyframes fadeSlideIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeSlideUp{from{opacity:0;transform:translateY(24px) scale(.97)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes fadeSlideLeft{from{opacity:0;transform:translateX(-16px)}to{opacity:1;transform:translateX(0)}}
@keyframes fadeScaleIn{from{opacity:0;transform:scale(.92)}to{opacity:1;transform:scale(1)}}
@keyframes panelIn{from{opacity:0;transform:translateY(20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes popIn{from{opacity:0;transform:scale(.5)}60%{transform:scale(1.12)}to{opacity:1;transform:scale(1)}}
@keyframes slideDown{from{opacity:0;transform:translateY(-100%)}to{opacity:1;transform:translateY(0)}}
@keyframes slideRight{from{opacity:0;transform:translateX(-100%)}to{opacity:1;transform:translateX(0)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes ripple{to{transform:scale(2.5);opacity:0}}
@keyframes rowIn{from{opacity:0;transform:translateX(-8px)}to{opacity:1;transform:translateX(0)}}
@keyframes dotPulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.8);opacity:.4}}

/* Toggle group */
.toggle-group{display:flex;background:var(--surface2);border-radius:6px;overflow:hidden;position:relative}
.toggle-group button{background:transparent;border:none;color:var(--muted);padding:5px 10px;font-size:11px;cursor:pointer;font-family:inherit;font-weight:500;transition:all .25s var(--ease-spring);position:relative;z-index:1}
.toggle-group button.active{background:var(--indigo);color:#fff;transform:scale(1.05);box-shadow:0 2px 8px rgba(99,102,241,.3)}
.toggle-group button:hover:not(.active){color:var(--text);background:rgba(255,255,255,.05)}

/* Layout */
.app{display:grid;grid-template-columns:220px 1fr;grid-template-rows:56px 1fr;height:100vh}
.topbar{grid-column:1/-1;background:var(--surface);box-shadow:0 1px 3px rgba(0,0,0,.06);display:flex;align-items:center;padding:0 24px;gap:16px;justify-content:space-between;animation:slideDown .5s var(--ease-out-expo) both;z-index:10}
.topbar h1{font-size:18px;font-weight:700;letter-spacing:1.5px}
.topbar h1 span{color:var(--indigo);display:inline-block;transition:transform .3s var(--ease-spring)}
.topbar h1:hover span{transform:scale(1.1) rotate(-2deg)}
.topbar .actions{display:flex;gap:8px;align-items:center}
.sidebar{background:var(--surface);box-shadow:1px 0 3px rgba(0,0,0,.06);padding:16px 12px;display:flex;flex-direction:column;gap:2px;overflow-y:auto;animation:slideRight .5s var(--ease-out-expo) .1s both}
.nav-item{padding:10px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:var(--dim);display:flex;align-items:center;gap:10px;transition:all .25s var(--ease-spring);user-select:none;animation:fadeSlideLeft .4s var(--ease-out-back) both}
.nav-item:nth-child(1){animation-delay:.15s}.nav-item:nth-child(2){animation-delay:.2s}.nav-item:nth-child(3){animation-delay:.25s}.nav-item:nth-child(4){animation-delay:.3s}.nav-item:nth-child(5){animation-delay:.35s}.nav-item:nth-child(7){animation-delay:.4s}
.nav-item:hover{background:var(--surface2);color:var(--text);transform:translateX(4px);padding-left:18px}
.nav-item:active{transform:translateX(2px) scale(.97)}
.nav-item.active{background:var(--indigo-bg);color:var(--indigo);box-shadow:inset 3px 0 0 var(--indigo)}
.nav-item .icon{width:18px;text-align:center;font-size:15px;transition:transform .3s var(--ease-spring)}
.nav-item:hover .icon{transform:scale(1.2) rotate(-5deg)}
.content{padding:24px;overflow-y:auto;overflow-x:hidden;animation:fadeScaleIn .5s var(--ease-out-expo) .2s both}

/* Components */
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:20px}
.card{background:var(--surface);border-radius:10px;padding:16px 18px;position:relative;overflow:hidden;transition:transform .35s var(--ease-spring),box-shadow .35s ease;animation:fadeSlideUp .5s var(--ease-out-back) both;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.card:nth-child(1){animation-delay:.1s}.card:nth-child(2){animation-delay:.15s}.card:nth-child(3){animation-delay:.2s}.card:nth-child(4){animation-delay:.25s}.card:nth-child(5){animation-delay:.3s}.card:nth-child(6){animation-delay:.35s}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:2px;transition:height .3s var(--ease-spring)}
.card:hover::before{height:3px}
.card:hover{transform:translateY(-4px) scale(1.02);box-shadow:0 8px 25px rgba(0,0,0,.12)}
.card:active{transform:translateY(-1px) scale(.99)}
.card.i1::before{background:var(--indigo)}.card.i2::before{background:var(--cyan)}.card.i3::before{background:var(--green)}.card.i4::before{background:var(--purple)}.card.i5::before{background:var(--red)}
.card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.card .value{font-size:24px;font-weight:700;letter-spacing:-.5px;transition:transform .3s var(--ease-spring)}
.card:hover .value{transform:scale(1.05)}
.card .sub{font-size:11px;color:var(--muted);margin-top:4px}
.c-indigo{color:var(--indigo)}.c-cyan{color:var(--cyan)}.c-green{color:var(--green)}.c-purple{color:var(--purple)}.c-red{color:var(--red)}.c-amber{color:var(--amber)}

.panel{background:var(--surface);border-radius:10px;padding:18px;margin-bottom:16px;transition:transform .3s var(--ease-spring),box-shadow .3s ease;animation:panelIn .5s var(--ease-out-back) both;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.panel:hover{box-shadow:0 4px 20px rgba(0,0,0,.1)}
.panel h3{font-size:13px;font-weight:600;margin-bottom:14px;color:var(--text);display:flex;align-items:center;gap:8px}
.panel h3 .dot{width:6px;height:6px;border-radius:50%;background:var(--indigo);animation:dotPulse 2s ease infinite}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.chart-box{position:relative;height:260px}

table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:10px 10px;color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0;background:var(--surface)}
td{padding:10px 10px;transition:background .2s ease;white-space:nowrap}
td:last-child{max-width:180px;overflow:hidden;text-overflow:ellipsis}
tbody tr:nth-child(even){background:rgba(255,255,255,.015)}
:root[data-theme="light"] tbody tr:nth-child(even){background:rgba(0,0,0,.015)}
tr{animation:rowIn .3s var(--ease-out-back) both}
tr:nth-child(1){animation-delay:0s}tr:nth-child(2){animation-delay:.03s}tr:nth-child(3){animation-delay:.06s}tr:nth-child(4){animation-delay:.09s}tr:nth-child(5){animation-delay:.12s}tr:nth-child(6){animation-delay:.15s}tr:nth-child(7){animation-delay:.18s}tr:nth-child(8){animation-delay:.21s}tr:nth-child(9){animation-delay:.24s}tr:nth-child(10){animation-delay:.27s}
tr:hover td{background:rgba(255,255,255,.05)}
:root[data-theme="light"] tr:hover td{background:rgba(0,0,0,.04)}
:root[data-theme="light"]::-webkit-scrollbar-thumb{background:#ced4da}
.date-bar input[type=date]::-webkit-calendar-picker-indicator{filter:invert(1)}
:root[data-theme="light"] .date-bar input[type=date]::-webkit-calendar-picker-indicator{filter:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.3px;transition:all .25s var(--ease-spring);animation:popIn .4s var(--ease-spring) both}
.badge:hover{transform:scale(1.1)}
.b-claude{background:var(--indigo-bg);color:var(--indigo)}.b-codex{background:var(--amber-bg);color:var(--amber)}.b-cline{background:var(--green-bg);color:var(--green)}

/* Date picker */
.date-bar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap;animation:fadeSlideIn .4s var(--ease-out-back) .1s both}
.date-bar label{font-size:12px;color:var(--muted)}
.date-bar input[type=date]{background:var(--surface2);border:1px solid rgba(127,127,127,.15);color:var(--text);padding:6px 10px;border-radius:6px;font-size:12px;font-family:inherit;transition:all .25s var(--ease-spring)}
.date-bar input[type=date]:focus{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,.15);transform:scale(1.02)}
.btn{padding:7px 16px;border-radius:6px;border:none;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .25s var(--ease-spring);position:relative;overflow:hidden}
.btn::after{content:'';position:absolute;inset:0;background:radial-gradient(circle,rgba(255,255,255,.2) 10%,transparent 10.01%);transform:scale(0);opacity:0;transition:transform .5s,opacity .4s}
.btn:active::after{transform:scale(2.5);opacity:0;transition:0s}
.btn-primary{background:var(--indigo);color:#fff}.btn-primary:hover{opacity:.9;transform:translateY(-1px);box-shadow:0 4px 12px rgba(99,102,241,.3)}.btn-primary:active{transform:translateY(0) scale(.97)}
.btn-ghost{background:transparent;border:1px solid rgba(127,127,127,.15);color:var(--dim)}.btn-ghost:hover{border-color:var(--indigo);color:var(--indigo);transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.06)}.btn-ghost:active{transform:scale(.96)}
.btn-sm{padding:5px 12px;font-size:11px}
.preset{display:flex;gap:4px}
.preset .btn{font-size:11px;padding:5px 10px;transition:all .2s var(--ease-spring)}
.preset .btn:active{transform:scale(.9)}

/* Compare */
.compare-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.compare-card{background:var(--surface);border-radius:10px;padding:16px;transition:transform .3s var(--ease-spring);box-shadow:0 1px 3px rgba(0,0,0,.06)}
.compare-card:hover{transform:translateY(-2px)}
.compare-card h4{font-size:12px;color:var(--muted);margin-bottom:12px;font-weight:500}
.compare-card .metric{display:flex;justify-content:space-between;padding:6px 0;transition:background .2s ease}
.compare-card .metric:hover{background:rgba(255,255,255,.02)}
.compare-card .metric .k{font-size:12px;color:var(--dim)}
.compare-card .metric .v{font-size:12px;font-weight:600}
.diff{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.diff .item{background:var(--surface);border-radius:8px;padding:10px 14px;min-width:140px;transition:all .3s var(--ease-spring);animation:fadeSlideUp .5s var(--ease-out-back) both;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.diff .item:nth-child(1){animation-delay:.05s}.diff .item:nth-child(2){animation-delay:.1s}.diff .item:nth-child(3){animation-delay:.15s}.diff .item:nth-child(4){animation-delay:.2s}.diff .item:nth-child(5){animation-delay:.25s}
.diff .item:hover{transform:translateY(-3px) scale(1.03);box-shadow:0 6px 20px rgba(0,0,0,.1)}
.diff .item .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.diff .item .v{font-size:16px;font-weight:700;margin-top:4px}
.diff .item .change{font-size:11px;margin-top:2px}
.up{color:var(--red)}.down{color:var(--green)}.same{color:var(--muted)}

.section{display:none;opacity:0;transform:translateY(12px)}.section.active{display:block;animation:fadeSlideIn .4s var(--ease-out-expo) forwards}

/* Loading overlay */
.loading-overlay{position:fixed;inset:0;z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--bg);transition:opacity .4s ease}
.loading-overlay.fade-out{opacity:0;pointer-events:none}
.spinner{width:40px;height:40px;border:3px solid var(--border);border-top-color:var(--indigo);border-radius:50%;animation:spin .8s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{margin-top:16px;font-size:13px;color:var(--dim);font-weight:500;letter-spacing:.5px}

.table-scroll{max-height:400px;overflow-y:auto;overflow-x:auto;-webkit-overflow-scrolling:touch}

/* Responsive */
@media(max-width:1200px){.cards{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){
  .app{grid-template-columns:1fr;grid-template-rows:56px auto 1fr}
  .sidebar{flex-direction:row;overflow-x:auto;overflow-y:hidden;padding:8px 12px;gap:4px;box-shadow:0 1px 3px rgba(0,0,0,.06)}
  .sidebar .nav-item{white-space:nowrap;padding:8px 12px;font-size:12px}
  .sidebar>div[style]{display:none}
  .content{padding:16px}
  .cards{grid-template-columns:repeat(2,1fr);gap:10px}
  .grid2,.grid3,.compare-grid{grid-template-columns:1fr}
  .diff{gap:8px}
  .diff .item{min-width:120px;padding:8px 10px}
}
@media(max-width:600px){
  .cards{grid-template-columns:1fr}
  .topbar h1{font-size:15px}
  .topbar{padding:0 12px}
  .content{padding:12px}
}
</style>
</head>
<body>
<div class="loading-overlay" id="loadingOverlay">
  <div class="spinner"></div>
  <div class="loading-text" id="loadingText">加载中...</div>
</div>
<div class="app">
  <div class="topbar">
    <h1>TOKEN<span>BANK</span></h1>
    <div class="actions">
      <span id="status" style="font-size:11px;color:var(--muted)"></span>
      <button class="btn btn-ghost btn-sm" id="reloadBtn" onclick="reload()">Reload</button>
    </div>
  </div>
  <div class="sidebar">
    <div class="nav-item active" data-s="overview"><span class="icon">&#9673;</span>Overview</div>
    <div class="nav-item" data-s="daily"><span class="icon">&#9776;</span>Daily</div>
    <div class="nav-item" data-s="projects"><span class="icon">&#128193;</span>Projects</div>
    <div class="nav-item" data-s="sessions"><span class="icon">&#128196;</span>Sessions</div>
    <div class="nav-item" data-s="compare"><span class="icon">&#9878;</span>Compare</div>
    <div style="flex:1"></div>
    <div class="nav-item" data-s="settings"><span class="icon">&#9881;</span>Settings</div>
    <div style="padding:8px 14px;font-size:10px;color:var(--muted)">
      <div id="appLabel">Claude Code + Codex</div>
      <div style="margin-top:2px" id="recordCount"></div>
    </div>
  </div>
  <div class="content">

    <!-- Overview -->
    <div class="section active" id="s-overview">
      <div class="cards" id="summaryCards"></div>
      <div class="grid2">
        <div class="panel"><h3 data-i18n="dailyTrend"><span class="dot"></span>Daily Trend</h3><div class="chart-box"><canvas id="cTrend"></canvas></div></div>
        <div class="panel"><h3 data-i18n="costDist"><span class="dot"></span>Cost Distribution</h3><div class="chart-box"><canvas id="cCost"></canvas></div></div>
      </div>
      <div class="grid2">
        <div class="panel"><h3 data-i18n="byApp"><span class="dot"></span>By Application</h3><div class="table-scroll"><table id="tApp"></table></div></div>
        <div class="panel"><h3 data-i18n="byModel"><span class="dot"></span>By Model</h3><div class="table-scroll"><table id="tModel"></table></div></div>
      </div>
      <div class="panel"><h3 data-i18n="recentMsgs"><span class="dot"></span>Recent Messages</h3><div class="table-scroll"><table id="tRecent"></table></div></div>
    </div>

    <!-- Daily -->
    <div class="section" id="s-daily">
      <div class="date-bar">
        <label data-i18n="range">Range:</label>
        <input type="date" id="dStart">
        <label>to</label>
        <input type="date" id="dEnd">
        <button class="btn btn-primary btn-sm" data-i18n="apply" onclick="applyDaily()">Apply</button>
        <div class="preset">
          <button class="btn btn-ghost btn-sm" onclick="presetDaily(1)">1D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetDaily(7)">7D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetDaily(14)">14D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetDaily(30)">30D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetDaily(0)">All</button>
        </div>
      </div>
      <div class="cards" id="dailyCards"></div>
      <div class="grid2">
        <div class="panel"><h3 data-i18n="tokenUsage"><span class="dot"></span>Token Usage</h3><div class="chart-box"><canvas id="cDailyToken"></canvas></div></div>
        <div class="panel"><h3 data-i18n="dailyCost"><span class="dot"></span>Daily Cost</h3><div class="chart-box"><canvas id="cDailyCost"></canvas></div></div>
      </div>
      <div class="panel"><h3 data-i18n="dailyDetail"><span class="dot"></span>Daily Detail</h3><div class="table-scroll"><table id="tDaily"></table></div></div>
    </div>

    <!-- Projects -->
    <div class="section" id="s-projects">
      <div class="panel"><h3 data-i18n="perProject"><span class="dot"></span>Per-Project Usage</h3><div class="table-scroll"><table id="tProject"></table></div></div>
    </div>

    <!-- Sessions -->
    <div class="section" id="s-sessions">
      <div class="panel"><h3 data-i18n="topSessions"><span class="dot"></span>Top Sessions by Usage</h3><div class="table-scroll"><table id="tSession"></table></div></div>
    </div>

    <!-- Compare -->
    <div class="section" id="s-compare">
      <div class="date-bar">
        <label data-i18n="periodA">Period A:</label>
        <input type="date" id="c1s"><label>to</label><input type="date" id="c1e">
        <label style="margin-left:12px" data-i18n="periodB">Period B:</label>
        <input type="date" id="c2s"><label>to</label><input type="date" id="c2e">
        <button class="btn btn-primary btn-sm" data-i18n="compareBtn" onclick="doCompare()">Compare</button>
        <div class="preset">
          <button class="btn btn-ghost btn-sm" data-i18n="tdYd" onclick="presetCompare('day')">Today/Yesterday</button>
          <button class="btn btn-ghost btn-sm" data-i18n="twLw" onclick="presetCompare('week')">This/Last Week</button>
          <button class="btn btn-ghost btn-sm" data-i18n="tmLm" onclick="presetCompare('month')">This/Last Month</button>
        </div>
      </div>
      <div class="diff" id="diffCards"></div>
      <div class="grid2">
        <div class="panel"><h3><span class="dot" style="background:var(--cyan)"></span><span data-i18n="periodA">Period A</span></h3><div class="table-scroll"><table id="tC1"></table></div></div>
        <div class="panel"><h3><span class="dot" style="background:var(--amber)"></span><span data-i18n="periodB">Period B</span></h3><div class="table-scroll"><table id="tC2"></table></div></div>
      </div>
      <div class="panel"><h3 data-i18n="compChart"><span class="dot"></span>Comparison Chart</h3><div class="chart-box"><canvas id="cCompare"></canvas></div></div>
    </div>

    <!-- Settings -->
    <div class="section" id="s-settings">
      <div class="panel">
        <h3 data-i18n="settings"><span class="dot"></span>Settings</h3>
        <div style="display:flex;flex-direction:column;gap:20px;padding:8px 0">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="language">Language</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="langDesc">Interface language</div></div>
            <div class="toggle-group" id="langToggle"><button onclick="setLang('en')">EN</button><button class="active" onclick="setLang('zh')">ZH</button></div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="theme">Theme</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="themeDesc">Light or dark appearance</div></div>
            <div class="toggle-group" id="themeToggle"><button onclick="setTheme('dark')">Dark</button><button class="active" onclick="setTheme('light')">Light</button></div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="numFmt">Number Format</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="numFmtDesc">万/亿 or K/M/B</div></div>
            <div class="toggle-group" id="numFmtToggle"><button onclick="setNumFmt('en')">K/M</button><button class="active" onclick="setNumFmt('cn')">万/亿</button></div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="autoRefresh">Auto Refresh</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="autoRefreshDesc">Automatically reload data from disk on a timer</div></div>
            <div class="toggle-group" id="autoRefreshToggle"><button onclick="setAutoRefresh('off')">Off</button><button class="active" onclick="setAutoRefresh('on')">On</button></div>
          </div>
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="refreshInterval">Refresh Interval</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="refreshIntervalDesc">Time interval between auto-refreshes (seconds)</div></div>
            <div class="toggle-group" id="intervalToggle"><button onclick="setRefreshInterval(15)">15s</button><button class="active" onclick="setRefreshInterval(30)">30s</button><button onclick="setRefreshInterval(60)">60s</button><button onclick="setRefreshInterval(120)">120s</button></div>
          </div>
        </div>
      </div>
      <div class="panel">
        <h3 data-i18n="pricingEditor"><span class="dot"></span>Pricing Editor</h3>
        <div style="font-size:11px;color:var(--muted);margin-bottom:12px" data-i18n="pricingDesc">USD per 1M tokens. Edit and save.</div>
        <div class="table-scroll"><table id="tPricing"></table></div>
        <div style="display:flex;gap:8px;margin-top:12px;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm" onclick="savePricing()" data-i18n="savePricing">Save Pricing</button>
          <button class="btn btn-ghost btn-sm" onclick="addModel()" data-i18n="addModel">+ Add Model</button>
          <button class="btn btn-ghost btn-sm" onclick="resetPricing()" data-i18n="resetPricing">Reset to Defaults</button>
        </div>
      </div>
    </div>

  </div>
</div>

<script>
const $ = s => document.querySelector(s);
const $$ = s => document.querySelectorAll(s);
let charts = {};
let allDates = [];
let numFmt='cn';

// ── Formatting ──
function fmt(n){
  if(numFmt==='cn'){
    if(n>=1e8)return(n/1e8).toFixed(2)+'亿';
    if(n>=1e4)return(n/1e4).toFixed(1)+'万';
    return String(n);
  }
  if(n>=1e9)return(n/1e9).toFixed(1)+'B';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return String(n);
}
function fmtC(c){if(c===0)return'$0.00';if(c<.01)return'$'+c.toFixed(4);return'$'+c.toFixed(2)}
function pct(a,b){if(!b)return'-';return((a-b)/b*100).toFixed(1)+'%'}
function pctClass(a,b){if(!b)return'same';return a>b?'up':'down'}

// ── i18n ──
let lang=(function(){try{return localStorage.getItem('tb-lang')||'zh'}catch(e){return 'zh'}})();
numFmt=(function(){try{return localStorage.getItem('tb-numfmt')||(lang==='zh'?'cn':'en')}catch(e){return 'cn'}})();
const T={
  en:{
    overview:'Overview',daily:'Usage Report',projects:'Projects',sessions:'Sessions',compare:'Compare',
    appLabel:'Claude Code + Codex',records:'records',reload:'Reload',loading:'Loading...',
    loaded:'Loaded',loadedSuff:'records',
    sRecords:'Records',sInput:'Input Tokens',sOutput:'Output Tokens',sCache:'Cache Read',sCost:'Est. Cost',sCacheRate:'Cache Rate',
    sTotalMsg:'Total messages',sSent:'Sent to models',sGen:'Generated',sPrompt:'Prompt cache',sUsd:'USD',sCacheRateSub:'cache read / total input',
    sTotalInput:'Total Input',sNonCacheInput:'Non-cached Input',
    dailyTrend:'Daily Trend',costDist:'Cost Distribution',byApp:'By Application',byModel:'By Model',
    recentMsgs:'Recent Messages',thTime:'Time',thSummary:'Summary',hourlyDetail:'Hourly Detail',thHour:'Hour',
    tokenUsage:'Token Usage',dailyCost:'Daily Cost',dailyDetail:'Daily Detail',
    perProject:'Per-Project Usage',topSessions:'Top Sessions by Usage',
    range:'Range:',apply:'Apply',periodA:'Period A:',periodB:'Period B:',
    compareBtn:'Compare',tdYd:'Today/Yesterday',twLw:'This/Last Week',tmLm:'This/Last Month',compChart:'Comparison Chart',
    vsPrev:'vs prev',up:'up',down:'down',same:'same',
    thApp:'App',thModel:'Model',thDate:'Date',thSess:'Sess',thMsgs:'Msgs',
    thInput:'Input',thOutput:'Output',thCache:'Cache',thTotal:'Total',thCost:'Cost',
    thProject:'Project',thSession:'Session',thTotalInput:'Total Input',thNonCacheInput:'Non-cached',
    input:'Input',output:'Output',cacheRead:'Cache Read',cacheCreate:'Cache Create',cost:'Cost($)',costLabel:'Cost',
    recordsLabel:'Records',
    periodALabel:'Period A',periodBLabel:'Period B',
    settings:'Settings',language:'Language',theme:'Theme',
    langDesc:'Interface language',themeDesc:'Light or dark appearance',
    numFmt:'Number Format',numFmtDesc:'万/亿 or K/M/B',
    pricingEditor:'Pricing Editor',pricingDesc:'USD per 1M tokens. Edit and save.',
    savePricing:'Save Pricing',resetPricing:'Reset to Defaults',addModel:'+ Add Model',deleteModel:'Delete',
    noCostData:'No cost data available',singleModel:'Only one model - no distribution chart needed',
    topSessions:'Top Sessions',
    autoRefresh:'Auto Refresh',autoRefreshDesc:'Automatically reload data from disk on a timer',
    refreshInterval:'Refresh Interval',refreshIntervalDesc:'Time interval between auto-refreshes (seconds)',
  },
  zh:{
    overview:'概览',daily:'使用报告',projects:'项目',sessions:'会话',compare:'对比',
    appLabel:'Claude Code + Codex',records:'条记录',reload:'刷新',loading:'加载中...',
    loaded:'已加载',loadedSuff:'条记录',
    sRecords:'记录数',sInput:'输入Token',sOutput:'输出Token',sCache:'缓存读取',sCost:'预估费用',sCacheRate:'缓存率',
    sTotalMsg:'总消息数',sSent:'发送至模型',sGen:'模型生成',sPrompt:'提示缓存',sUsd:'美元',sCacheRateSub:'缓存读取 / 总输入',
    sTotalInput:'总输入',sNonCacheInput:'非缓存输入',
    dailyTrend:'每日趋势',costDist:'费用分布',byApp:'按应用',byModel:'按模型',
    recentMsgs:'最近消息',thTime:'时间',thSummary:'摘要',hourlyDetail:'逐时详情',thHour:'时段',
    tokenUsage:'Token用量',dailyCost:'每日费用',dailyDetail:'每日详情',
    perProject:'项目用量',topSessions:'热门会话',
    range:'日期范围:',apply:'应用',periodA:'时段A:',periodB:'时段B:',
    compareBtn:'对比',tdYd:'今天/昨天',twLw:'本周/上周',tmLm:'本月/上月',compChart:'对比图表',
    vsPrev:'对比上期',up:'增长',down:'下降',same:'持平',
    thApp:'应用',thModel:'模型',thDate:'日期',thSess:'会话',thMsgs:'消息',
    thInput:'输入',thOutput:'输出',thCache:'缓存',thTotal:'总计',thCost:'费用',
    thProject:'项目',thSession:'会话',thTotalInput:'总输入',thNonCacheInput:'非缓存',
    input:'输入',output:'输出',cacheRead:'缓存读取',cacheCreate:'缓存创建',cost:'费用($)',costLabel:'费用',
    recordsLabel:'记录数',
    periodALabel:'时段A',periodBLabel:'时段B',
    settings:'设置',language:'语言',theme:'主题',
    langDesc:'界面语言',themeDesc:'浅色或深色外观',
    numFmt:'数字格式',numFmtDesc:'万/亿 或 K/M/B',
    pricingEditor:'模型计费',pricingDesc:'每百万Token美元价格，修改后点击保存。',
    savePricing:'保存计费',resetPricing:'恢复默认',addModel:'+ 添加模型',deleteModel:'删除',
    noCostData:'暂无费用数据',singleModel:'仅一个模型，无需分布图',
    topSessions:'会话排行',
    autoRefresh:'自动刷新',autoRefreshDesc:'定时自动从磁盘重新加载数据',
    refreshInterval:'刷新间隔',refreshIntervalDesc:'自动刷新的时间间隔（秒）',
  }
};
function t(k){return(T[lang]&&T[lang][k])||T.en[k]||k}

// ── Settings ──
function lsGet(k,d){try{return localStorage.getItem(k)||d}catch(e){return d}}
function lsSet(k,v){try{localStorage.setItem(k,v)}catch(e){}}
function setLang(l){lang=l;lsSet('tb-lang',l);numFmt=(l==='zh'?'cn':'en');lsSet('tb-numfmt',numFmt);syncSettingsUI();applyLang()}
function setTheme(th){document.documentElement.setAttribute('data-theme',th);lsSet('tb-theme',th);syncSettingsUI()}
function setNumFmt(f){numFmt=f;lsSet('tb-numfmt',f);syncSettingsUI();if(fullData){render(fullData);renderDaily(fullData)}}

// ── Auto-refresh ──
var autoRefreshEnabled=true;
var refreshIntervalSec=30;
var _autoRefreshTimer=null;
function setAutoRefresh(v){
  autoRefreshEnabled=v==='on';
  lsSet('tb-autorefresh',v);
  syncSettingsUI();
  startAutoRefresh();
}
function setRefreshInterval(sec){
  refreshIntervalSec=sec;
  lsSet('tb-refreshinterval',String(sec));
  syncSettingsUI();
  startAutoRefresh();
}
function startAutoRefresh(){
  if(_autoRefreshTimer){clearInterval(_autoRefreshTimer);_autoRefreshTimer=null}
  if(autoRefreshEnabled&&refreshIntervalSec>0){
    _autoRefreshTimer=setInterval(function(){
      pywebview.api.reload().then(function(r){
        var d=JSON.parse(r);fullData=d;
        render(d);renderDaily(d);
        if(d.daily.length){allDates=d.daily.map(function(x){return x.date})}
        $('#status').textContent=t('loaded')+' '+d.summary.total_records+' '+t('loadedSuff');
      });
    },refreshIntervalSec*1000);
  }
}
function syncSettingsUI(){
  var lt=$('#langToggle');if(lt)lt.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b.textContent===lang.toUpperCase())});
  var tt=$('#themeToggle');if(tt)tt.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b.textContent.toLowerCase()===document.documentElement.getAttribute('data-theme'))});
  var nt=$('#numFmtToggle');if(nt)nt.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b.textContent===(numFmt==='cn'?'万/亿':'K/M'))});
  var ar=$('#autoRefreshToggle');if(ar)ar.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b.textContent.toLowerCase()===(autoRefreshEnabled?'on':'off'))});
  var it=$('#intervalToggle');if(it)it.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b.textContent===refreshIntervalSec+'s')});
}
function initSettings(){
  var savedTheme=lsGet('tb-theme','light');
  document.documentElement.setAttribute('data-theme',savedTheme);
  numFmt=lsGet('tb-numfmt',lang==='zh'?'cn':'en');
  autoRefreshEnabled=lsGet('tb-autorefresh','on')==='on';
  refreshIntervalSec=parseInt(lsGet('tb-refreshinterval','30'),10)||30;
  syncSettingsUI();
  applyLang();
}
function applyLang(){
  document.documentElement.lang=lang==='zh'?'zh-CN':'en';
  $$('.nav-item').forEach(function(n){var key=n.dataset.s,icon=n.querySelector('.icon');if(key&&icon){n.innerHTML='';n.appendChild(icon);n.appendChild(document.createTextNode(t(key)))}});
  $$('[data-i18n]').forEach(function(el){
    var key=el.dataset.i18n;if(!key)return;
    if(el.querySelector('[data-i18n]'))return;
    var dot=el.querySelector('.dot');
    if(dot){el.innerHTML='';el.appendChild(dot);el.appendChild(document.createTextNode(t(key)))}
    else{el.textContent=t(key)}
  });
  var appL=$('#appLabel');if(appL)appL.textContent=t('appLabel');
  var rBtn=$('#reloadBtn');if(rBtn)rBtn.textContent=t('reload');
  if(fullData){render(fullData);renderDaily(fullData)}
}

// ── Chart helpers ──
function kill(k){if(charts[k]){charts[k].destroy();delete charts[k]}}
const _easeSpring='cubicBezier(.34,1.56,.64,1)';
function bar(k,labels,datasets,stacked=true,opts={}){
  kill(k);const ctx=document.getElementById(k);if(!ctx)return;
  charts[k]=new Chart(ctx,{type:'bar',data:{labels,datasets},options:{
    responsive:true,maintainAspectRatio:false,
    animation:{duration:800,easing:_easeSpring,delay:function(c){return c.dataIndex*40+c.datasetIndex*100}},
    plugins:{legend:{display:datasets.length>1,labels:{color:'#a1a1aa',font:{size:10},boxWidth:10,padding:8}}},
    scales:{x:{stacked,ticks:{color:'#71717a',font:{size:9},maxRotation:45},grid:{display:false}},
            y:{stacked,ticks:{color:'#71717a',font:{size:9},callback:v=>fmt(v)},grid:{color:'rgba(63,63,70,.3)'}},
            ...opts.scales||{}}}});
}
function line(k,labels,datasets,opts={}){
  kill(k);const ctx=document.getElementById(k);if(!ctx)return;
  charts[k]=new Chart(ctx,{type:'line',data:{labels,datasets},options:{
    responsive:true,maintainAspectRatio:false,
    animation:{duration:1000,easing:'easeOutQuart',delay:function(c){return c.dataIndex*30}},
    plugins:{legend:{display:datasets.length>1,labels:{color:'#a1a1aa',font:{size:10},boxWidth:10,padding:8}}},
    scales:{x:{ticks:{color:'#71717a',font:{size:9},maxRotation:45},grid:{display:false}},
            y:{ticks:{color:'#71717a',font:{size:9},callback:v=>fmt(v)},grid:{color:'rgba(63,63,70,.3)'}},
            ...opts.scales||{}},
    elements:{point:{radius:2,hoverRadius:5,hoverBorderWidth:2,hoverBackgroundColor:'#fff'},line:{tension:.3,borderWidth:2}}}});
}
function doughnut(k,labels,data){
  kill(k);const ctx=document.getElementById(k);if(!ctx)return;
  const bg=['#818cf8','#22d3ee','#4ade80','#fbbf24','#f87171','#c084fc','#f472b6','#60a5fa'];
  setTimeout(function(){
    if(!ctx.parentElement)return;
    charts[k]=new Chart(ctx,{type:'doughnut',data:{labels,datasets:[{data,backgroundColor:bg,borderWidth:0}]},
      options:{responsive:true,maintainAspectRatio:false,cutout:'65%',
        animation:{animateRotate:true,animateScale:true,duration:1200,easing:'easeOutQuart'},
        plugins:{legend:{position:'right',labels:{color:'#a1a1aa',font:{size:10},boxWidth:8,padding:6}}}}});
  },80);
}

// ── Table helpers ──
function mkT(el,hdrs,rows){
  let h='<thead><tr>'+hdrs.map(h=>'<th>'+h+'</th>').join('')+'</tr></thead><tbody>';
  rows.forEach(r=>{h+='<tr>'+r.map((c,i)=>{
    const isNum=typeof c==='number';
    const cls=isNum?' class="num"':'';
    return'<td'+cls+'>'+c+'</td>'}).join('')+'</tr>'});h+='</tbody>';
  el.innerHTML=h;
}
function badge(a){return`<span class="badge b-${a}">${a.toUpperCase()}</span>`}

// ── Render ──
function render(d){
  const s=d.summary;
  const totalInput=s.total_input_full||s.total_input;
  const cacheRate=s.cache_rate||0;
  $('#summaryCards').innerHTML=[
    {l:t('sRecords'),v:fmt(s.total_records),c:'i1',s:t('sTotalMsg'),cls:'c-indigo'},
    {l:t('sInput'),v:fmt(totalInput),c:'i2',s:t('sSent'),cls:'c-cyan'},
    {l:t('sOutput'),v:fmt(s.total_output),c:'i3',s:t('sGen'),cls:'c-green'},
    {l:t('sCache'),v:fmt(s.total_cache_read),c:'i4',s:t('sPrompt'),cls:'c-purple'},
    {l:t('sCost'),v:fmtC(s.total_cost),c:'i5',s:t('sUsd'),cls:'c-red'},
    {l:t('sCacheRate'),v:cacheRate.toFixed(1)+'%',c:'i2',s:t('sCacheRateSub'),cls:'c-cyan'},
  ].map(c=>`<div class="card ${c.c}"><div class="label">${c.l}</div><div class="value ${c.cls}">${c.v}</div><div class="sub">${c.s}</div></div>`).join('');
  $('#recordCount').textContent=s.total_records+' '+t('records');

  // Overview charts
  const dates=d.daily.map(x=>x.date.slice(5));
  bar('cTrend',dates,[
    {label:t('input'),data:d.daily.map(x=>x.input),backgroundColor:'#60a5fa'},
    {label:t('output'),data:d.daily.map(x=>x.output),backgroundColor:'#4ade80'},
    {label:t('cacheRead'),data:d.daily.map(x=>x.cache_read),backgroundColor:'#c084fc'},
    {label:t('cacheCreate'),data:d.daily.map(x=>x.cache_create||0),backgroundColor:'#f472b6'},
  ]);
  const allModels=Object.entries(d.by_model);
  const costModels=allModels.filter(m=>m[1].cost>0);
  if(costModels.length>1){doughnut('cCost',costModels.map(m=>m[0]),costModels.map(m=>m[1].cost))}
  else{kill('cCost');const ctx=document.getElementById('cCost');if(ctx){ctx.parentElement.innerHTML='<div style="display:flex;align-items:center;justify-content:center;height:100%;color:var(--muted);font-size:12px">'+(costModels.length===0?t('noCostData'):t('singleModel'))+'</div>'}}

  // App table
  const appR=Object.entries(d.by_app).sort((a,b)=>b[1].input-a[1].input).map(([k,v])=>[
    badge(k),fmt(v.count),fmt(v.input),fmt(v.output),fmt(v.cache_read),fmt(v.input+v.output+v.cache_read),fmtC(v.cost)
  ]);
  mkT($('#tApp'),[t('thApp'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],appR);

  // Model table
  const modR=allModels.map(([k,v])=>[k,fmt(v.count),fmt(v.input),fmt(v.output),fmt(v.cache_read),fmt(v.input+v.output+v.cache_read),fmtC(v.cost)]);
  mkT($('#tModel'),[t('thModel'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],modR);

  // Recent messages
  if(d.recent&&d.recent.length){
    const recR=d.recent.map(r=>{
      const ts=r.timestamp?r.timestamp.replace('T',' ').slice(0,16):'';
      const sm=r.summary?r.summary.replace(/^[\s﻿\xA0]+|[\s﻿\xA0]+$/g,''):'';
      const label=sm?sm.substring(0,100)+(sm.length>100?'...':''):r.session.substring(0,12)+'...';
      const ti=(r.input||0)+(r.cache_read||0)+(r.cache_create||0);
      return[ts,badge(r.app),r.model,fmt(ti),fmt(r.input),fmt(r.output),fmtC(r.cost),label]
    });
    mkT($('#tRecent'),[t('thTime'),t('thApp'),t('thModel'),t('thTotalInput'),t('thNonCacheInput'),t('thOutput'),t('thCost'),t('thSummary')],recR);
  }

  // Projects
  const projR=d.projects.map(p=>[p.name,fmt(p.sessions),fmt(p.count),fmt(p.input),fmt(p.output),fmt(p.cache_read),fmt(p.total),fmtC(p.cost)]);
  mkT($('#tProject'),[t('thProject'),t('thSess'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],projR);

  // Sessions
  const sessR=d.sessions.map(s=>{
    const sm=s.summary?s.summary.replace(/^[\s﻿\xA0]+|[\s﻿\xA0]+$/g,''):'';
    const label=sm?sm.substring(0,80)+(sm.length>80?'...':''):s.id.substring(0,16)+'...';
    return[fmt(s.total),badge(s.app),s.model,s.date,fmt(s.count),fmt(s.input),fmt(s.output),fmtC(s.cost),label]
  });
  mkT($('#tSession'),[t('thTotal'),t('thApp'),t('thModel'),t('thDate'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCost'),t('thSession')],sessR);
}

// ── Daily with filter ──
let fullData=null;
function renderDailyCards(s){
  const totalInput=s.total_input_full||s.total_input;
  const cacheRate=s.cache_rate||0;
  $('#dailyCards').innerHTML=[
    {l:t('sRecords'),v:fmt(s.total_records),c:'i1',cls:'c-indigo'},
    {l:t('sInput'),v:fmt(totalInput),c:'i2',cls:'c-cyan'},
    {l:t('sOutput'),v:fmt(s.total_output),c:'i3',cls:'c-green'},
    {l:t('sCache'),v:fmt(s.total_cache_read),c:'i4',cls:'c-purple'},
    {l:t('sCost'),v:fmtC(s.total_cost),c:'i5',cls:'c-red'},
    {l:t('sCacheRate'),v:cacheRate.toFixed(1)+'%',c:'i2',cls:'c-cyan'},
  ].map(c=>`<div class="card ${c.c}"><div class="label">${c.l}</div><div class="value ${c.cls}">${c.v}</div></div>`).join('');
}
function renderDaily(data){
  if(!data)return;
  fullData=data;
  if(data.summary)renderDailyCards(data.summary);
  const dd=data.daily;
  const dates=dd.map(x=>x.date.slice(5));
  line('cDailyToken',dates,[
    {label:t('input'),data:dd.map(x=>x.input),borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true},
    {label:t('output'),data:dd.map(x=>x.output),borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,.1)',fill:true},
    {label:t('cacheRead'),data:dd.map(x=>x.cache_read),borderColor:'#c084fc',backgroundColor:'rgba(192,132,252,.1)',fill:true},
  ]);
  line('cDailyCost',dates,[
    {label:t('costLabel'),data:dd.map(x=>x.cost),borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.15)',fill:true},
  ]);
  const rows=dd.map(d=>[d.date,fmt(d.count),fmt(d.input),fmt(d.output),fmt(d.cache_read),fmt(d.total),fmtC(d.cost)]);
  mkT($('#tDaily'),[t('thDate'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],rows);
  var detail=$$('#s-daily .panel h3[data-i18n]');
  detail.forEach(function(el){if(el.dataset.i18n==='dailyDetail'){el.lastChild.textContent=t('dailyDetail')}});
}

function renderHourly(data){
  if(!data||!data.hourly)return;
  const hh=data.hourly;
  var hs={total_records:0,total_input:0,total_output:0,total_cache_read:0,total_cache_create:0,total_cost:0,total_input_full:0,cache_rate:0};
  hh.forEach(function(h){
    hs.total_records+=h.count;hs.total_input+=h.input;hs.total_output+=h.output;
    hs.total_cache_read+=h.cache_read;hs.total_cache_create+=h.cache_create;hs.total_cost+=h.cost;
  });
  hs.total_input_full=hs.total_input+hs.total_cache_read+hs.total_cache_create;
  var denom=hs.total_input+hs.total_cache_read+hs.total_cache_create;
  hs.cache_rate=denom?Math.round(hs.total_cache_read/denom*1000)/10:0;
  renderDailyCards(hs);
  const hours=hh.map(x=>x.hour);
  line('cDailyToken',hours,[
    {label:t('input'),data:hh.map(x=>x.input),borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true},
    {label:t('output'),data:hh.map(x=>x.output),borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,.1)',fill:true},
    {label:t('cacheRead'),data:hh.map(x=>x.cache_read),borderColor:'#c084fc',backgroundColor:'rgba(192,132,252,.1)',fill:true},
  ]);
  line('cDailyCost',hours,[
    {label:t('costLabel'),data:hh.map(x=>x.cost),borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.15)',fill:true},
  ]);
  var rows=hh.map(h=>[h.hour,fmt(h.count),fmt(h.input),fmt(h.output),fmt(h.cache_read),fmt(h.total),fmtC(h.cost)]);
  mkT($('#tDaily'),[t('thHour'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],rows);
  var detail=$$('#s-daily .panel h3[data-i18n]');
  detail.forEach(function(el){if(el.dataset.i18n==='dailyDetail'){el.lastChild.textContent=lang==='zh'?'逐时详情':'Hourly Detail'}});
}

function applyDaily(){
  const s=$('#dStart').value,e=$('#dEnd').value;
  if(!s||!e)return;
  if(s===e){pywebview.api.get_hourly(s).then(r=>{renderHourly(JSON.parse(r))})}
  else{pywebview.api.get_filtered(s,e).then(r=>{renderDaily(JSON.parse(r))})}
}
function presetDaily(n){
  if(!allDates.length)return;
  if(n===0){renderDaily(fullData);$('#dStart').value=allDates[0];$('#dEnd').value=allDates[allDates.length-1];return}
  const end=allDates[allDates.length-1];
  if(n===1){
    $('#dStart').value=end;$('#dEnd').value=end;
    pywebview.api.get_hourly(end).then(r=>{renderHourly(JSON.parse(r))});
    return;
  }
  const si=Math.max(0,allDates.length-n);
  const start=allDates[si];
  $('#dStart').value=start;$('#dEnd').value=end;
  pywebview.api.get_filtered(start,end).then(r=>{renderDaily(JSON.parse(r))});
}

// ── Compare ──
function doCompare(){
  const s1=$('#c1s').value,e1=$('#c1e').value,s2=$('#c2s').value,e2=$('#c2e').value;
  if(!s1||!e1||!s2||!e2)return;
  pywebview.api.compare(s1,e1,s2,e2).then(r=>{
    const d=JSON.parse(r);
    const p1=d.period1.summary,p2=d.period2.summary;
    const tif1=p1.total_input_full||p1.total_input,tif2=p2.total_input_full||p2.total_input;
    const metrics=[[t('sTotalInput'),tif1,tif2],[t('sNonCacheInput'),p1.total_input,p2.total_input],
      [t('output'),p1.total_output,p2.total_output],[t('cacheRead'),p1.total_cache_read,p2.total_cache_read],
      [t('costLabel'),p1.total_cost,p2.total_cost],[t('recordsLabel'),p1.total_records,p2.total_records]];
    $('#diffCards').innerHTML=metrics.map(([k,a,b])=>{
      const diff=a-b;const cls=diff>0?'up':diff<0?'down':'same';const sign=diff>0?'+':'';
      const isCost=k===t('costLabel');const v=isCost?fmtC(Math.abs(diff)):typeof a==='number'&&a>1000?fmt(diff):diff;
      return`<div class="item"><div class="k">${k}</div><div class="v">${isCost?fmtC(a):fmt(a)}</div><div class="change ${cls}">${sign}${v} ${t('vsPrev')}</div></div>`;
    }).join('');

    // Tables
    const mkRows=p=>{const d=p.by_app;return Object.entries(d).sort((a,b)=>b[1].input-a[1].input).map(([k,v])=>[badge(k),fmt(v.count),fmt(v.input),fmt(v.output),fmt(v.cache_read),fmtC(v.cost)])};
    mkT($('#tC1'),[t('thApp'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thCost')],mkRows(d.period1));
    mkT($('#tC2'),[t('thApp'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thCost')],mkRows(d.period2));

    // Compare chart
    const labels=[t('sTotalInput'),t('sNonCacheInput'),t('output'),t('cacheRead'),t('cost')];
    const v1=[tif1,p1.total_input,p1.total_output,p1.total_cache_read,p1.total_cost];
    const v2=[tif2,p2.total_input,p2.total_output,p2.total_cache_read,p2.total_cost];
    bar('cCompare',labels,[
      {label:`A: ${s1} ~ ${e1}`,data:v1,backgroundColor:'rgba(34,211,238,.7)'},
      {label:`B: ${s2} ~ ${e2}`,data:v2,backgroundColor:'rgba(251,191,36,.7)'},
    ],false,{scales:{y:{ticks:{color:'#71717a',callback:v=>fmt(v)},grid:{color:'rgba(63,63,70,.3)'}}}});
  });
}
function presetCompare(mode){
  if(!allDates.length)return;
  const last=allDates[allDates.length-1];
  const d=new Date(last);
  if(mode==='day'){
    const yesterday=new Date(d);yesterday.setDate(d.getDate()-1);
    $('#c1s').value=fmtD(d);$('#c1e').value=fmtD(d);
    $('#c2s').value=fmtD(yesterday);$('#c2e').value=fmtD(yesterday);
  }else if(mode==='week'){
    const dow=d.getDay()||7;
    const thisMon=new Date(d);thisMon.setDate(d.getDate()-dow+1);
    const lastMon=new Date(thisMon);lastMon.setDate(thisMon.getDate()-7);
    const lastSun=new Date(thisMon);lastSun.setDate(thisMon.getDate()-1);
    $('#c1s').value=fmtD(thisMon);$('#c1e').value=fmtD(new Date(d));
    $('#c2s').value=fmtD(lastMon);$('#c2e').value=fmtD(lastSun);
  }else{
    const this1=new Date(d.getFullYear(),d.getMonth(),1);
    const last1=new Date(d.getFullYear(),d.getMonth()-1,1);
    const lastEnd=new Date(d.getFullYear(),d.getMonth(),0);
    $('#c1s').value=fmtD(this1);$('#c1e').value=fmtD(d);
    $('#c2s').value=fmtD(last1);$('#c2e').value=fmtD(lastEnd);
  }
  doCompare();
}
function fmtD(d){return d.toISOString().slice(0,10)}

// ── Nav ──
$$('.nav-item').forEach(n=>n.addEventListener('click',function(){
  $$('.nav-item').forEach(function(x){x.classList.remove('active')});
  $$('.section').forEach(function(x){x.classList.remove('active')});
  n.classList.add('active');
  var section=$('#s-'+n.dataset.s);
  section.classList.add('active');
  if(n.dataset.s==='settings')loadPricingEditor();
  if(n.dataset.s==='compare')presetCompare('day');
}));

// ── Pricing editor ──
var pricingData=null;
var _iptStyle='width:70px;background:var(--surface2);border:1px solid rgba(127,127,127,.15);color:var(--text);padding:4px 6px;border-radius:4px;font-size:11px;font-family:inherit';
var _nameStyle='width:160px;background:var(--surface2);border:1px solid rgba(127,127,127,.15);color:var(--text);padding:4px 6px;border-radius:4px;font-size:11px;font-family:inherit';
var _delStyle='background:none;border:none;color:var(--red);cursor:pointer;font-size:12px;padding:4px 8px;font-family:inherit';
function _ipt(model,field,val){return '<input data-model="'+model+'" data-field="'+field+'" type="number" step="0.01" value="'+val+'" style="'+_iptStyle+'">'}
function _nameIpt(model){return '<input data-model="'+model+'" data-field="__name__" type="text" value="'+model+'" placeholder="model-name" style="'+_nameStyle+'">'}
function _delBtn(){return '<button onclick="deleteModel(this)" style="'+_delStyle+'" title="'+t('deleteModel')+'">&times;</button>'}
function loadPricingEditor(){
  pywebview.api.get_pricing_data().then(function(r){
    pricingData=JSON.parse(r);
    var all={};
    Object.keys(pricingData.models).forEach(function(k){all[k]=pricingData.models[k]});
    Object.keys(pricingData.custom).forEach(function(k){all[k]=pricingData.custom[k]});
    var rows=Object.entries(all).map(function(e){
      var m=e[0],p=e[1];
      return[_nameIpt(m),_ipt(m,'input',p.input),_ipt(m,'output',p.output),_ipt(m,'cache_read',p.cache_read),_delBtn()]
    });
    mkT($('#tPricing'),[t('thModel'),t('thInput')+' $/1M',t('thOutput')+' $/1M',t('thCache')+' $/1M',''],rows);
  });
}
function addModel(){
  var tbody=$('#tPricing').querySelector('tbody');
  if(!tbody)return;
  var tr=document.createElement('tr');
  tr.innerHTML='<td>'+_nameIpt('')+'</td><td>'+_ipt('','input','0')+'</td><td>'+_ipt('','output','0')+'</td><td>'+_ipt('','cache_read','0')+'</td><td>'+_delBtn()+'</td>';
  tbody.appendChild(tr);
  tr.querySelector('input[data-field="__name__"]').focus();
}
function deleteModel(btn){
  var tr=btn.closest('tr');
  if(tr)tr.remove();
}
function savePricing(){
  var rows=$('#tPricing').querySelectorAll('tbody tr');
  var models={};
  rows.forEach(function(row){
    var nameInput=row.querySelector('input[data-field="__name__"]');
    var model=nameInput?nameInput.value.trim():'';
    if(!model)return;
    var inp=parseFloat(row.querySelector('input[data-field="input"]').value)||0;
    var out=parseFloat(row.querySelector('input[data-field="output"]').value)||0;
    var cr=parseFloat(row.querySelector('input[data-field="cache_read"]').value)||0;
    models[model]={input:inp,output:out,cache_read:cr};
  });
  pywebview.api.save_all_pricing(JSON.stringify(models)).then(function(){
    pywebview.api.reload().then(function(r){var d=JSON.parse(r);fullData=d;render(d);renderDaily(d)});
  });
}
function resetPricing(){
  pywebview.api.reset_pricing().then(function(){loadPricingEditor();pywebview.api.reload().then(function(r){var d=JSON.parse(r);fullData=d;render(d);renderDaily(d)})});
}

// ── Load ──
function reload(){
  $('#status').textContent=t('loading');
  pywebview.api.reload().then(function(r){
    var d=JSON.parse(r);fullData=d;
    render(d);renderDaily(d);
    if(d.daily.length){$('#dStart').value=d.daily[0].date;$('#dEnd').value=d.daily[d.daily.length-1].date}
    allDates=d.daily.map(function(x){return x.date});
    if(allDates.length>=14){
      var mid=Math.floor(allDates.length/2);
      $('#c1s').value=allDates[mid];$('#c1e').value=allDates[allDates.length-1];
      $('#c2s').value=allDates[0];$('#c2e').value=allDates[mid-1];
    }
    $('#status').textContent=t('loaded')+' '+d.summary.total_records+' '+t('loadedSuff');
    startAutoRefresh();
    var overlay=$('#loadingOverlay');
    if(overlay){overlay.classList.add('fade-out');overlay.addEventListener('transitionend',function(){overlay.remove()})}
  });
}

window.addEventListener('pywebviewready',function(){initSettings();reload()});
</script>
</body>
</html>"""


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    api = Api()
    window = webview.create_window(
        "TOKENBANK",
        html=HTML,
        js_api=api,
        width=1280,
        height=800,
        min_size=(960, 600),
        background_color="#ffffff",
        on_top=False,
    )
    _app_window = window
    window.events.closing += on_closing

    tray_thread = threading.Thread(target=setup_tray, daemon=True)
    tray_thread.start()

    dialog_thread = threading.Thread(target=dialog_worker, daemon=True)
    dialog_thread.start()

    webview.start(debug=False)
