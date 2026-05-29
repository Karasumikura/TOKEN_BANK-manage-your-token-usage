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
        non_cached = max(0, max_input - max_cached)
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
        summary = re.sub(r'^\s+|\s+$', '', task_text)[:160]
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
_saved_window_size = None
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

    def get_sessions_filtered(self, start_date, end_date):
        records = [r for r in self.load_data() if start_date <= r["date"] <= end_date]
        proc = self._process(records)
        return json.dumps({"sessions": proc["sessions"], "summary": proc["summary"]})

    def get_hourly_compare(self, date1, date2):
        return json.dumps({
            "day1": json.loads(self.get_hourly(date1)),
            "day2": json.loads(self.get_hourly(date2)),
        })

    def get_all_dates(self):
        dates = sorted(set(r["date"] for r in self.load_data() if r["date"]))
        return json.dumps(dates)

    def get_hourly(self, date_str):
        records = [r for r in self.load_data() if r.get("date") == date_str and r.get("timestamp")]
        by_hour = defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0})
        for r in records:
            ts = r["timestamp"]
            hour = ts[11:13] if len(ts) >= 13 else "00"
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

    def get_session_detail(self, session_id):
        messages = []
        for jsonl_path in CLAUDE_PROJECTS.rglob("*.jsonl"):
            if "subagents" in str(jsonl_path):
                continue
            if jsonl_path.stem == session_id:
                try:
                    with open(jsonl_path, encoding="utf-8") as f:
                        for line in f:
                            try:
                                obj = json.loads(line.strip())
                                tp = obj.get("type", "")
                                ts = obj.get("timestamp", "")
                                if tp == "assistant":
                                    usage = obj.get("message", {}).get("usage", {})
                                    model = obj.get("message", {}).get("model", "")
                                    inp = usage.get("input_tokens", 0)
                                    out = usage.get("output_tokens", 0)
                                    cr = usage.get("cache_read_input_tokens", 0)
                                    messages.append({"type": "assistant", "ts": ts, "model": model,
                                                     "input": inp, "output": out, "cache_read": cr,
                                                     "cost": calc_cost(model, inp, out, cr)})
                                elif tp in ("human", "user"):
                                    content = obj.get("message", {}).get("content", "")
                                    if isinstance(content, list):
                                        content = " ".join(str(c.get("text", "")) if isinstance(c, dict) else str(c) for c in content)
                                    messages.append({"type": "user", "ts": ts, "text": str(content)[:300]})
                                elif tp == "system":
                                    messages.append({"type": "system", "ts": ts, "text": obj.get("content", "")[:200]})
                            except json.JSONDecodeError:
                                continue
                except (UnicodeDecodeError, OSError):
                    pass
                break
        if not messages:
            for jsonl_path in CODEX_SESSIONS.rglob("*.jsonl"):
                if jsonl_path.stem == session_id:
                    try:
                        with open(jsonl_path, encoding="utf-8") as f:
                            for line in f:
                                try:
                                    obj = json.loads(line.strip())
                                    if obj.get("type") == "event_msg":
                                        payload = obj.get("payload", {})
                                        mt = payload.get("type", "")
                                        ts = obj.get("timestamp", "")
                                        if mt == "user_message":
                                            messages.append({"type": "user", "ts": ts, "text": str(payload.get("message", ""))[:300]})
                                        elif mt == "token_count":
                                            info = payload.get("info", {})
                                            for field in ("total_token_usage", "last_token_usage"):
                                                u = info.get(field, {})
                                                if u.get("total_tokens", 0) > 0:
                                                    messages.append({"type": "token", "ts": ts,
                                                                     "input": u.get("input_tokens", 0),
                                                                     "output": u.get("output_tokens", 0),
                                                                     "cache_read": u.get("cached_input_tokens", 0)})
                                                    break
                                except json.JSONDecodeError:
                                    continue
                    except (UnicodeDecodeError, OSError):
                        pass
                    break
        return json.dumps({"id": session_id, "messages": messages})

    def get_heatmap(self, start_date="", end_date=""):
        records = self.load_data()
        if start_date and end_date:
            records = [r for r in records if r.get("date", "") and start_date <= r["date"] <= end_date]
        # Determine if short range (<=7 days) -> date x hour, else weekday x hour
        dates_set = set()
        for r in records:
            if r.get("date"):
                dates_set.add(r["date"])
        short_range = len(dates_set) <= 7 and start_date and end_date
        if short_range:
            sorted_dates = sorted(dates_set)
            date_idx = {d: i for i, d in enumerate(sorted_dates)}
            heatmap = [[0] * 24 for _ in range(len(sorted_dates))]
            labels = sorted_dates
        else:
            heatmap = [[0] * 24 for _ in range(7)]
            labels = []
        for r in records:
            ts = r.get("timestamp", "")
            if ts and len(ts) >= 13:
                try:
                    dt = datetime.fromisoformat(ts)
                    if short_range:
                        row = date_idx.get(r.get("date", ""))
                        if row is not None:
                            heatmap[row][dt.hour] += r["input"] + r["output"] + r["cache_read"]
                    else:
                        heatmap[dt.weekday()][dt.hour] += r["input"] + r["output"] + r["cache_read"]
                except (ValueError, IndexError):
                    pass
        return json.dumps({"heatmap": heatmap, "labels": labels})

    def get_model_daily(self, start_date="", end_date=""):
        records = self.load_data()
        if start_date and end_date:
            records = [r for r in records if r.get("date", "") and start_date <= r["date"] <= end_date]
        by_date_model = defaultdict(lambda: defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0, "cost": 0.0}))
        for r in records:
            if r["date"]:
                d = by_date_model[r["date"]][r["model"]]
                d["input"] += r["input"]
                d["output"] += r["output"]
                d["cache_read"] += r["cache_read"]
                d["cost"] += r["cost"]
        result = []
        for date in sorted(by_date_model.keys()):
            models = {m: {**dict(v), "total": v["input"] + v["output"] + v["cache_read"]} for m, v in by_date_model[date].items()}
            result.append({"date": date, "models": models})
        return json.dumps(result)

    def get_model_hourly(self, date_str):
        records = [r for r in self.load_data() if r.get("date") == date_str and r.get("timestamp")]
        by_hour_model = defaultdict(lambda: defaultdict(lambda: {"input": 0, "output": 0, "cache_read": 0}))
        for r in records:
            ts = r["timestamp"]
            hour = ts[11:13] if len(ts) >= 13 else "00"
            d = by_hour_model[hour][r["model"]]
            d["input"] += r["input"]
            d["output"] += r["output"]
            d["cache_read"] += r["cache_read"]
        result = []
        for hour in sorted(by_hour_model.keys()):
            models = {m: {"total": v["input"] + v["output"] + v["cache_read"]} for m, v in by_hour_model[hour].items()}
            result.append({"hour": hour + ":00", "models": models})
        return json.dumps(result)

    def toggle_mini(self, on):
        global _app_window, _saved_window_size
        if _app_window:
            if on == "1":
                _saved_window_size = (_app_window.width, _app_window.height)
                _app_window.min_size = (320, 240)
                _app_window.resize(420, 320)
            else:
                w, h = _saved_window_size if _saved_window_size else (1280, 800)
                _app_window.min_size = (960, 600)
                _app_window.resize(w, h)
        return json.dumps({"ok": True})

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
        _new = lambda: {"input": 0, "output": 0, "cache_read": 0, "cache_create": 0, "cost": 0.0, "count": 0}
        by_app = defaultdict(_new)
        by_model = defaultdict(_new)
        by_date = defaultdict(_new)
        by_proj = defaultdict(lambda: {**_new(), "sessions": set()})
        by_sess = defaultdict(lambda: {"app": "", "project": "", "model": "", "date": "", "summary": "", **_new()})
        for r in records:
            cc = r.get("cache_create", 0)
            cnt = r.get("count", 1)
            a = by_app[r["app"]]
            a["input"] += r["input"]; a["output"] += r["output"]; a["cache_read"] += r["cache_read"]; a["cache_create"] += cc; a["cost"] += r["cost"]; a["count"] += cnt
            m = by_model[r["model"]]
            m["input"] += r["input"]; m["output"] += r["output"]; m["cache_read"] += r["cache_read"]; m["cache_create"] += cc; m["cost"] += r["cost"]; m["count"] += cnt
            if r["date"]:
                d = by_date[r["date"]]
                d["input"] += r["input"]; d["output"] += r["output"]; d["cache_read"] += r["cache_read"]; d["cache_create"] += cc; d["cost"] += r["cost"]; d["count"] += cnt
            p = by_proj[r["project"]]
            p["input"] += r["input"]; p["output"] += r["output"]; p["cache_read"] += r["cache_read"]; p["cache_create"] += cc; p["cost"] += r["cost"]; p["count"] += cnt; p["sessions"].add(r["session"])
            s = by_sess[r["session"]]
            s["app"] = r["app"]; s["project"] = r["project"]; s["model"] = r["model"]; s["date"] = r["date"]
            if r.get("summary"): s["summary"] = r["summary"]
            s["input"] += r["input"]; s["output"] += r["output"]; s["cache_read"] += r["cache_read"]; s["cache_create"] += cc; s["cost"] += r["cost"]; s["count"] += cnt

        total_input_full = sum(a["input"] + a["cache_read"] + a["cache_create"] for a in by_app.values())
        total_cache_read = sum(a["cache_read"] for a in by_app.values())
        cache_rate = (total_cache_read / total_input_full * 100) if total_input_full else 0
        sorted_records = sorted(records, key=lambda r: r.get("timestamp", ""), reverse=True)
        seen = set()
        recent_deduped = []
        for r in sorted_records:
            key = (r["session"], r.get("timestamp", ""))
            if key not in seen:
                seen.add(key)
                recent_deduped.append(r)
            if len(recent_deduped) >= 10:
                break
        recent_out = [{"timestamp": r.get("timestamp", ""), "app": r["app"], "model": r["model"],
                        "session": r["session"], "summary": r.get("summary", ""),
                        "input": r["input"], "output": r["output"],
                        "cache_read": r["cache_read"], "cache_create": r.get("cache_create", 0),
                        "cost": r["cost"], "count": r.get("count", 1)} for r in recent_deduped]
        return {
            "summary": {
                "total_input": sum(a["input"] for a in by_app.values()),
                "total_input_full": total_input_full,
                "total_output": sum(a["output"] for a in by_app.values()),
                "total_cache_read": total_cache_read,
                "total_cache_create": sum(a["cache_create"] for a in by_app.values()),
                "total_cost": sum(a["cost"] for a in by_app.values()),
                "total_records": len(records),
                "cache_rate": round(cache_rate, 1),
            },
            "by_app": {k: {**dict(v), "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"]} for k, v in by_app.items()},
            "by_model": {k: {**dict(v), "total": v["input"] + v["output"] + v["cache_read"] + v["cache_create"]} for k, v in sorted(by_model.items(), key=lambda x: x[1]["input"] + x[1]["cache_read"] + x[1]["output"], reverse=True)},
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
:root{--glass-bg:rgba(24,24,27,.72);--glass-border:rgba(255,255,255,.08);--glass-blur:20px;--glass-sat:saturate(180%)}
:root[data-theme="light"]{--glass-bg:rgba(248,249,250,.72);--glass-border:rgba(255,255,255,.55)}
.app{background:linear-gradient(135deg,rgba(129,140,248,.06) 0%,rgba(34,211,238,.04) 50%,rgba(248,113,113,.03) 100%)}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:transparent}::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}

/* Easing tokens */
:root{
  --ease-spring:cubic-bezier(.34,1.56,.64,1);
  --ease-out-back:cubic-bezier(.34,1.2,.64,1);
  --ease-out-expo:cubic-bezier(.16,1,.3,1);
  --ease-snap:cubic-bezier(.2,0,0,1);
  --ease-elastic:cubic-bezier(.68,-.55,.27,1.55);
  --ease-anticipate:cubic-bezier(.36,0,.66,-.56);
  --ease-smooth:cubic-bezier(.25,.1,.25,1);
  --ease-bounce:cubic-bezier(.34,1.8,.64,1);
}

