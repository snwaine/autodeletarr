import os
import json
import signal
import uuid
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages, send_from_directory
)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

APP_DIR = Path(__file__).resolve().parent
APP_LOGO_DIR = APP_DIR / "logo"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mediareaparr-secret")


# ----------------------------
# Utils
# ----------------------------
def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clamp_int(v, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_html(s: Any) -> str:
    return html_escape(str(s or ""), quote=True)


def make_job_id() -> str:
    return uuid.uuid4().hex[:10]


def make_app_id() -> str:
    return uuid.uuid4().hex[:10]


def checkbox(name: str) -> bool:
    return request.form.get(name) == "on"


def cron_from_day_hour(day_key: str, hour: int) -> str:
    hour = clamp_int(hour, 0, 23, 3)
    dow_map = {
        "daily": "*",
        "sun": "0",
        "mon": "1",
        "tue": "2",
        "wed": "3",
        "thu": "4",
        "fri": "5",
        "sat": "6",
    }
    dow = dow_map.get((day_key or "daily").lower(), "*")
    return f"15 {hour} * * {dow}"


def schedule_label(day_key: str, hour: int) -> str:
    day_key = (day_key or "daily").lower()
    names = {
        "daily": "Daily",
        "mon": "Monday",
        "tue": "Tuesday",
        "wed": "Wednesday",
        "thu": "Thursday",
        "fri": "Friday",
        "sat": "Saturday",
        "sun": "Sunday",
    }
    day_txt = names.get(day_key, "Daily")
    h = clamp_int(hour, 0, 23, 3)
    return f"{day_txt} • {h:02d}:00"


def parse_iso_date(s: str):
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


# ----------------------------
# Sonarr delete modes
# ----------------------------
SONARR_DELETE_MODES = [
    "episodes_only",
    "episodes_then_series_if_empty",
    "series_whole",
]

SONARR_DELETE_MODE_LABELS = {
    "episodes_only": "Episodes only",
    "episodes_then_series_if_empty": "Episodes → remove empty series",
    "series_whole": "Whole series",
}


def sonarr_delete_mode_label(mode: str) -> str:
    mode = (mode or "").strip()
    return SONARR_DELETE_MODE_LABELS.get(mode, SONARR_DELETE_MODE_LABELS["episodes_only"])


# ----------------------------
# Apps (dynamic list)
# ----------------------------
def app_defaults() -> Dict[str, Any]:
    return {
        "id": make_app_id(),
        "type": "radarr",        # radarr|sonarr
        "name": "New App",
        "enabled": True,
        "url": "",
        "api_key": "",
        "ok": False,
        "created_at": now_iso(),
    }


def normalize_app(a: Dict[str, Any]) -> Dict[str, Any]:
    d = app_defaults()
    d.update(a or {})
    d["id"] = str(d.get("id") or make_app_id())

    t = str(d.get("type") or "radarr").strip().lower()
    if t not in ("radarr", "sonarr"):
        t = "radarr"
    d["type"] = t

    default_name = "Radarr" if t == "radarr" else "Sonarr"
    d["name"] = str(d.get("name") or default_name).strip()[:60] or default_name

    d["enabled"] = bool(d.get("enabled", True))
    d["url"] = str(d.get("url") or "").strip().rstrip("/")
    d["api_key"] = str(d.get("api_key") or "").strip()
    d["ok"] = bool(d.get("ok", False))
    d["created_at"] = str(d.get("created_at") or now_iso())
    return d


def find_app(cfg: Dict[str, Any], app_id: str) -> Optional[Dict[str, Any]]:
    app_id = str(app_id or "").strip()
    if not app_id:
        return None
    for a in (cfg.get("APPS") or []):
        if str(a.get("id")) == app_id:
            return normalize_app(a)
    return None


def migrate_legacy_apps(cfg: Dict[str, Any]) -> None:
    """
    Backward compatibility: if old RADARR_* / SONARR_* exist and APPS is empty,
    migrate them into APPS.
    """
    apps = cfg.get("APPS") or []
    if isinstance(apps, list) and len(apps) > 0:
        return

    migrated: List[Dict[str, Any]] = []

    r_url = str(cfg.get("RADARR_URL") or "").strip().rstrip("/")
    r_key = str(cfg.get("RADARR_API_KEY") or "").strip()
    r_enabled = bool(cfg.get("RADARR_ENABLED", True))
    r_ok = bool(cfg.get("RADARR_OK", False))
    if r_url or r_key:
        migrated.append(normalize_app({
            "type": "radarr",
            "name": "Radarr",
            "enabled": r_enabled,
            "url": r_url,
            "api_key": r_key,
            "ok": r_ok,
        }))

    s_url = str(cfg.get("SONARR_URL") or "").strip().rstrip("/")
    s_key = str(cfg.get("SONARR_API_KEY") or "").strip()
    s_enabled = bool(cfg.get("SONARR_ENABLED", False))
    s_ok = bool(cfg.get("SONARR_OK", False))
    if s_url or s_key:
        migrated.append(normalize_app({
            "type": "sonarr",
            "name": "Sonarr",
            "enabled": s_enabled,
            "url": s_url,
            "api_key": s_key,
            "ok": s_ok,
        }))

    cfg["APPS"] = migrated


def is_app_ready(cfg: Dict[str, Any], app_id: str) -> bool:
    a = find_app(cfg, app_id)
    if not a:
        return False
    return bool(a.get("enabled") and a.get("url") and a.get("api_key") and a.get("ok"))


# ----------------------------
# Jobs
# ----------------------------
def job_defaults() -> Dict[str, Any]:
    return {
        "id": make_job_id(),
        "name": "New Job",
        "enabled": True,
        "APP_ID": "",
        "TAG_LABEL": "",
        "DAYS_OLD": 30,
        "SCHED_DAY": "daily",
        "SCHED_HOUR": 3,
        "DRY_RUN": True,
        "DELETE_FILES": True,
        "ADD_IMPORT_EXCLUSION": False,
        "SONARR_DELETE_MODE": "episodes_only",
    }


def normalize_job(j: Dict[str, Any]) -> Dict[str, Any]:
    d = job_defaults()
    d.update(j or {})

    d["id"] = str(d.get("id") or make_job_id())
    d["name"] = str(d.get("name") or "Job").strip()[:60] or "Job"
    d["enabled"] = bool(d.get("enabled", True))

    d["APP_ID"] = str(d.get("APP_ID") or "").strip()
    d["TAG_LABEL"] = str(d.get("TAG_LABEL") or "").strip()
    d["DAYS_OLD"] = clamp_int(d.get("DAYS_OLD", 30), 1, 36500, 30)

    d["SCHED_DAY"] = str(d.get("SCHED_DAY") or "daily").lower()
    if d["SCHED_DAY"] not in ("daily", "mon", "tue", "wed", "thu", "fri", "sat", "sun"):
        d["SCHED_DAY"] = "daily"
    d["SCHED_HOUR"] = clamp_int(d.get("SCHED_HOUR", 3), 0, 23, 3)

    d["DRY_RUN"] = bool(d.get("DRY_RUN", True))
    d["DELETE_FILES"] = bool(d.get("DELETE_FILES", True))
    d["ADD_IMPORT_EXCLUSION"] = bool(d.get("ADD_IMPORT_EXCLUSION", False))

    mode = str(d.get("SONARR_DELETE_MODE") or "episodes_only").strip()
    if mode not in SONARR_DELETE_MODES:
        mode = "episodes_only"
    d["SONARR_DELETE_MODE"] = mode

    return d


def find_job(cfg: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    job_id = str(job_id or "").strip()
    if not job_id:
        return None
    for j in (cfg.get("JOBS") or []):
        if str(j.get("id")) == job_id:
            return normalize_job(j)
    return None


# ----------------------------
# Run now modal + button
# ----------------------------
def run_now_modal_html() -> str:
    return """
    <div class="modalBack" id="runNowBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="runNowTitle">
        <div class="mh">
          <h3 id="runNowTitle">Run Now confirmation</h3>
        </div>
        <div class="mb">
          <div style="margin-bottom:10px;">
            <div class="muted">App: <b><span id="rn_app">App</span></b></div>
            <div class="muted">Dry Run: <b><span id="rn_dry">OFF</span></b> • Delete Files: <b><span id="rn_del">ON</span></b> • Job: <b><span id="rn_enabled">Enabled</span></b></div>
          </div>

          <p><b id="rn_msg">Dry Run is OFF — this will perform real actions.</b></p>

          <p id="rn_hint_delete" class="muted">
            With <b>Delete Files</b> enabled, it may delete files from disk via the app.
          </p>

          <p id="rn_hint_no_delete" class="muted" style="display:none;">
            With <b>Delete Files</b> disabled, it should avoid deleting from disk.
          </p>

          <p class="muted">If you’re not sure, edit the job and enable <b>Dry Run</b>, then use Preview.</p>
        </div>
        <div class="mf">
          <button class="btn" type="button" onclick="hideModal('runNowBack')">Cancel</button>
          <form id="runNowFormConfirm" method="post" action="/jobs/run-now" style="margin:0;">
            <input type="hidden" id="runNowJobId" name="job_id" value="">
            <button class="btn bad" type="button" onclick="runNowSubmitConfirm()">Yes, run now</button>
          </form>
        </div>
      </div>
    </div>
    """


def run_now_button_html(job: Dict[str, Any], app_label: str = "App") -> str:
    job = normalize_job(job)
    if not job["enabled"]:
        return '<button class="btn" type="button" disabled title="Enable this job to run now">Run Now</button>'

    jid = safe_html(job["id"])
    delete_files = str(bool(job.get("DELETE_FILES", True))).lower()
    enabled = str(bool(job.get("enabled", True))).lower()
    app_lbl = safe_html(app_label)

    if job.get("DRY_RUN", True):
        return f"""
          <form method="post" action="/jobs/run-now" style="margin:0;">
            <input type="hidden" name="job_id" value="{jid}">
            <button class="btn good" type="submit">Run Now</button>
          </form>
        """

    return f"""
      <button class="btn bad" type="button"
        onclick="openRunNowConfirm('{jid}', {{
          appLabel: '{app_lbl}',
          dryRun: false,
          deleteFiles: {delete_files},
          enabled: {enabled}
        }})">Run Now</button>
    """


# ----------------------------
# Config/state
# ----------------------------
def load_config() -> Dict[str, Any]:
    # Include legacy keys so migration can read them if present.
    cfg = {
        # Dynamic apps list
        "APPS": [],

        # Legacy (only for migration/backward compat)
        "RADARR_URL": env_default("RADARR_URL", "http://radarr:7878").rstrip("/"),
        "RADARR_API_KEY": env_default("RADARR_API_KEY", ""),
        "RADARR_ENABLED": True,
        "RADARR_OK": False,

        "SONARR_URL": env_default("SONARR_URL", "").rstrip("/"),
        "SONARR_API_KEY": env_default("SONARR_API_KEY", ""),
        "SONARR_ENABLED": False,
        "SONARR_OK": False,

        # WebUI/global
        "HTTP_TIMEOUT_SECONDS": int(env_default("HTTP_TIMEOUT_SECONDS", "30")),
        "UI_THEME": env_default("UI_THEME", "dark"),
        "UI_SCALE": float(env_default("UI_SCALE", "1.0")),

        # Jobs
        "JOBS": [],
    }

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            # Update only known keys (but legacy keys are included above)
            for k in list(cfg.keys()):
                if k in data:
                    cfg[k] = data[k]
        except Exception:
            pass

    # Normalize theme/scale/timeout
    t = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = t if t in ("dark", "light", "reaparr") else "dark"
    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 30), 5, 300, 30)
    try:
        cfg["UI_SCALE"] = float(cfg.get("UI_SCALE", 1.0))
    except Exception:
        cfg["UI_SCALE"] = 1.0
    cfg["UI_SCALE"] = max(0.75, min(1.5, cfg["UI_SCALE"]))

    # Migrate legacy fixed config into APPS if needed
    migrate_legacy_apps(cfg)

    apps = cfg.get("APPS") or []
    if not isinstance(apps, list):
        apps = []
    cfg["APPS"] = [normalize_app(a) for a in apps]

    # Normalize jobs
    jobs = cfg.get("JOBS") or []
    if not isinstance(jobs, list):
        jobs = []
    jobs = [normalize_job(j) for j in jobs]
    if not jobs:
        j = job_defaults()
        j["name"] = "Default Job"
        jobs = [normalize_job(j)]
    cfg["JOBS"] = jobs

    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