/* Entrance animations */
@keyframes fadeSlideIn{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeSlideUp{from{opacity:0;transform:translateY(30px) scale(.95)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes fadeSlideLeft{from{opacity:0;transform:translateX(-20px) rotate(-1deg)}to{opacity:1;transform:translateX(0) rotate(0)}}
@keyframes fadeScaleIn{from{opacity:0;transform:scale(.88)}to{opacity:1;transform:scale(1)}}
@keyframes panelIn{from{opacity:0;transform:translateY(24px) scale(.96)}to{opacity:1;transform:translateY(0) scale(1)}}
@keyframes popIn{0%{opacity:0;transform:scale(.3)}50%{transform:scale(1.15)}70%{transform:scale(.95)}to{opacity:1;transform:scale(1)}}
@keyframes slideDown{from{opacity:0;transform:translateY(-100%)}to{opacity:1;transform:translateY(0)}}
@keyframes slideRight{from{opacity:0;transform:translateX(-100%)}to{opacity:1;transform:translateX(0)}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.5}}
@keyframes bounce{0%,100%{transform:translateY(0)}50%{transform:translateY(-4px)}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes ripple{to{transform:scale(2.5);opacity:0}}
@keyframes rowIn{from{opacity:0;transform:translateX(-12px) scale(.98)}to{opacity:1;transform:translateX(0) scale(1)}}
@keyframes dotPulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(2);opacity:.3}}
@keyframes elasticPop{0%{transform:scale(0)}40%{transform:scale(1.2)}60%{transform:scale(.9)}80%{transform:scale(1.05)}to{transform:scale(1)}}
@keyframes glowPulse{0%,100%{box-shadow:0 0 0 0 rgba(129,140,248,0)}50%{box-shadow:0 0 20px 4px rgba(129,140,248,.15)}}
@keyframes floatUp{0%,100%{transform:translateY(0)}50%{transform:translateY(-6px)}}
@keyframes breathe{0%,100%{transform:scale(1)}50%{transform:scale(1.02)}}
@keyframes morphIn{0%{opacity:0;transform:translateY(20px) scale(.9) rotate(-1deg)}60%{transform:translateY(-4px) scale(1.02) rotate(.5deg)}to{opacity:1;transform:translateY(0) scale(1) rotate(0)}}
@keyframes slideReveal{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
@keyframes countUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes borderGlow{0%,100%{border-color:rgba(129,140,248,.2)}50%{border-color:rgba(129,140,248,.5)}}
@keyframes cardFloat{0%{transform:translateY(0) rotate(0)}25%{transform:translateY(-3px) rotate(.3deg)}75%{transform:translateY(1px) rotate(-.2deg)}100%{transform:translateY(0) rotate(0)}}

/* Toggle group */
.toggle-group{display:flex;background:rgba(127,127,127,.1);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid var(--glass-border);border-radius:6px;overflow:hidden;position:relative}
.toggle-group button{background:transparent;border:none;color:var(--muted);padding:5px 10px;font-size:11px;cursor:pointer;font-family:inherit;font-weight:500;transition:all .3s var(--ease-elastic);position:relative;z-index:1}
.toggle-group button:active{transform:scale(.93)}
.toggle-group button.active{background:var(--indigo);color:#fff;transform:scale(1.05);box-shadow:0 2px 8px rgba(99,102,241,.3)}
.toggle-group button:hover:not(.active){color:var(--text);background:rgba(255,255,255,.05)}

/* Layout */
.app{display:grid;grid-template-columns:220px 1fr;grid-template-rows:56px 1fr;height:100vh}
.topbar{grid-column:1/-1;background:var(--glass-bg);backdrop-filter:blur(var(--glass-blur)) var(--glass-sat);-webkit-backdrop-filter:blur(var(--glass-blur)) var(--glass-sat);border-bottom:1px solid var(--glass-border);box-shadow:0 1px 3px rgba(0,0,0,.06);display:flex;align-items:center;padding:0 24px;gap:16px;justify-content:space-between;animation:slideDown .6s var(--ease-elastic) both;z-index:10}
.topbar h1{font-size:18px;font-weight:700;letter-spacing:1.5px}
.topbar h1 span{color:var(--indigo);display:inline-block;transition:transform .3s var(--ease-spring)}
.topbar h1:hover span{transform:scale(1.1) rotate(-2deg)}
.topbar .actions{display:flex;gap:8px;align-items:center}
.sidebar{background:var(--glass-bg);backdrop-filter:blur(var(--glass-blur)) var(--glass-sat);-webkit-backdrop-filter:blur(var(--glass-blur)) var(--glass-sat);border-right:1px solid var(--glass-border);box-shadow:1px 0 3px rgba(0,0,0,.06);padding:16px 12px;display:flex;flex-direction:column;gap:2px;overflow-y:auto;overflow-x:clip;animation:slideRight .6s var(--ease-elastic) .1s both}
.nav-item{padding:10px 14px;border-radius:8px;cursor:pointer;font-size:13px;font-weight:500;color:var(--dim);display:flex;align-items:center;gap:10px;transition:all .3s var(--ease-elastic);user-select:none;animation:fadeSlideLeft .5s var(--ease-elastic) both;position:relative}
.nav-item:nth-child(1){animation-delay:.12s}.nav-item:nth-child(2){animation-delay:.18s}.nav-item:nth-child(3){animation-delay:.24s}.nav-item:nth-child(4){animation-delay:.3s}.nav-item:nth-child(5){animation-delay:.36s}.nav-item:nth-child(6){animation-delay:.42s}.nav-item:nth-child(7){animation-delay:.48s}.nav-item:nth-child(9){animation-delay:.54s}
.nav-item:hover{background:var(--surface2);color:var(--text);transform:translateX(4px);padding-left:18px}
.nav-item:active{transform:translateX(2px) scale(.97)}
.nav-item.active{background:var(--indigo-bg);color:var(--indigo);box-shadow:inset 3px 0 0 var(--indigo)}
.nav-item .icon{width:18px;text-align:center;font-size:15px;transition:transform .35s var(--ease-elastic)}
.nav-item:hover .icon{transform:scale(1.2) rotate(-5deg)}
.nav-item.active .icon{animation:elasticPop .5s var(--ease-bounce)}
.content{padding:24px;overflow-y:auto;overflow-x:clip;animation:fadeScaleIn .5s var(--ease-out-expo) .2s both}

/* Components */
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:14px;margin-bottom:20px}
.card{background:var(--glass-bg);backdrop-filter:blur(12px) var(--glass-sat);-webkit-backdrop-filter:blur(12px) var(--glass-sat);border:1px solid var(--glass-border);border-radius:12px;padding:16px 18px;position:relative;overflow:hidden;transition:transform .4s var(--ease-elastic),box-shadow .4s var(--ease-smooth),border-color .3s ease;animation:morphIn .6s var(--ease-elastic) both;box-shadow:0 2px 8px rgba(0,0,0,.04)}
.card:nth-child(1){animation-delay:.08s}.card:nth-child(2){animation-delay:.14s}.card:nth-child(3){animation-delay:.2s}.card:nth-child(4){animation-delay:.26s}.card:nth-child(5){animation-delay:.32s}.card:nth-child(6){animation-delay:.38s}
.card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;transition:height .4s var(--ease-elastic),opacity .3s ease;opacity:.8}
.card::after{content:'';position:absolute;top:0;left:0;right:0;height:50%;background:linear-gradient(180deg,rgba(255,255,255,.06) 0%,transparent 100%);pointer-events:none;border-radius:12px 12px 0 0}
.card:hover{transform:translateY(-4px) scale(1.02);box-shadow:0 8px 25px rgba(0,0,0,.12);border-color:rgba(129,140,248,.15)}
.card:hover::before{height:4px;opacity:1}
.card:active{transform:translateY(-1px) scale(.99)}
.card.i1::before{background:var(--indigo)}.card.i2::before{background:var(--cyan)}.card.i3::before{background:var(--green)}.card.i4::before{background:var(--purple)}.card.i5::before{background:var(--red)}
.card .label{font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px}
.card .value{font-size:24px;font-weight:700;letter-spacing:-.5px;transition:transform .4s var(--ease-elastic),color .3s ease;animation:countUp .5s var(--ease-out-back) .3s both}
.card:hover .value{transform:scale(1.05)}
.card .sub{font-size:11px;color:var(--muted);margin-top:4px}
.c-indigo{color:var(--indigo)}.c-cyan{color:var(--cyan)}.c-green{color:var(--green)}.c-purple{color:var(--purple)}.c-red{color:var(--red)}.c-amber{color:var(--amber)}

.panel{background:var(--glass-bg);backdrop-filter:blur(12px) var(--glass-sat);-webkit-backdrop-filter:blur(12px) var(--glass-sat);border:1px solid var(--glass-border);border-radius:12px;padding:18px;margin-bottom:16px;position:relative;transition:transform .4s var(--ease-elastic),box-shadow .4s var(--ease-smooth);animation:panelIn .6s var(--ease-elastic) both;box-shadow:0 2px 8px rgba(0,0,0,.04)}
.panel::after{content:'';position:absolute;top:0;left:0;right:0;height:40%;background:linear-gradient(180deg,rgba(255,255,255,.04) 0%,transparent 100%);pointer-events:none;border-radius:12px 12px 0 0}
.panel:hover{transform:translateY(-2px);box-shadow:0 6px 24px rgba(0,0,0,.08);border-color:rgba(129,140,248,.15)}
.panel h3{font-size:13px;font-weight:600;margin-bottom:14px;color:var(--text);display:flex;align-items:center;gap:8px}
.panel h3 .dot{width:6px;height:6px;border-radius:50%;background:var(--indigo);animation:dotPulse 2s ease infinite}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
.chart-box{position:relative;height:260px}

table{width:100%;border-collapse:collapse;font-size:12px}
th{text-align:left;padding:10px 10px;color:var(--muted);font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.5px;position:sticky;top:0;background:var(--glass-bg);backdrop-filter:blur(12px) var(--glass-sat);-webkit-backdrop-filter:blur(12px) var(--glass-sat);z-index:1}
td{padding:10px 10px;transition:background .2s ease;white-space:nowrap}
td:last-child{max-width:180px;overflow:hidden;text-overflow:ellipsis}
tbody tr:nth-child(even){background:rgba(255,255,255,.015)}
:root[data-theme="light"] tbody tr:nth-child(even){background:rgba(0,0,0,.015)}
tr{animation:rowIn .4s var(--ease-elastic) both}
tr:nth-child(1){animation-delay:0s}tr:nth-child(2){animation-delay:.04s}tr:nth-child(3){animation-delay:.08s}tr:nth-child(4){animation-delay:.1s}tr:nth-child(5){animation-delay:.13s}tr:nth-child(6){animation-delay:.16s}tr:nth-child(7){animation-delay:.19s}tr:nth-child(8){animation-delay:.21s}tr:nth-child(9){animation-delay:.24s}tr:nth-child(10){animation-delay:.27s}
tr:hover td{background:rgba(255,255,255,.05)}
:root[data-theme="light"] tr:hover td{background:rgba(0,0,0,.04)}
:root[data-theme="light"]::-webkit-scrollbar-thumb{background:#ced4da}
.date-bar input[type=date]::-webkit-calendar-picker-indicator{filter:invert(1)}
:root[data-theme="light"] .date-bar input[type=date]::-webkit-calendar-picker-indicator{filter:none}
.num{text-align:right;font-variant-numeric:tabular-nums}
.badge{display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:600;letter-spacing:.3px;transition:all .3s var(--ease-elastic);animation:popIn .5s var(--ease-bounce) both}
.badge:hover{transform:scale(1.1)}
.b-claude{background:var(--indigo-bg);color:var(--indigo)}.b-codex{background:var(--amber-bg);color:var(--amber)}.b-cline{background:var(--green-bg);color:var(--green)}

/* Date picker */
.date-bar{display:flex;gap:10px;align-items:center;margin-bottom:16px;flex-wrap:wrap;animation:fadeSlideIn .4s var(--ease-out-back) .1s both}
.date-bar label{font-size:12px;color:var(--muted)}
.date-bar input[type=date]{background:rgba(127,127,127,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid var(--glass-border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:12px;font-family:inherit;transition:all .35s var(--ease-elastic)}
.date-bar input[type=date]:focus{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(129,140,248,.15);transform:scale(1.01)}
.btn{padding:7px 16px;border-radius:8px;border:none;font-size:12px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .3s var(--ease-elastic);position:relative;overflow:hidden}
.btn:hover{transform:translateY(-1px) scale(1.02);box-shadow:0 4px 12px rgba(0,0,0,.1)}
.btn:active{transform:scale(.96);transition-duration:.1s}
.btn::after{content:'';position:absolute;inset:0;background:radial-gradient(circle,rgba(255,255,255,.25) 10%,transparent 10.01%);transform:scale(0);opacity:0;transition:transform .6s var(--ease-out-expo),opacity .4s ease}
.btn:active::after{transform:scale(2.5);opacity:0;transition:0s}
.btn-primary{background:var(--indigo);color:#fff}.btn-primary:hover{opacity:.9;transform:translateY(-1px);box-shadow:0 4px 12px rgba(99,102,241,.3)}.btn-primary:active{transform:translateY(0) scale(.97)}
.btn-ghost{background:transparent;border:1px solid rgba(127,127,127,.15);color:var(--dim)}.btn-ghost:hover{border-color:var(--indigo);color:var(--indigo);transform:translateY(-1px);box-shadow:0 2px 8px rgba(0,0,0,.06)}.btn-ghost:active{transform:scale(.96)}
.btn-sm{padding:5px 12px;font-size:11px}
.preset{display:flex;gap:4px}
.preset .btn{font-size:11px;padding:5px 10px;transition:all .2s var(--ease-spring)}
.preset .btn:active{transform:scale(.9)}

/* Compare */
.compare-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
.compare-card{background:var(--surface);border-radius:12px;padding:16px;transition:transform .4s var(--ease-elastic),box-shadow .4s var(--ease-smooth);box-shadow:0 2px 8px rgba(0,0,0,.04)}
.compare-card:hover{transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.08)}
.compare-card h4{font-size:12px;color:var(--muted);margin-bottom:12px;font-weight:500}
.compare-card .metric{display:flex;justify-content:space-between;padding:6px 0;transition:background .2s ease}
.compare-card .metric:hover{background:rgba(255,255,255,.02)}
.compare-card .metric .k{font-size:12px;color:var(--dim)}
.compare-card .metric .v{font-size:12px;font-weight:600}
.diff{display:flex;gap:12px;align-items:center;margin-bottom:16px;flex-wrap:wrap}
.diff .item{background:var(--glass-bg);backdrop-filter:blur(12px) var(--glass-sat);-webkit-backdrop-filter:blur(12px) var(--glass-sat);border:1px solid var(--glass-border);border-radius:10px;padding:10px 14px;min-width:140px;transition:all .4s var(--ease-elastic);animation:morphIn .6s var(--ease-elastic) both;box-shadow:0 2px 8px rgba(0,0,0,.04)}
.diff .item:nth-child(1){animation-delay:.05s}.diff .item:nth-child(2){animation-delay:.12s}.diff .item:nth-child(3){animation-delay:.19s}.diff .item:nth-child(4){animation-delay:.26s}.diff .item:nth-child(5){animation-delay:.33s}
.diff .item:hover{transform:translateY(-3px) scale(1.02);box-shadow:0 6px 20px rgba(0,0,0,.08)}
.diff .item .k{font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.diff .item .v{font-size:16px;font-weight:700;margin-top:4px}
.diff .item .change{font-size:11px;margin-top:2px}
.up{color:var(--red)}.down{color:var(--green)}.same{color:var(--muted)}

.section{display:none;opacity:0;transform:translateY(16px)}.section.active{display:block;animation:morphIn .5s var(--ease-elastic) forwards}

/* Loading overlay */
.loading-overlay{position:fixed;inset:0;z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;background:var(--glass-bg);backdrop-filter:blur(30px) var(--glass-sat);-webkit-backdrop-filter:blur(30px) var(--glass-sat);transition:opacity .4s ease}
.loading-overlay.fade-out{opacity:0;pointer-events:none}
.spinner{width:40px;height:40px;border:3px solid var(--border);border-top-color:var(--indigo);border-right-color:var(--purple);border-radius:50%;animation:spin .7s var(--ease-smooth) infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.loading-text{margin-top:16px;font-size:13px;color:var(--dim);font-weight:500;letter-spacing:.5px}

.table-scroll{max-height:400px;overflow-y:auto;overflow-x:clip;-webkit-overflow-scrolling:touch}

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

/* Budget warning */
.budget-warn{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:12px;font-size:10px;font-weight:600;animation:fadeSlideIn .4s var(--ease-out-back) both}
.budget-ok{background:var(--green-bg);color:var(--green)}
.budget-warn-80{background:var(--amber-bg);color:var(--amber)}
.budget-warn-100{background:var(--red-bg);color:var(--red)}

/* Heatmap */
.heatmap-wrap{overflow-x:clip}
.heatmap-canvas{border-radius:4px}
.heatmap-legend{display:flex;align-items:center;gap:6px;margin-top:8px;font-size:10px;color:var(--muted)}
.heatmap-legend .bar{width:120px;height:8px;border-radius:4px;background:linear-gradient(90deg,#1e1b4b,#4338ca,#818cf8,#c7d2fe)}

/* Session detail */
.sess-detail-row{cursor:pointer}
.sess-detail-row:hover td{background:var(--indigo-bg)}
.sess-detail{display:none}
.sess-detail.open{display:table-row}
.sess-detail td{padding:0;background:var(--surface2)}
.sess-detail-inner{padding:12px 16px;max-height:300px;overflow-y:auto}
.sess-msg{display:flex;gap:8px;padding:4px 0;font-size:11px;border-bottom:1px solid var(--border)}
.sess-msg:last-child{border-bottom:none}
.sess-msg .role{font-weight:600;min-width:60px;text-transform:uppercase;font-size:10px;letter-spacing:.3px}
.sess-msg .role-user{color:var(--cyan)}
.sess-msg .role-assistant{color:var(--indigo)}
.sess-msg .role-system{color:var(--muted)}
.sess-msg .body{flex:1;color:var(--dim);word-break:break-all}
.sess-msg .meta{font-size:10px;color:var(--muted);white-space:nowrap}

/* Search bar */
.search-bar{display:flex;gap:8px;align-items:center;margin-bottom:12px;flex-wrap:wrap;animation:fadeSlideIn .4s var(--ease-out-back) .1s both}
.search-bar input,.search-bar select{background:rgba(127,127,127,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid var(--glass-border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:12px;font-family:inherit;transition:all .35s var(--ease-elastic)}
.search-bar input:focus,.search-bar select:focus{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(129,140,248,.15);transform:scale(1.01)}
.search-bar input{width:200px}

/* Export button */
.export-btn{background:transparent;border:1px solid rgba(127,127,127,.15);color:var(--muted);padding:4px 10px;border-radius:6px;font-size:10px;font-weight:600;cursor:pointer;font-family:inherit;transition:all .3s var(--ease-elastic);letter-spacing:.3px}
.export-btn:hover{transform:translateY(-1px);border-color:var(--indigo);color:var(--indigo)}

/* Budget section */
.budget-hero{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:20px;animation:fadeSlideUp .5s var(--ease-out-back) both}
.budget-ring-card{background:var(--glass-bg);backdrop-filter:blur(16px) var(--glass-sat);-webkit-backdrop-filter:blur(16px) var(--glass-sat);border:1px solid var(--glass-border);border-radius:16px;padding:28px 24px;display:flex;align-items:center;gap:24px;box-shadow:0 2px 12px rgba(0,0,0,.06);transition:transform .45s var(--ease-elastic),box-shadow .45s var(--ease-smooth);position:relative;overflow:hidden;animation:glowPulse 3s ease infinite}
.budget-ring-card:hover{transform:translateY(-4px) scale(1.01);box-shadow:0 8px 30px rgba(0,0,0,.1)}
.budget-ring-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px;border-radius:16px 16px 0 0}
.budget-ring-card:nth-child(1)::before{background:linear-gradient(90deg,var(--indigo),var(--cyan))}
.budget-ring-card:nth-child(2)::before{background:linear-gradient(90deg,var(--purple),var(--pink))}
.budget-ring-wrap{position:relative;width:120px;height:120px;flex-shrink:0}
.budget-ring{width:100%;height:100%;transform:rotate(-90deg)}
.budget-ring-bg{fill:none;stroke:var(--surface2);stroke-width:8}
.budget-ring-fill{fill:none;stroke-width:8;stroke-linecap:round;stroke-dasharray:326.73;stroke-dashoffset:326.73;transition:stroke-dashoffset 1.4s var(--ease-elastic),stroke .4s ease}
.budget-ring-center{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);text-align:center}
.budget-ring-value{font-size:22px;font-weight:800;line-height:1}
.budget-ring-label{font-size:10px;color:var(--muted);margin-top:2px}
.budget-ring-info{flex:1;min-width:0}
.budget-ring-title{font-size:13px;font-weight:600;color:var(--text)}
.budget-ring-amount{font-size:28px;font-weight:800;margin:6px 0;letter-spacing:-.5px}
.budget-ring-sub{font-size:11px;color:var(--muted);line-height:1.4}
.budget-form{display:flex;flex-direction:column;gap:16px}
.budget-form-row{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.budget-form-row label{font-size:13px;font-weight:600;min-width:100px}
.budget-input-group{display:flex;align-items:center;background:rgba(127,127,127,.08);backdrop-filter:blur(8px);-webkit-backdrop-filter:blur(8px);border:1px solid var(--glass-border);border-radius:8px;overflow:hidden;transition:border-color .35s var(--ease-elastic),box-shadow .35s var(--ease-smooth),transform .3s var(--ease-elastic)}
.budget-input-group:focus-within{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(129,140,248,.15);transform:scale(1.01)}
.budget-input-group:focus-within{border-color:var(--indigo);box-shadow:0 0 0 3px rgba(99,102,241,.15)}
.budget-currency{padding:8px 10px;font-size:14px;font-weight:600;color:var(--muted);background:rgba(127,127,127,.06)}
.budget-input-group input{width:100px;padding:8px 12px;background:transparent;border:none;color:var(--text);font-size:14px;font-family:inherit;outline:none}
.budget-hint{font-size:11px;color:var(--muted)}
.budget-form-actions{display:flex;gap:8px;margin-top:4px}
.budget-status-ok{color:var(--green)}.budget-status-warn{color:var(--amber)}.budget-status-over{color:var(--red)}

/* Mini mode */
.mini .sidebar,.mini .topbar .actions #budgetWarn,.mini .topbar .actions .budget-btn,.mini .content>.section:not(#s-mini){display:none!important}
.mini .app{grid-template-columns:1fr}
.mini .topbar{grid-column:1/-1}
.mini .topbar h1{font-size:14px}
.mini .content{padding:12px}
#s-mini{display:none}
.mini #s-mini{display:block!important;animation:none!important;opacity:1!important;transform:none!important}
.mini-cards{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px}
.mini-card{background:var(--glass-bg);backdrop-filter:blur(12px) var(--glass-sat);-webkit-backdrop-filter:blur(12px) var(--glass-sat);border:1px solid var(--glass-border);border-radius:8px;padding:10px 12px;text-align:center}
.mini-card .label{font-size:9px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
.mini-card .value{font-size:18px;font-weight:700;margin-top:2px}
.mini-card .sub{font-size:9px;color:var(--muted);margin-top:1px}
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
      <span id="budgetWarn" onclick="jumpBudget()" style="cursor:pointer" title="Click to open budget settings"></span>
      <button class="btn btn-ghost btn-sm budget-btn" onclick="jumpBudget()" title="Budget Settings" style="font-size:13px;padding:4px 8px">&#128176;</button>
      <span id="status" style="font-size:11px;color:var(--muted)"></span>
      <button class="btn btn-ghost btn-sm" id="reloadBtn" onclick="reload()">Reload</button>
      <button class="btn btn-ghost btn-sm" id="miniBtn" onclick="toggleMini()" title="Mini mode">Mini</button>
    </div>
  </div>
  <div class="sidebar">
    <div class="nav-item active" data-s="overview"><span class="icon">&#9673;</span>Overview</div>
    <div class="nav-item" data-s="daily"><span class="icon">&#9776;</span>Daily</div>
    <div class="nav-item" data-s="projects"><span class="icon">&#128193;</span>Projects</div>
    <div class="nav-item" data-s="sessions"><span class="icon">&#128196;</span>Sessions</div>
    <div class="nav-item" data-s="compare"><span class="icon">&#9878;</span>Compare</div>
    <div class="nav-item" data-s="report"><span class="icon">&#128203;</span>Report</div>
    <div class="nav-item" data-s="budget"><span class="icon">&#128176;</span>Budget</div>
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
      <div class="grid2">
        <div class="panel"><h3 data-i18n="efficiency"><span class="dot" style="background:var(--green)"></span>Efficiency</h3><div id="efficiencyMetrics"></div></div>
        <div class="panel"><h3><span class="dot" style="background:var(--amber)"></span><span data-i18n="forecast">Cost Forecast</span><button class="export-btn" style="margin-left:auto" onclick="exportCSV('overview')">CSV</button></h3><div id="forecastPanel"></div></div>
      </div>
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
          <button class="btn btn-ghost btn-sm" id="hourlyCmpBtn" onclick="toggleHourlyCompare(true)" style="display:none" data-i18n="cmpPrevDay">Compare Prev Day</button>
        </div>
      </div>
      <div class="cards" id="dailyCards"></div>
      <div class="grid2">
        <div class="panel"><h3 data-i18n="tokenUsage"><span class="dot"></span>Token Usage</h3><div class="chart-box"><canvas id="cDailyToken"></canvas></div></div>
        <div class="panel"><h3 data-i18n="dailyCost"><span class="dot"></span>Daily Cost</h3><div class="chart-box"><canvas id="cDailyCost"></canvas></div></div>
      </div>
      <div class="panel"><h3 data-i18n="dailyDetail"><span class="dot"></span>Daily Detail<button class="export-btn" style="margin-left:auto" onclick="exportCSV('daily')">CSV</button></h3><div class="table-scroll"><table id="tDaily"></table></div></div>
      <div class="panel"><h3 data-i18n="modelRanking"><span class="dot" style="background:var(--amber)"></span>Model Ranking</h3><div class="chart-box"><canvas id="cModelRanking"></canvas></div></div>
      <div class="grid2">
        <div class="panel"><h3 data-i18n="heatmap"><span class="dot" style="background:var(--purple)"></span>Heatmap</h3><div class="heatmap-wrap"><canvas id="cHeatmap" class="heatmap-canvas" width="600" height="180"></canvas></div><div class="heatmap-legend"><span data-i18n="low">Low</span><div class="bar"></div><span data-i18n="high">High</span></div></div>
        <div class="panel"><h3 data-i18n="modelTrends"><span class="dot" style="background:var(--pink)"></span>Model Trends</h3><div class="chart-box"><canvas id="cModelTrends"></canvas></div></div>
      </div>
      <div class="panel" id="hourlyComparePanel" style="display:none"><h3><span class="dot" style="background:var(--cyan)"></span><span data-i18n="hourlyCompare">Hourly Comparison</span><button class="btn btn-ghost btn-sm" style="margin-left:auto" onclick="toggleHourlyCompare(false)" data-i18n="hide">Hide</button></h3><div class="chart-box"><canvas id="cHourlyCompare"></canvas></div></div>
    </div>

    <!-- Projects -->
    <div class="section" id="s-projects">
      <div class="panel"><h3 data-i18n="perProject"><span class="dot"></span>Per-Project Usage<button class="export-btn" style="margin-left:auto" onclick="exportCSV('projects')">CSV</button></h3><div class="table-scroll"><table id="tProject"></table></div></div>
    </div>

    <!-- Sessions -->
    <div class="section" id="s-sessions">
      <div class="date-bar">
        <label data-i18n="range">Range:</label>
        <input type="date" id="sessStart">
        <label>to</label>
        <input type="date" id="sessEnd">
        <button class="btn btn-primary btn-sm" data-i18n="apply" onclick="applySessionFilter()">Apply</button>
        <div class="preset">
          <button class="btn btn-ghost btn-sm" onclick="presetSession(1)">1D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetSession(7)">7D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetSession(30)">30D</button>
          <button class="btn btn-ghost btn-sm" onclick="presetSession(0)">All</button>
        </div>
      </div>
      <div class="search-bar">
        <input type="text" id="sessSearch" placeholder="Search..." oninput="filterSessions()">
        <select id="sessAppFilter" onchange="filterSessions()"><option value="">All Apps</option></select>
        <select id="sessModelFilter" onchange="filterSessions()"><option value="">All Models</option></select>
      </div>
      <div class="panel"><h3 data-i18n="topSessions"><span class="dot"></span>Top Sessions by Usage<button class="export-btn" style="margin-left:auto" onclick="exportCSV('sessions')">CSV</button></h3><div class="table-scroll"><table id="tSession"></table></div></div>
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
      <div class="grid2">
        <div class="panel"><h3 data-i18n="compChartToken"><span class="dot"></span>Token Comparison</h3><div class="chart-box"><canvas id="cCompareToken"></canvas></div></div>
        <div class="panel"><h3 data-i18n="compChartCost"><span class="dot"></span>Cost Comparison</h3><div class="chart-box"><canvas id="cCompareCost"></canvas></div></div>
      </div>
    </div>

    <!-- Report -->
    <div class="section" id="s-report">
      <div class="panel">
        <h3><span class="dot" style="background:var(--green)"></span><span data-i18n="report">Usage Report</span><button class="export-btn" style="margin-left:auto" onclick="copyReport()" data-i18n="copyReport">Copy</button></h3>
        <div style="display:flex;gap:8px;margin-bottom:12px">
          <button class="btn btn-ghost btn-sm" onclick="genReport(7)" data-i18n="last7d">Last 7 Days</button>
          <button class="btn btn-ghost btn-sm" onclick="genReport(30)" data-i18n="last30d">Last 30 Days</button>
          <button class="btn btn-ghost btn-sm" onclick="genReport(0)" data-i18n="allTime">All Time</button>
        </div>
        <pre id="reportText" style="font-size:12px;line-height:1.6;color:var(--dim);white-space:pre-wrap;word-break:break-all;max-height:500px;overflow-y:auto;background:var(--surface2);padding:14px;border-radius:8px"></pre>
      </div>
    </div>

    <!-- Budget -->
    <div class="section" id="s-budget">
      <div class="panel" style="margin-bottom:16px">
        <div style="display:flex;align-items:center;justify-content:space-between">
          <h3><span class="dot" style="background:var(--indigo)"></span><span data-i18n="budgetTitle">Budget Management</span></h3>
          <div class="toggle-group" id="budgetModeToggle">
            <button class="active" onclick="setBudgetMode('token')" data-i18n="budgetModeToken">Token Budget</button>
            <button onclick="setBudgetMode('price')" data-i18n="budgetModePrice">Price Budget</button>
          </div>
        </div>
      </div>
      <div class="budget-hero">
        <div class="budget-ring-card" id="budgetDailyCard">
          <div class="budget-ring-wrap">
            <svg class="budget-ring" viewBox="0 0 120 120">
              <circle class="budget-ring-bg" cx="60" cy="60" r="52" />
              <circle class="budget-ring-fill" id="budgetDailyRing" cx="60" cy="60" r="52" />
            </svg>
            <div class="budget-ring-center">
              <div class="budget-ring-value" id="budgetDailyPct">--</div>
              <div class="budget-ring-label" data-i18n="budgetToday">Today</div>
            </div>
          </div>
          <div class="budget-ring-info">
            <div class="budget-ring-title" data-i18n="budgetDaily">Daily Budget</div>
            <div class="budget-ring-amount" id="budgetDailyAmount">$0.00</div>
            <div class="budget-ring-sub" id="budgetDailySub"></div>
          </div>
        </div>
        <div class="budget-ring-card" id="budgetMonthlyCard">
          <div class="budget-ring-wrap">
            <svg class="budget-ring" viewBox="0 0 120 120">
              <circle class="budget-ring-bg" cx="60" cy="60" r="52" />
              <circle class="budget-ring-fill" id="budgetMonthlyRing" cx="60" cy="60" r="52" />
            </svg>
            <div class="budget-ring-center">
              <div class="budget-ring-value" id="budgetMonthlyPct">--</div>
              <div class="budget-ring-label" data-i18n="budgetThisMonth">This Month</div>
            </div>
          </div>
          <div class="budget-ring-info">
            <div class="budget-ring-title" data-i18n="budgetMonthly">Monthly Budget</div>
            <div class="budget-ring-amount" id="budgetMonthlyAmount">$0.00</div>
            <div class="budget-ring-sub" id="budgetMonthlySub"></div>
          </div>
        </div>
      </div>
      <div class="grid2">
        <div class="panel">
          <h3><span class="dot" style="background:var(--indigo)"></span><span data-i18n="budgetByModel">By Model</span></h3>
          <div class="table-scroll"><table id="tBudgetModel"></table></div>
        </div>
        <div class="panel">
          <h3><span class="dot" style="background:var(--cyan)"></span><span data-i18n="budgetByApp">By App</span></h3>
          <div class="table-scroll"><table id="tBudgetApp"></table></div>
        </div>
      </div>
      <div class="panel">
        <h3><span class="dot" style="background:var(--green)"></span><span data-i18n="budgetPerApp">Per-App Budget</span></h3>
        <div style="font-size:11px;color:var(--muted);margin-bottom:8px" data-i18n="budgetAppDesc">Monthly limit per application (0 = disabled)</div>
        <div class="table-scroll"><table id="tBudgetAppDetail"></table></div>
      </div>
      <div class="panel">
        <h3><span class="dot" style="background:var(--pink)"></span><span data-i18n="budgetPerModel">Per-Model Budget</span></h3>
        <div style="font-size:11px;color:var(--muted);margin-bottom:8px" data-i18n="budgetModelDesc">Monthly limit per model (0 = disabled)</div>
        <div class="table-scroll"><table id="tBudgetModelDetail"></table></div>
      </div>
      <div class="panel">
        <h3><span class="dot" style="background:var(--amber)"></span><span data-i18n="budgetTitle">Budget Settings</span></h3>
        <div class="budget-form">
          <div class="budget-form-row">
            <label data-i18n="budgetDaily">Daily Budget</label>
            <div class="budget-input-group">
              <span class="budget-currency" id="budgetCurD">$</span>
              <input type="number" id="budgetDailyInput2" min="0" step="1" value="0" onchange="setBudgetFromForm()">
            </div>
            <span class="budget-hint" id="budgetHintD" data-i18n="budgetSetDesc">Set spending limits (0 = disabled)</span>
          </div>
          <div class="budget-form-row">
            <label data-i18n="budgetMonthly">Monthly Budget</label>
            <div class="budget-input-group">
              <span class="budget-currency" id="budgetCurM">$</span>
              <input type="number" id="budgetMonthlyInput2" min="0" step="1" value="0" onchange="setBudgetFromForm()">
            </div>
            <span class="budget-hint" id="budgetHintM" data-i18n="budgetSetDesc">Set spending limits (0 = disabled)</span>
          </div>
          <div class="budget-form-actions">
            <button class="btn btn-primary btn-sm" onclick="setBudgetFromForm()" data-i18n="budgetSave">Save Budget</button>
            <button class="btn btn-ghost btn-sm" onclick="resetBudget()" data-i18n="budgetReset">Reset</button>
          </div>
        </div>
      </div>
    </div>

    <!-- Mini mode -->
    <div class="section" id="s-mini">
      <div class="mini-cards" id="miniCards"></div>
      <div style="text-align:center">
        <button class="btn btn-primary btn-sm" onclick="toggleMini()" data-i18n="restoreFull">Restore Full View</button>
      </div>
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
          <div style="display:flex;align-items:center;justify-content:space-between">
            <div><div style="font-size:13px;font-weight:600" data-i18n="budgetTermLabel">Budget Term</div><div style="font-size:11px;color:var(--muted);margin-top:2px" data-i18n="budgetTermDesc">Display "Limit" or "Target" for budget values</div></div>
            <div class="toggle-group" id="budgetTermToggle"><button class="active" onclick="setBudgetTerm('limit')" data-i18n="budgetTermLimit">Limit</button><button onclick="setBudgetTerm('target')" data-i18n="budgetTermTarget">Target</button></div>
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
function truncW(s,maxW){
  if(!s)return'';var w=0,i=0;var len=s.length;
  while(i<len){var c=s.charCodeAt(i);w+=(c>0x7f)?2:1;if(w>maxW)break;i++}
  return i<len?s.substring(0,i)+'...':s;
}
function pct(a,b){if(!b)return'-';return((a-b)/b*100).toFixed(1)+'%'}
function pctClass(a,b){if(!b)return'same';return a>b?'up':'down'}

// ── i18n ──
let lang=(function(){try{return localStorage.getItem('tb-lang')||'zh'}catch(e){return 'zh'}})();
numFmt=(function(){try{return localStorage.getItem('tb-numfmt')||(lang==='zh'?'cn':'en')}catch(e){return 'cn'}})();
const T={
  en:{
    overview:'Overview',daily:'Trends',budget:'Budget',projects:'Projects',sessions:'Sessions',compare:'Compare',
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
    compareBtn:'Compare',tdYd:'Today/Yesterday',twLw:'This/Last Week',tmLm:'This/Last Month',compChartToken:'Token Comparison',compChartCost:'Cost Comparison',
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
    dailyBudget:'Daily Budget',dailyBudgetDesc:'USD daily spending limit (0 = disabled)',
    monthlyBudget:'Monthly Budget',monthlyBudgetDesc:'USD monthly spending limit (0 = disabled)',
    efficiency:'Efficiency',forecast:'Cost Forecast',
    tokPerMsg:'Tokens/Msg',costPerMsg:'Cost/Msg',costPer1k:'Cost/1K Tokens',
    projMonthCost:'Projected monthly cost',basedOn:'Based on',
    heatmap:'Usage Heatmap',modelTrends:'Model Trends',modelRanking:'Model Token Ranking',
    low:'Low',high:'High',mo:'Mo',tu:'Tu',we:'We',th:'Th',fr:'Fr',sa:'Sa',su:'Su',
    report:'Usage Report',copyReport:'Copy',last7d:'Last 7 Days',last30d:'Last 30 Days',allTime:'All Time',
    restoreFull:'Restore Full View',miniMode:'Mini',
    reportTitle:'TOKENBANK Usage Report',reportPeriod:'Period',reportTotal:'Total',reportAvg:'Daily Avg',
    reportTopModels:'Top Models',reportTopProjects:'Top Projects',
    exported:'Exported',noData:'No data',
    budgetTitle:'Budget Management',budgetDaily:'Daily Budget',budgetMonthly:'Monthly Budget',
    budgetSetDesc:'Set spending limits (0 = disabled)',budgetSave:'Save Budget',budgetReset:'Reset',
    budgetToday:'Today',budgetThisMonth:'This Month',budgetRemaining:'Remaining',budgetSpent:'Spent',
    budgetProgress:'Usage Progress',budgetByModel:'Cost by Model',budgetByApp:'Cost by App',
    budgetNoLimit:'No limit set',budgetExceeded:'Exceeded!',budgetWarning:'Warning: approaching limit',
    budgetModeToken:'Token Budget',budgetModePrice:'Price Budget',budgetTokenUnit:'tokens',budgetPriceUnit:'USD',
    budgetPerApp:'Per-App Budget',budgetPerModel:'Per-Model Budget',budgetLimit:'Limit',budgetUsed:'Used',budgetPct:'Usage',
    budgetAppDesc:'Monthly limit per application (0 = disabled)',budgetModelDesc:'Monthly limit per model (0 = disabled)',
    budgetNoAppLimit:'No per-app limits set',budgetNoModelLimit:'No per-model limits set',
    budgetTermLabel:'Budget Term',budgetTermDesc:'Display "Limit" or "Target" for budget values',
    budgetTermLimit:'Limit',budgetTermTarget:'Target',
    hourlyCompare:'Hourly Comparison',cmpPrevDay:'Compare Prev Day',hide:'Hide',
    sessDateRange:'Session Date Range',
  },
  zh:{
    overview:'概览',daily:'趋势分析',budget:'预算',projects:'项目',sessions:'会话',compare:'对比',
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
    compareBtn:'对比',tdYd:'今天/昨天',twLw:'本周/上周',tmLm:'本月/上月',compChartToken:'Token 对比',compChartCost:'费用对比',
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
    dailyBudget:'每日预算',dailyBudgetDesc:'每日费用上限美元（0 = 不限制）',
    monthlyBudget:'每月预算',monthlyBudgetDesc:'每月费用上限美元（0 = 不限制）',
    efficiency:'效率指标',forecast:'费用预测',
    tokPerMsg:'Token/消息',costPerMsg:'费用/消息',costPer1k:'费用/千Token',
    projMonthCost:'预计本月费用',basedOn:'基于',
    heatmap:'使用热力图',modelTrends:'模型趋势',modelRanking:'模型Token总量排行',
    low:'低',high:'高',mo:'一',tu:'二',we:'三',th:'四',fr:'五',sa:'六',su:'日',
    report:'使用报告',copyReport:'复制',last7d:'近7天',last30d:'近30天',allTime:'全部',
    restoreFull:'恢复完整视图',miniMode:'迷你',
    reportTitle:'TOKENBANK 使用报告',reportPeriod:'时段',reportTotal:'合计',reportAvg:'日均',
    reportTopModels:'热门模型',reportTopProjects:'热门项目',
    exported:'已导出',noData:'暂无数据',
    budgetTitle:'预算管理',budgetDaily:'每日预算',budgetMonthly:'每月预算',
    budgetSetDesc:'设置费用上限（0 = 不启用）',budgetSave:'保存预算',budgetReset:'重置',
    budgetToday:'今日',budgetThisMonth:'本月',budgetRemaining:'剩余',budgetSpent:'已花费',
    budgetProgress:'使用进度',budgetByModel:'按模型费用',budgetByApp:'按应用费用',
    budgetNoLimit:'未设置限额',budgetExceeded:'已超支！',budgetWarning:'警告：即将达到限额',
    budgetModeToken:'Token 预算',budgetModePrice:'费用预算',budgetTokenUnit:'Token',budgetPriceUnit:'美元',
    budgetPerApp:'按应用预算',budgetPerModel:'按模型预算',budgetLimit:'限额',budgetUsed:'已用',budgetPct:'使用率',
    budgetAppDesc:'每个应用的月度限额（0 = 不限制）',budgetModelDesc:'每个模型的月度限额（0 = 不限制）',
    budgetNoAppLimit:'未设置按应用限额',budgetNoModelLimit:'未设置按模型限额',
    budgetTermLabel:'预算用词',budgetTermDesc:'预算数值显示为"限额"或"目标"',
    budgetTermLimit:'限额',budgetTermTarget:'目标',
    hourlyCompare:'逐时对比',cmpPrevDay:'对比前一天',hide:'隐藏',
    sessDateRange:'会话日期范围',
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
function _refreshCurrentView(){
  var s=$('#dStart').value,e=$('#dEnd').value;
  if(s&&e&&s===e){pywebview.api.get_hourly(s).then(function(r){renderHourly(JSON.parse(r))}).catch(function(e){console.error('hourly error:',e)})}
  else if(s&&e){pywebview.api.get_filtered(s,e).then(function(r){renderDaily(JSON.parse(r))}).catch(function(e){console.error('filtered error:',e)})}
  else{renderDaily(fullData)}
}
function startAutoRefresh(){
  if(_autoRefreshTimer){clearInterval(_autoRefreshTimer);_autoRefreshTimer=null}
  if(autoRefreshEnabled&&refreshIntervalSec>0){
    _autoRefreshTimer=setInterval(function(){
      _animEnabled=false;
      pywebview.api.reload().then(function(r){
        var d=JSON.parse(r);fullData=d;_fullDataAll=d;
        render(d);_refreshCurrentView();renderModelTrends();checkBudget();renderBudget();
        if(d.daily.length){allDates=d.daily.map(function(x){return x.date})}
        $('#status').textContent=t('loaded')+' '+d.summary.total_records+' '+t('loadedSuff');
        _animEnabled=true;
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
  var bm=$('#budgetModeToggle');if(bm)bm.querySelectorAll('button').forEach(function(b){
    var isToken=budgetMode==='token';
    b.classList.toggle('active',(isToken&&b.dataset.i18n==='budgetModeToken')||(!isToken&&b.dataset.i18n==='budgetModePrice'));
  });
  var bt=$('#budgetTermToggle');if(bt)bt.querySelectorAll('button').forEach(function(b){
    b.classList.toggle('active',(budgetTerm==='limit'&&b.dataset.i18n==='budgetTermLimit')||(budgetTerm==='target'&&b.dataset.i18n==='budgetTermTarget'));
  });
}
function initSettings(){
  var savedTheme=lsGet('tb-theme','light');
  document.documentElement.setAttribute('data-theme',savedTheme);
  numFmt=lsGet('tb-numfmt',lang==='zh'?'cn':'en');
  autoRefreshEnabled=lsGet('tb-autorefresh','on')==='on';
  refreshIntervalSec=parseInt(lsGet('tb-refreshinterval','30'),10)||30;
  budgetMode=lsGet('tb-budget-mode','token');
  budgetTerm=lsGet('tb-budget-term','limit');
  budgetDaily=parseFloat(lsGet('tb-budget-daily','0'))||0;
  budgetMonthly=parseFloat(lsGet('tb-budget-monthly','0'))||0;
  _loadBudgetMaps();
  syncBudgetInputs();
  syncSettingsUI();
  applyLang();
  // Event delegation for per-app/per-model budget inputs
  document.addEventListener('input',function(ev){
    var el=ev.target;
    if(!el.dataset||!el.dataset.budgetType)return;
    var val=parseFloat(el.value)||0;
    var name=el.dataset.budgetName;
    if(el.dataset.budgetType==='app'){_saveAppBudget(name,val)}
    else if(el.dataset.budgetType==='model'){_saveModelBudget(name,val)}
  });
}
function applyLang(){
  document.documentElement.lang=lang==='zh'?'zh-CN':'en';
  $$('.nav-item').forEach(function(n){var key=n.dataset.s,icon=n.querySelector('.icon');if(key&&icon){n.innerHTML='';n.appendChild(icon);n.appendChild(document.createTextNode(t(key)))}});
  $$('[data-i18n]').forEach(function(el){
    var key=el.dataset.i18n;if(!key)return;
    if(el.querySelector('[data-i18n]'))return;
    var dot=el.querySelector('.dot');
    if(dot){
      var tn=dot.nextSibling;
      if(tn&&tn.nodeType===3){tn.textContent=t(key)}
      else{el.insertBefore(document.createTextNode(t(key)),dot.nextSibling)}
    }
    else{el.textContent=t(key)}
  });
  var appL=$('#appLabel');if(appL)appL.textContent=t('appLabel');
  var rBtn=$('#reloadBtn');if(rBtn)rBtn.textContent=t('reload');
  if(fullData){render(fullData);_refreshCurrentView();renderHeatmap();renderModelTrends()}
}

// ── Chart helpers ──
var _animEnabled=true;
function kill(k){if(charts[k]){charts[k].destroy();delete charts[k]}}
const _easeSpring='cubicBezier(.34,1.56,.64,1)';
const _easeElastic='cubicBezier(.68,-.55,.27,1.55)';
const _easeBounce='cubicBezier(.34,1.8,.64,1)';
function _anim(dur,delayFn){return _animEnabled?{duration:dur,easing:_easeElastic,delay:delayFn}:{duration:0}}
function bar(k,labels,datasets,stacked=true,opts={}){
  kill(k);const ctx=document.getElementById(k);if(!ctx)return;
  charts[k]=new Chart(ctx,{type:'bar',data:{labels,datasets},options:{
    responsive:true,maintainAspectRatio:false,
    animation:_anim(800,function(c){return c.dataIndex*40+c.datasetIndex*100}),
    plugins:{legend:{display:datasets.length>1,labels:{color:'#a1a1aa',font:{size:10},boxWidth:10,padding:8}}},
    scales:{x:{stacked,ticks:{color:'#71717a',font:{size:9},maxRotation:45},grid:{display:false}},
            y:{stacked,ticks:{color:'#71717a',font:{size:9},callback:v=>fmt(v)},grid:{color:'rgba(63,63,70,.3)'}},
            ...opts.scales||{}}}});
}
function line(k,labels,datasets,opts={}){
  kill(k);const ctx=document.getElementById(k);if(!ctx)return;
  charts[k]=new Chart(ctx,{type:'line',data:{labels,datasets},options:{
    responsive:true,maintainAspectRatio:false,
    animation:_animEnabled?{duration:1000,easing:_easeElastic,delay:function(c){return c.dataIndex*30}}:{duration:0},
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
        animation:{animateRotate:_animEnabled,animateScale:_animEnabled,duration:_animEnabled?1200:0,easing:_easeElastic},
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
function badge(a){var e=escH(a);return'<span class="badge b-'+e+'">'+e.toUpperCase()+'</span>'}
function escH(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}

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

  // Recent messages — precompute session totals from sessions data
  if(d.recent&&d.recent.length){
    var sessMap={};
    (d.sessions||[]).forEach(function(s){sessMap[s.id]=s});
    const recR=d.recent.map(r=>{
      const ts=r.timestamp?r.timestamp.replace('T',' ').slice(0,16):'';
      const sm=r.summary?r.summary.replace(/^[\s﻿\xA0]+|[\s﻿\xA0]+$/g,''):'';
      const label=sm?truncW(sm,100):r.session.substring(0,12)+'...';
      // Message-level input (non-cache + total)
      const msgNonCache=r.input||0;
      const msgTotal=(r.input||0)+(r.cache_read||0)+(r.cache_create||0);
      const msgCell=fmt(msgNonCache)+(msgTotal>msgNonCache?' <span style="color:var(--muted);font-size:10px">('+fmt(msgTotal)+')</span>':'');
      // Session-level total from sessions aggregation
      const sess=sessMap[r.session];
      const sessInput=sess?(sess.input||0)+(sess.cache_read||0)+(sess.cache_create||0):msgTotal;
      const sessLabel=lang==='zh'?'会话':'session';
      const cnt=sess?sess.count:1;
      const sessTag=cnt>1?'<span class="badge" style="font-size:9px;padding:1px 4px;margin-left:4px;background:rgba(251,191,36,.2);color:#fbbf24">'+sessLabel+' ('+cnt+')</span>':'';
      return[ts,badge(r.app)+sessTag,r.model,fmt(sessInput),msgCell,fmt(r.output||0),label]
    });
    mkT($('#tRecent'),[t('thTime'),t('thApp'),t('thModel'),lang==='zh'?'会话总输入':'Sess Input',lang==='zh'?'消息输入':'Msg Input',t('thOutput'),t('thSummary')],recR);
  }

  // Projects
  const projR=d.projects.map(p=>[p.name,fmt(p.sessions),fmt(p.count),fmt(p.input),fmt(p.output),fmt(p.cache_read),fmt(p.total),fmtC(p.cost)]);
  mkT($('#tProject'),[t('thProject'),t('thSess'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCache'),t('thTotal'),t('thCost')],projR);

  // Sessions
  renderSessionTable(d.sessions);
  // Efficiency + Forecast + Budget
  renderEfficiency();renderForecast();checkBudget();
}

// ── Daily with filter ──
let fullData=null;
let _fullDataAll=null;
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
  var cmpBtn=$('#hourlyCmpBtn');if(cmpBtn)cmpBtn.style.display='none';
  var cmpPanel=$('#hourlyComparePanel');if(cmpPanel)cmpPanel.style.display='none';
  if(data.summary)renderDailyCards(data.summary);
  const dd=data.daily;
  if(!dd.length)return;
  const startDate=dd[0].date,endDate=dd[dd.length-1].date;
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
  renderHeatmap(startDate,endDate);
  pywebview.api.get_model_daily(startDate,endDate).then(function(r){
    var mdata=JSON.parse(r);
    renderModelRanking(mdata,function(d){return d.date.slice(5)});
  }).catch(function(e){console.error('model daily error:',e)});
  var detail=$$('#s-daily .panel h3[data-i18n]');
  detail.forEach(function(el){if(el.dataset.i18n==='dailyDetail'){var d=el.querySelector('.dot');if(d&&d.nextSibling)d.nextSibling.textContent=t('dailyDetail')}});
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
  renderHeatmap(data.date,data.date);
  pywebview.api.get_model_hourly(data.date).then(function(r){
    var mdata=JSON.parse(r);
    renderModelRanking(mdata,function(d){return d.hour});
  }).catch(function(e){console.error('model hourly error:',e)});
  var detail=$$('#s-daily .panel h3[data-i18n]');
  detail.forEach(function(el){if(el.dataset.i18n==='dailyDetail'){var d=el.querySelector('.dot');if(d&&d.nextSibling)d.nextSibling.textContent=lang==='zh'?'逐时详情':'Hourly Detail'}});
  // Show hourly compare button for single-day view
  var cmpBtn=$('#hourlyCmpBtn');if(cmpBtn)cmpBtn.style.display='';
  _currentHourlyDate=data.date;
}
var _currentHourlyDate=null;
function toggleHourlyCompare(on){
  var panel=$('#hourlyComparePanel');if(!panel)return;
  if(!on||!_currentHourlyDate){panel.style.display='none';return}
  var d=new Date(_currentHourlyDate);
  d.setDate(d.getDate()-1);
  var prevDate=fmtD(d);
  pywebview.api.get_hourly_compare(_currentHourlyDate,prevDate).then(function(r){
    var data=JSON.parse(r);
    renderHourlyCompare(data);
    panel.style.display='';
  }).catch(function(e){console.error('hourly compare error:',e)});
}
function renderHourlyCompare(data){
  var d1=data.day1,d2=data.day2;
  if(!d1||!d2||!d1.hourly||!d2.hourly)return;
  var hours=[];for(var i=0;i<24;i++){hours.push(('0'+i).slice(-2)+':00')}
  var map1={};d1.hourly.forEach(function(h){map1[h.hour]=h});
  var map2={};d2.hourly.forEach(function(h){map2[h.hour]=h});
  var t1=hours.map(function(h){return(map1[h]||{}).total||0});
  var t2=hours.map(function(h){return(map2[h]||{}).total||0});
  var c1=hours.map(function(h){return(map1[h]||{}).cost||0});
  var c2=hours.map(function(h){return(map2[h]||{}).cost||0});
  kill('cHourlyCompare');
  var ctx=document.getElementById('cHourlyCompare');if(!ctx)return;
  charts['cHourlyCompare']=new Chart(ctx,{type:'line',data:{labels:hours,datasets:[
    {label:d1.date+' Token',data:t1,borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true,yAxisID:'y'},
    {label:d2.date+' Token',data:t2,borderColor:'rgba(96,165,250,.4)',borderDash:[5,5],fill:false,yAxisID:'y'},
    {label:d1.date+' '+t('costLabel'),data:c1,borderColor:'#fbbf24',backgroundColor:'rgba(251,191,36,.1)',fill:true,yAxisID:'y1'},
    {label:d2.date+' '+t('costLabel'),data:c2,borderColor:'rgba(251,191,36,.4)',borderDash:[5,5],fill:false,yAxisID:'y1'},
  ]},options:{
    responsive:true,maintainAspectRatio:false,
    interaction:{mode:'index',intersect:false},
    plugins:{legend:{display:true,labels:{color:'#a1a1aa',font:{size:10},boxWidth:10,padding:8}}},
    scales:{
      x:{ticks:{color:'#71717a',font:{size:9},maxRotation:45},grid:{display:false}},
      y:{position:'left',ticks:{color:'#71717a',font:{size:9},callback:function(v){return fmt(v)}},grid:{color:'rgba(63,63,70,.3)'}},
      y1:{position:'right',ticks:{color:'#71717a',font:{size:9},callback:function(v){return fmtC(v)}},grid:{drawOnChartArea:false}}
    },
    elements:{point:{radius:2,hoverRadius:5},line:{tension:.3,borderWidth:2}}
  }});
}

function applyDaily(){
  const s=$('#dStart').value,e=$('#dEnd').value;
  if(!s||!e)return;
  if(s===e){pywebview.api.get_hourly(s).then(r=>{renderHourly(JSON.parse(r))}).catch(function(e){console.error('hourly error:',e)})}
  else{pywebview.api.get_filtered(s,e).then(r=>{renderDaily(JSON.parse(r))}).catch(function(e){console.error('filtered error:',e)})}
}
function presetDaily(n){
  if(!allDates.length)return;
  if(n===0){renderDaily(fullData);$('#dStart').value=allDates[0];$('#dEnd').value=allDates[allDates.length-1];return}
  const end=allDates[allDates.length-1];
  if(n===1){
    $('#dStart').value=end;$('#dEnd').value=end;
    pywebview.api.get_hourly(end).then(r=>{renderHourly(JSON.parse(r))}).catch(function(e){console.error('hourly error:',e)});
    return;
  }
  const si=Math.max(0,allDates.length-n);
  const start=allDates[si];
  $('#dStart').value=start;$('#dEnd').value=end;
  pywebview.api.get_filtered(start,end).then(r=>{renderDaily(JSON.parse(r))}).catch(function(e){console.error('filtered error:',e)});
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

    // Compare charts — tokens and cost on separate charts
    const tokenLabels=[t('sTotalInput'),t('sNonCacheInput'),t('output'),t('cacheRead')];
    const tv1=[tif1,p1.total_input,p1.total_output,p1.total_cache_read];
    const tv2=[tif2,p2.total_input,p2.total_output,p2.total_cache_read];
    bar('cCompareToken',tokenLabels,[
      {label:`A: ${s1} ~ ${e1}`,data:tv1,backgroundColor:'rgba(34,211,238,.7)'},
      {label:`B: ${s2} ~ ${e2}`,data:tv2,backgroundColor:'rgba(251,191,36,.7)'},
    ],false,{scales:{y:{ticks:{color:'#71717a',callback:v=>fmt(v)},grid:{color:'rgba(63,63,70,.3)'}}}});
    bar('cCompareCost',[t('costLabel')],[
      {label:`A: ${s1} ~ ${e1}`,data:[p1.total_cost],backgroundColor:'rgba(34,211,238,.7)'},
      {label:`B: ${s2} ~ ${e2}`,data:[p2.total_cost],backgroundColor:'rgba(251,191,36,.7)'},
    ],false,{scales:{y:{ticks:{color:'#71717a',callback:v=>fmtC(v)},grid:{color:'rgba(63,63,70,.3)'}}}});
  }).catch(function(e){console.error('compare error:',e)});
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
function fmtD(d){return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2)}

// ── Nav ──
$$('.nav-item').forEach(n=>n.addEventListener('click',function(){
  $$('.nav-item').forEach(function(x){x.classList.remove('active')});
  $$('.section').forEach(function(x){x.classList.remove('active')});
  n.classList.add('active');
  var section=$('#s-'+n.dataset.s);
  if(section)section.classList.add('active');
  if(n.dataset.s==='settings')loadPricingEditor();
  if(n.dataset.s==='compare')presetCompare('day');
  if(n.dataset.s==='budget')renderBudget();
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

// ── Budget ──
var budgetDaily=0,budgetMonthly=0,budgetMode='token',budgetTerm='limit';
var budgetAppModels={};var budgetModelModels={};
function jumpBudget(){
  $$('.nav-item').forEach(function(x){x.classList.remove('active')});
  $$('.section').forEach(function(x){x.classList.remove('active')});
  var ni=document.querySelector('.nav-item[data-s="budget"]');
  if(ni)ni.classList.add('active');
  var sec=$('#s-budget');if(sec)sec.classList.add('active');
  renderBudget();
}
function syncBudgetInputs(){
  var d2=$('#budgetDailyInput2'),m2=$('#budgetMonthlyInput2');
  if(d2)d2.value=budgetDaily;if(m2)m2.value=budgetMonthly;
}
function setBudgetMode(mode){
  budgetMode=mode;lsSet('tb-budget-mode',mode);
  var btns=$('#budgetModeToggle').querySelectorAll('button');
  btns.forEach(function(b){b.classList.toggle('active',b.textContent.trim()===t(mode==='token'?'budgetModeToken':'budgetModePrice'))});
  syncBudgetInputs();renderBudget();checkBudget();
}
function setBudgetTerm(term){
  budgetTerm=term;lsSet('tb-budget-term',term);
  syncSettingsUI();renderBudget();
}
function setBudget(){
  var d2=$('#budgetDailyInput2'),m2=$('#budgetMonthlyInput2');
  budgetDaily=d2?parseFloat(d2.value)||0:budgetDaily;
  budgetMonthly=m2?parseFloat(m2.value)||0:budgetMonthly;
  lsSet('tb-budget-daily',String(budgetDaily));
  lsSet('tb-budget-monthly',String(budgetMonthly));
  syncBudgetInputs();checkBudget();renderBudget();
}
function setBudgetFromForm(){setBudget()}
function resetBudget(){
  budgetDaily=0;budgetMonthly=0;
  lsSet('tb-budget-daily','0');lsSet('tb-budget-monthly','0');
  budgetAppModels={};budgetModelModels={};
  lsSet('tb-budget-app','{}');lsSet('tb-budget-model','{}');
  syncBudgetInputs();checkBudget();renderBudget();
}
function _loadBudgetMaps(){
  try{budgetAppModels=JSON.parse(localStorage.getItem('tb-budget-app')||'{}')}catch(e){budgetAppModels={}}
  try{budgetModelModels=JSON.parse(localStorage.getItem('tb-budget-model')||'{}')}catch(e){budgetModelModels={}}
}
function _saveAppBudget(name,val){
  if(val>0)budgetAppModels[name]=val;else delete budgetAppModels[name];
  lsSet('tb-budget-app',JSON.stringify(budgetAppModels));_refreshBudgetDist();checkBudget();
}
function _saveModelBudget(name,val){
  if(val>0)budgetModelModels[name]=val;else delete budgetModelModels[name];
  lsSet('tb-budget-model',JSON.stringify(budgetModelModels));_refreshBudgetDist();checkBudget();
}
function _isEditingBudgetInput(){
  var el=document.activeElement;
  return el&&el.tagName==='INPUT'&&el.type==='number'&&(el.closest('#tBudgetAppDetail')||el.closest('#tBudgetModelDetail'));
}
function _refreshBudgetDist(){
  var _d=_fullDataAll||fullData;if(!_d)return;
  var isToken=budgetMode==='token';var valKey=isToken?'total':'cost';
  var fmtVal=function(v){return isToken?fmt(v):fmtC(v)};
  _loadBudgetMaps();
  var sortFn=function(a,b){return(b[1][valKey]||0)-(a[1][valKey]||0)};
  var modelEntries=Object.entries(_d.by_model||{}).sort(sortFn);
  var totalM=modelEntries.reduce(function(s,e){return s+(e[1][valKey]||0)},0)||1;
  var appEntries=Object.entries(_d.by_app||{}).sort(sortFn);
  var totalA=appEntries.reduce(function(s,e){return s+(e[1][valKey]||0)},0)||1;
  var rateLabel=lang==='zh'?'使用率':'Usage';var ratioLabel=lang==='zh'?'占比':'Ratio';
  var th='<tr><th>'+t('thModel')+'</th><th>'+(isToken?t('thTotal'):t('thCost'))+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
  th+=modelEntries.map(function(e){var v=e[1][valKey]||0;var ratio=v/totalM*100;var limit=budgetModelModels[e[0]]||0;var rate=limit>0?v/limit*100:0;return'<tr><td>'+e[0]+'</td><td>'+fmtVal(v)+'</td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>'}).join('');
  $('#tBudgetModel').innerHTML=th;
  var th2='<tr><th>'+t('thApp')+'</th><th>'+(isToken?t('thTotal'):t('thCost'))+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
  th2+=appEntries.map(function(e){var v=e[1][valKey]||0;var ratio=v/totalA*100;var limit=budgetAppModels[e[0]]||0;var rate=limit>0?v/limit*100:0;return'<tr><td>'+e[0]+'</td><td>'+fmtVal(v)+'</td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>'}).join('');
  $('#tBudgetApp').innerHTML=th2;
}
var RING_CIRC=2*Math.PI*52;
function setRing(id,pct){
  var el=document.getElementById(id);if(!el)return;
  var p=Math.min(pct,100);
  el.style.strokeDashoffset=RING_CIRC*(1-p/100);
  if(pct>=100)el.style.stroke='var(--red)';
  else if(pct>=80)el.style.stroke='var(--amber)';
  else el.style.stroke='var(--green)';
}
function _localDateStr(){var d=new Date();return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)+'-'+('0'+d.getDate()).slice(-2)}
function _localMonthStr(){var d=new Date();return d.getFullYear()+'-'+('0'+(d.getMonth()+1)).slice(-2)}
function _bTodayTokens(){
  var today=_localDateStr();
  var t={cost:0,tokens:0};
  var dd=(_fullDataAll||fullData);
  ((dd&&dd.daily)||[]).forEach(function(d){
    if(d.date===today){t.cost=d.cost;t.tokens=d.total||0}
  });
  return t;
}
function _bMonthTokens(){
  var thisMonth=_localMonthStr();
  var t={cost:0,tokens:0};
  var dd=(_fullDataAll||fullData);
  ((dd&&dd.daily)||[]).forEach(function(d){
    if(d.date&&d.date.startsWith(thisMonth)){t.cost+=d.cost;t.tokens+=d.total||0}
  });
  return t;
}
function _pctBar(pct){
  var c=pct>=100?'var(--red)':pct>=80?'var(--amber)':'var(--green)';
  return'<div style="display:flex;align-items:center;gap:6px"><div style="flex:1;height:6px;background:var(--surface2);border-radius:3px;overflow:hidden"><div style="width:'+Math.min(pct,100)+'%;height:100%;background:'+c+';border-radius:3px;transition:width .8s var(--ease-elastic)"></div></div><span style="font-size:11px;font-weight:600;min-width:36px;text-align:right;color:'+c+'">'+Math.round(pct)+'%</span></div>';
}
function _fmtBudget(v,isToken){return isToken?fmt(v)+' tok':'$'+v.toFixed(2)}
function _budgetLabel(){return budgetTerm==='target'?t('budgetTermTarget'):t('budgetTermLimit')}
function renderBudget(){
  var _d=_fullDataAll||fullData;
  if(!_d)return;
  var isToken=budgetMode==='token';
  var cur=t(isToken?'budgetTokenUnit':'budgetPriceUnit');
  var today=_bTodayTokens(),month=_bMonthTokens();
  var todayVal=isToken?today.tokens:today.cost;
  var monthVal=isToken?month.tokens:month.cost;
  $('#budgetDailyAmount').textContent=_fmtBudget(todayVal,isToken);
  $('#budgetMonthlyAmount').textContent=_fmtBudget(monthVal,isToken);
  var cd=$('#budgetCurD'),cm=$('#budgetCurM');
  if(cd)cd.textContent=cur;if(cm)cm.textContent=cur;
  // Daily ring
  if(budgetDaily>0){
    var pct=todayVal/budgetDaily*100;
    $('#budgetDailyPct').textContent=Math.round(pct)+'%';
    setRing('budgetDailyRing',pct);
    var rem=budgetDaily-todayVal;
    var cls=pct>=100?'budget-status-over':pct>=80?'budget-status-warn':'budget-status-ok';
    $('#budgetDailySub').innerHTML='<span class="'+cls+'">'+(rem>=0?t('budgetRemaining')+': '+_fmtBudget(rem,isToken):t('budgetExceeded')+' '+_fmtBudget(Math.abs(rem),isToken))+'</span>';
    $('#budgetDailyPct').className='budget-ring-value '+cls;
  }else{
    $('#budgetDailyPct').textContent='--';setRing('budgetDailyRing',0);
    $('#budgetDailySub').innerHTML='<span style="color:var(--muted)">'+(lang==='zh'?'未设置'+_budgetLabel():_budgetLabel()+' not set')+'</span>';
    $('#budgetDailyPct').className='budget-ring-value';
  }
  // Monthly ring
  if(budgetMonthly>0){
    var pct=monthVal/budgetMonthly*100;
    $('#budgetMonthlyPct').textContent=Math.round(pct)+'%';
    setRing('budgetMonthlyRing',pct);
    var rem=budgetMonthly-monthVal;
    var cls=pct>=100?'budget-status-over':pct>=80?'budget-status-warn':'budget-status-ok';
    $('#budgetMonthlySub').innerHTML='<span class="'+cls+'">'+(rem>=0?t('budgetRemaining')+': '+_fmtBudget(rem,isToken):t('budgetExceeded')+' '+_fmtBudget(Math.abs(rem),isToken))+'</span>';
    $('#budgetMonthlyPct').className='budget-ring-value '+cls;
  }else{
    $('#budgetMonthlyPct').textContent='--';setRing('budgetMonthlyRing',0);
    $('#budgetMonthlySub').innerHTML='<span style="color:var(--muted)">'+(lang==='zh'?'未设置'+_budgetLabel():_budgetLabel()+' not set')+'</span>';
    $('#budgetMonthlyPct').className='budget-ring-value';
  }
  syncBudgetInputs();
  // Compute per-model and per-app data
  var valKey=isToken?'total':'cost';
  var fmtVal=function(v){return isToken?fmt(v):fmtC(v)};
  _loadBudgetMaps();
  var sortFn=function(a,b){return(b[1][valKey]||0)-(a[1][valKey]||0)};
  var modelEntries=Object.entries(_d.by_model||{}).sort(sortFn);
  var totalM=modelEntries.reduce(function(s,e){return s+(e[1][valKey]||0)},0)||1;
  var appEntries=Object.entries(_d.by_app||{}).sort(sortFn);
  var totalA=appEntries.reduce(function(s,e){return s+(e[1][valKey]||0)},0)||1;
  var rateLabel=lang==='zh'?'使用率':'Usage';
  var ratioLabel=lang==='zh'?'占比':'Ratio';
  // Model distribution: usage rate (vs limit) + ratio (vs total)
  var th='<tr><th>'+t('thModel')+'</th><th>'+(isToken?t('thTotal'):t('thCost'))+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
  th+=modelEntries.map(function(e){var v=e[1][valKey]||0;var ratio=v/totalM*100;var limit=budgetModelModels[e[0]]||0;var rate=limit>0?v/limit*100:0;return'<tr><td>'+e[0]+'</td><td>'+fmtVal(v)+'</td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>'}).join('');
  $('#tBudgetModel').innerHTML=th;
  // App distribution: usage rate (vs limit) + ratio (vs total)
  var th2='<tr><th>'+t('thApp')+'</th><th>'+(isToken?t('thTotal'):t('thCost'))+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
  th2+=appEntries.map(function(e){var v=e[1][valKey]||0;var ratio=v/totalA*100;var limit=budgetAppModels[e[0]]||0;var rate=limit>0?v/limit*100:0;return'<tr><td>'+e[0]+'</td><td>'+fmtVal(v)+'</td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>'}).join('');
  $('#tBudgetApp').innerHTML=th2;
  // Per-app/per-model budget detail tables — skip re-render if user is editing
  if(!_isEditingBudgetInput()){
    var appH='<tr><th>'+t('thApp')+'</th><th>'+(lang==='zh'?'已用':'Used')+'</th><th>'+_budgetLabel()+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
    appEntries.forEach(function(e){
      var name=e[0],val=e[1][valKey]||0,limit=budgetAppModels[name]||0;
      var rate=limit>0?val/limit*100:0;var ratio=val/totalA*100;
      appH+='<tr><td>'+name+'</td><td>'+fmtVal(val)+'</td><td><input type="number" min="0" step="1" value="'+limit+'" data-budget-type="app" data-budget-name="'+escH(name)+'" style="width:80px;background:var(--surface2);border:1px solid rgba(127,127,127,.15);color:var(--text);padding:3px 6px;border-radius:4px;font-size:11px;font-family:inherit"></td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>';
    });
    $('#tBudgetAppDetail').innerHTML=appH;
    var modH='<tr><th>'+t('thModel')+'</th><th>'+(lang==='zh'?'已用':'Used')+'</th><th>'+_budgetLabel()+'</th><th>'+rateLabel+'</th><th>'+ratioLabel+'</th></tr>';
    modelEntries.forEach(function(e){
      var name=e[0],val=e[1][valKey]||0,limit=budgetModelModels[name]||0;
      var rate=limit>0?val/limit*100:0;var ratio=val/totalM*100;
      modH+='<tr><td>'+name+'</td><td>'+fmtVal(val)+'</td><td><input type="number" min="0" step="1" value="'+limit+'" data-budget-type="model" data-budget-name="'+escH(name)+'" style="width:80px;background:var(--surface2);border:1px solid rgba(127,127,127,.15);color:var(--text);padding:3px 6px;border-radius:4px;font-size:11px;font-family:inherit"></td><td>'+(limit>0?_pctBar(rate):'<span style="color:var(--muted);font-size:11px">-</span>')+'</td><td>'+_pctBar(ratio)+'</td></tr>';
    });
    $('#tBudgetModelDetail').innerHTML=modH;
  }
}
function checkBudget(){
  var _d=_fullDataAll||fullData;
  if(!_d)return;
  var el=$('#budgetWarn');if(!el)return;
  _loadBudgetMaps();
  var isToken=budgetMode==='token';
  var today=_bTodayTokens(),month=_bMonthTokens();
  var todayVal=isToken?today.tokens:today.cost;
  var monthVal=isToken?month.tokens:month.cost;
  var valKey=isToken?'total':'cost';
  var msgs=[];
  if(budgetDaily>0){
    var pct=todayVal/budgetDaily*100;
    var label=isToken?fmt(Math.round(todayVal))+'/'+fmt(budgetDaily)+' tok':'$'+todayVal.toFixed(2)+'/'+budgetDaily;
    if(pct>=100)msgs.push({t:label,c:'budget-warn-100'});
    else if(pct>=80)msgs.push({t:label,c:'budget-warn-80'});
  }
  if(budgetMonthly>0){
    var pct=monthVal/budgetMonthly*100;
    var label=isToken?'M '+fmt(Math.round(monthVal))+'/'+fmt(budgetMonthly)+' tok':'M $'+monthVal.toFixed(2)+'/'+budgetMonthly;
    if(pct>=100)msgs.push({t:label,c:'budget-warn-100'});
    else if(pct>=80)msgs.push({t:label,c:'budget-warn-80'});
  }
  // Per-app budget warnings
  var appData=_d.by_app||{};
  Object.keys(budgetAppModels).forEach(function(name){
    var limit=budgetAppModels[name];if(!limit)return;
    var val=(appData[name]||{})[valKey]||0;
    var pct=val/limit*100;
    if(pct>=100)msgs.push({t:name+': '+_fmtBudget(val,isToken),c:'budget-warn-100'});
    else if(pct>=80)msgs.push({t:name+': '+_fmtBudget(val,isToken),c:'budget-warn-80'});
  });
  // Per-model budget warnings
  var modelData=_d.by_model||{};
  Object.keys(budgetModelModels).forEach(function(name){
    var limit=budgetModelModels[name];if(!limit)return;
    var val=(modelData[name]||{})[valKey]||0;
    var pct=val/limit*100;
    if(pct>=100)msgs.push({t:name+': '+_fmtBudget(val,isToken),c:'budget-warn-100'});
    else if(pct>=80)msgs.push({t:name+': '+_fmtBudget(val,isToken),c:'budget-warn-80'});
  });
  if(msgs.length){
    el.innerHTML=msgs.map(function(m){return'<span class="budget-warn '+m.c+'">'+m.t+'</span>'}).join('');
  }else{el.innerHTML=''}
}

// ── Forecast ──
function renderForecast(){
  if(!fullData||!fullData.daily||!fullData.daily.length)return;
  var el=$('#forecastPanel');if(!el)return;
  var dd=fullData.daily;
  var n=Math.min(dd.length,7);
  var recent=dd.slice(-n);
  var avgCost=recent.reduce(function(s,d){return s+d.cost},0)/n;
  var now=new Date();var daysInMonth=new Date(now.getFullYear(),now.getMonth()+1,0).getDate();
  var projCost=avgCost*daysInMonth;
  var html='<div style="font-size:24px;font-weight:700;color:var(--amber)">'+fmtC(projCost)+'</div>';
  html+='<div style="font-size:11px;color:var(--muted);margin-top:4px">'+t('projMonthCost')+'</div>';
  html+='<div style="font-size:10px;color:var(--muted);margin-top:2px">'+t('basedOn')+' '+n+'d avg: '+fmtC(avgCost)+'/day</div>';
  el.innerHTML=html;
}

// ── Efficiency ──
function renderEfficiency(){
  if(!fullData)return;
  var el=$('#efficiencyMetrics');if(!el)return;
  var s=fullData.summary;
  var totalMsg=s.total_records||1;
  var totalTokens=(s.total_input_full||s.total_input)+s.total_output;
  var tokPerMsg=Math.round(totalTokens/totalMsg);
  var costPerMsg=s.total_cost/totalMsg;
  var costPer1k=s.total_cost>0?s.total_cost/(totalTokens/1000):0;
  var html='<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">';
  html+='<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">'+t('tokPerMsg')+'</div><div style="font-size:20px;font-weight:700;color:var(--cyan);margin-top:4px">'+fmt(tokPerMsg)+'</div></div>';
  html+='<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">'+t('costPerMsg')+'</div><div style="font-size:20px;font-weight:700;color:var(--amber);margin-top:4px">'+fmtC(costPerMsg)+'</div></div>';
  html+='<div><div style="font-size:10px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px">'+t('costPer1k')+'</div><div style="font-size:20px;font-weight:700;color:var(--red);margin-top:4px">'+fmtC(costPer1k)+'</div></div>';
  html+='</div>';
  el.innerHTML=html;
}

// ── Model Ranking ──
function renderModelRanking(timeModelData,labelFn){
  if(!timeModelData||!timeModelData.length)return;
  // Flatten: each model+timePeriod as separate entry, sort by total desc
  var entries=[];
  var modelColors={};
  var colorPool=['#818cf8','#22d3ee','#4ade80','#fbbf24','#f87171','#c084fc','#f472b6','#60a5fa','#34d399','#fb923c'];
  var ci=0;
  timeModelData.forEach(function(d){
    var periodLabel=labelFn(d);
    Object.keys(d.models).forEach(function(m){
      if(!modelColors[m]){modelColors[m]=colorPool[ci%colorPool.length];ci++}
      var total=d.models[m].total||0;
      if(total>0)entries.push({label:m+' '+periodLabel,total:total,color:modelColors[m]});
    });
  });
  entries.sort(function(a,b){return b.total-a.total});
  if(!entries.length)return;
  var labels=entries.map(function(e){return e.label});
  var data=entries.map(function(e){return e.total});
  var bg=entries.map(function(e){return e.color+'cc'});
  kill('cModelRanking');
  var ctx=document.getElementById('cModelRanking');if(!ctx)return;
  // Dynamic height based on entry count
  var h=Math.max(200,entries.length*22+40);
  var box=ctx.parentElement;if(box)box.style.height=h+'px';
  charts['cModelRanking']=new Chart(ctx,{type:'bar',data:{labels:labels,datasets:[{data:data,backgroundColor:bg,borderRadius:3}]},
    options:{
      indexAxis:'y',responsive:true,maintainAspectRatio:false,
      animation:_anim(600,function(c){return c.dataIndex*30}),
      plugins:{legend:{display:false},tooltip:{callbacks:{label:function(c){return fmt(c.raw)+' tokens'}}}},
      scales:{x:{ticks:{color:'#71717a',font:{size:9},callback:function(v){return fmt(v)}},grid:{color:'rgba(63,63,70,.3)'}},
              y:{ticks:{color:'#a1a1aa',font:{size:10},padding:4},grid:{display:false}}}
    }
  });
}

// ── Heatmap ──
function renderHeatmap(start,end){
  var canvas=document.getElementById('cHeatmap');
  if(!canvas)return;
  var ctx=canvas.getContext('2d');if(!ctx)return;
  var dpr=window.devicePixelRatio||1;
  var cw=600,ch=180;
  canvas.width=cw*dpr;canvas.height=ch*dpr;
  canvas.style.width=cw+'px';canvas.style.height=ch+'px';
  ctx.scale(dpr,dpr);
  ctx.clearRect(0,0,cw,ch);
  ctx.font='10px Inter,sans-serif';ctx.fillStyle='#71717a';
  ctx.fillText(t('loading'),cw/2-20,ch/2);
  pywebview.api.get_heatmap(start||'',end||'').then(function(r){
    var resp=JSON.parse(r);
    var data=resp.heatmap||resp;
    var labels=resp.labels||[];
    ctx.setTransform(1,0,0,1,0,0);
    ctx.scale(dpr,dpr);
    ctx.clearRect(0,0,cw,ch);
    if(!Array.isArray(data)||data.length===0){ctx.font='11px Inter,sans-serif';ctx.fillStyle='#71717a';ctx.fillText(t('noData'),cw/2-20,ch/2);return}
    var numRows=data.length;
    var maxVal=0;
    for(var d=0;d<numRows;d++){if(data[d])for(var h=0;h<24;h++){var v=data[d][h]||0;if(v>maxVal)maxVal=v}}
    if(maxVal===0){ctx.font='12px Inter,sans-serif';ctx.fillStyle='#71717a';ctx.fillText(t('noData'),cw/2-20,ch/2);return}
    var cellW=22,cellH=20,padL=labels.length?42:28,padT=22;
    var rowLabels=labels.length?labels:[t('mo'),t('tu'),t('we'),t('th'),t('fr'),t('sa'),t('su')];
    // Resize canvas if needed
    var neededH=padT+numRows*cellH+4;
    if(neededH>ch){
      ch=neededH;
      canvas.height=ch*dpr;canvas.style.height=ch+'px';
      ctx.setTransform(1,0,0,1,0,0);ctx.scale(dpr,dpr);ctx.clearRect(0,0,cw,ch);
    }
    ctx.font='9px Inter,sans-serif';ctx.fillStyle='#71717a';
    for(var h=0;h<24;h++){ctx.fillText(h+'',padL+h*cellW+2,14)}
    for(var d=0;d<numRows;d++){
      var lbl=rowLabels[d]||'';
      if(labels.length)lbl=lbl.slice(5); // MM-DD for date labels
      ctx.fillText(lbl,2,padT+d*cellH+14)
    }
    // Pre-compute 100 color buckets
    var colorBuckets=[];
    for(var i=0;i<=100;i++){
      var r2=Math.round(30+i*0.99);var g2=Math.round(27+i*1.13);var b2=Math.round(75+i*1.73);
      colorBuckets[i]='rgb('+r2+','+g2+','+b2+')';
    }
    for(var d=0;d<numRows;d++){
      for(var h=0;h<24;h++){
        var v=data[d][h];
        var bucket=Math.round(maxVal>0?v/maxVal*100:0);
        ctx.fillStyle=colorBuckets[bucket];
        ctx.fillRect(padL+h*cellW,padT+d*cellH,cellW-2,cellH-2);
      }
    }
  }).catch(function(e){console.error('Heatmap error:',e);try{ctx.setTransform(1,0,0,1,0,0);ctx.scale(dpr,dpr);ctx.clearRect(0,0,cw,ch);ctx.font='11px Inter,sans-serif';ctx.fillStyle='#f87171';ctx.fillText('Error: '+e,cw/2-40,ch/2)}catch(ex){}});
}

// ── Model Trends ──
function renderModelTrends(){
  pywebview.api.get_model_daily().then(function(r){
    var data=JSON.parse(r);
    if(!data.length)return;
    var models={};
    data.forEach(function(d){Object.keys(d.models).forEach(function(m){if(!models[m])models[m]=true})});
    var modelNames=Object.keys(models);
    var labels=data.map(function(d){return d.date.slice(5)});
    var colors=['#818cf8','#22d3ee','#4ade80','#fbbf24','#f87171','#c084fc','#f472b6','#60a5fa','#34d399','#fb923c'];
    var datasets=modelNames.map(function(m,i){
      return{label:m,data:data.map(function(d){return d.models[m]?d.models[m].input+d.models[m].output+d.models[m].cache_read:0}),
             borderColor:colors[i%colors.length],backgroundColor:colors[i%colors.length]+'22',fill:true,tension:.3,borderWidth:1.5}
    });
    line('cModelTrends',labels,datasets);
  }).catch(function(e){console.error('model trends error:',e)});
}

// ── Export ──
function exportCSV(section){
  var csv='',rows=[];
  if(section==='overview'){
    if(!fullData)return;
    var s=fullData.summary;
    csv='Metric,Value\n';
    csv+='Records,'+s.total_records+'\n';
    csv+='Input Tokens,'+(s.total_input_full||s.total_input)+'\n';
    csv+='Output Tokens,'+s.total_output+'\n';
    csv+='Cache Read,'+s.total_cache_read+'\n';
    csv+='Estimated Cost,'+s.total_cost.toFixed(4)+'\n';
    csv+='Cache Rate,'+s.cache_rate+'%\n';
  }else if(section==='daily'){
    rows=extractTableRows('#tDaily');
  }else if(section==='projects'){
    rows=extractTableRows('#tProject');
  }else if(section==='sessions'){
    rows=extractTableRows('#tSession');
  }else if(section==='compare'){
    rows=extractTableRows('#tC1');
    var r2=extractTableRows('#tC2');
    if(r2.length>1){rows.push(['']);rows.push(['--- Period B ---']);rows=rows.concat(r2.slice(1))}
  }
  if(rows.length>0){
    csv=rows.map(function(r){return r.join(',')}).join('\n');
  }
  downloadFile('tokenbank_'+section+'_'+new Date().toISOString().slice(0,10)+'.csv',csv,'text/csv');
}
function extractTableRows(sel){
  var table=$(sel);if(!table)return[];
  var rows=[];
  table.querySelectorAll('thead tr').forEach(function(tr){
    var cols=[];tr.querySelectorAll('th').forEach(function(th){cols.push('"'+th.textContent.trim()+'"')});rows.push(cols);
  });
  table.querySelectorAll('tbody tr').forEach(function(tr){
    if(tr.classList.contains('sess-detail'))return;
    var cols=[];tr.querySelectorAll('td').forEach(function(td){cols.push('"'+td.textContent.trim()+'"')});rows.push(cols);
  });
  return rows;
}
function downloadFile(name,content,type){
  var blob=new Blob([content],{type:type});
  var url=URL.createObjectURL(blob);
  var a=document.createElement('a');a.href=url;a.download=name;a.click();
  setTimeout(function(){URL.revokeObjectURL(url)},1000);
}

// ── Report ──
function genReport(days){
  if(!fullData)return;
  var dd=fullData.daily;
  var filtered=dd;
  if(days>0&&dd.length>days)filtered=dd.slice(-days);
  var totalIn=0,totalOut=0,totalCache=0,totalCost=0,totalMsg=0;
  filtered.forEach(function(d){totalIn+=d.input;totalOut+=d.output;totalCache+=d.cache_read;totalCost+=d.cost;totalMsg+=d.count});
  var periodLabel=days>0?days+' days':'All time';
  if(filtered.length>0)periodLabel=filtered[0].date+' ~ '+filtered[filtered.length-1].date;
  var numDays=filtered.length||1;
  var lines=[];
  lines.push('=== '+t('reportTitle')+' ===');
  lines.push('');
  lines.push(t('reportPeriod')+': '+periodLabel);
  lines.push('');
  lines.push('--- '+t('reportTotal')+' ---');
  lines.push(t('sRecords')+': '+totalMsg);
  lines.push(t('sInput')+': '+fmt(totalIn));
  lines.push(t('sOutput')+': '+fmt(totalOut));
  lines.push(t('sCache')+': '+fmt(totalCache));
  lines.push(t('sCost')+': '+fmtC(totalCost));
  lines.push('');
  lines.push('--- '+t('reportAvg')+' ---');
  lines.push(t('sRecords')+'/'+t('daily')+': '+Math.round(totalMsg/numDays));
  lines.push(t('sInput')+'/'+t('daily')+': '+fmt(Math.round(totalIn/numDays)));
  lines.push(t('sCost')+'/'+t('daily')+': '+fmtC(totalCost/numDays));
  lines.push('');
  lines.push('--- '+t('reportTopModels')+' ---');
  var models=Object.entries(fullData.by_model).sort(function(a,b){return b[1].input+b[1].output-a[1].input-a[1].output}).slice(0,5);
  models.forEach(function(m){lines.push('  '+m[0]+': '+fmt(m[1].input+m[1].output)+' tok, '+fmtC(m[1].cost))});
  lines.push('');
  lines.push('--- '+t('reportTopProjects')+' ---');
  fullData.projects.slice(0,5).forEach(function(p){lines.push('  '+p.name+': '+fmt(p.total)+' tok, '+fmtC(p.cost))});
  $('#reportText').textContent=lines.join('\n');
}
function copyReport(){
  var text=$('#reportText').textContent;
  if(!text)return;
  navigator.clipboard.writeText(text).catch(function(){
    var ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');document.body.removeChild(ta);
  });
}

// ── Session Detail ──
var _openDetail=null;
function toggleSessionDetail(sessionId,rowEl){
  document.querySelectorAll('.sess-detail.open').forEach(function(el){el.classList.remove('open')});
  if(_openDetail===sessionId){_openDetail=null;return}
  var detailRow=rowEl.nextElementSibling;
  if(!detailRow||!detailRow.classList.contains('sess-detail'))return;
  _openDetail=sessionId;
  if(detailRow.dataset.loaded){
    detailRow.classList.add('open');return;
  }
  pywebview.api.get_session_detail(sessionId).then(function(r){
    if(_openDetail!==sessionId)return;
    var d=JSON.parse(r);
    var html='<div class="sess-detail-inner">';
    d.messages.forEach(function(m){
      var role=m.type==='user'?'user':m.type==='assistant'?'assistant':'system';
      var roleLabel=role.toUpperCase();
      var body='';
      if(m.text)body=escH(m.text);
      else if(m.type==='assistant')body='['+escH(m.model||'')+'] in:'+fmt(m.input)+' out:'+fmt(m.output)+' cache:'+fmt(m.cache_read)+' $'+(m.cost||0).toFixed(4);
      else if(m.type==='token')body='in:'+fmt(m.input)+' out:'+fmt(m.output)+' cache:'+fmt(m.cache_read);
      var ts=m.ts?m.ts.replace('T',' ').slice(0,19):'';
      html+='<div class="sess-msg"><span class="role role-'+role+'">'+roleLabel+'</span><span class="body">'+body+'</span><span class="meta">'+escH(ts)+'</span></div>';
    });
    if(!d.messages.length)html+='<div style="color:var(--muted);font-size:11px">'+t('noData')+'</div>';
    html+='</div>';
    detailRow.querySelector('td').innerHTML=html;
    detailRow.dataset.loaded='1';
    detailRow.classList.add('open');
    _openDetail=sessionId;
  });
}

// ── Search/Filter ──
function renderSessionTable(sessions){
  const sessR=sessions.map(s=>{
    const sm=s.summary?s.summary.replace(/^[\s﻿\xA0]+|[\s﻿\xA0]+$/g,''):'';
    const label=sm?truncW(sm,80):s.id.substring(0,16)+'...';
    return[fmt(s.total),badge(s.app),s.model,s.date,fmt(s.count),fmt(s.input),fmt(s.output),fmtC(s.cost),label]
  });
  mkT($('#tSession'),[t('thTotal'),t('thApp'),t('thModel'),t('thDate'),t('thMsgs'),t('thInput'),t('thOutput'),t('thCost'),t('thSession')],sessR);
  _allSessionRows=[];
  var apps={},models={};
  var tbody=$('#tSession').querySelector('tbody');
  if(tbody){
    var trs=tbody.querySelectorAll('tr');
    sessions.forEach(function(s,i){
      var tr=trs[i];if(!tr)return;
      tr.classList.add('sess-detail-row');
      tr.style.cursor='pointer';
      tr.setAttribute('onclick','toggleSessionDetail("'+s.id+'",this)');
      var detailTr=document.createElement('tr');
      detailTr.className='sess-detail';detailTr.dataset.id=s.id;
      detailTr.innerHTML='<td colspan="9"></td>';
      tr.parentNode.insertBefore(detailTr,tr.nextSibling);
      var txt=(s.summary||'')+' '+s.id+' '+s.model+' '+s.app;
      _allSessionRows.push({tr:tr,app:s.app,model:s.model,text:txt.toLowerCase()});
      apps[s.app]=true;models[s.model]=true;
    });
  }
  var appSel=$('#sessAppFilter');if(appSel){
    var val=appSel.value;
    appSel.innerHTML='<option value="">'+(lang==='zh'?'全部应用':'All Apps')+'</option>'+Object.keys(apps).map(function(a){return'<option value="'+a+'">'+a.toUpperCase()+'</option>'}).join('');
    appSel.value=val;
  }
  var modSel=$('#sessModelFilter');if(modSel){
    var val=modSel.value;
    modSel.innerHTML='<option value="">'+(lang==='zh'?'全部模型':'All Models')+'</option>'+Object.keys(models).map(function(m){return'<option value="'+m+'">'+m+'</option>'}).join('');
    modSel.value=val;
  }
}
var _allSessionRows=[];
var _filterTimer=null;
function applySessionFilter(){
  var s=$('#sessStart').value,e=$('#sessEnd').value;
  if(!s||!e)return;
  pywebview.api.get_sessions_filtered(s,e).then(function(r){
    var d=JSON.parse(r);
    renderSessionTable(d.sessions);
  }).catch(function(e){console.error('session filter error:',e)});
}
function presetSession(n){
  if(!allDates.length)return;
  var end=allDates[allDates.length-1];
  if(n===0){$('#sessStart').value=allDates[0];$('#sessEnd').value=end}
  else{var si=Math.max(0,allDates.length-n);$('#sessStart').value=allDates[si];$('#sessEnd').value=end}
  applySessionFilter();
}
function filterSessions(){
  if(_filterTimer)clearTimeout(_filterTimer);
  _filterTimer=setTimeout(function(){
    var q=($('#sessSearch').value||'').toLowerCase();
    var appF=$('#sessAppFilter').value;
    var modelF=$('#sessModelFilter').value;
    _allSessionRows.forEach(function(item){
      var show=true;
      if(q&&item.text.indexOf(q)<0)show=false;
      if(appF&&item.app!==appF)show=false;
      if(modelF&&item.model!==modelF)show=false;
      item.tr.style.display=show?'':'none';
      var detail=item.tr.nextElementSibling;
      if(detail&&detail.classList.contains('sess-detail'))detail.style.display=show?'':'none';
    });
  },150);
}

// ── Mini Mode ──
var _miniMode=false;
function toggleMini(){
  _miniMode=!_miniMode;
  document.body.classList.toggle('mini',_miniMode);
  pywebview.api.toggle_mini(_miniMode?'1':'0');
  if(_miniMode)renderMiniCards();
  else{setTimeout(function(){window.dispatchEvent(new Event('resize'));renderBudget()},100)}
}
function renderMiniCards(){
  if(!fullData)return;
  var s=fullData.summary;
  var totalIn=s.total_input_full||s.total_input;
  $('#miniCards').innerHTML=[
    {l:t('sInput'),v:fmt(totalIn),cls:'c-cyan'},
    {l:t('sOutput'),v:fmt(s.total_output),cls:'c-green'},
    {l:t('sCost'),v:fmtC(s.total_cost),cls:'c-red'},
  ].map(function(c){return'<div class="mini-card"><div class="label">'+c.l+'</div><div class="value '+c.cls+'">'+c.v+'</div></div>'}).join('');
}

// ── Load ──
function _removeOverlay(){var o=$('#loadingOverlay');if(o){o.classList.add('fade-out');setTimeout(function(){if(o.parentNode)o.remove()},1500)}}
function reload(){
  $('#status').textContent=t('loading');
  pywebview.api.reload().then(function(r){
    var d=JSON.parse(r);fullData=d;_fullDataAll=d;
    try{render(d)}catch(e){console.error('render error:',e)}
    try{renderModelTrends()}catch(e){console.error('trends error:',e)}
    try{checkBudget()}catch(e){console.error('budget error:',e)}
    try{renderBudget()}catch(e){console.error('budget render error:',e)}
    if(d.daily&&d.daily.length){
      allDates=d.daily.map(function(x){return x.date});
      $('#dStart').value=d.daily[0].date;$('#dEnd').value=d.daily[d.daily.length-1].date;
      var ss=$('#sessStart'),se=$('#sessEnd');if(ss)ss.value=d.daily[0].date;if(se)se.value=d.daily[d.daily.length-1].date;
    }
    if(allDates.length>=14){
      var mid=Math.floor(allDates.length/2);
      $('#c1s').value=allDates[mid];$('#c1e').value=allDates[allDates.length-1];
      $('#c2s').value=allDates[0];$('#c2e').value=allDates[mid-1];
    }
    // Default to 1D view
    try{if(allDates.length){presetDaily(1)}else{renderDaily(d)}}catch(e){console.error('daily render error:',e)}
    $('#status').textContent=t('loaded')+' '+d.summary.total_records+' '+t('loadedSuff');
    startAutoRefresh();
    _removeOverlay();
  }).catch(function(e){
    console.error('Load error:',e);
    $('#status').textContent='Error: '+e;
    _removeOverlay();
  });
}

window.addEventListener('resize',function(){
  if(typeof Chart!=='undefined'){Chart.instances&&Object.values(Chart.instances).forEach(function(c){try{c.resize()}catch(e){}})}
});
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