# ----------------------------
# API helpers
# ----------------------------
def api_get(base_url: str, api_key: str, timeout_s: int, path: str):
    url = (base_url or "").rstrip("/") + path
    r = requests.get(url, headers={"X-Api-Key": api_key or ""}, timeout=timeout_s)
    r.raise_for_status()
    return r.json()


def app_get(cfg: Dict[str, Any], app_obj: Dict[str, Any], path: str):
    return api_get(
        app_obj.get("url", ""),
        app_obj.get("api_key", ""),
        int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)),
        path
    )


def get_tag_labels(cfg: Dict[str, Any], app_id: str) -> List[str]:
    if not is_app_ready(cfg, app_id):
        return []
    app_obj = find_app(cfg, app_id)
    if not app_obj:
        return []
    tags = app_get(cfg, app_obj, "/api/v3/tag")
    return sorted({t.get("label") for t in (tags or []) if t.get("label")}, key=lambda x: str(x).lower())


# ----------------------------
# Preview candidates (uses selected app instance)
# ----------------------------
def preview_candidates_radarr(cfg: Dict[str, Any], app_obj: Dict[str, Any], job: Dict[str, Any]):
    if not app_obj.get("enabled", True):
        return {"error": "This Radarr app is disabled.", "candidates": [], "cutoff": ""}

    tag_label = (job.get("TAG_LABEL") or "").strip()
    if not tag_label:
        return {"error": "Tag is empty. Edit the job and select a tag.", "candidates": [], "cutoff": ""}

    days_old = int(job.get("DAYS_OLD", 30))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_old)

    tags = app_get(cfg, app_obj, "/api/v3/tag")
    tag = next((t for t in tags if t.get("label") == tag_label), None)
    if not tag:
        return {"error": f"Tag '{tag_label}' not found in Radarr.", "candidates": [], "cutoff": cutoff.isoformat()}

    tag_id = tag["id"]
    movies = app_get(cfg, app_obj, "/api/v3/movie")

    candidates = []
    for m in movies:
        if tag_id not in (m.get("tags") or []):
            continue
        added_str = m.get("added")
        added = parse_iso_date(added_str) if added_str else None
        if not added:
            continue
        if added < cutoff:
            age_days = int((now - added).total_seconds() // 86400)
            candidates.append({
                "kind": "movie",
                "id": m.get("id"),
                "title": m.get("title"),
                "year": m.get("year"),
                "added": added_str,
                "age_days": age_days,
                "path": m.get("path"),
            })

    candidates.sort(key=lambda x: x["age_days"], reverse=True)
    return {"error": None, "candidates": candidates, "tag_id": tag_id, "cutoff": cutoff.isoformat()}


def preview_candidates_sonarr(cfg: Dict[str, Any], app_obj: Dict[str, Any], job: Dict[str, Any]):
    if not app_obj.get("enabled", True):
        return {"error": "This Sonarr app is disabled.", "candidates": [], "cutoff": ""}

    tag_label = (job.get("TAG_LABEL") or "").strip()
    if not tag_label:
        return {"error": "Tag is empty. Edit the job and select a tag.", "candidates": [], "cutoff": ""}

    days_old = int(job.get("DAYS_OLD", 30))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_old)

    tags = app_get(cfg, app_obj, "/api/v3/tag")
    tag = next((t for t in tags if t.get("label") == tag_label), None)
    if not tag:
        return {"error": f"Tag '{tag_label}' not found in Sonarr.", "candidates": [], "cutoff": cutoff.isoformat()}

    tag_id = tag["id"]
    series_list = app_get(cfg, app_obj, "/api/v3/series")

    candidates = []
    for s in series_list:
        if tag_id not in (s.get("tags") or []):
            continue
        added_str = s.get("added")
        added = parse_iso_date(added_str) if added_str else None
        if not added:
            continue
        if added < cutoff:
            age_days = int((now - added).total_seconds() // 86400)
            candidates.append({
                "kind": "series",
                "id": s.get("id"),
                "title": s.get("title"),
                "year": s.get("year"),
                "added": added_str,
                "age_days": age_days,
                "path": s.get("path"),
            })

    candidates.sort(key=lambda x: x["age_days"], reverse=True)
    return {"error": None, "candidates": candidates, "tag_id": tag_id, "cutoff": cutoff.isoformat()}


# ----------------------------
# Toasts
# ----------------------------
def render_toasts() -> str:
    msgs = get_flashed_messages(with_categories=True)
    if not msgs:
        return ""
    items = []
    for cat, msg in msgs:
        t = "ok" if cat == "success" else "err"
        items.append(f'<div class="toast {t}">{safe_html(msg)}</div>')
    return f'<div id="toastHost" class="toastHost">{"".join(items)}</div>'


# ----------------------------
# UI shell
# ----------------------------
BASE_HEAD = """
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root{
    --bg:#111827;
    --panel:#1f2937;
    --panel2:#1b2431;
    --muted:#9ca3af;
    --text:#f1f5f9;
    --line:#334155;
    --line2:#475569;

    --accent:#22c55e;
    --accent2:#16a34a;

    --warn:#f59e0b;
    --bad:#ef4444;
    --shadow: 0 12px 28px rgba(0,0,0,.28);

    --ui: 1;

    --top-h: 60px;
    --sidebar-w: 210px;
    --pageHeaderBackgroundColor: #1b2431;

    --fs-1: calc(13px * var(--ui));
    --fs-3: calc(16px * var(--ui));

    --btn-fs: calc(10px * var(--ui));
    --btn-py: calc(7px * var(--ui));
    --btn-px: calc(9px * var(--ui));
    --btn-gap: calc(6px * var(--ui));

    --switch-w: calc(42px * var(--ui));
    --switch-h: calc(20px * var(--ui));
    --switch-thumb: calc(14px * var(--ui));
    --switch-pad: calc(3px * var(--ui));
    --switch-travel: calc(var(--switch-w) - var(--switch-thumb) - (var(--switch-pad) * 2));
  }

  [data-theme="light"]{
    --bg:#f7f8fb;
    --panel:#ffffff;
    --panel2:#ffffff;
    --muted:#526171;
    --text:#0b1220;
    --line:#e5e7eb;
    --line2:#d1d5db;
    --pageHeaderBackgroundColor:#f3f4f6;
    --accent:#6d28d9;
    --accent2:#7c3aed;

    --warn:#d97706;
    --bad:#dc2626;
    --shadow: 0 12px 30px rgba(0,0,0,.08);
  }

  [data-theme="reaparr"]{
    --bg:#070a0d;
    --panel:#0f1620;
    --panel2:#121b26;
    --pageHeaderBackgroundColor:#121b26;
    --muted:rgba(255,255,255,.64);
    --text:rgba(255,255,255,.92);
    --line:rgba(255,255,255,.10);
    --line2:rgba(255,255,255,.07);

    --accent:#26e08a;
    --accent2:#16b86e;

    --reaparr_accent:#a7d541;
    --light_accent:#a7d541;
    --dark_accent:#a7d541;

    --warn:#ffb020;
    --bad:#ff5c6c;

    --shadow: 0 12px 28px rgba(0,0,0,.55);
  }

  body.sbCollapsed{ --sidebar-w: 0px; }

  *, *::before, *::after { box-sizing: border-box; }
  html, body{ height: 100%; }

  body{
    margin:0;
    min-height: 100vh;
    font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Apple Color Emoji","Segoe UI Emoji";
    color: var(--text);

    background:
      radial-gradient(900px 520px at 18% 8%, rgba(34,197,94,.22), transparent 62%),
      radial-gradient(880px 520px at 92% 10%, rgba(22,163,74,.16), transparent 60%),
      radial-gradient(700px 460px at 50% 105%, rgba(34,197,94,.10), transparent 60%),
      linear-gradient(135deg, rgba(34,197,94,.10), rgba(22,163,74,.06)),
      var(--bg);
    background-attachment: fixed;

    overflow: hidden;
  }

  body[data-theme="reaparr"]{
    background:
      radial-gradient(900px 450px at 20% -10%, rgba(38,224,138,.14), transparent 60%),
      radial-gradient(800px 420px at 90% 0%, rgba(38,224,138,.10), transparent 55%),
      radial-gradient(700px 460px at 50% 105%, rgba(38,224,138,.08), transparent 60%),
      linear-gradient(180deg, #070a0d, #0b0f14 40%, #070a0d);
    background-attachment: fixed;
  }

  body[data-theme="dark"] { color-scheme: dark; }
  body[data-theme="light"] { color-scheme: light; }
  body[data-theme="reaparr"] { color-scheme: dark; }

  body:after{
    content:"";
    position: fixed;
    left: 0; right: 0; bottom: 0;
    height: 140px;
    pointer-events: none;
    background: linear-gradient(to bottom, rgba(0,0,0,0), rgba(0,0,0,.35));
    z-index: 1;
  }
  body[data-theme="light"]:after{ background: linear-gradient(to bottom, rgba(255,255,255,0), rgba(0,0,0,.08)); }
  body[data-theme="reaparr"]:after{ background: linear-gradient(to bottom, rgba(7,10,13,0), rgba(7,10,13,.92)); }

  a{ color: var(--text); text-decoration: none; }
  a:hover{ text-decoration: underline; }

  .wrap{ width: 100vw; height: 100vh; overflow: hidden; position: relative; }
  .layoutReaparr{ position: relative; width: 100vw; height: 100vh; }

  .pageContent{
    flex: 1 1 auto;
    min-height: 0;
    overflow: hidden;
    padding: 0px;
  }

  .pageContent .grid{
    min-height: 100%;
    height: 100%;
  }

  .pageContent .grid > .card:only-child{
    min-height: 100%;
    display: flex;
    flex-direction: column;
  }

  .pageContent .grid > .card:only-child > .bd{
    flex: 1 1 auto;
    min-height: 0;
    overflow: auto;
  }

  .pageHeader{
    position: fixed;
    top: 0; left: 0; right: 0;
    height: var(--top-h);
    z-index: 8000;
    overflow: hidden;
    margin: 0 !important;
  }

  .pageHeader .ptIn{
    height: var(--top-h);
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr auto;
    align-items: center;
    padding: 0;
    background: var(--pageHeaderBackgroundColor);
  }

  .ptRightActions{
    display: flex;
    align-items: center;
    gap: 14px;
    justify-self: end;
    padding-right: 16px;
  }

  .pageTopLogo{
    width: var(--sidebar-w);
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: flex-start;
    padding-left: 0px;
  }

  .pageTopLogo img{
    max-width: 210px;
    max-height: 40px;
    width: auto;
    height: auto;
    object-fit: contain;
    display: block;
  }

  .sidebar{
    position: fixed;
    top: var(--top-h);
    left: 0;
    width: var(--sidebar-w);
    height: calc(100vh - var(--top-h));
    border-right: 3px solid var(--line);
    background: var(--panel2);
    box-shadow: none;
    overflow: hidden;
    z-index: 7000;
    display:flex;
    flex-direction: column;
  }

  .sbNav{
    padding: 0px;
    display:flex;
    flex-direction: column;
    gap: 0px;
    flex: 1 1 auto;
    overflow: hidden;
    min-height: 0;
  }

  .sbItem,.sbItem:hover,.sbItem:focus,.sbItem:active{
    text-decoration: none;
  }

  .sbItem{
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 10px;
    padding: 16px 30px;
    border: none;
    background: none;
    font-size: 14px;
    text-decoration: none;
    cursor:pointer;
  }

  /* Prevent browser default active link color */
  .sbItem:active, .sbItem:active span, .sbItem:focus, .sbItem:focus span{
    color: inherit;
  }

  .sbItem:hover{
    color: #97c13d;
  }

  .sbItem.active{
    box-shadow: none !important;
  }

  body[data-theme="reaparr"] .sbItem:hover{
    color: var(--reaparr_accent);
  }

  /* ACTIVE INDICATOR = LEFT BORDER */
  body[data-theme="reaparr"] .sbItem.active{
    background: #15212f;
    color: var(--reaparr_accent);
    border-left: 3px solid var(--reaparr_accent);
  }

  body[data-theme="light"] .sbItem.active{
    background: #e5e7eb;
    color: var(--light_accent);
    border-left: 3px solid var(--light_accent);
  }

  body[data-theme="dark"] .sbItem.active{
    background: #97c13d;
    color: var(--dark_accent);
    border-left: 3px solid var(--dark_accent);
  }

  .sbNav form{ margin: 0; }
  button.sbItem{ width: 100%; text-align: left; color: var(--text); }

  body.sbCollapsed .sbItem{ justify-content: center; }
  body.sbCollapsed .sbItem span.sbText{ display:none; }

  .mainArea{
    position: fixed;
    top: var(--top-h);
    left: var(--sidebar-w);
    right: 0;
    bottom: 0;
    display: flex;
    flex-direction: column;
    overflow: hidden;
    min-width: 0;
    padding: 0;
    z-index: 2;
  }

  @media (max-width: 900px){
    :root{ --sidebar-w: 220px; }
  }
  @media (max-width: 740px){
    :root{ --sidebar-w: 200px; }
  }
  @media (max-width: 620px){
    body:not(.sbPinnedOpen){ --sidebar-w: 72px; }
    body:not(.sbPinnedOpen) .sbItem{ justify-content:center; }
    body:not(.sbPinnedOpen) .sbItem span.sbText{ display:none; }
  }

  .grid{
    display: grid;
    grid-template-columns: repeat(12, 1fr);
    gap: 14px;
    min-height: 0;
  }

  .card{
    grid-column: span 12;
    border: none;
    background: var(--panel);
    box-shadow: var(--shadow);
    overflow:hidden;
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
  }

  .card .hd{
    padding: 14px 16px;
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap:12px;
    background: var(--panel2);
    overflow: hidden;
    position: sticky;
    top: 0;
    z-index: 2;
    flex: 0 0 auto;
  }

  [data-theme="light"] .card .hd{ background: #f3f4f6; }

  .card .hd h2{
    margin:0;
    font-size: 14px;
    letter-spacing:.2px;
  }

  .card .bd{
    padding: 14px 16px;
    background: var(--panel);
    min-height: 0;
    overflow: auto;
    flex: 1 1 auto;
  }

  body[data-theme="reaparr"] .card{
    background: var(--panel);
  }
  body[data-theme="reaparr"] .card .hd,
  body[data-theme="reaparr"] .jobHeader,
  body[data-theme="reaparr"] .modal .mh,
  body[data-theme="reaparr"] .modal .mf,
  body[data-theme="reaparr"] .pageHeader .ptIn{
    background: linear-gradient(180deg, rgba(255,255,255,.03), transparent), var(--pageHeaderBackgroundColor);
  }

  .muted{ color: var(--muted); }
  .btnrow{ display:flex; gap:10px; flex-wrap: wrap; align-items:center; }

  .btn{
    border: 1px solid var(--line2);
    background: var(--panel2);
    color: var(--text);
    padding: var(--btn-py) var(--btn-px);
    font-weight: 600;
    font-size: var(--btn-fs);
    gap: var(--btn-gap);
    cursor:pointer;
    display: inline-flex;
    align-items: center;
    transition: box-shadow .18s ease, border-color .18s ease, transform .18s ease, filter .18s ease;
  }
  a.btn:hover{ text-decoration: none; }

  .btn:hover{
    border-color: rgba(34,197,94,.55);
    box-shadow: 0 0 0 3px rgba(34,197,94,.10), 0 10px 22px rgba(0,0,0,.22);
    transform: translateY(-1px);
  }
  body[data-theme="reaparr"] .btn:hover{
    border-color: rgba(38,224,138,.55);
    box-shadow: 0 0 0 3px rgba(38,224,138,.10), 0 10px 22px rgba(0,0,0,.45);
  }

  .btn:active{
    transform: translateY(0);
    box-shadow: 0 0 0 2px rgba(34,197,94,.08), 0 6px 14px rgba(0,0,0,.18);
  }

  .btn:disabled{
    opacity: .45;
    cursor: not-allowed;
    filter: grayscale(0.35);
  }

  .btn.primary{
    border-color: rgba(34,197,94,.45);
    background: linear-gradient(135deg, rgba(34,197,94,.26), rgba(34,197,94,.10));
  }
  .btn.good{
    border-color: rgba(34,197,94,.45);
    background: linear-gradient(135deg, rgba(34,197,94,.20), rgba(34,197,94,.08));
  }
  .btn.warn{
    border-color: rgba(245,158,11,.55);
    background: linear-gradient(135deg, rgba(245,158,11,.22), rgba(245,158,11,.08));
  }
  .btn.bad{
    border-color: rgba(239,68,68,.55);
    background: linear-gradient(135deg, rgba(239,68,68,.20), rgba(239,68,68,.08));
  }
  body[data-theme="reaparr"] .btn.primary,
  body[data-theme="reaparr"] .btn.good{
    border-color: rgba(38,224,138,.45);
    background: linear-gradient(135deg, rgba(38,224,138,.20), rgba(38,224,138,.08));
  }
  body[data-theme="reaparr"] .btn.bad{
    border-color: rgba(255,92,108,.55);
    background: linear-gradient(135deg, rgba(255,92,108,.20), rgba(255,92,108,.08));
  }

  .form{ display:grid; grid-template-columns: minmax(0, 1fr); gap: 12px; }
  @media(min-width: 900px){ .form{ grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); } }

  .field{
    border: 1px solid var(--line);
    padding: 10px 12px;
    background: var(--panel2);
    position: relative;
    min-width: 0;
  }
  [data-theme="light"] .field{ background: var(--panel); }

  .field label{ display:block; font-size: 12px; color: var(--muted); margin-bottom: 8px; }

  .field input[type=text],
  .field input[type=password],
  .field input[type=number],
  .field select,
  .field textarea{
    width: 100%;
    max-width: 100%;
    min-width: 0;
    border: 1px solid var(--line2);
    background: var(--panel);
    color: var(--text);
    padding: 10px 10px;
    outline: none;
  }

  body[data-theme="reaparr"] .field input[type=text],
  body[data-theme="reaparr"] .field input[type=password],
  body[data-theme="reaparr"] .field input[type=number],
  body[data-theme="reaparr"] .field select,
  body[data-theme="reaparr"] .field textarea{ background: rgba(0,0,0,.22); }

  [data-theme="light"] .field input,
  [data-theme="light"] .field select,
  [data-theme="light"] .field textarea{ background: #ffffff; }

  .field select{
    appearance: none;
    -webkit-appearance: none;
    -moz-appearance: none;
    padding-right: 36px;
    cursor: pointer;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--muted) 50%),
      linear-gradient(135deg, var(--muted) 50%, transparent 50%);
    background-position:
      calc(100% - 18px) 50%,
      calc(100% - 12px) 50%;
    background-size: 6px 6px, 6px 6px;
    background-repeat: no-repeat;
  }

  body[data-theme="dark"] .field select option{ background-color: #1f2937; color: #f1f5f9; }
  body[data-theme="light"] .field select option{ background-color: #ffffff; color: #0b1220; }
  body[data-theme="reaparr"] .field select option{ background-color: #0f1620; color: rgba(255,255,255,.92); }

  .field input:focus, .field select:focus, .field textarea:focus{
    border-color: rgba(34,197,94,.55);
    box-shadow: 0 0 0 3px rgba(34,197,94,.14);
  }
  body[data-theme="reaparr"] .field input:focus,
  body[data-theme="reaparr"] .field select:focus,
  body[data-theme="reaparr"] .field textarea:focus{
    border-color: rgba(38,224,138,.55);
    box-shadow: 0 0 0 3px rgba(38,224,138,.14);
  }

  .checks{ display:flex; flex-direction: column; gap: 10px; margin-top: 4px; }
  .check{
    display:flex; align-items:center; gap:10px;
    border: 1px solid var(--line);
    padding: 10px 12px;
    background: var(--panel2);
  }
  [data-theme="light"] .check{ background: #ffffff; }
  .check input{ transform: scale(calc(1.2 * var(--ui))); }

  .switch{ position: relative; width: var(--switch-w); height: var(--switch-h); display: inline-block; flex: 0 0 auto; }
  .switch input{ opacity: 0; width: 0; height: 0; }
  .slider{
    position: absolute;
    inset: 0;
    cursor: pointer;
    background: rgba(255,255,255,.10);
    border: 1px solid var(--line2);
    transition: .18s ease;
  }
  .slider:before{
    position: absolute;
    content: "";
    height: var(--switch-thumb);
    width: var(--switch-thumb);
    left: var(--switch-pad);
    top: 50%;
    transform: translateY(-50%);
    background: rgba(255,255,255,.85);
    transition: .18s ease;
    box-shadow: 0 4px 10px rgba(0,0,0,.25);
  }
  .switch input:checked + .slider{
    background: linear-gradient(135deg, rgba(34,197,94,.60), rgba(22,163,74,.35));
    border-color: rgba(34,197,94,.55);
  }
  body[data-theme="reaparr"] .switch input:checked + .slider{
    background: linear-gradient(135deg, rgba(38,224,138,.60), rgba(22,184,110,.35));
    border-color: rgba(38,224,138,.55);
  }
  .switch input:checked + .slider:before{
    transform: translate(var(--switch-travel), -50%);
    background: rgba(255,255,255,.92);
  }

  .jobsGrid{
    display:grid;
    gap: 12px;
    grid-template-columns: 1fr;
    justify-content: center;
  }
  .jobCard{
    border: 1px solid var(--line);
    background: var(--panel2);
    overflow:hidden;
    max-width: none;
    width: 100%;
  }
  @media (min-width: 700px){ .jobsGrid{ grid-template-columns: repeat(2, minmax(300px, 1fr)); } }
  @media (min-width: 1200px){ .jobsGrid{ grid-template-columns: repeat(3, minmax(300px, 1fr)); gap: 16px; } }
  @media (min-width: 1800px){ .jobsGrid{ grid-template-columns: repeat(4, minmax(300px, 1fr)); gap: 20px; } }

  [data-theme="light"] .jobCard{ background: #ffffff; }

  .jobHeader{
    padding: 12px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--panel2);
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 10px;
  }
  [data-theme="light"] .jobHeader{ background: #f3f4f6; }

  .jobHeaderLeft{ justify-self: start; min-width: 0; }
  .jobHeaderCenter{ justify-self: center; }
  .jobHeaderRight{ justify-self: end; display:flex; align-items:center; gap:10px; }

  .jobName{
    font-weight: 900;
    letter-spacing: .2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .enableWrap{ display:flex; align-items:center; gap:10px; }
  .enableLbl{ font-size: 12px; color: var(--muted); white-space: nowrap; }

  .jobBody{
    padding: 12px 12px;
    background: var(--panel2);
    display: grid;
    grid-template-columns: 1fr 70px;
    gap: 12px;
    align-items: start;
  }
  [data-theme="light"] .jobBody{ background: #ffffff; }

  .jobRail{
    display: flex;
    flex-direction: column;
    gap: 10px;
    align-self: start;
  }
  .jobRail .btn{
    width: 100%;
    text-align: center;
    justify-content: center;
    padding: 10px 8px;
  }

  .metaStack{ display:flex; flex-direction: column; gap: 6px; font-size: calc(11px * var(--ui)); }
  .metaRow{ display:flex; align-items: baseline; gap: 8px; line-height: 1.35; }
  .metaLabel{ width: 110px; color: var(--muted); flex: 0 0 auto; }
  .metaVal{ color: var(--text); flex: 1 1 auto; min-width: 0; word-break: break-word; }

  /* Apps grid like your screenshot */
  .appsGrid{
    display: grid;
    gap: 16px;
    justify-content: start;
    align-content: start;
    grid-template-columns: repeat(5, 300px);
  }
  @media (max-width: 1700px){ .appsGrid{ grid-template-columns: repeat(4, 300px); } }
  @media (max-width: 1400px){ .appsGrid{ grid-template-columns: repeat(3, 300px); } }
  @media (max-width: 1100px){ .appsGrid{ grid-template-columns: repeat(2, 300px); } }
  @media (max-width: 760px){ .appsGrid{ grid-template-columns: 1fr; } }

  .appCard{
    width: 300px;
    height: 128px;
    border: 1px solid var(--line);
    background: var(--panel2);
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 14px 14px;
    position: relative;
    user-select: none;
  }
  [data-theme="light"] .appCard{ background:#ffffff; }

  .appCardTop{
    display:flex;
    align-items:flex-start;
    justify-content: space-between;
    gap: 12px;
    min-width: 0;
  }
  .appTitle{
    font-size: 18px;
    font-weight: 600;
    line-height: 1.12;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .appSub{
    font-size: 11px;
    color: var(--muted);
    margin-top: 6px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .appCardIconBtn{
    width: 26px; height: 26px;
    display:flex; align-items:center; justify-content:center;
    border: 1px solid var(--line2);
    background: transparent;
    cursor: pointer;
    opacity: .9;
  }
  .appCardIconBtn:hover{ opacity: 1; }

  .pill{
    display:inline-flex;
    align-items:center;
    gap: 6px;
    border: 1px solid var(--line2);
    padding: 3px 8px;
    font-size: 11px;
    font-weight: 700;
    background: rgba(34,197,94,.12);
  }
  .pill.good{ border-color: rgba(34,197,94,.45); }
  .pill.bad{ border-color: rgba(239,68,68,.55); background: rgba(239,68,68,.10); }
  body[data-theme="reaparr"] .pill.good{ border-color: rgba(38,224,138,.45); background: rgba(38,224,138,.10); }

  .addAppCard{
    align-items: center;
    justify-content: center;
    padding: 0;
    cursor: pointer;
  }
  .addAppCardInner{
    width: 86px;
    height: 54px;
    border: 1px solid var(--line2);
    display:flex;
    align-items:center;
    justify-content:center;
    font-size: 42px;
    color: var(--muted);
    line-height: 1;
  }

  .modalBack{
    position: fixed; inset: 0;
    background: rgba(0,0,0,.68);
    backdrop-filter: blur(6px);
    display:none;
    align-items:center;
    justify-content:center;
    z-index: 9999;
    padding: 18px;
  }
  .modal{
    width: min(720px, 100%);
    border: 1px solid var(--line);
    background: var(--panel);
    box-shadow: var(--shadow);
    overflow:hidden;
    max-height: calc(100vh - 40px);
    display:flex;
    flex-direction: column;
    min-height: 0;
  }
  .modal .mh{
    padding: 14px 16px;
    border-bottom: 1px solid var(--line);
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 12px;
    background: var(--panel2);
    flex: 0 0 auto;
  }
  [data-theme="light"] .modal .mh{ background: #f3f4f6; }
  .modal .mh h3{ margin:0; font-size: 14px; letter-spacing: .2px; }

  .modal form{
    display:flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
  }

  .modal .mb{
    padding: 14px 16px;
    background: var(--panel);
    overflow: auto;
    flex: 1 1 auto;
    min-height: 0;
    -webkit-overflow-scrolling: touch;
  }
  .modal .mf{
    padding: 14px 16px;
    border-top: 1px solid var(--line);
    display:flex;
    justify-content: flex-end;
    gap: 10px;
    background: var(--panel2);
    flex: 0 0 auto;
  }
  [data-theme="light"] .modal .mf{ background: #f3f4f6; }

  table{ width:100%; border-collapse: collapse; overflow:hidden; border: 1px solid var(--line); }
  th, td{ padding: 10px 10px; border-bottom: 1px solid var(--line); font-size: var(--fs-1); vertical-align: top; }
  th{ text-align:left; color:#cbd5e1; background: rgba(255,255,255,.04); position: sticky; top: 0; }
  [data-theme="light"] th{ color:#111827; background: rgba(0,0,0,.03); }
  .tablewrap{ max-height: 420px; overflow:auto; border: 1px solid var(--line); }

  .toastHost{
    position: fixed;
    right: 16px;
    bottom: 16px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    z-index: 99999;
    pointer-events: none;
    max-width: min(420px, calc(100vw - 32px));
  }
  .toast{
    pointer-events: auto;
    border: 1px solid var(--line2);
    background: var(--panel);
    box-shadow: var(--shadow);
    padding: 12px 12px;
    font-size: var(--fs-1);
    color: var(--text);
    opacity: 0;
    transform: translateY(10px);
    animation: toastIn .18s ease-out forwards, toastOut .25s ease-in forwards;
    animation-delay: 0s, 5s;
  }
  .toast.ok{ border-color: rgba(34,197,94,.45); }
  .toast.err{ border-color: rgba(239,68,68,.55); }
  body[data-theme="reaparr"] .toast.ok{ border-color: rgba(38,224,138,.45); }
  body[data-theme="reaparr"] .toast.err{ border-color: rgba(255,92,108,.55); }
  @keyframes toastIn { to { opacity: 1; transform: translateY(0); } }
  @keyframes toastOut { to { opacity: 0; transform: translateY(10px); } }

  .card, .jobCard{ border-radius: 0 !important; }
  .card .hd, .card .bd{ border-radius: 0 !important; }
</style>

<script>
  function $(id){ return document.getElementById(id); }
  function showModal(id){ const el = $(id); if (el) el.style.display = "flex"; }
  function hideModal(id){ const el = $(id); if (el) el.style.display = "none"; }
  function setVal(id, v){ const el = $(id); if (el) el.value = v; }
  function setChecked(id, v){ const el = $(id); if (el) el.checked = !!v; }

  function escHtml(s){
    return (s ?? "").toString()
      .replaceAll("&","&amp;")
      .replaceAll("<","&lt;")
      .replaceAll(">","&gt;")
      .replaceAll('"',"&quot;")
      .replaceAll("'","&#39;");
  }

  function setSidebarCollapsed(collapsed){
    if (collapsed) document.body.classList.add("sbCollapsed");
    else document.body.classList.remove("sbCollapsed");
    try { localStorage.setItem("sbCollapsed", collapsed ? "1" : "0"); } catch(e){}
  }

  function toggleSidebar(){
    const collapsed = document.body.classList.contains("sbCollapsed");
    setSidebarCollapsed(!collapsed);
  }

  // -----------------------
  // Apps wizard
  // -----------------------
  function openAddAppModal(){
    setVal("appPick", "radarr");
    showModal("appPickBack");
  }

  function confirmAddSelectedApp(){
    const t = ($("appPick")?.value || "radarr").toLowerCase();

    // New app
    setVal("app_id", "");
    setVal("app_type", t);
    setVal("app_name", (t === "sonarr") ? "Sonarr" : "Radarr");
    setChecked("app_enabled", true);
    setVal("app_url", "");
    setVal("app_key", "");

    onAppTypeChanged();

    hideModal("appPickBack");
    showModal("appBack");
  }

  function onAppTypeChanged(){
    const t = ($("app_type")?.value || "radarr").toLowerCase();
    const hint = $("appHint");
    if (hint){
      hint.textContent = (t === "sonarr")
        ? "Configure Sonarr. Save then Test Connection."
        : "Configure Radarr. Save then Test Connection.";
    }
    const nm = $("app_name");
    if (nm && !(nm.value || "").trim()){
      nm.value = (t === "sonarr") ? "Sonarr" : "Radarr";
    }
  }

  function openEditApp(appId){
    const apps = (window.__APP_CFG && window.__APP_CFG.APPS) ? window.__APP_CFG.APPS : [];
    const a = apps.find(x => (x.id || "") === (appId || ""));
    if (!a) return;

    setVal("app_id", a.id || "");
    setVal("app_type", (a.type || "radarr"));
    setVal("app_name", a.name || ((a.type === "sonarr") ? "Sonarr" : "Radarr"));
    setChecked("app_enabled", !!a.enabled);
    setVal("app_url", a.url || "");
    setVal("app_key", a.api_key || "");

    onAppTypeChanged();
    showModal("appBack");
  }

  function submitDeleteApp(){
    const id = ($("app_id")?.value || "").trim();
    if (!id) return;
    if (!confirm("Delete this app? Jobs using it will need updating.")) return;
    const f = $("appDeleteForm");
    const hid = $("app_delete_id");
    if (hid) hid.value = id;
    if (f) f.submit();
  }

  // -----------------------
  // Job modal dirty tracking
  // -----------------------
  window.__JOB_MODAL_INITIAL = "";
  window.__JOB_MODAL_DIRTY = false;

  function jobFormSnapshot(){
    const form = $("jobForm");
    if (!form) return "";
    const fd = new FormData(form);
    const entries = [];
    for (const [k, v] of fd.entries()){
      entries.push([k, (v ?? "").toString()]);
    }
    const cbs = form.querySelectorAll('input[type="checkbox"][name]');
    for (const cb of cbs){
      if (!fd.has(cb.name)) entries.push([cb.name, ""]);
    }
    entries.sort((a,b) => (a[0]+a[1]).localeCompare(b[0]+b[1]));
    return JSON.stringify(entries);
  }

  function jobModalMarkClean(){
    window.__JOB_MODAL_INITIAL = jobFormSnapshot();
    window.__JOB_MODAL_DIRTY = false;
  }

  function jobModalUpdateDirty(){
    const snap = jobFormSnapshot();
    window.__JOB_MODAL_DIRTY = (snap !== window.__JOB_MODAL_INITIAL);
  }

  function maybeCloseJobModal(){
    const back = $("jobBack");
    if (!back || back.style.display !== "flex") {
      hideModal("jobBack");
      return;
    }
    jobModalUpdateDirty();
    if (window.__JOB_MODAL_DIRTY){
      if (!confirm("Discard changes to this job?")) return;
    }
    hideModal("jobBack");
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      hideModal("runNowBack");
      hideModal("appBack");
      hideModal("appPickBack");
      maybeCloseJobModal();
    }
  });

  function ensureSelectOption(selectId, value, labelSuffix){
    const sel = $(selectId);
    if (!sel) return;
    const v = (value ?? "").toString();
    if (!v) return;

    for (const opt of sel.options){
      if (opt.value === v) return;
    }
    const opt = document.createElement("option");
    opt.value = v;
    opt.textContent = v + (labelSuffix || "");
    sel.insertBefore(opt, sel.firstChild);
  }

  function rebuildTagOptions(appId, selectedValue){
    const sel = $("job_tag");
    if (!sel) return;

    const tags = (window.__TAGS && window.__TAGS[appId]) ? window.__TAGS[appId] : [];
    const out = ['<option value="" selected disabled>-- Select a tag --</option>'];

    for (const t of tags){
      const esc = escHtml(t || "");
      out.push(`<option value="${esc}">${esc}</option>`);
    }

    sel.innerHTML = out.join("");
    if (selectedValue){
      ensureSelectOption("job_tag", selectedValue, " (missing)");
      setVal("job_tag", selectedValue);
    }
  }

  function updateSonarrModeVisibility(appId){
    const wrap = $("sonarrDeleteModeField");
    const sel = $("job_sonarr_mode");
    const t = (window.__APP_TYPES && window.__APP_TYPES[appId]) ? window.__APP_TYPES[appId] : "radarr";
    const isSonarr = (t === "sonarr");
    if (wrap) wrap.style.display = isSonarr ? "" : "none";
    if (sel) sel.disabled = !isSonarr;
  }

  function onJobAppChanged(){
    const appSel = $("job_app");
    const appId = appSel ? (appSel.value || "") : "";
    rebuildTagOptions(appId, "");
    updateSonarrModeVisibility(appId);
    setTimeout(jobModalUpdateDirty, 0);
  }

  function openNewJob(){
    const form = $("jobForm");
    if (!form) return;

    form.action = "/jobs/save";
    setVal("job_id", "");
    setVal("job_name", "New Job");

    const appSel = $("job_app");
    const defApp = appSel?.getAttribute("data-default-app") || "";
    if (defApp) setVal("job_app", defApp);

    if (appSel && appSel.selectedIndex < 0 && appSel.options.length > 0) appSel.selectedIndex = 0;
    const actualApp = appSel ? (appSel.value || defApp) : defApp;

    rebuildTagOptions(actualApp, "");
    updateSonarrModeVisibility(actualApp);

    setVal("job_sonarr_mode", "episodes_only");
    setVal("job_days", "30");
    setVal("job_day", "daily");
    setVal("job_hour", "3");
    setChecked("job_dry", true);
    setChecked("job_delete", true);
    setChecked("job_excl", false);
    setVal("job_enabled", "1");

    const t = $("jobTitle");
    if (t) t.textContent = "Add Job";
    showModal("jobBack");
    setTimeout(jobModalMarkClean, 0);
  }

  function openEditJob(btn){
    const form = $("jobForm");
    if (!form || !btn) return;

    form.action = "/jobs/save";
    setVal("job_id", btn.getAttribute("data-id") || "");
    setVal("job_name", btn.getAttribute("data-name") || "Job");

    const appId = btn.getAttribute("data-app-id") || "";
    setVal("job_app", appId);

    const tag = btn.getAttribute("data-tag") || "";
    rebuildTagOptions(appId, tag);
    updateSonarrModeVisibility(appId);

    const smode = btn.getAttribute("data-sonarr-mode") || "episodes_only";
    setVal("job_sonarr_mode", smode);

    setVal("job_days", btn.getAttribute("data-days") || "30");
    setVal("job_day", btn.getAttribute("data-day") || "daily");
    setVal("job_hour", btn.getAttribute("data-hour") || "3");
    setChecked("job_dry", (btn.getAttribute("data-dry") || "1") === "1");
    setChecked("job_delete", (btn.getAttribute("data-del") || "1") === "1");
    setChecked("job_excl", (btn.getAttribute("data-excl") || "0") === "1");
    setVal("job_enabled", (btn.getAttribute("data-enabled") || "1"));

    const t = $("jobTitle");
    if (t) t.textContent = "Edit Job";
    showModal("jobBack");
    setTimeout(jobModalMarkClean, 0);
  }

  function openRunNowConfirm(jobId, opts){
    opts = opts || {};
    const appLabel = (opts.appLabel || "App");
    const dryRun = !!opts.dryRun;
    const deleteFiles = !!opts.deleteFiles;
    const enabled = (opts.enabled === undefined) ? true : !!opts.enabled;

    const hid = $("runNowJobId");
    if (hid) hid.value = jobId || "";

    const elApp = $("rn_app");
    const elDry = $("rn_dry");
    const elDel = $("rn_del");
    const elEnabled = $("rn_enabled");

    if (elApp) elApp.textContent = appLabel;
    if (elDry) elDry.textContent = dryRun ? "ON" : "OFF";
    if (elDel) elDel.textContent = deleteFiles ? "ON" : "OFF";
    if (elEnabled) elEnabled.textContent = enabled ? "Enabled" : "Disabled";

    const msg = $("rn_msg");
    if (msg){
      const parts = [];
      if (!dryRun) parts.push("Dry Run is OFF — this will perform real actions.");
      parts.push(deleteFiles ? "Delete Files is ON — files may be removed from disk." : "Delete Files is OFF — it should avoid disk deletes.");
      msg.textContent = parts.join(" ");
    }

    const hintDelete = $("rn_hint_delete");
    const hintNoDelete = $("rn_hint_no_delete");
    if (hintDelete) hintDelete.style.display = deleteFiles ? "" : "none";
    if (hintNoDelete) hintNoDelete.style.display = deleteFiles ? "none" : "";

    showModal("runNowBack");
  }

  function runNowSubmitConfirm(){
    const form = $("runNowFormConfirm");
    if (form) form.submit();
  }

  document.addEventListener("input", (e) => {
    const back = $("jobBack");
    if (back && back.style.display === "flex") {
      const form = $("jobForm");
      if (form && form.contains(e.target)) jobModalUpdateDirty();
    }
  });
  document.addEventListener("change", (e) => {
    const back = $("jobBack");
    if (back && back.style.display === "flex") {
      const form = $("jobForm");
      if (form && form.contains(e.target)) jobModalUpdateDirty();
    }
  });

  document.addEventListener("DOMContentLoaded", () => {
    // Sidebar collapsed state
    try {
      const v = localStorage.getItem("sbCollapsed");
      if (v === "1") document.body.classList.add("sbCollapsed");
    } catch(e){}

    // Bind addAppCard
    const addCard = $("addAppCard");
    if (addCard){
      addCard.style.cursor = "pointer";
      addCard.addEventListener("click", (e) => {
        e.preventDefault();
        openAddAppModal();
      });
      addCard.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " "){
          e.preventDefault();
          openAddAppModal();
        }
      });
    }

    const host = $("toastHost");
    if (host) setTimeout(() => { try { host.remove(); } catch(e){} }, 6000);

    // Ensure sonarr mode visibility based on selected app (jobs modal if present)
    const appSel = $("job_app");
    if (appSel){
      updateSonarrModeVisibility(appSel.value || "");
    }

    // UI scale live apply
    const uiScale = $("uiScale");
    const uiScaleVal = $("uiScaleVal");
    function applyUiScale(v){
      const n = Math.max(0.75, Math.min(1.5, Number(v) || 1));
      document.documentElement.style.setProperty("--ui", String(n));
      if (uiScaleVal) uiScaleVal.textContent = Math.round(n * 100) + "%";
    }
    if (uiScale){
      applyUiScale(uiScale.value);
      uiScale.addEventListener("input", (e) => applyUiScale(e.target.value));
      uiScale.addEventListener("change", (e) => applyUiScale(e.target.value));
    }
  });
</script>
"""


def shell(page_title: str, active: str, body: str):
    cfg = load_config()
    theme = (cfg.get("UI_THEME") or "dark").lower()
    if theme not in ("dark", "light", "reaparr"):
        theme = "dark"

    def sb_item(name, href, key):
        cls = "sbItem active" if active == key else "sbItem"
        return f'<a class="{cls}" href="{href}"><span class="sbText">{safe_html(name)}</span></a>'

    next_theme = {"dark": "light", "light": "reaparr", "reaparr": "dark"}.get(theme, "dark")
    next_label = {"dark": "Dark", "light": "Light", "reaparr": "Reaparr"}.get(next_theme, "Dark")

    theme_btn_sidebar = f"""
      <form method="post" action="/toggle-theme">
        <button class="sbItem" type="submit"><span class="sbText">Theme: {safe_html(next_label)}</span></button>
      </form>
    """

    sidebar = f"""
      <div class="sidebar">
        <div class="sbNav">
          {sb_item("Dashboard", "/dashboard", "dash")}
          {sb_item("Jobs", "/jobs", "jobs")}
          {sb_item("Apps", "/apps", "apps")}
          {sb_item("Settings", "/settings", "settings")}
          {sb_item("Status", "/status", "status")}
          <div style="height:6px;"></div>
          {theme_btn_sidebar}
        </div>
      </div>
    """

    topbar = """
      <div class="pageHeader">
        <div class="ptIn">
          <div class="pageTopLogo">
            <img src="/logo/logo-full.png" alt="MediaReaparr">
          </div>

          <div class="ptSpacer"></div>

          <div class="ptRightActions">
            <button class="btn" type="button" onclick="toggleSidebar()" title="Toggle sidebar">☰</button>
          </div>
        </div>
      </div>
    """

    return f"""
<!doctype html>
<html>
<head>
  <title>{safe_html(page_title)}</title>
  {BASE_HEAD}
</head>
<body data-theme="{safe_html(theme)}" style="--ui:{cfg.get('UI_SCALE',1.0)};">
  <div class="wrap">
    <div class="layoutReaparr">
      {topbar}
      {sidebar}
      <div class="mainArea">
        <div class="pageContent">
          {body}
        </div>
      </div>
    </div>
  </div>
  {render_toasts()}
</body>
</html>
"""


# ----------------------------
# Routes
# ----------------------------
@app.get("/")
def home():
    return redirect("/dashboard")


@app.get("/logo/<path:filename>")
def serve_logo_assets(filename):
    if filename != "logo-full.png":
        return ("", 404)
    if not APP_LOGO_DIR.exists():
        return ("", 404)
    return send_from_directory(str(APP_LOGO_DIR), "logo-full.png")


@app.post("/toggle-theme")
def toggle_theme():
    cfg = load_config()
    cur = (cfg.get("UI_THEME") or "dark").lower()
    nxt = {"dark": "light", "light": "reaparr", "reaparr": "dark"}.get(cur, "dark")
    cfg["UI_THEME"] = nxt
    save_config(cfg)
    flash(f"Theme set to {cfg['UI_THEME']} ✔", "success")
    return redirect(request.referrer or "/dashboard")


# ----------------------------
# Legacy settings endpoints (kept for old bookmarks)
# ----------------------------
@app.post("/reset-radarr")
def reset_radarr_legacy():
    flash("Radarr settings are now managed under Apps.", "error")
    return redirect("/apps")


@app.post("/reset-sonarr")
def reset_sonarr_legacy():
    flash("Sonarr settings are now managed under Apps.", "error")
    return redirect("/apps")


@app.post("/test-radarr")
def test_radarr_legacy():
    flash("Radarr test is now managed under Apps.", "error")
    return redirect("/apps")


@app.post("/test-sonarr")
def test_sonarr_legacy():
    flash("Sonarr test is now managed under Apps.", "error")
    return redirect("/apps")


def _test_connection(kind: str, url: str, api_key: str, timeout_s: int):
    r = requests.get(
        (url or "").rstrip("/") + "/api/v3/system/status",
        headers={"X-Api-Key": api_key or ""},
        timeout=timeout_s,
    )
    if r.status_code in (401, 403):
        raise PermissionError(f"{kind} connection failed: Unauthorized (API key incorrect).")
    r.raise_for_status()
    return True


# ----------------------------
# Settings (WebUI only)
# ----------------------------
@app.get("/settings")
def settings():
    cfg = load_config()

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Settings</h2>
            <div class="btnrow">
              <a class="btn" href="/apps">Manage Apps</a>
              <a class="btn" href="/jobs">Manage Jobs</a>
              <form method="post" action="/apply-cron" style="margin:0;">
                <button class="btn warn" type="submit">Apply Cron</button>
              </form>
            </div>
          </div>

          <div class="bd">
            <form method="post" action="/save-settings" style="margin:0;">
              <div class="field" style="margin-bottom:12px;">
                <label>HTTP Timeout Seconds</label>
                <input type="number" min="5" name="HTTP_TIMEOUT_SECONDS" value="{cfg["HTTP_TIMEOUT_SECONDS"]}">
              </div>

              <div class="field" style="margin-bottom:12px;">
                <label>UI Scale <span class="muted" id="uiScaleVal" style="margin-left:6px;"></span></label>
                <input id="uiScale"
                       type="range"
                       min="0.75"
                       max="1.5"
                       step="0.05"
                       name="UI_SCALE"
                       value="{safe_html(str(cfg.get('UI_SCALE', 1.0)))}">
              </div>

              <div class="field" style="margin-bottom:12px;">
                <label>UI Theme</label>
                <select name="UI_THEME">
                  <option value="dark" {"selected" if cfg.get("UI_THEME","dark")=="dark" else ""}>Dark</option>
                  <option value="light" {"selected" if cfg.get("UI_THEME","dark")=="light" else ""}>Light</option>
                  <option value="reaparr" {"selected" if cfg.get("UI_THEME","dark")=="reaparr" else ""}>Reaparr</option>
                </select>
              </div>

              <div class="btnrow" style="margin-top:14px;">
                <button class="btn primary" type="submit">Save Settings</button>
              </div>

              <div class="muted" style="margin-top:14px;">
                App connections are managed in <a href="/apps"><b>Apps</b></a>.
              </div>
            </form>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Settings", "settings", body))


@app.post("/save-settings")
def save_settings():
    cfg = load_config()

    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(request.form.get("HTTP_TIMEOUT_SECONDS") or 30, 5, 300, 30)
    cfg["UI_THEME"] = (request.form.get("UI_THEME") or cfg.get("UI_THEME", "dark")).lower()

    try:
        cfg["UI_SCALE"] = float(request.form.get("UI_SCALE") or cfg.get("UI_SCALE", 1.0))
    except Exception:
        cfg["UI_SCALE"] = float(cfg.get("UI_SCALE", 1.0))
    cfg["UI_SCALE"] = max(0.75, min(1.5, cfg["UI_SCALE"]))

    if cfg["UI_THEME"] not in ("dark", "light", "reaparr"):
        cfg["UI_THEME"] = "dark"

    save_config(cfg)
    flash("Settings saved ✔", "success")
    return redirect("/settings")


# ----------------------------
# Apps page + modals
# ----------------------------
def app_selector_modal_html() -> str:
    return """
    <div class="modalBack" id="appPickBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="appPickTitle" style="width:min(520px,100%);">
        <div class="mh">
          <h3 id="appPickTitle">Add App</h3>
        </div>
        <div class="mb">
          <div class="form" style="grid-template-columns: minmax(0,1fr);">
            <div class="field">
              <label>Select an app type</label>
              <select id="appPick">
                <option value="radarr">Radarr</option>
                <option value="sonarr">Sonarr</option>
              </select>
            </div>
          </div>
          <div class="muted" style="margin-top:10px;">
            Choose an app type, then click <b>Add</b> to configure it.
          </div>
        </div>
        <div class="mf">
          <button class="btn" type="button" onclick="hideModal('appPickBack')">Cancel</button>
          <button class="btn primary" type="button" onclick="confirmAddSelectedApp()">Add</button>
        </div>
      </div>
    </div>
    """


def app_modal_html(cfg: Dict[str, Any]) -> str:
    cfg_js = {
        "APPS": [normalize_app(a) for a in (cfg.get("APPS") or [])]
    }
    return f"""
    <script>
      window.__APP_CFG = {json.dumps(cfg_js)};
    </script>

    <form id="appDeleteForm" method="post" action="/apps/delete" style="display:none;">
      <input type="hidden" id="app_delete_id" name="APP_ID" value="">
    </form>

    <div class="modalBack" id="appBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="appTitle">
        <div class="mh">
          <h3 id="appTitle">Configure App</h3>
        </div>

        <form method="post" action="/apps/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" id="app_id" name="APP_ID" value="">

            <div class="form">
              <div class="field">
                <label>Type</label>
                <select id="app_type" name="APP_TYPE" onchange="onAppTypeChanged()">
                  <option value="radarr">Radarr</option>
                  <option value="sonarr">Sonarr</option>
                </select>
              </div>

              <div class="field">
                <label>Name</label>
                <input id="app_name" type="text" name="APP_NAME" value="">
              </div>

              <div class="field">
                <label>Enabled</label>
                <label class="switch" title="Enable/Disable this app" style="margin-top:6px;">
                  <input id="app_enabled" name="APP_ENABLED" type="checkbox" checked>
                  <span class="slider"></span>
                </label>
              </div>

              <div class="field">
                <label>Base URL</label>
                <input id="app_url" type="text" name="APP_URL" value="">
              </div>

              <div class="field">
                <label>API Key</label>
                <input id="app_key" type="password" name="APP_API_KEY" value="">
              </div>
            </div>

            <div class="muted" id="appHint" style="margin-top:10px;">Configure Radarr. Save then Test Connection.</div>

            <div class="btnrow" style="margin-top:14px;">
              <button class="btn good" type="submit" formaction="/apps/test" formmethod="post">Test Connection</button>
              <div class="muted">Testing saves the values and marks the app as connected.</div>
            </div>
          </div>

          <div class="mf">
            <button class="btn" type="button" onclick="hideModal('appBack')">Cancel</button>
            <button class="btn bad" type="button" onclick="submitDeleteApp()">Delete</button>
            <button class="btn primary" type="submit">Save</button>
          </div>
        </form>
      </div>
    </div>
    """


@app.get("/apps")
def apps():
    cfg = load_config()
    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    def card(a: Dict[str, Any]) -> str:
        a = normalize_app(a)
        kind = a.get("type", "radarr")
        title = a.get("name", "App")
        ok = bool(a.get("ok", False))
        enabled = bool(a.get("enabled", True))
        url = str(a.get("url") or "")
        app_id = safe_html(a.get("id"))

        href = (url or "").strip()
        ext = ""
        if href:
            ext = f"""<a class="appCardIconBtn" href="{safe_html(href)}" target="_blank" rel="noreferrer" title="Open {safe_html(title)}">
              ↗
            </a>"""
        else:
            ext = """<div class="appCardIconBtn" title="No URL set" style="opacity:.4; cursor:default;">↗</div>"""

        if not enabled:
            pill = '<span class="pill bad">Disabled</span>'
        else:
            pill = '<span class="pill good">Connected</span>' if ok else '<span class="pill bad">Not Connected</span>'

        type_label = "Radarr" if kind == "radarr" else "Sonarr"

        return f"""
        <div class="appCard" role="button" tabindex="0"
             onclick="openEditApp('{app_id}')"
             onkeydown="if(event.key==='Enter'||event.key===' '){{
                event.preventDefault(); openEditApp('{app_id}');
             }}"
             title="Configure {safe_html(title)}">
          <div class="appCardTop">
            <div style="min-width:0;">
              <div class="appTitle">{safe_html(title)}</div>
              <div class="appSub">{safe_html(type_label)} • {safe_html(url or 'No URL')}</div>
            </div>
            {ext}
          </div>
          <div>{pill}</div>
        </div>
        """

    app_cards = "".join(card(a) for a in apps_list)

    add_card = """
      <div class="appCard addAppCard" id="addAppCard" role="button" tabindex="0" title="Add an app">
        <div class="addAppCardInner">+</div>
      </div>
    """

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Apps</h2>
            <div class="muted">Application integrations</div>
          </div>
          <div class="bd">
            <div class="appsGrid">
              {app_cards}
              {add_card}
            </div>
          </div>
        </div>
      </div>

      {app_selector_modal_html()}
      {app_modal_html(cfg)}
    """
    return render_template_string(shell("mediareaparr • Apps", "apps", body))


@app.post("/apps/save")
def apps_save():
    cfg = load_config()

    app_id = (request.form.get("APP_ID") or "").strip()
    app_type = (request.form.get("APP_TYPE") or "radarr").strip().lower()
    name = (request.form.get("APP_NAME") or "").strip()
    enabled = checkbox("APP_ENABLED")
    url = (request.form.get("APP_URL") or "").strip().rstrip("/")
    api_key = (request.form.get("APP_API_KEY") or "").strip()

    if app_type not in ("radarr", "sonarr"):
        flash("Unknown app type.", "error")
        return redirect("/apps")

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    if app_id:
        updated = False
        for i, a in enumerate(apps_list):
            if a["id"] == app_id:
                a["type"] = app_type
                a["name"] = name or a["name"]
                a["enabled"] = enabled
                a["url"] = url
                a["api_key"] = api_key
                a["ok"] = False  # requires test after edits
                apps_list[i] = normalize_app(a)
                updated = True
                break
        if not updated:
            app_id = ""

    if not app_id:
        default_name = "Radarr" if app_type == "radarr" else "Sonarr"
        apps_list.append(normalize_app({
            "id": make_app_id(),
            "type": app_type,
            "name": name or default_name,
            "enabled": enabled,
            "url": url,
            "api_key": api_key,
            "ok": False,
        }))

    cfg["APPS"] = [normalize_app(a) for a in apps_list]
    save_config(cfg)
    flash("App saved ✔ (run Test Connection to mark connected)", "success")
    return redirect("/apps")


@app.post("/apps/test")
def apps_test():
    cfg = load_config()

    app_id = (request.form.get("APP_ID") or "").strip()
    app_type = (request.form.get("APP_TYPE") or "radarr").strip().lower()
    name = (request.form.get("APP_NAME") or "").strip()
    enabled = checkbox("APP_ENABLED")
    url = (request.form.get("APP_URL") or "").strip().rstrip("/")
    api_key = (request.form.get("APP_API_KEY") or "").strip()

    if app_type not in ("radarr", "sonarr"):
        flash("Unknown app type.", "error")
        return redirect("/apps")

    if not url:
        flash("URL is empty.", "error")
        return redirect("/apps")
    if not api_key:
        flash("API Key is empty.", "error")
        return redirect("/apps")

    kind = "Radarr" if app_type == "radarr" else "Sonarr"

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    try:
        _test_connection(kind, url, api_key, int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)))

        if app_id:
            for i, a in enumerate(apps_list):
                if a["id"] == app_id:
                    a["type"] = app_type
                    a["name"] = name or a["name"]
                    a["enabled"] = enabled
                    a["url"] = url
                    a["api_key"] = api_key
                    a["ok"] = True
                    apps_list[i] = normalize_app(a)
                    break
        else:
            apps_list.append(normalize_app({
                "id": make_app_id(),
                "type": app_type,
                "name": name or kind,
                "enabled": enabled,
                "url": url,
                "api_key": api_key,
                "ok": True,
            }))

        cfg["APPS"] = [normalize_app(a) for a in apps_list]
        save_config(cfg)
        flash(f"{kind} connected ✔", "success")
        return redirect("/apps")

    except PermissionError as e:
        flash(str(e), "error")
    except requests.exceptions.ConnectTimeout:
        flash(f"{kind} connection failed: timeout connecting to the host.", "error")
    except requests.exceptions.ConnectionError:
        flash(f"{kind} connection failed: could not connect (URL/host/network).", "error")
    except Exception as e:
        flash(f"{kind} connection failed: {e}", "error")

    # Save user entered values even if test fails
    if app_id:
        for i, a in enumerate(apps_list):
            if a["id"] == app_id:
                a["type"] = app_type
                a["name"] = name or a["name"]
                a["enabled"] = enabled
                a["url"] = url
                a["api_key"] = api_key
                a["ok"] = False
                apps_list[i] = normalize_app(a)
                break
    else:
        apps_list.append(normalize_app({
            "id": make_app_id(),
            "type": app_type,
            "name": name or kind,
            "enabled": enabled,
            "url": url,
            "api_key": api_key,
            "ok": False,
        }))

    cfg["APPS"] = [normalize_app(a) for a in apps_list]
    save_config(cfg)
    return redirect("/apps")


@app.post("/apps/delete")
def apps_delete():
    cfg = load_config()
    app_id = (request.form.get("APP_ID") or "").strip()
    if not app_id:
        return redirect("/apps")

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]
    apps_list = [a for a in apps_list if a["id"] != app_id]
    cfg["APPS"] = apps_list

    # Note: we do NOT auto-rewrite jobs; we leave them as-is (jobs will show missing app).
    save_config(cfg)
    flash("App deleted ✔", "success")
    return redirect("/apps")


# ----------------------------
# Jobs
# ----------------------------
@app.post("/jobs/toggle-enabled")
def jobs_toggle_enabled():
    cfg = load_config()
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        return redirect("/jobs")

    enabled = checkbox("enabled")

    jobs = cfg.get("JOBS") or []
    for i, j in enumerate(jobs):
        if str(j.get("id")) == job_id:
            jj = normalize_job(j)
            jj["enabled"] = enabled
            jobs[i] = jj
            break

    cfg["JOBS"] = [normalize_job(j) for j in jobs]
    save_config(cfg)
    return redirect("/jobs")


@app.get("/jobs")
def jobs_page():
    cfg = load_config()

    apps_all = [normalize_app(a) for a in (cfg.get("APPS") or [])]
    ready_apps = [a for a in apps_all if is_app_ready(cfg, a["id"])]

    tags_map = {a["id"]: get_tag_labels(cfg, a["id"]) for a in ready_apps}
    types_map = {a["id"]: a.get("type", "radarr") for a in ready_apps}

    default_app_id = ready_apps[0]["id"] if ready_apps else ""

    app_disabled_attr = "disabled" if len(ready_apps) <= 1 else ""
    app_options_html = ""
    for a in ready_apps:
        label = f"{'Radarr' if a['type']=='radarr' else 'Sonarr'} • {a.get('name','App')}"
        app_options_html += f'<option value="{safe_html(a["id"])}">{safe_html(label)}</option>'

    hour_opts = "".join([f'<option value="{h}">{h:02d}:00</option>' for h in range(0, 24)])

    tags_js = f"""
    <script>
      window.__TAGS = {json.dumps(tags_map)};
      window.__APP_TYPES = {json.dumps(types_map)};
    </script>
    """

    sonarr_mode_opts = "".join(
        f'<option value="{safe_html(k)}">{safe_html(sonarr_delete_mode_label(k))}</option>'
        for k in SONARR_DELETE_MODES
    )

    job_modal = f"""
    <div class="modalBack" id="jobBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="jobTitle">
        <div class="mh">
          <h3 id="jobTitle">Add Job</h3>
        </div>

        <form id="jobForm" method="post" action="/jobs/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" name="job_id" id="job_id" value="">

            <div class="form">
              <div class="field">
                <label>Job Name</label>
                <input type="text" name="name" id="job_name" value="New Job" required>
              </div>

              <div class="field">
                <label>App</label>
                <select name="APP_ID" id="job_app" onchange="onJobAppChanged()"
                        data-default-app="{safe_html(default_app_id)}" {app_disabled_attr} required>
                  {app_options_html}
                </select>
              </div>

              <div class="field">
                <label>Tag Label</label>
                <select name="TAG_LABEL" id="job_tag" required>
                  <option value="" selected disabled>-- Select a tag --</option>
                </select>
              </div>

              <div class="field">
                <label>Days Old</label>
                <input type="number" min="1" name="DAYS_OLD" id="job_days" value="30" required>
              </div>

              <div class="field" id="sonarrDeleteModeField" style="display:none;">
                <label>Sonarr Delete Mode</label>
                <select name="SONARR_DELETE_MODE" id="job_sonarr_mode">
                  {sonarr_mode_opts}
                </select>
              </div>

              <div class="field">
                <label>Scheduler Day</label>
                <select name="SCHED_DAY" id="job_day">
                  <option value="daily">Daily</option>
                  <option value="mon">Monday</option>
                  <option value="tue">Tuesday</option>
                  <option value="wed">Wednesday</option>
                  <option value="thu">Thursday</option>
                  <option value="fri">Friday</option>
                  <option value="sat">Saturday</option>
                  <option value="sun">Sunday</option>
                </select>
              </div>

              <div class="field">
                <label>Scheduler Time</label>
                <select name="SCHED_HOUR" id="job_hour">
                  {hour_opts}
                </select>
              </div>

              <div class="field">
                <label>Enabled</label>
                <select name="enabled" id="job_enabled">
                  <option value="1">Enabled</option>
                  <option value="0">Disabled</option>
                </select>
              </div>
            </div>

            <div class="checks" style="margin-top:12px;">
              <label class="check">
                <input type="checkbox" id="job_dry" name="DRY_RUN" checked>
                <div>
                  <div style="font-weight:700;">Dry Run</div>
                  <div class="muted">Log only; no deletes.</div>
                </div>
              </label>

              <label class="check">
                <input type="checkbox" id="job_delete" name="DELETE_FILES" checked>
                <div>
                  <div style="font-weight:700;">Delete Files</div>
                  <div class="muted">Remove files from disk.</div>
                </div>
              </label>

              <label class="check">
                <input type="checkbox" id="job_excl" name="ADD_IMPORT_EXCLUSION">
                <div>
                  <div style="font-weight:700;">Add Import Exclusion</div>
                  <div class="muted">Prevents re-import.</div>
                </div>
              </label>
            </div>
          </div>

          <div class="mf">
            <button class="btn" type="button" onclick="maybeCloseJobModal()">Cancel</button>
            <button class="btn primary" type="submit">Save Job</button>
          </div>
        </form>
      </div>
    </div>
    """

    job_cards = []
    for j0 in cfg["JOBS"]:
        j = normalize_job(j0)
        a = find_app(cfg, j.get("APP_ID"))
        if a:
            app_kind = a.get("type", "radarr")
            app_label = f"{'Radarr' if app_kind=='radarr' else 'Sonarr'} • {a.get('name','App')}"
        else:
            app_kind = "radarr"
            app_label = "Missing app"

        sched = schedule_label(j["SCHED_DAY"], j["SCHED_HOUR"])
        tag_val = j.get("TAG_LABEL") or "—"

        dry_val = "ON" if j.get("DRY_RUN") else "OFF"
        del_val = "ON" if j.get("DELETE_FILES") else "OFF"
        excl_val = "ON" if j.get("ADD_IMPORT_EXCLUSION") else "OFF"

        sonarr_mode_line = ""
        if a and a.get("type") == "sonarr":
            sonarr_mode_line = f"""
              <div class="metaRow">
                <div class="metaLabel">Sonarr mode:</div>
                <div class="metaVal"><b>{safe_html(sonarr_delete_mode_label(j.get("SONARR_DELETE_MODE")))}</b></div>
              </div>
            """

        edit_btn = f"""
          <button class="btn"
                  type="button"
                  onclick="openEditJob(this)"
                  data-id="{safe_html(j["id"])}"
                  data-name="{safe_html(j["name"])}"
                  data-enabled="{ '1' if j["enabled"] else '0' }"
                  data-app-id="{safe_html(j.get("APP_ID",""))}"
                  data-tag="{safe_html(j["TAG_LABEL"])}"
                  data-sonarr-mode="{safe_html(j.get('SONARR_DELETE_MODE','episodes_only'))}"
                  data-days="{j["DAYS_OLD"]}"
                  data-day="{safe_html(j["SCHED_DAY"])}"
                  data-hour="{j["SCHED_HOUR"]}"
                  data-dry="{ '1' if j["DRY_RUN"] else '0' }"
                  data-del="{ '1' if j["DELETE_FILES"] else '0' }"
                  data-excl="{ '1' if j["ADD_IMPORT_EXCLUSION"] else '0' }">Edit</button>
        """

        delete_btn = f"""
          <form method="post" action="/jobs/delete" style="margin:0;"
                onsubmit="return confirm('Are you sure you want to delete this job?');">
            <input type="hidden" name="job_id" value="{safe_html(j["id"])}">
            <button class="btn bad" type="submit">Delete</button>
          </form>
        """

        job_cards.append(f"""
          <div class="jobCard">
            <div class="jobHeader">
              <div class="jobHeaderLeft">
                <div class="jobName">{safe_html(j["name"])}</div>
              </div>

              <div class="jobHeaderCenter">
                <a class="btn" href="/preview?job_id={safe_html(j["id"])}">Preview</a>
              </div>

              <div class="jobHeaderRight">
                <form method="post" action="/jobs/toggle-enabled" style="margin:0;">
                  <input type="hidden" name="job_id" value="{safe_html(j["id"])}">
                  <div class="enableWrap">
                    <div class="enableLbl">Enable</div>
                    <label class="switch" title="Enable/Disable Job">
                      <input type="checkbox" name="enabled" {"checked" if j["enabled"] else ""} onchange="this.form.submit()">
                      <span class="slider"></span>
                    </label>
                  </div>
                </form>
              </div>
            </div>

            <div class="jobBody">
              <div class="metaStack">
                <div class="metaRow">
                  <div class="metaLabel">App:</div>
                  <div class="metaVal"><b>{safe_html(app_label)}</b></div>
                </div>

                <div class="metaRow">
                  <div class="metaLabel">Tag:</div>
                  <div class="metaVal"><b>{safe_html(tag_val)}</b></div>
                </div>

                <div class="metaRow">
                  <div class="metaLabel">Older than:</div>
                  <div class="metaVal"><b>{int(j["DAYS_OLD"])} days</b></div>
                </div>

                {sonarr_mode_line}

                <div class="metaRow">
                  <div class="metaLabel">Schedule:</div>
                  <div class="metaVal"><b>{safe_html(sched)}</b></div>
                </div>

                <div class="metaRow">
                  <div class="metaLabel">Delete files:</div>
                  <div class="metaVal"><b>{del_val}</b></div>
                </div>

                <div class="metaRow">
                  <div class="metaLabel">Import Exclusion:</div>
                  <div class="metaVal"><b>{excl_val}</b></div>
                </div>

                <div class="metaRow">
                  <div class="metaLabel">Dry-run:</div>
                  <div class="metaVal"><b>{dry_val}</b></div>
                </div>
              </div>

              <div class="jobRail">
                {run_now_button_html(j, app_label)}
                {edit_btn}
                {delete_btn}
              </div>
            </div>
          </div>
        """)

    can_add_job = len(ready_apps) > 0
    add_job_disabled_attr = "" if can_add_job else "disabled"
    add_job_title = "Add Job" if can_add_job else "Connect an app in Apps (Test Connection) to add a job."

    add_job_button = f"""
      <button class="btn primary" type="button" onclick="openNewJob()" {add_job_disabled_attr}
              title="{safe_html(add_job_title)}">Add Job</button>
    """

    hint_html = ""
    if not can_add_job:
        hint_html = """
          <div class="muted" style="margin-top:12px;">
            Add Job is disabled because no connected apps exist.
            Go to <a href="/apps"><b>Apps</b></a> and use <b>Test Connection</b>.
          </div>
        """

    body = f"""
      {tags_js}

      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Jobs</h2>
            <div class="btnrow">
              {add_job_button}
              <form method="post" action="/apply-cron" style="margin:0;">
                <button class="btn warn" type="submit">Apply Cron</button>
              </form>
            </div>
          </div>

          <div class="bd">
            <div class="jobsGrid">
              {''.join(job_cards)}
            </div>
            {hint_html}
          </div>
        </div>
      </div>

      {job_modal}
      {run_now_modal_html()}
    """
    return render_template_string(shell("mediareaparr • Jobs", "jobs", body))


@app.post("/jobs/save")
def jobs_save():
    cfg = load_config()
    try:
        job_id = (request.form.get("job_id") or "").strip()
        name = (request.form.get("name") or "Job").strip()
        enabled = (request.form.get("enabled") or "1").strip() == "1"

        app_id = (request.form.get("APP_ID") or "").strip()
        app_obj = find_app(cfg, app_id)
        if not app_obj or not is_app_ready(cfg, app_id):
            raise ValueError("Selected app is not available/connected. Go to Apps and Test Connection.")

        tag_label = (request.form.get("TAG_LABEL") or "").strip()
        if not tag_label:
            raise ValueError("Please select a tag.")

        sonarr_mode = (request.form.get("SONARR_DELETE_MODE") or "episodes_only").strip()
        if sonarr_mode not in SONARR_DELETE_MODES:
            sonarr_mode = "episodes_only"
        if app_obj.get("type") != "sonarr":
            sonarr_mode = "episodes_only"

        job = {
            "id": job_id or make_job_id(),
            "name": name,
            "enabled": enabled,
            "APP_ID": app_id,
            "TAG_LABEL": tag_label,
            "DAYS_OLD": clamp_int(request.form.get("DAYS_OLD") or 30, 1, 36500, 30),
            "SONARR_DELETE_MODE": sonarr_mode,
            "SCHED_DAY": (request.form.get("SCHED_DAY") or "daily").lower(),
            "SCHED_HOUR": clamp_int(request.form.get("SCHED_HOUR") or 3, 0, 23, 3),
            "DRY_RUN": checkbox("DRY_RUN"),
            "DELETE_FILES": checkbox("DELETE_FILES"),
            "ADD_IMPORT_EXCLUSION": checkbox("ADD_IMPORT_EXCLUSION"),
        }
        job = normalize_job(job)

        jobs = cfg.get("JOBS") or []
        replaced = False
        for i, jj in enumerate(jobs):
            if str(jj.get("id")) == job["id"]:
                jobs[i] = job
                replaced = True
                break
        if not replaced:
            jobs.append(job)

        cfg["JOBS"] = [normalize_job(x) for x in jobs]
        save_config(cfg)

        flash("Job saved ✔", "success")
        return redirect("/jobs")

    except Exception as e:
        flash(str(e), "error")
        return redirect("/jobs")


@app.post("/jobs/delete")
def jobs_delete():
    cfg = load_config()
    job_id = (request.form.get("job_id") or "").strip()
    jobs = [j for j in (cfg.get("JOBS") or []) if str(j.get("id")) != job_id]
    if not jobs:
        j = job_defaults()
        j["name"] = "Default Job"
        jobs = [normalize_job(j)]

    cfg["JOBS"] = [normalize_job(j) for j in jobs]
    save_config(cfg)
    flash("Job deleted ✔", "success")
    return redirect("/jobs")


@app.post("/jobs/run-now")
def jobs_run_now():
    cfg = load_config()
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        flash("Missing job id.", "error")
        return redirect("/jobs")

    job = find_job(cfg, job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect("/jobs")

    if not job.get("enabled", False):
        flash("This job is disabled. Enable it before running.", "error")
        return redirect("/jobs")

    app_obj = find_app(cfg, job.get("APP_ID"))
    if not app_obj or not is_app_ready(cfg, app_obj["id"]):
        flash("This job's app is missing or not connected. Fix it in Apps.", "error")
        return redirect("/apps")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (CONFIG_DIR / f"run_now_{job_id}.flag").write_text(now_iso(), encoding="utf-8")
    flash("Run Now triggered ✔ (check logs/dashboard)", "success")
    return redirect("/dashboard")


@app.post("/apply-cron")
def apply_cron():
    cfg = load_config()
    jobs = cfg.get("JOBS") or []
    enabled_jobs = [j for j in jobs if j.get("enabled")]

    if not enabled_jobs:
        flash("No enabled jobs to schedule.", "error")
        return redirect(request.referrer or "/jobs")

    log_path = "/var/log/mediareaparr.log"
    lines = []
    for j in enabled_jobs:
        cron = cron_from_day_hour(j.get("SCHED_DAY", "daily"), int(j.get("SCHED_HOUR", 3)))
        jid = str(j.get("id"))
        lines.append(f"{cron} python /app/app.py --job-id {jid} >> {log_path} 2>&1")

    cron_text = "\n".join(lines) + "\n"

    try:
        with open("/etc/crontabs/root", "w", encoding="utf-8") as f:
            f.write(cron_text)
        os.kill(1, signal.SIGHUP)
        flash("Cron schedule applied successfully ✔", "success")
    except Exception as e:
        flash(f"Failed to apply cron: {e}", "error")

    return redirect(request.referrer or "/jobs")


# ----------------------------
# Preview
# ----------------------------
@app.get("/preview")
def preview():
    cfg = load_config()
    job_id = (request.args.get("job_id") or "").strip()

    job = find_job(cfg, job_id)
    if not job:
        jobs = [normalize_job(j) for j in (cfg.get("JOBS") or [])]
        preferred = None
        for jj in jobs:
            a = find_app(cfg, jj.get("APP_ID"))
            if jj.get("enabled") and a and is_app_ready(cfg, a["id"]):
                preferred = jj
                break
        if preferred:
            job = preferred
        elif jobs:
            job = jobs[0]
        else:
            job = normalize_job(job_defaults())

    app_obj = find_app(cfg, job.get("APP_ID"))
    if not app_obj:
        flash("Job app not found. Edit the job and select an app.", "error")
        return redirect("/jobs")
    if not is_app_ready(cfg, app_obj["id"]):
        flash("Selected app is not connected/enabled. Fix it in Apps.", "error")
        return redirect("/apps")

    try:
        result = preview_candidates_sonarr(cfg, app_obj, job) if app_obj.get("type") == "sonarr" else preview_candidates_radarr(cfg, app_obj, job)

        error = result.get("error")
        candidates = result.get("candidates", [])
        cutoff = result.get("cutoff", "")

        if error:
            flash(error, "error")
            return redirect("/jobs")

        rows = ""
        for c in candidates[:500]:
            rows += f"""
              <tr>
                <td>{c["age_days"]}</td>
                <td>{safe_html(c.get("title",""))}</td>
                <td>{safe_html(str(c.get("year","")))}</td>
                <td><code>{safe_html(c.get("added",""))}</code></td>
                <td>{safe_html(str(c.get("id","")))}</td>
                <td class="muted">{safe_html(c.get("path","") or "")}</td>
              </tr>
            """

        app_label = f"{'Sonarr' if app_obj.get('type')=='sonarr' else 'Radarr'} • {app_obj.get('name','App')}"
        sonarr_mode_line = ""
        if app_obj.get("type") == "sonarr":
            sonarr_mode_line = f" • Mode: <b>{safe_html(sonarr_delete_mode_label(job.get('SONARR_DELETE_MODE')))}</b>"

        body = f"""
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Preview candidates</h2>
                <div class="btnrow">
                  <a class="btn" href="/jobs">Back to Jobs</a>
                  {run_now_button_html(job, app_label)}
                </div>
              </div>
              <div class="bd">
                <div class="muted">
                  App: <b>{safe_html(app_label)}</b>{sonarr_mode_line} • Job: <b>{safe_html(job["name"])}</b> • Tag <code>{safe_html(job["TAG_LABEL"])}</code> • Older than <code>{job["DAYS_OLD"]}</code> days
                </div>
                <div class="muted" style="margin-top:6px;">Found <b>{len(candidates)}</b> candidate(s). Preview only (no deletes).</div>
                <div class="muted" style="margin-top:6px;">Cutoff: <code>{safe_html(cutoff)}</code></div>

                <div class="tablewrap" style="margin-top:12px;">
                  <table>
                    <thead>
                      <tr>
                        <th>Age (days)</th>
                        <th>Title</th>
                        <th>Year</th>
                        <th>Added</th>
                        <th>ID</th>
                        <th>Path</th>
                      </tr>
                    </thead>
                    <tbody>{rows}</tbody>
                  </table>
                </div>
                <div class="muted" style="margin-top:10px;">Showing up to 500.</div>
              </div>
            </div>
          </div>
          {run_now_modal_html()}
        """
        return render_template_string(shell("mediareaparr • Preview", "jobs", body))

    except Exception as e:
        flash(f"Preview failed: {e}", "error")
        return redirect("/dashboard")


# ----------------------------
# Dashboard
# ----------------------------
@app.get("/dashboard")
def dashboard():
    state = load_state()
    last_run = state.get("last_run")

    if not last_run:
        body = """
          <div class="grid">
            <div class="card">
              <div class="hd">
                <h2>Dashboard</h2>
                <div class="btnrow">
                  <a class="btn" href="/jobs">Jobs</a>
                  <a class="btn" href="/apps">Apps</a>
                  <a class="btn" href="/settings">Settings</a>
                </div>
              </div>
              <div class="bd">
                <div class="muted">No runs recorded yet.</div>
              </div>
            </div>
          </div>
        """
        return render_template_string(shell("mediareaparr • Dashboard", "dash", body))

    status_text = str(last_run.get("status") or "").upper()
    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Dashboard</h2>
            <div class="btnrow">
              <a class="btn" href="/jobs">Jobs</a>
              <a class="btn" href="/apps">Apps</a>
              <a class="btn" href="/settings">Settings</a>
            </div>
          </div>
          <div class="bd">
            <div class="muted">Last run status: <b>{safe_html(status_text)}</b></div>
            <div class="muted" style="margin-top:6px;">Job: <b>{safe_html(str(last_run.get("job_name","")))}</b> (<code>{safe_html(str(last_run.get("job_id","")))}</code>)</div>
            <div class="muted" style="margin-top:6px;">Finished: <code>{safe_html(str(last_run.get("finished_at","")))}</code></div>
            <div class="muted" style="margin-top:6px;">Candidates: <b>{safe_html(str(last_run.get("candidates_found",0)))}</b></div>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Dashboard", "dash", body))


# ----------------------------
# Status
# ----------------------------
@app.get("/status")
def status():
    cfg = load_config()
    state = load_state()

    def render_kv(d: Dict[str, Any]) -> str:
        rows = []
        for k, v in d.items():
            if k == "APPS":
                apps_list = [normalize_app(a) for a in (v or [])]
                parts = []
                for a in apps_list[:50]:
                    typ = a.get("type")
                    nm = a.get("name")
                    ok = "ok" if a.get("ok") else "not-ok"
                    en = "enabled" if a.get("enabled") else "disabled"
                    parts.append(f"{nm} ({typ}, {en}, {ok}, url={a.get('url','')})")
                summary = "; ".join(parts) + (" …" if len(apps_list) > 50 else "")
                rows.append(
                    f"<tr><td><code>{safe_html(k)}</code></td>"
                    f"<td class='muted'>{safe_html(summary) if summary else safe_html(f'[{len(apps_list)} apps]')}</td></tr>"
                )
            elif k == "JOBS":
                jobs = [normalize_job(x) for x in (v or [])]
                parts = []
                for j in jobs[:50]:
                    parts.append(f"{j.get('name','Job')} (app_id={j.get('APP_ID','')}, tag={j.get('TAG_LABEL','')})")
                summary = "; ".join(parts) + (" …" if len(jobs) > 50 else "")
                rows.append(
                    f"<tr><td><code>{safe_html(k)}</code></td>"
                    f"<td class='muted'>{safe_html(summary) if summary else safe_html(f'[{len(jobs)} jobs]')}</td></tr>"
                )
            elif "API_KEY" in str(k).upper():
                rows.append(f"<tr><td><code>{safe_html(k)}</code></td><td class='muted'>***</td></tr>")
            else:
                rows.append(f"<tr><td><code>{safe_html(k)}</code></td><td class='muted'>{safe_html(v)}</td></tr>")
        return "".join(rows)

    # Mask API keys inside APPS when rendering full cfg
    cfg_for_view = dict(cfg)
    cfg_for_view["APPS"] = []
    for a in (cfg.get("APPS") or []):
        aa = normalize_app(a)
        aa["api_key"] = "***" if aa.get("api_key") else ""
        cfg_for_view["APPS"].append(aa)

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd"><h2>Status</h2></div>
          <div class="bd">
            <div class="muted">Config file: <code>{safe_html(str(CONFIG_PATH))}</code> (exists: <b>{str(CONFIG_PATH.exists()).lower()}</b>)</div>
            <div class="muted" style="margin-top:8px;">State file: <code>{safe_html(str(STATE_PATH))}</code> (exists: <b>{str(STATE_PATH.exists()).lower()}</b>)</div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>Config Key</th><th>Value</th></tr></thead>
                <tbody>{render_kv(cfg_for_view)}</tbody>
              </table>
            </div>

            <div style="margin-top:14px;" class="tablewrap">
              <table>
                <thead><tr><th>State Key</th><th>Value</th></tr></thead>
                <tbody>{render_kv(state)}</tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Status", "status", body))


if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "7575")))
    args = p.parse_args()
    app.run(host=args.host, port=args.port)

