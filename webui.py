import os
import json
import signal
import uuid
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import requests
import logging
from logging.handlers import RotatingFileHandler
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages, send_from_directory, Response
)

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

LOG_PATH = Path(os.environ.get("LOG_PATH", str(CONFIG_DIR / "mediareaparr.log")))

APP_DIR = Path(__file__).resolve().parent
APP_LOGO_DIR = APP_DIR / "logo"

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mediareaparr-secret")


# ----------------------------
# Logging
# ----------------------------
def _setup_logging() -> logging.Logger:
    """Log to stdout + rotating log file at LOG_PATH (used by /logs + Status page)."""
    logger = logging.getLogger("mediareaparr.webui")
    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    fmt = logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
    # File handler
    try:
        fh = RotatingFileHandler(str(LOG_PATH), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setLevel(logging.INFO)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception:
        # If file isn't writable, we'll still have stdout logs
        pass

    sh = logging.StreamHandler()
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    logger.propagate = False
    logger.info("WebUI logging initialised. LOG_PATH=%s", str(LOG_PATH))
    return logger


log = _setup_logging()

@app.before_request
def _log_request():
    try:
        log.info("HTTP %s %s from %s", request.method, request.path, request.remote_addr)
    except Exception:
        pass

@app.after_request
def _log_response(resp):
    try:
        log.info("HTTP %s %s -> %s", request.method, request.path, resp.status_code)
    except Exception:
        pass
    return resp




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
        "type": "radarr",  # radarr|sonarr
        "name": "New App",
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


def is_app_ready(cfg: Dict[str, Any], app_id: str) -> bool:
    a = find_app(cfg, app_id)
    if not a:
        return False
    return bool(a.get("url") and a.get("api_key") and a.get("ok"))


def _norm_url_key(url: str, api_key: str) -> tuple:
    u = (url or "").strip().rstrip("/").lower()
    k = (api_key or "").strip()
    return (u, k)


def find_duplicate_app(apps_list: List[Dict[str, Any]], url: str, api_key: str, exclude_id: str = "") -> Optional[
    Dict[str, Any]]:
    u, k = _norm_url_key(url, api_key)
    if not u or not k:
        return None
    ex = (exclude_id or "").strip()
    for a in apps_list:
        aa = normalize_app(a)
        if ex and aa.get("id") == ex:
            continue
        au, ak = _norm_url_key(aa.get("url"), aa.get("api_key"))
        if au == u and ak == k:
            return aa
    return None


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
        # Radarr-only: delete movie if avg score below threshold
        "RADARR_SCORE_FILTER_ENABLED": False,
        "RADARR_MIN_AVG_SCORE": 60,  # 0-100
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

    # Radarr-only score filter
    d["RADARR_SCORE_FILTER_ENABLED"] = bool(d.get("RADARR_SCORE_FILTER_ENABLED", False))
    d["RADARR_MIN_AVG_SCORE"] = clamp_int(d.get("RADARR_MIN_AVG_SCORE", 60), 0, 100, 60)

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
    cfg = {
        # Dynamic apps list (NO migration, NO defaults)
        "APPS": [],

        # WebUI/global
        "HTTP_TIMEOUT_SECONDS": int(env_default("HTTP_TIMEOUT_SECONDS", "30")),
        "UI_THEME": env_default("UI_THEME", "dark"),

        # Jobs
        "JOBS": [],
    }

    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k in list(cfg.keys()):
                if k in data:
                    cfg[k] = data[k]
        except Exception:
            pass

    # Normalize theme/timeout
    t = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = t if t in ("dark", "light") else "dark"
    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 30), 5, 300, 30)

    apps = cfg.get("APPS") or []
    if not isinstance(apps, list):
        apps = []
    cfg["APPS"] = [normalize_app(a) for a in apps]

    jobs = cfg.get("JOBS") or []
    if not isinstance(jobs, list):
        jobs = []
    jobs = [normalize_job(j) for j in jobs]
    if not jobs:
        # Keep a default job so UI isn't empty, but it won't be runnable without apps
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
    """
    Best-effort tag fetch. Never throws (so Jobs page can't 500).
    """
    if not is_app_ready(cfg, app_id):
        return []
    app_obj = find_app(cfg, app_id)
    if not app_obj:
        return []
    try:
        tags = app_get(cfg, app_obj, "/api/v3/tag")
        return sorted(
            {t.get("label") for t in (tags or []) if t.get("label")},
            key=lambda x: str(x).lower()
        )
    except Exception:
        # App is offline / timeout / bad gateway / etc.
        return []


def _score_to_0_100(v) -> Optional[int]:
    try:
        if v is None:
            return None
        f = float(v)
        # If it's a 0–10 style rating, convert to 0–100
        if 0 <= f <= 10:
            return int(round(f * 10))
        # If it's already 0–100
        if 0 <= f <= 100:
            return int(round(f))
    except Exception:
        return None
    return None


def radarr_movie_score_0_100(movie: Dict[str, Any]) -> Optional[int]:
    ratings = movie.get("ratings") or {}
    values = []

    for src in ratings.values():
        if not isinstance(src, dict):
            continue
        v = src.get("value")
        if v is None:
            continue
        try:
            v = float(v)
        except Exception:
            continue

        if 0 <= v <= 10:
            values.append(v * 10)
        elif 0 <= v <= 100:
            values.append(v)

    if not values:
        return None

    return int(round(sum(values) / len(values)))


# ----------------------------
# Preview candidates (uses selected app instance)
# ----------------------------
def preview_candidates_radarr(cfg: Dict[str, Any], app_obj: Dict[str, Any], job: Dict[str, Any]):
    tag_label = (job.get("TAG_LABEL") or "").strip()
    if not tag_label:
        return {"error": "Tag is empty. Edit the job and select a tag.", "candidates": [], "cutoff": ""}

    days_old = int(job.get("DAYS_OLD", 30))
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days_old)

    # Radarr score filter (preview should match delete logic)
    score_filter_enabled = bool(job.get("RADARR_SCORE_FILTER_ENABLED", False))
    min_score = clamp_int(job.get("RADARR_MIN_AVG_SCORE", 60), 0, 100, 60)

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
        if added >= cutoff:
            continue

        # Compute score once so we can filter + display consistently
        score = radarr_movie_score_0_100(m)

        # Apply score gating (only include items that would be deleted)
        if score_filter_enabled:
            # If no score is available, skip from preview to avoid false positives
            if score is None:
                continue
            # Movies scoring >= threshold should NOT be shown in delete preview
            if score >= min_score:
                continue

        age_days = int((now - added).total_seconds() // 86400)
        candidates.append({
            "kind": "movie",
            "id": m.get("id"),
            "title": m.get("title"),
            "year": m.get("year"),
            "added": added_str,
            "age_days": age_days,
            "score": score,
            "path": m.get("path"),
        })

    candidates.sort(key=lambda x: x["age_days"], reverse=True)
    return {"error": None, "candidates": candidates, "tag_id": tag_id, "cutoff": cutoff.isoformat()}


def preview_candidates_sonarr(cfg: Dict[str, Any], app_obj: Dict[str, Any], job: Dict[str, Any]):
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
    --accent:#a7d541;
    --accent2:#16a34a;
    --warn:#f59e0b;
    --bad:#ef4444;
    --shadow: 0 12px 28px rgba(0,0,0,.28);
    --top-h: 60px;
    --sidebar-w: 210px;
    --HeaderBackgroundColor: #1b2431;
    --fs-1: 13px;
    --fs-3: 16px;
    --btn-fs: 10px;
    --btn-py: 7px;
    --btn-px: 9px;
    --btn-gap: 6px;
    --switch-w: 42px;
    --switch-h: 20px;
    --switch-thumb: 14px;
    --switch-pad: 3px;
    --switch-travel: calc(var(--switch-w) - var(--switch-thumb) - (var(--switch-pad) * 2));
  }

  [data-theme="dark"]{
    --bg:#111827;
    --panel:#1f2937;
    --panel2:#1b2431;
    --HeaderBackgroundColor:#2a2a2a;
    --sidebarBackgroundColor:#2a2a2a;
    --BackgroundColor1:#333333;
    --FieldinptuColor:#595959;
    --sidebarActiveBackgroundColor:#333333;
    --toolbarBackgroundColor:#262626;
    --pageBackgroundColor:#202020;
    --muted:#9ca3af;
    --text:#f1f5f9;
    --line:#212d3d;
    --line2:#212d3d;
    --inputbox_border:#505d6f;
    --inputbox_background:#1f2937;
    --accent:#a7d541;
    --accent2:#16a34a;
    --warn:#f59e0b;
    --bad:#ef4444;
    --shadow: 0 12px 28px rgba(0,0,0,.55);
  }

  [data-theme="light"]{
    --bg:#f6f7fb;
    --panel:#ffffff;
    --panel2:#f1f5f9;
    --HeaderBackgroundColor:#ffffff;
    --sidebarBackgroundColor:#ffffff;
    --sidebarActiveBackgroundColor:#e5e7eb;
    --toolbarBackgroundColor:#ffffff;
    --pageBackgroundColor:#f5f7fa;
    --text:#0f172a;
    --muted:#475569;
    --line:#e2e8f0;
    --line2:#cbd5e1;
    --accent:#a7d541;
    --accent2:#16a34a;
    --BackgroundColor1:#ffffff;
    --FieldinptuColor:#ffffff;
    --inputbox_border:#cbd5e1;
    --inputbox_background:#ffffff;
    --warn:#b45309;
    --bad:#dc2626;
    --shadow: 0 10px 24px rgba(2,6,23,.10);
  }

  body[data-theme="dark"] input[type="checkbox"],
  body[data-theme="light"] input[type="checkbox"]{
    accent-color: var(--accent);
  }

  body[data-theme="light"] a,
  body[data-theme="light"] a:hover{ color: #0f172a; }

  body[data-theme="light"] .btn{
    border-color: var(--line2);
  }

   /* ------------------------------------------------
      FakeSelect (custom dropdown) - per theme
      ------------------------------------------------ */
   .nativeSelect{
     position:absolute !important;
     left:-9999px !important;
     width:1px !important;
     height:1px !important;
     opacity:0 !important;
     pointer-events:none !important;
   }

   .fakeSelect{ width:100%; position:relative; }
   .fakeSelectBtn{
     width:100%;
     border:1px solid var(--BackgroundColor1);
     border-radius:8px;
     background:var(--FieldinptuColor);
     color:var(--text);
     padding:10px 15px 10px 10px;
     outline:none;
     cursor:pointer;
     display:flex;
     align-items:center;
     justify-content:space-between;
     gap:10px;
   }
   .fakeSelectBtn:disabled{
     opacity:.45;
     cursor:not-allowed;
     filter:grayscale(.35);
   }
   .fakeSelectValue{
     min-width:0;
     overflow:hidden;
     text-overflow:ellipsis;
     white-space:nowrap;
     text-align:left;
   }
   .fakeSelectChevron{
     width:10px; height:10px;
     transform: rotate(45deg);
     border-right:2px solid var(--muted);
     border-bottom:2px solid var(--muted);
     flex:0 0 auto;
     margin-top:-2px;
   }
   .fakeSelectMenu{
     position:absolute;
     left:0; right:0;
     top:calc(100% + 3px);
     z-index:2000;
     background:var(--FieldinptuColor);
     border:1px solid var(--line2);
     box-shadow:var(--shadow);
     max-height:260px;
     overflow:auto;
     display:none;
     border-radius:10px;
     padding:4px;
   }
   .fakeSelect.open .fakeSelectMenu{ display:block; }

   .fakeOpt{
     padding:6px 8px;
     border-radius:8px;
     cursor:pointer;
     color:var(--text);
     user-select:none;
   }
   .fakeOpt + .fakeOpt{ margin-top:4px; }
   .fakeOpt[data-disabled="1"]{ opacity:.6; cursor:not-allowed; }

   /* per-theme highlight colours */
   body[data-theme="dark"] .fakeOpt:hover,
   body[data-theme="dark"] .fakeOpt[aria-selected="true"],
   body[data-theme="dark"] .fakeOpt.active{
     background: #e5e7eb;
     color: #111827;
   }

   body[data-theme="light"] .fakeOpt:hover,
   body[data-theme="light"] .fakeOpt[aria-selected="true"],
   body[data-theme="light"] .fakeOpt.active{
     background: rgba(34,197,94,.85);
     color: #04130a;
   }

  body.sbCollapsed{ --sidebar-w: 0px; }

  *, *::before, *::after { box-sizing: border-box; }
  html, body{ height: 100%; }

  /* ===========================
     Themed scrollbars
     =========================== */

  /* ---------- Firefox ---------- */
  body[data-theme="dark"] *,
  body[data-theme="light"] *{
    scrollbar-width: thin;
    scrollbar-color: var(--accent) var(--panel2);
  }

  /* ---------- WebKit (Chrome / Edge / Safari) ---------- */

  ::-webkit-scrollbar{
    width: 10px;
    height: 10px;
  }

  ::-webkit-scrollbar-track{
    background: var(--panel2);
  }

  /* DARK THEME — green thumb */
  body[data-theme="dark"] ::-webkit-scrollbar-thumb{
    background: rgba(167,213,65,.45);
     border-radius: 8px;
    border: 2px solid var(--panel2);
  }

  body[data-theme="dark"] ::-webkit-scrollbar-thumb:hover{
    background: rgba(167,213,65,.65);
  }

  /* LIGHT THEME — neutral thumb */
  body[data-theme="light"] ::-webkit-scrollbar-thumb{
   background: var(--FieldinptuColor);
   border-radius: 8px;
   border: 2px solid var(--BackgroundColor1);
  }

  body[data-theme="light"] ::-webkit-scrollbar-thumb:hover{
    background: rgba(148,163,184,.85);
  }

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

  /* Light theme: soften the bottom landing gradient (dark one looks heavy on light UI) */
  body[data-theme="light"]:after{
    background: linear-gradient(to bottom, rgba(255,255,255,0), rgba(2,6,23,.06));
  }

  body:after{
    content:"";
    position: fixed;
    left: 0; right: 0; bottom: 0;
    height: 140px;
    pointer-events: none;
    background: linear-gradient(to bottom, rgba(0,0,0,0), rgba(0,0,0,.35));
    z-index: 0;
  }

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
    z-index: 50;
    overflow: hidden;
    margin: 0 !important;
    box-shadow: 0 0px 28px rgba(0, 0, 0, .55);
  }

  /* Light theme header shadow should be subtle */
  body[data-theme="light"] .pageHeader{
    box-shadow: 0 8px 18px rgba(2,6,23,.10);
  }

  body[data-theme="light"] .sidebar{
    border-right: 3px solid var(--line);
  }

  .pageHeader .ptIn{
    height: var(--top-h);
    display: grid;
    grid-template-columns: var(--sidebar-w) 1fr auto;
    align-items: center;
    padding: 0;
    background: var(--HeaderBackgroundColor);
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
    border-right: 3px solid var(--sidebarActiveBackgroundColor);
    background: var(--sidebarBackgroundColor);
    box-shadow: none;
    overflow: hidden;
    z-index: 40;
    display:flex;
    flex-direction: column;
  }

  /* Light theme sidebar items should read as dark text */
  body[data-theme="light"] button.sbItem,
  body[data-theme="light"] .sbItem{
    color: var(--text);
  }

  body[data-theme="light"] .sbItem:hover{ color: var(--accent2); }

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

  .sbItem:active, .sbItem:active span, .sbItem:focus, .sbItem:focus span{
    color: inherit;
  }

  .sbItem:hover{
    color: #97c13d;
  }

  .sbItem.active{
    box-shadow: none;
  }

  body[data-theme="light"] .sbItem.active{
    background: var(--sidebarActiveBackgroundColor);
    color: var(--accent);
    border-left: 3px solid var(--accent);
  }

  body[data-theme="dark"] .sbItem.active{
    background: var(--sidebarActiveBackgroundColor);
    color: var(--accent);
    border-left: 3px solid var(--accent);
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
    z-index: 10;
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
    background: var(--pageBackgroundColor);
    box-shadow: var(--shadow);
    overflow:hidden;
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
  }

  /* Light theme cards: slightly clearer separation */
  body[data-theme="light"] .card{
    border: 1px solid var(--line);
  }
  body[data-theme="light"] .jobCard{
    border: 1px solid var(--line);
  }

  .card .hd{
    padding: 14px 16px;
    display:flex;
    align-items:center;
    height: 60px;
    min-height: 60px;
    max-height: 60px;
    justify-content: space-between;
    gap:12px;
    background: var(--toolbarBackgroundColor);
    overflow: hidden;
    position: sticky;
    top: 0;
    z-index: 2;
    flex: 0 0 auto;
  }

  .card .hd h2{
    margin:0;
    font-size: 14px;
    letter-spacing:.2px;
  }

  .card .bd{
    padding: 14px 16px;
    background: var(--pageBackgroundColor);
    min-height: 0;
    overflow: auto;
    flex: 1 1 auto;
  }

  .muted{ color: var(--muted); }
  .btnrow{ display:flex; gap:10px; flex-wrap: wrap; align-items:center; }

  .btn{
    border: 1px solid var(--line2);
    background: var(--HeaderBackgroundColor);
    color: var(--text);
    padding: var(--btn-py) var(--btn-px);
    font-weight: 600;
    font-size: var(--btn-fs);
    gap: var(--btn-gap);
    border-radius: 6px;
    cursor:pointer;
    display: inline-flex;
    align-items: center;
    transition: box-shadow .18s ease, border-color .18s ease, transform .18s ease, filter .18s ease;
  }

  /* Light theme: hover glow should be lighter (avoid dark heavy glow) */
  body[data-theme="light"] .btn:hover{
    box-shadow: 0 0 0 3px rgba(167,213,65,.16), 0 10px 18px rgba(2,6,23,.10);
  }

  a.btn:hover{ text-decoration: none; }

  .btn:hover{
    border-color: rgba(34,197,94,.55);
    box-shadow: 0 0 0 3px rgba(34,197,94,.10), 0 10px 22px rgba(0,0,0,.22);
    transform: translateY(-1px);
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

  .field{
    padding: 4px 7px;
    background: var(--BackgroundColor1);
    position: relative;
    min-width: 0;
  }

  /* Light theme inputs: ensure borders are visible */
  body[data-theme="light"] .field input[type=text],
  body[data-theme="light"] .field input[type=password],
  body[data-theme="light"] .field input[type=number],
  body[data-theme="light"] .field select,
  body[data-theme="light"] .field textarea{
    border-color: var(--line2);
    background: var(--FieldinptuColor);
    color: var(--text);
  }

  .field label{ display:block; font-size: 14px; color: var(--text); margin-bottom: 8px; }

  .field input[type=text],
  .field input[type=password],
  .field input[type=number],
  .field select,
  .field textarea{
    width: 100%;
    max-width: 100%;
    min-width: 0;
    border: 1px solid var(--BackgroundColor1);
    border-radius: 8px;
    background: var(--FieldinptuColor);
    color: var(--text);
    padding: 10px 10px;
    outline: none;
  }

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

  .field input:focus, .field select:focus, .field textarea:focus{
    border-color: var(--BackgroundColor1);
    box-shadow: 0 0 0 3px var(--BackgroundColor1);
  }

  .checks{ display:flex; flex-direction: column; gap: 10px; margin-top: 4px; }
  .check{
    display:flex; align-items:center; gap:10px;
    padding: 10px 12px;
  }
  .check input{ transform: scale(1.2); }

  .check input:focus-visible{
    outline: none;
    box-shadow: 0 0 0 3px rgba(34,197,94,.35);
  }

  .check input:disabled{
    opacity: .45;
    cursor: not-allowed;
  }

  /* Radarr score filter row (match existing field/check styling) */
  .scoreRow{
    display:flex;
    gap:10px;
    align-items:center;
    flex-wrap:wrap;
  }
  .scoreInline{
    display:flex;
    align-items:center;
    gap:10px;
    margin:0;
    flex:1 1 auto;
  }

.scoreNumInput{
  width:90px;
  min-width:90px;
}
  .scoreRow .scoreCheck{
    flex: 1 1 260px;
    min-width: 240px;
  }
  .scoreRow .scoreNum{
    width: 140px;
    min-width: 140px;
  }
  .switch{ position: relative; width: var(--switch-w); height: var(--switch-h); display: inline-block; flex: 0 0 auto; }
  .switch input{ opacity: 0; width: 0; height: 0; }
  .slider{
    position: absolute;
    inset: 0;
    border-radius: 999px;
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
    border-radius: 50%;
    transform: translateY(-50%);
    background: rgba(255,255,255,.85);
    transition: .18s ease;
    box-shadow: 0 4px 10px rgba(0,0,0,.25);
  }
  .switch input:checked + .slider{
    background: linear-gradient(135deg, rgba(34,197,94,.60), rgba(22,163,74,.35));
    border-color: rgba(34,197,94,.55);
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
    border: 3px solid var(--BackgroundColor1);
    background: var(--BackgroundColor1);
    box-shadow: var(--shadow);
    overflow:hidden;
    max-width: none;
    width: 100%;
  }
  @media (min-width: 700px){ .jobsGrid{ grid-template-columns: repeat(2, minmax(300px, 1fr)); } }
  @media (min-width: 1200px){ .jobsGrid{ grid-template-columns: repeat(3, minmax(300px, 1fr)); gap: 16px; } }
  @media (min-width: 1800px){ .jobsGrid{ grid-template-columns: repeat(4, minmax(300px, 1fr)); gap: 20px; } }

  .jobHeader{
    padding: 12px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--HeaderBackgroundColor);
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 10px;
  }

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
    background: var(--BackgroundColor1);
    display: grid;
    grid-template-columns: 1fr 70px;
    gap: 12px;
    align-items: start;
  }

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

  .metaStack{ display:flex; flex-direction: column; gap: 6px; font-size: 11px; }
  .metaRow{ display:flex; align-items: baseline; gap: 8px; line-height: 1.35; }
  .metaLabel{ width: 110px; color: var(--muted); flex: 0 0 auto; }
  .metaVal{ color: var(--text); flex: 1 1 auto; min-width: 0; word-break: break-word; }

  /* Apps grid */
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
    border: 3px solid var(--line);
    background: var(--panel2);
    box-shadow: var(--shadow);
    display: flex;
    flex-direction: column;
    justify-content: space-between;
    padding: 14px 14px;
    position: relative;
    user-select: none;
  }

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

  .appCardLinkBtn{
    width: 26px; height: 26px;
    display:flex; align-items:center; justify-content:center;
    background: transparent;
    cursor: pointer;
    opacity: .9;
  }
  .appCardLinkBtn:hover{ opacity: 1; }

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

  .addAppCard{
    align-items: center;
    justify-content: center;
    padding: 0;
    cursor: pointer;
  }
  .addAppCardInner{
    width: 86px;
    height: 54px;
    border: 2px solid #8ba3af;
    display:flex;
    justify-content:center;
    font-size: 42px;
    color: var(--muted);
    line-height: 1;
  }

  /* ---------------------------
     Add App picker (tile grid)
     --------------------------- */
  .pickGrid{
    display: grid;
    gap: 12px;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  @media (max-width: 520px){
    .pickGrid{ grid-template-columns: 1fr; }
  }
  .pickTile{
    border: 1px solid var(--line);
    background: var(--panel2);
    box-shadow: var(--shadow);
    padding: 14px 14px;
    cursor: pointer;
    user-select: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 110px;
  }

  [data-theme="light"] .pickTile{ background: #ffffff; }
  .pickTile:hover{
    border-color: rgba(34,197,94,.55);
    box-shadow: 0 0 0 3px rgba(34,197,94,.10), 0 10px 22px rgba(0,0,0,.22);
    transform: translateY(-1px);
  }
  .pickTop{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }
  .pickTitle{
    font-weight: 800;
    font-size: 14px;
    letter-spacing: .2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .pickMeta{
    font-size: 12px;
    color: var(--muted);
    line-height: 1.35;
  }
  .pickActions{
    display: flex;
    gap: 8px;
    margin-top: auto;
    align-items: center;
    justify-content: flex-start;
  }
  .pickMini{
    padding: 6px 8px;
    font-size: 10px;
  }

  .modalBack{
    position: fixed; inset: 0;
    background: rgba(0,0,0,.68);
    backdrop-filter: blur(6px);
    display:none;
    align-items:center;
    justify-content:center;
    z-index: 1000;
    padding: 18px;
  }

  /* ---------------------------
     Modal open: lock background UI
     --------------------------- */
  body.modalOpen{
    overflow: hidden;
  }

  /* Darken + blur header & sidebar when modal is open */
  body.modalOpen .pageHeader,
  body.modalOpen .sidebar{
    filter: blur(4px) brightness(0.5);
    transition: filter .0s ease;
  }

  /* Prevent interaction with blurred UI */
  body.modalOpen .pageHeader,
  body.modalOpen .sidebar,
  body.modalOpen .mainArea{
    pointer-events: none;
  }

  /* Keep modal interactive */
  body.modalOpen .modalBack{
    pointer-events: auto;
  }

  body.modalOpen .pageContent,
  body.modalOpen .card .bd{
    overflow: hidden !important;
  }  

  .modalBack.jobsModal{
    z-index: 1010;
  }

  .jobsModal .modal{
    z-index: 1011;
  }

  .modal{
    width: min(475px, 100%);
    border: 3px solid var(--BackgroundColor1);
    background: var(--BackgroundColor1);
    box-shadow: var(--shadow);
    overflow:hidden;
    max-height: calc(100vh - 315px);
    display:flex;
    flex-direction: column;
    min-height: 0;
  }
  .modal .mh{
    padding: 14px 16px;
    border-bottom: 3px solid var(--BackgroundColor1);
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 12px;
    background: var(--HeaderBackgroundColor);
    overflow: auto;
    flex: 0 0 auto;
  }
  .modal .mh h3{ margin:0; font-size: 14px; letter-spacing: .2px; }

  .modal form{
    display:flex;
    flex-direction: column;
    flex: 1 1 auto;
    min-height: 0;
  }

  .modal .mb{
    padding: 14px 16px;
    background: var(--BackgroundColor1);
    overflow: auto;
    flex: 1 1 auto;
    min-height: 0;
    -webkit-overflow-scrolling: touch;
  }
  .modal .mf{
    padding: 14px 16px;
    border-top: 1px solid var(--BackgroundColor1);
    display:flex;
    justify-content: flex-end;
    gap: 10px;
    background: var(--BackgroundColor1);
    flex: 0 0 auto;
  }

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
    z-index: 1100;
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
  @keyframes toastIn { to { opacity: 1; transform: translateY(0); } }
  @keyframes toastOut { to { opacity: 0; transform: translateY(10px); } }

  .card, .jobCard{ border-radius: 0 !important; }
  .card .hd, .card .bd{ border-radius: 0 !important; }

  /* ---------------------------
     App config modal (match screenshot)
     --------------------------- */
  .appModalShell{ width: min(400px, 100%); }

  .modalCloseX{
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 20px;
    line-height: 1;
    cursor: pointer;
    padding: 6px 8px;
  }
  .modalCloseX:hover{ color: var(--text); }

  .appGrid{
    display: flex;
    flex-wrap: wrap;
    column-gap: 16px;
    row-gap: 14px;
    align-items: flex-start;
  }
  .appGrid > .appLbl{
    width: 170px;
    flex: 0 0 170px;
  }
  .appGrid > .appCtrl{
    flex: 1 1 calc(100% - 170px);
    min-width: 0;
  }
  @media (max-width: 720px){
    .appGrid{ grid-template-columns: 1fr; }
  }
  .appLbl{
    padding-top: 10px;
    font-weight: 600;
    color: var(--text);
  }
  @media (max-width: 720px){
    .appLbl{ padding-top: 0; }
  }
  .appCtrl{ min-width: 0; }
  .appCtrl input[type=text],
  .appCtrl input[type=password]{
    width: 100%;
    max-width: 100%;
    min-width: 0;
    border: 3px solid var(--inputbox_border);
    border-radius: 8px;
    background: var(--inputbox_background);
    color: var(--text);
    padding: 10px 10px;
    outline: none;
  }
  [data-theme="light"] .appCtrl input[type=text],
  [data-theme="light"] .appCtrl input[type=password]{ background: #ffffff; }

  .appHelp{
    margin-top: 6px;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.35;
  }

  .appFooter{
    display:flex;
    align-items:center;
    justify-content: flex-end;
    gap: 10px;
    width: 100%;
  }

  /* When Delete is visible, pin it left while keeping the other buttons right */
  .appFooter > .btn.bad{
    margin-right: auto;
  }

  .appFooterRight{
    display:flex;
    align-items:center;
    justify-content: flex-end;
    gap: 10px;
  }

  /* ----------------------------
     Status log window
     ---------------------------- */
  .logTools{
    display:flex;
    gap:10px;
    align-items:center;
    flex-wrap:wrap;
    margin-top:12px;
  }
  .logBox{
    margin-top:10px;
    border:1px solid var(--line);
    background:rgba(0,0,0,.25);
    padding:12px;
    border-radius:12px;
    height:360px;
    overflow:auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    font-size: 12px;
    line-height: 1.35;
    white-space: pre;
  }
  body[data-theme="light"] .logBox{
    background: rgba(2,6,23,.03);
  }

</style>

<script>
  function $(id){ return document.getElementById(id); }
  function setVal(id, v){ const el = $(id); if (el) el.value = v; }
  function setChecked(id, v){ const el = $(id); if (el) el.checked = !!v; }

   // -----------------------
   // FakeSelect (custom dropdown) - keeps native <select> hidden for form submit
   // -----------------------
   function initFakeSelect(fake){
     if (!fake) return;
     const selectId = fake.getAttribute("data-for");
     const native = document.getElementById(selectId);
     if (!native) return;

     const btn = fake.querySelector(".fakeSelectBtn");
     const val = fake.querySelector(".fakeSelectValue");
     const menu = fake.querySelector(".fakeSelectMenu");

     function syncDisabled(){
       const dis = !!native.disabled;
       if (btn) btn.disabled = dis;
       fake.classList.toggle("disabled", dis);
     }

     function close(){
       fake.classList.remove("open");
       if (btn) btn.setAttribute("aria-expanded", "false");
     }

     function open(){
       if (native.disabled) return;
       buildMenu();
       fake.classList.add("open");
       if (btn) btn.setAttribute("aria-expanded", "true");
       if (menu) menu.focus();
     }

     function setSelectedByValue(v){
       native.value = v;
       native.dispatchEvent(new Event("change", { bubbles:true }));
       buildMenu(); // refresh active/selected
     }

     function buildMenu(){
       if (!menu) return;
       menu.innerHTML = "";
       syncDisabled();

       const opts = Array.from(native.options || []);
       for (const o of opts){
         const item = document.createElement("div");
         item.className = "fakeOpt";
         item.setAttribute("role","option");
         item.setAttribute("data-value", o.value);

         const isDisabled = !!o.disabled || (o.value === "" && o.disabled);
         if (isDisabled) item.setAttribute("data-disabled","1");

         const isSelected = (native.value === o.value);
         item.setAttribute("aria-selected", isSelected ? "true" : "false");
         if (isSelected) item.classList.add("active");

         item.textContent = o.textContent;
         item.addEventListener("click", () => {
           if (isDisabled) return;
           setSelectedByValue(o.value);
           close();
           if (btn) btn.focus();
         });
         menu.appendChild(item);
       }

       // button text
       const cur = native.options[native.selectedIndex];
       if (val) val.textContent = cur ? cur.textContent : "-- Select --";
     }

     // initial
     buildMenu();

     if (btn){
       btn.addEventListener("click", (e) => {
         e.preventDefault();
         if (fake.classList.contains("open")) close();
         else open();
       });
     }

     // outside click close
     document.addEventListener("mousedown", (e) => {
       if (!fake.contains(e.target)) close();
     });

     // keyboard in menu
     if (menu){
       menu.addEventListener("keydown", (e) => {
         const items = Array.from(menu.querySelectorAll(".fakeOpt"))
           .filter(x => x.getAttribute("data-disabled") !== "1");
         if (!items.length) return;

         const active = menu.querySelector(".fakeOpt.active") || items[0];
         let idx = items.indexOf(active);

         if (e.key === "Escape"){
           e.preventDefault();
           close();
           if (btn) btn.focus();
           return;
         }
         if (e.key === "ArrowDown"){
           e.preventDefault();
           idx = Math.min(items.length - 1, idx + 1);
           items.forEach(x => x.classList.remove("active"));
           items[idx].classList.add("active");
           items[idx].scrollIntoView({ block:"nearest" });
           return;
         }
         if (e.key === "ArrowUp"){
           e.preventDefault();
           idx = Math.max(0, idx - 1);
           items.forEach(x => x.classList.remove("active"));
           items[idx].classList.add("active");
           items[idx].scrollIntoView({ block:"nearest" });
           return;
         }
         if (e.key === "Enter" || e.key === " "){
           e.preventDefault();
           const v = items[idx].getAttribute("data-value");
           setSelectedByValue(v);
           close();
           if (btn) btn.focus();
           return;
         }
       });
     }

     // if native changes (your code rebuilds options), refresh fake
     native.addEventListener("change", () => buildMenu());

     // expose hooks for manual rebuild / set
     fake.__rebuild = buildMenu;
     fake.__set = setSelectedByValue;
     fake.__syncDisabled = syncDisabled;
   }

   function initAllFakeSelects(root){
     const scope = root || document;
     scope.querySelectorAll(".fakeSelect").forEach(initFakeSelect);
   }
  function updateModalState(){
    const backs = document.querySelectorAll(".modalBack");
    let anyOpen = false;
    for (const el of backs){
      if (el && getComputedStyle(el).display !== "none"){
        anyOpen = true;
        break;
      }
    }
    document.body.classList.toggle("modalOpen", anyOpen);
  }

  function showModal(id){
    const el = $(id);
    if (el) el.style.display = "flex";
    updateModalState();
  }

  function hideModal(id){
    const el = $(id);
    if (el) el.style.display = "none";
    updateModalState();
  }

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
  // Apps: dirty tracking + cancel confirmation
  // -----------------------
  window.__APP_MODAL_INITIAL = { r:"", s:"" };
  window.__APP_MODAL_DIRTY = { r:false, s:false };

  function appFormSnapshot(prefix){
    const form = $("appForm_" + prefix);
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

  function appModalMarkClean(prefix){
    window.__APP_MODAL_INITIAL[prefix] = appFormSnapshot(prefix);
    window.__APP_MODAL_DIRTY[prefix] = false;
  }

  function appModalUpdateDirty(prefix){
    const snap = appFormSnapshot(prefix);
    window.__APP_MODAL_DIRTY[prefix] = (snap !== window.__APP_MODAL_INITIAL[prefix]);
  }

  function maybeCloseAppModal(prefix){
    const back = (prefix === "r") ? $("appBackRadarr") : $("appBackSonarr");
    if (!back || back.style.display !== "flex") {
      hideModal(prefix === "r" ? "appBackRadarr" : "appBackSonarr");
      return;
    }
    appModalUpdateDirty(prefix);
    if (window.__APP_MODAL_DIRTY[prefix]){
      if (!confirm("Discard changes to this application?")) return;
    }
    hideModal(prefix === "r" ? "appBackRadarr" : "appBackSonarr");
  }

  // -----------------------
  // Apps: selector -> open correct modal
  // -----------------------
  function openRadarrAdd(){
    // Add mode
    setVal("app_id_r", "");
    setVal("app_mode_r", "add");
    setVal("app_test_ok_r", "0");
    setVal("app_name_r", "Radarr");
    setVal("app_url_r", "http://localhost:7878");
    setVal("app_key_r", "");
    const used = $("appUsedWarn_r");
    if (used) used.style.display = "none";
    const st = $("appTestStatus_r");
    if (st) st.textContent = "";

    // (we cannot setVal on h3; do via textContent)
    const t = $("appModalTitle_r"); if (t) t.textContent = "Add Application - Radarr";

    // Delete hidden in add mode
    const del = $("appDeleteBtn_r"); if (del) del.style.display = "none";

    refreshAppButtons("r");
    showModal("appBackRadarr");
    setTimeout(() => appModalMarkClean("r"), 0);
  }

  function openSonarrAdd(){
    setVal("app_id_s", "");
    setVal("app_mode_s", "add");
    setVal("app_test_ok_s", "0");
    setVal("app_name_s", "Sonarr");
    setVal("app_url_s", "http://localhost:8989");
    setVal("app_key_s", "");
    const used = $("appUsedWarn_s");
    if (used) used.style.display = "none";
    const st = $("appTestStatus_s");
    if (st) st.textContent = "";

    const t = $("appModalTitle_s"); if (t) t.textContent = "Add Application - Sonarr";

    const del = $("appDeleteBtn_s"); if (del) del.style.display = "none";

    refreshAppButtons("s");
    showModal("appBackSonarr");
    setTimeout(() => appModalMarkClean("s"), 0);
  }

  function openEditApp(appId){
    const apps = (window.__APP_CFG && window.__APP_CFG.APPS) ? window.__APP_CFG.APPS : [];
    const a = apps.find(x => (x.id || "") === (appId || ""));
    if (!a) return;

    const usage = (window.__APP_CFG && window.__APP_CFG.APP_USAGE) ? window.__APP_CFG.APP_USAGE : {};
    const usedN = Number(usage[a.id] || 0);

    if ((a.type || "radarr") === "sonarr"){
      setVal("app_id_s", a.id || "");
      setVal("app_mode_s", "edit");
      setVal("app_test_ok_s", (a.ok ? "1" : "0"));
      setVal("app_name_s", a.name || "Sonarr");
      setVal("app_url_s", a.url || "");
      setVal("app_key_s", a.api_key || "");

      const title = $("appModalTitle_s"); if (title) title.textContent = "Edit Application - Sonarr";

      const used = $("appUsedWarn_s");
      if (used){
        if (usedN > 0){
          used.style.display = "";
          used.textContent = `This application is used by ${usedN} job${usedN===1?"":"s"}.`;
        } else {
          used.style.display = "none";
        }
      }

      const del = $("appDeleteBtn_s"); if (del) del.style.display = "";
      const st = $("appTestStatus_s"); if (st) st.textContent = "";

      refreshAppButtons("s");
      showModal("appBackSonarr");
      setTimeout(() => appModalMarkClean("s"), 0);
      return;
    }

    // radarr
    setVal("app_id_r", a.id || "");
    setVal("app_mode_r", "edit");
    setVal("app_test_ok_r", (a.ok ? "1" : "0"));
    setVal("app_name_r", a.name || "Radarr");
    setVal("app_url_r", a.url || "");
    setVal("app_key_r", a.api_key || "");

    const title = $("appModalTitle_r"); if (title) title.textContent = "Edit Application - Radarr";

    const used = $("appUsedWarn_r");
    if (used){
      if (usedN > 0){
        used.style.display = "";
        used.textContent = `This application is used by ${usedN} job${usedN===1?"":"s"}.`;
      } else {
        used.style.display = "none";
      }
    }

    const del = $("appDeleteBtn_r"); if (del) del.style.display = "";
    const st = $("appTestStatus_r"); if (st) st.textContent = "";

    refreshAppButtons("r");
    showModal("appBackRadarr");
    setTimeout(() => appModalMarkClean("r"), 0);
  }

  function submitDeleteApp(prefix){
    const id = ($("app_id_" + prefix)?.value || "").trim();
    if (!id) return;
    if (!confirm("Delete this app? Jobs using it must be updated first.")) return;
    const f = $("appDeleteForm");
    const hid = $("app_delete_id");
    if (hid) hid.value = id;
    if (f) f.submit();
  }

  // -----------------------
  // Apps: button gating
  // - Disable Test if URL OR API key empty
  // - Disable Save until test passes
  // - If URL/API changes, reset test_ok -> 0
  // -----------------------
  function refreshAppButtons(prefix){
    const url = (($("app_url_" + prefix)?.value || "") + "").trim();
    const key = (($("app_key_" + prefix)?.value || "") + "").trim();
    const testBtn = $("appTestBtn_" + prefix);
    const saveBtn = $("appSaveBtn_" + prefix);
    const testOk = ($("app_test_ok_" + prefix)?.value || "") === "1";

    if (testBtn) testBtn.disabled = (url === "" || key === "");
    if (saveBtn) saveBtn.disabled = !testOk;
  }

  function invalidateAppTest(prefix){
    const ok = $("app_test_ok_" + prefix);
    if (ok) ok.value = "0";
    refreshAppButtons(prefix);
  }

  async function submitAppTest(prefix){
    const form = $("appForm_" + prefix);
    if (!form) return;

    // Do not close modal on Test (AJAX)
    const statusEl = $("appTestStatus_" + prefix);
    if (statusEl) statusEl.textContent = "Testing...";

    // Ensure gating
    refreshAppButtons(prefix);
    const testBtn = $("appTestBtn_" + prefix);
    if (testBtn && testBtn.disabled) {
      if (statusEl) statusEl.textContent = "Enter URL and API key to test.";
      return;
    }

    try{
      const fd = new FormData(form);
      const resp = await fetch("/apps/test?ajax=1", { method:"POST", body: fd });
      const data = await resp.json();

      if (data && data.ok){
        // Accept only if detected_type matches modal kind
        const expected = (prefix === "s") ? "sonarr" : "radarr";
        const detected = (data.detected_type || expected).toLowerCase();

        if (detected !== expected){
          if (statusEl) statusEl.textContent = `Connected, but detected ${detected}. Use the ${detected.charAt(0).toUpperCase()+detected.slice(1)} modal instead.`;
          setVal("app_test_ok_" + prefix, "0");
        } else {
          if (statusEl) statusEl.textContent = data.message || "Connection OK";
          setVal("app_test_ok_" + prefix, "1");
        }
      } else {
        if (statusEl) statusEl.textContent = (data && data.message) ? data.message : "Connection failed.";
        setVal("app_test_ok_" + prefix, "0");
      }
    } catch(e){
      if (statusEl) statusEl.textContent = "Test failed (network/response).";
      setVal("app_test_ok_" + prefix, "0");
    }

    refreshAppButtons(prefix);
    setTimeout(() => appModalUpdateDirty(prefix), 0);
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
      hideModal("appPickBack");
      maybeCloseAppModal("r");
      maybeCloseAppModal("s");
      maybeCloseJobModal();
    }
  });

  // -----------------------
  // Add Application picker (tile grid)
  // -----------------------
  function openAddAppModal(){
    showModal("appPickBack");
  }

  function pickAppType(t){
  t = (t || "radarr").toLowerCase();
  hideModal("appPickBack");
  if (t === "sonarr") openSonarrAdd();
  else openRadarrAdd();
  }

  function appMoreInfo(t){
    t = (t || "").toLowerCase();
    const supported = (t === "radarr" || t === "sonarr");
    const msg = supported
      ? (t === "radarr"
          ? "Radarr manages movies. MediaReaparr can clean up by tag + age."
          : "Sonarr manages series. MediaReaparr can clean up by tag + age, plus a delete mode.")
      : "Coming soon in MediaReaparr.";
    alert(msg);
  }

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
      // refresh fake select UI
      const fake = $("fake_job_tag");
      if (fake && fake.__rebuild) fake.__rebuild();
  }

  function updateSonarrModeVisibility(appId){
    const wrap = $("sonarrDeleteModeField");
    const sel = $("job_sonarr_mode");
    const fakeWrap = $("fakeWrap_job_sonarr_mode");
    const t = (window.__APP_TYPES && window.__APP_TYPES[appId]) ? window.__APP_TYPES[appId] : "radarr";
    const isSonarr = (t === "sonarr");
    if (wrap) wrap.style.display = isSonarr ? "" : "none";
    if (fakeWrap) fakeWrap.style.display = isSonarr ? "" : "none";
    if (sel) sel.disabled = !isSonarr;
    const fake = $("fake_job_sonarr_mode");
    if (fake && fake.__syncDisabled) fake.__syncDisabled();
  }

   function updateRadarrScoreVisibility(appId){
     const wrap = $("radarrScoreField");
     const cb = $("job_score_enabled");
     const num = $("job_score_min");
     const t = (window.__APP_TYPES && window.__APP_TYPES[appId]) ? window.__APP_TYPES[appId] : "radarr";
     const isRadarr = (t === "radarr");
     if (wrap) wrap.style.display = isRadarr ? "" : "none";
     if (cb) cb.disabled = !isRadarr;
     if (num) num.disabled = !isRadarr;
   }

  function onJobAppChanged(){
    const appSel = $("job_app");
    const appId = appSel ? (appSel.value || "") : "";
    rebuildTagOptions(appId, "");
    updateSonarrModeVisibility(appId);
    updateRadarrScoreVisibility(appId);
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
    updateRadarrScoreVisibility(actualApp);

    setVal("job_sonarr_mode", "episodes_only");
    setVal("job_days", "30");
    setVal("job_day", "daily");
    setVal("job_hour", "3");
    setChecked("job_dry", true);
    setChecked("job_delete", true);
    setChecked("job_excl", false);
    setVal("job_enabled", "1");

    // Radarr score filter defaults
    setChecked("job_score_enabled", false);
    setVal("job_score_min", "60");

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
    updateRadarrScoreVisibility(appId);

    const smode = btn.getAttribute("data-sonarr-mode") || "episodes_only";
    setVal("job_sonarr_mode", smode);

    setVal("job_days", btn.getAttribute("data-days") || "30");
    setVal("job_day", btn.getAttribute("data-day") || "daily");
    setVal("job_hour", btn.getAttribute("data-hour") || "3");
    setChecked("job_dry", (btn.getAttribute("data-dry") || "1") === "1");
    setChecked("job_delete", (btn.getAttribute("data-del") || "1") === "1");
    setChecked("job_excl", (btn.getAttribute("data-excl") || "0") === "1");
    setVal("job_enabled", (btn.getAttribute("data-enabled") || "1"));

    // Radarr score filter
    setChecked("job_score_enabled", (btn.getAttribute("data-score-en") || "0") === "1");
    setVal("job_score_min", btn.getAttribute("data-score-min") || "60");

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

    const br = $("appBackRadarr");
    if (br && br.style.display === "flex") {
      const form = $("appForm_r");
      if (form && form.contains(e.target)) {
        if (e.target && (e.target.id === "app_url_r" || e.target.id === "app_key_r")) invalidateAppTest("r");
        appModalUpdateDirty("r");
        refreshAppButtons("r");
      }
    }
    const bs = $("appBackSonarr");
    if (bs && bs.style.display === "flex") {
      const form = $("appForm_s");
      if (form && form.contains(e.target)) {
        if (e.target && (e.target.id === "app_url_s" || e.target.id === "app_key_s")) invalidateAppTest("s");
        appModalUpdateDirty("s");
        refreshAppButtons("s");
      }
    }
  });
  document.addEventListener("change", (e) => {
    const back = $("jobBack");
    if (back && back.style.display === "flex") {
      const form = $("jobForm");
      if (form && form.contains(e.target)) jobModalUpdateDirty();
    }
    const br = $("appBackRadarr");
    if (br && br.style.display === "flex") appModalUpdateDirty("r");
    const bs = $("appBackSonarr");
    if (bs && bs.style.display === "flex") appModalUpdateDirty("s");
  });

  document.addEventListener("DOMContentLoaded", () => {
    try {
      const v = localStorage.getItem("sbCollapsed");
      if (v === "1") document.body.classList.add("sbCollapsed");
    } catch(e){}

    // init FakeSelects
    initAllFakeSelects(document);

    // Prevent clicks on interactive elements inside an appCard (links/buttons)
    // from also triggering the card's click handler (which opens Edit).
    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!t) return;
      const card = t.closest ? t.closest(".appCard") : null;
      if (!card) return;
      const interactive = t.closest ? t.closest("a,button") : null;
      if (interactive) e.stopPropagation();
    }, true);

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
  });
</script>
"""


def shell(page_title: str, active: str, body: str):
    cfg = load_config()
    theme = (cfg.get("UI_THEME") or "dark").lower()
    if theme not in ("dark", "light"):
        theme = "dark"

    def sb_item(name, href, key):
        cls = "sbItem active" if active == key else "sbItem"
        return f'<a class="{cls}" href="{href}"><span class="sbText">{safe_html(name)}</span></a>'

    next_theme = "light" if theme == "dark" else "dark"
    next_label = "Light" if theme == "dark" else "Dark"

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
<body data-theme="{safe_html(theme)}">
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
    nxt = "light" if cur == "dark" else "dark"
    cfg["UI_THEME"] = nxt
    save_config(cfg)
    flash(f"Theme set to {cfg['UI_THEME']} ✔", "success")
    return redirect(request.referrer or "/dashboard")


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
                <label>UI Theme</label>
                 <select class="nativeSelect" id="settings_theme" name="UI_THEME">
                   <option value="dark" {"selected" if cfg.get("UI_THEME", "dark") == "dark" else ""}>Dark</option>
                   <option value="light" {"selected" if cfg.get("UI_THEME", "dark") == "light" else ""}>Light</option>
                 </select>

                 <div class="fakeSelect" data-for="settings_theme" id="fake_settings_theme">
                   <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                     <span class="fakeSelectValue">Theme</span>
                     <span class="fakeSelectChevron" aria-hidden="true"></span>
                   </button>
                   <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
                 </div>
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

    if cfg["UI_THEME"] not in ("dark", "light"):
        cfg["UI_THEME"] = "dark"

    save_config(cfg)
    flash("Settings saved ✔", "success")
    return redirect("/settings")


# ----------------------------
# Apps page + modals
# ----------------------------
def app_modals_html(cfg: Dict[str, Any], usage: Dict[str, int]) -> str:
    cfg_js = {
        "APPS": [normalize_app(a) for a in (cfg.get("APPS") or [])],
        "APP_USAGE": usage or {}
    }

    return f"""
    <script>
      window.__APP_CFG = {json.dumps(cfg_js)};
    </script>

    <form id="appDeleteForm" method="post" action="/apps/delete" style="display:none;">
      <input type="hidden" id="app_delete_id" name="APP_ID" value="">
    </form>

    <!-- ADD APP PICKER (this was missing, so the + card had nothing to open) -->
    <div class="modalBack" id="appPickBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="appPickTitle">
        <div class="mh">
          <h3 id="appPickTitle">Add Application</h3>
          <button class="modalCloseX" type="button" onclick="hideModal('appPickBack')" aria-label="Close">×</button>
        </div>
        <div class="mb">
          <div class="muted" style="margin-bottom:10px;">
            Choose what you want to connect.
          </div>

          <div class="pickGrid">
            <div class="pickTile" role="button" tabindex="0"
                 onclick="pickAppType('radarr')"
                 onkeydown="if(event.key==='Enter'||event.key===' '){{ event.preventDefault(); pickAppType('radarr'); }}">
              <div class="pickTop">
                <div class="pickTitle">Radarr</div>
                <span class="pill good">Movies</span>
              </div>
              <div class="pickMeta">Manage movies. Clean up by tag + age.</div>
              <div class="pickActions">
                <button class="btn pickMini" type="button" onclick="event.stopPropagation(); appMoreInfo('radarr')">More info</button>
                <button class="btn primary pickMini" type="button" onclick="event.stopPropagation(); pickAppType('radarr')">Add</button>
              </div>
            </div>

            <div class="pickTile" role="button" tabindex="0"
                 onclick="pickAppType('sonarr')"
                 onkeydown="if(event.key==='Enter'||event.key===' '){{ event.preventDefault(); pickAppType('sonarr'); }}">
              <div class="pickTop">
                <div class="pickTitle">Sonarr</div>
                <span class="pill good">TV</span>
              </div>
              <div class="pickMeta">Manage series. Clean up by tag + age + delete mode.</div>
              <div class="pickActions">
                <button class="btn pickMini" type="button" onclick="event.stopPropagation(); appMoreInfo('sonarr')">More info</button>
                <button class="btn primary pickMini" type="button" onclick="event.stopPropagation(); pickAppType('sonarr')">Add</button>
              </div>
            </div>
          </div>
        </div>
        <div class="mf">
          <button class="btn" type="button" onclick="hideModal('appPickBack')">Cancel</button>
        </div>
      </div>
    </div>


    <!-- RADARR MODAL -->
    <div class="modalBack" id="appBackRadarr">
      <div class="modal appModalShell" role="dialog" aria-modal="true" aria-labelledby="appModalTitle_r">
        <div class="mh">
          <h3 id="appModalTitle_r">Add Application - Radarr</h3>
          <button class="modalCloseX" type="button" onclick="maybeCloseAppModal('r')" aria-label="Close">×</button>
        </div>

        <form id="appForm_r" method="post" action="/apps/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" id="app_id_r" name="APP_ID" value="">
            <input type="hidden" id="app_mode_r" name="APP_MODE" value="add">
            <input type="hidden" id="app_test_ok_r" name="APP_TEST_OK" value="0">
            <input type="hidden" name="APP_TYPE" value="radarr">

            <div class="appGrid">
              <div class="appLbl">Name</div>
              <div class="appCtrl">
                <input id="app_name_r" type="text" name="APP_NAME" value="">
              </div>

              <div class="appLbl">Radarr Server</div>
              <div class="appCtrl">
                <input id="app_url_r" type="text" name="APP_URL" value="">
                <div class="appHelp">URL used to connect to Radarr server, including http(s)://, port, and urlbase if required</div>
              </div>

              <div class="appLbl">API Key</div>
              <div class="appCtrl">
                <input id="app_key_r" type="password" name="APP_API_KEY" value="">
                <div class="appHelp">The ApiKey generated by Radarr in Settings/General</div>
              </div>
            </div>

            <div id="appUsedWarn_r" class="muted" style="margin-top:10px; display:none;"></div>
            <div class="muted" id="appTestStatus_r" style="margin-top:8px;"></div>
          </div>

          <div class="mf">
            <div class="appFooter">
              <button class="btn bad" type="button" id="appDeleteBtn_r" style="display:none;" onclick="submitDeleteApp('r')">Delete</button>
              <div class="appFooterRight">
                <button class="btn good" id="appTestBtn_r" type="button" onclick="submitAppTest('r')">Test</button>
                <button class="btn" type="button" onclick="maybeCloseAppModal('r')">Cancel</button>
                <button class="btn primary" id="appSaveBtn_r" type="submit" disabled>Save</button>
              </div>
            </div>
          </div>
        </form>
      </div>
    </div>

    <!-- SONARR MODAL -->
    <div class="modalBack" id="appBackSonarr">
      <div class="modal appModalShell" role="dialog" aria-modal="true" aria-labelledby="appModalTitle_s">
        <div class="mh">
          <h3 id="appModalTitle_s">Add Application - Sonarr</h3>
          <button class="modalCloseX" type="button" onclick="maybeCloseAppModal('s')" aria-label="Close">×</button>
        </div>

        <form id="appForm_s" method="post" action="/apps/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" id="app_id_s" name="APP_ID" value="">
            <input type="hidden" id="app_mode_s" name="APP_MODE" value="add">
            <input type="hidden" id="app_test_ok_s" name="APP_TEST_OK" value="0">
            <input type="hidden" name="APP_TYPE" value="sonarr">

            <div class="appGrid">
              <div class="appLbl">Name</div>
              <div class="appCtrl">
                <input id="app_name_s" type="text" name="APP_NAME" value="">
              </div>

              <div class="appLbl">Sonarr Server</div>
              <div class="appCtrl">
                <input id="app_url_s" type="text" name="APP_URL" value="">
                <div class="appHelp">URL used to connect to Sonarr server, including http(s)://, port, and urlbase if required</div>
              </div>

              <div class="appLbl">API Key</div>
              <div class="appCtrl">
                <input id="app_key_s" type="password" name="APP_API_KEY" value="">
                <div class="appHelp">The ApiKey generated by Sonarr in Settings/General</div>
              </div>
            </div>

            <div id="appUsedWarn_s" class="muted" style="margin-top:10px; display:none;"></div>
            <div class="muted" id="appTestStatus_s" style="margin-top:8px;"></div>
          </div>

          <div class="mf">
            <div class="appFooter">
              <button class="btn bad" type="button" id="appDeleteBtn_s" style="display:none;" onclick="submitDeleteApp('s')">Delete</button>
              <div class="appFooterRight">
                <button class="btn good" id="appTestBtn_s" type="button" onclick="submitAppTest('s')">Test</button>
                <button class="btn" type="button" onclick="maybeCloseAppModal('s')">Cancel</button>
                <button class="btn primary" id="appSaveBtn_s" type="submit" disabled>Save</button>
              </div>
            </div>
          </div>
        </form>
      </div>
    </div>
    """


@app.get("/apps")
def apps():
    cfg = load_config()
    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    # usage map: app_id -> number of jobs referencing it
    jobs = [normalize_job(j) for j in (cfg.get("JOBS") or [])]
    usage: Dict[str, int] = {}
    for j in jobs:
        aid = str(j.get("APP_ID") or "").strip()
        if not aid:
            continue
        usage[aid] = usage.get(aid, 0) + 1

    def card(a: Dict[str, Any]) -> str:
        a = normalize_app(a)
        kind = a.get("type", "radarr")
        title = a.get("name", "App")
        ok = bool(a.get("ok", False))
        url = str(a.get("url") or "")
        app_id = safe_html(a.get("id"))

        href = (url or "").strip()
        ext = ""
        if href:
            ext = f"""<a class="appCardLinkBtn" href="{safe_html(href)}" target="_blank" rel="noreferrer" title="Open {safe_html(title)}">
              ↗
            </a>"""
        else:
            ext = """<div class="appCardLinkBtn" title="No URL set" style="opacity:.4; cursor:default;">↗</div>"""

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
              {add_card}
              {app_cards}
            </div>
          </div>
        </div>
      </div>

      {app_modals_html(cfg, usage)}
    """
    return render_template_string(shell("mediareaparr • Apps", "apps", body))


def _system_status(url: str, api_key: str, timeout_s: int) -> Dict[str, Any]:
    r = requests.get(
        (url or "").rstrip("/") + "/api/v3/system/status",
        headers={"X-Api-Key": api_key or ""},
        timeout=timeout_s,
    )
    if r.status_code in (401, 403):
        raise PermissionError("Unauthorized (API key incorrect).")
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


def detect_app_type_from_status(status: Dict[str, Any]) -> Optional[str]:
    if not isinstance(status, dict):
        return None
    candidates = [
        status.get("appName"),
        status.get("applicationName"),
        status.get("name"),
        status.get("instanceName"),
    ]
    txt = " ".join([str(x) for x in candidates if x]).lower()
    if "sonarr" in txt:
        return "sonarr"
    if "radarr" in txt:
        return "radarr"
    return None


@app.post("/apps/save")
def apps_save():
    cfg = load_config()

    app_id = (request.form.get("APP_ID") or "").strip()
    app_type = (request.form.get("APP_TYPE") or "radarr").strip().lower()
    name = (request.form.get("APP_NAME") or "").strip()
    url = (request.form.get("APP_URL") or "").strip().rstrip("/")
    api_key = (request.form.get("APP_API_KEY") or "").strip()
    test_ok = (request.form.get("APP_TEST_OK") or "0").strip() == "1"

    if app_type not in ("radarr", "sonarr"):
        flash("Unknown app type.", "error")
        return redirect("/apps")

    # Save disabled unless Test passes (server-side enforcement)
    if not test_ok:
        flash("Please run Test and ensure it passes before saving.", "error")
        return redirect("/apps")

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    # Duplicate detection (same URL + API key)
    dup = find_duplicate_app(apps_list, url, api_key, exclude_id=app_id)
    if dup:
        flash(f"Duplicate application detected: matches '{dup.get('name', 'App')}'. (Same URL + API key)", "error")
        return redirect("/apps")

    if app_id:
        updated = False
        for i, a in enumerate(apps_list):
            if a["id"] == app_id:
                a["type"] = app_type
                a["name"] = name or a["name"]
                a["url"] = url
                a["api_key"] = api_key
                a["ok"] = True  # since test_ok is true
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
            "url": url,
            "api_key": api_key,
            "ok": True,
        }))

    cfg["APPS"] = [normalize_app(a) for a in apps_list]
    save_config(cfg)
    flash("App saved ✔", "success")
    return redirect("/apps")


@app.post("/apps/test")
def apps_test():
    cfg = load_config()

    app_id = (request.form.get("APP_ID") or "").strip()
    app_type = (request.form.get("APP_TYPE") or "radarr").strip().lower()
    name = (request.form.get("APP_NAME") or "").strip()
    url = (request.form.get("APP_URL") or "").strip().rstrip("/")
    api_key = (request.form.get("APP_API_KEY") or "").strip()

    is_ajax = (request.args.get("ajax") or "").strip() == "1"

    if app_type not in ("radarr", "sonarr"):
        msg = "Unknown app type."
        if is_ajax:
            return {"ok": False, "message": msg}
        flash(msg, "error")
        return redirect("/apps")

    if not url or not api_key:
        msg = "URL and API key are required to test."
        if is_ajax:
            return {"ok": False, "message": msg}
        flash(msg, "error")
        return redirect("/apps")

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]

    # Duplicate detection (same URL + API key)
    dup = find_duplicate_app(apps_list, url, api_key, exclude_id=app_id)
    if dup:
        msg = f"Duplicate application detected: matches '{dup.get('name', 'App')}'. (Same URL + API key)"
        if is_ajax:
            return {"ok": False, "message": msg}
        flash(msg, "error")
        return redirect("/apps")

    kind = "Radarr" if app_type == "radarr" else "Sonarr"

    try:
        status = _system_status(url, api_key, int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)))
        detected = detect_app_type_from_status(status) or app_type

        if is_ajax:
            # Do NOT close modal; JS will decide whether to accept test_ok based on detected_type
            return {"ok": True, "message": f"{kind} connection OK", "detected_type": detected}

        # Non-AJAX fallback: store ok and redirect
        if app_id:
            for i, a in enumerate(apps_list):
                if a["id"] == app_id:
                    a["type"] = app_type
                    a["name"] = name or a["name"]
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
                "url": url,
                "api_key": api_key,
                "ok": True,
            }))

        cfg["APPS"] = [normalize_app(a) for a in apps_list]
        save_config(cfg)
        flash(f"{kind} connected ✔", "success")
        return redirect("/apps")

    except PermissionError as e:
        msg = f"{kind} connection failed: {e}"
    except requests.exceptions.ConnectTimeout:
        msg = f"{kind} connection failed: timeout connecting to the host."
    except requests.exceptions.ConnectionError:
        msg = f"{kind} connection failed: could not connect (URL/host/network)."
    except Exception as e:
        msg = f"{kind} connection failed: {e}"

    if is_ajax:
        return {"ok": False, "message": msg, "detected_type": app_type}

    flash(msg, "error")
    return redirect("/apps")


@app.post("/apps/delete")
def apps_delete():
    cfg = load_config()
    app_id = (request.form.get("APP_ID") or "").strip()
    if not app_id:
        return redirect("/apps")

    # Prevent deleting an app referenced by jobs
    jobs = [normalize_job(j) for j in (cfg.get("JOBS") or [])]
    used_by = [j for j in jobs if str(j.get("APP_ID") or "") == app_id]
    if used_by:
        names = ", ".join([str(j.get("name") or "Job") for j in used_by[:6]])
        more = " …" if len(used_by) > 6 else ""
        flash(f"Cannot delete this app: used by job(s): {names}{more}. Update/delete those jobs first.", "error")
        return redirect("/apps")

    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]
    apps_list = [a for a in apps_list if a["id"] != app_id]
    cfg["APPS"] = apps_list
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
    state = load_state()
    last_runs = state.get("last_runs") if isinstance(state.get("last_runs"), dict) else {}

    apps_all = [normalize_app(a) for a in (cfg.get("APPS") or [])]
    ready_apps = [a for a in apps_all if is_app_ready(cfg, a["id"])]

    tags_map = {}
    types_map = {}
    for a in ready_apps:
        types_map[a["id"]] = a.get("type", "radarr")
        tags_map[a["id"]] = get_tag_labels(cfg, a["id"])

    default_app_id = ready_apps[0]["id"] if ready_apps else ""

    app_disabled_attr = "disabled" if len(ready_apps) <= 0 else ""
    app_options_html = ""
    for a in ready_apps:
        label = f"{'Radarr' if a['type'] == 'radarr' else 'Sonarr'} • {a.get('name', 'App')}"
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
    <div class="modalBack jobsModal" id="jobBack">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="jobTitle">
        <div class="mh">
          <h3 id="jobTitle">Add Job</h3>
          <button class="modalCloseX" type="button" onclick="maybeCloseJobModal()" aria-label="Close">×</button>
        </div>

        <form id="jobForm" method="post" action="/jobs/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" name="job_id" id="job_id" value="">

            <div class="field" style="margin-bottom:12px;">
              <label>Job Name</label>
              <input type="text" name="name" id="job_name" value="New Job" required>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>App</label>
               <select class="nativeSelect" name="APP_ID" id="job_app" onchange="onJobAppChanged()"
                       data-default-app="{safe_html(default_app_id)}" {app_disabled_attr} required>
                 {app_options_html}
               </select>
               <div class="fakeSelect" data-for="job_app" id="fake_job_app">
                 <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                   <span class="fakeSelectValue">Select app</span>
                   <span class="fakeSelectChevron" aria-hidden="true"></span>
                 </button>
                 <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
               </div>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>Tag Label</label>
               <select class="nativeSelect" name="TAG_LABEL" id="job_tag" required>
                 <option value="" selected disabled>-- Select a tag --</option>
               </select>
               <div class="fakeSelect" data-for="job_tag" id="fake_job_tag">
                 <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                   <span class="fakeSelectValue">-- Select a tag --</span>
                   <span class="fakeSelectChevron" aria-hidden="true"></span>
                 </button>
                 <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
               </div>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>Days Old</label>
              <input type="number" min="1" name="DAYS_OLD" id="job_days" value="30" required>
            </div>

            <!-- Radarr-only: score filter (styled like existing fields/checks) -->
             <div class="field" id="radarrScoreField" style="display:none; margin-bottom:12px;">
               <label>Radarr score filter</label>
               <div class="scoreRow">
                 <label class="check scoreInline">
                   <input type="checkbox" id="job_score_enabled" name="RADARR_SCORE_FILTER_ENABLED">
                   <span><b>Delete if average score is below</b></span>
                 </label>

                 <input
                   type="number"
                   min="0"
                   max="100"
                   step="1"
                   id="job_score_min"
                   name="RADARR_MIN_AVG_SCORE"
                   value="60"
                   class="scoreNumInput"
                 >
               </div>
             </div>

            <div class="field" id="sonarrDeleteModeField" style="display:none; margin-bottom:12px;">
              <label>Sonarr Delete Mode</label>
               <select class="nativeSelect" name="SONARR_DELETE_MODE" id="job_sonarr_mode">
                 {sonarr_mode_opts}
               </select>
               <div id="fakeWrap_job_sonarr_mode">
                 <div class="fakeSelect" data-for="job_sonarr_mode" id="fake_job_sonarr_mode">
                   <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                     <span class="fakeSelectValue">Select mode</span>
                     <span class="fakeSelectChevron" aria-hidden="true"></span>
                   </button>
                   <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
                 </div>
               </div>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>Scheduler Day</label>
               <select class="nativeSelect" name="SCHED_DAY" id="job_day">
                 <option value="daily">Daily</option>
                 <option value="mon">Monday</option>
                 <option value="tue">Tuesday</option>
                 <option value="wed">Wednesday</option>
                 <option value="thu">Thursday</option>
                 <option value="fri">Friday</option>
                 <option value="sat">Saturday</option>
                 <option value="sun">Sunday</option>
               </select>
               <div class="fakeSelect" data-for="job_day" id="fake_job_day">
                 <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                   <span class="fakeSelectValue">Daily</span>
                   <span class="fakeSelectChevron" aria-hidden="true"></span>
                 </button>
                 <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
               </div>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>Scheduler Time</label>
               <select class="nativeSelect" name="SCHED_HOUR" id="job_hour">
                 {hour_opts}
               </select>
               <div class="fakeSelect" data-for="job_hour" id="fake_job_hour">
                 <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                   <span class="fakeSelectValue">03:00</span>
                   <span class="fakeSelectChevron" aria-hidden="true"></span>
                 </button>
                 <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
               </div>
            </div>

            <div class="field" style="margin-bottom:12px;">
              <label>Enabled</label>
               <select class="nativeSelect" name="enabled" id="job_enabled">
                 <option value="1">Enabled</option>
                 <option value="0">Disabled</option>
               </select>
               <div class="fakeSelect" data-for="job_enabled" id="fake_job_enabled">
                 <button type="button" class="fakeSelectBtn" aria-haspopup="listbox" aria-expanded="false">
                   <span class="fakeSelectValue">Enabled</span>
                   <span class="fakeSelectChevron" aria-hidden="true"></span>
                 </button>
                 <div class="fakeSelectMenu" role="listbox" tabindex="-1"></div>
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
            app_label = f"{'Radarr' if app_kind == 'radarr' else 'Sonarr'} • {a.get('name', 'App')}"
        else:
            app_kind = "radarr"
            app_label = "Missing app"

        radarr_score_line = ""
        if a and a.get("type") == "radarr":
            if j.get("RADARR_SCORE_FILTER_ENABLED"):
                radarr_score_line = f"""
                  <div class="metaRow">
                   <div class="metaLabel">Score filter:</div>
                   <div class="metaVal"><b>ON</b> • delete if avg score &lt; <b>{int(j.get("RADARR_MIN_AVG_SCORE", 60))}</b></div>
                  </div>
                """
            else:
                radarr_score_line = """
                  <div class="metaRow">
                    <div class="metaLabel">Score filter:</div>
                    <div class="metaVal"><b>OFF</b></div>
                  </div>
                """

        lr = last_runs.get(j.get("id")) if isinstance(last_runs, dict) else None
        lr_status = (str(lr.get("status")) if isinstance(lr, dict) else "").upper()
        lr_avg = None
        if isinstance(lr, dict):
            for k in ("avg_score", "average_score", "avg_score_0_100", "average_score_0_100",
                      "average_score_0_100_int"):
                if k in lr and lr.get(k) is not None:
                    lr_avg = lr.get(k)
                    break

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
                  data-enabled="{'1' if j["enabled"] else '0'}"
                  data-app-id="{safe_html(j.get("APP_ID", ""))}"
                  data-tag="{safe_html(j["TAG_LABEL"])}"
                  data-sonarr-mode="{safe_html(j.get('SONARR_DELETE_MODE', 'episodes_only'))}"
                  data-score-en="{'1' if j.get('RADARR_SCORE_FILTER_ENABLED') else '0'}"
                  data-score-min="{int(j.get('RADARR_MIN_AVG_SCORE', 60))}"
                  data-days="{j["DAYS_OLD"]}"
                  data-day="{safe_html(j["SCHED_DAY"])}"
                  data-hour="{j["SCHED_HOUR"]}"
                  data-dry="{'1' if j["DRY_RUN"] else '0'}"
                  data-del="{'1' if j["DELETE_FILES"] else '0'}"
                  data-excl="{'1' if j["ADD_IMPORT_EXCLUSION"] else '0'}">Edit</button>
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
                <div class="muted" style="font-size:11px; margin-top:4px;">
                  {"Last: <b>" + safe_html(lr_status) + "</b>" if lr_status else "Last: —"}
                  {" • Avg score: <b>" + safe_html(lr_avg) + "</b>" if lr_avg is not None else ""}
                </div>
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
                {radarr_score_line}

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
    add_job_title = "Add Job" if can_add_job else "Connect an app in Apps (Test + Save) to add a job."

    add_job_button = f"""
      <button class="btn primary" type="button" onclick="openNewJob()" {add_job_disabled_attr}
              title="{safe_html(add_job_title)}">Add Job</button>
    """

    hint_html = ""
    if not can_add_job:
        hint_html = """
          <div class="muted" style="margin-top:12px;">
            Add Job is disabled because no connected apps exist.
            Go to <a href="/apps"><b>Apps</b></a>, add an app, run <b>Test</b>, then <b>Save</b>.
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
            raise ValueError("Selected app is not available/connected. Go to Apps, Test + Save.")

        tag_label = (request.form.get("TAG_LABEL") or "").strip()
        if not tag_label:
            raise ValueError("Please select a tag.")

        sonarr_mode = (request.form.get("SONARR_DELETE_MODE") or "episodes_only").strip()
        if sonarr_mode not in SONARR_DELETE_MODES:
            sonarr_mode = "episodes_only"
        if app_obj.get("type") != "sonarr":
            sonarr_mode = "episodes_only"

        # Radarr score filter (only meaningful for radarr jobs)
        score_enabled = checkbox("RADARR_SCORE_FILTER_ENABLED")
        score_min = clamp_int(request.form.get("RADARR_MIN_AVG_SCORE") or 60, 0, 100, 60)

        if app_obj.get("type") != "radarr":
            score_enabled = False

        job = {
            "id": job_id or make_job_id(),
            "name": name,
            "enabled": enabled,
            "APP_ID": app_id,
            "TAG_LABEL": tag_label,
            "DAYS_OLD": clamp_int(request.form.get("DAYS_OLD") or 30, 1, 36500, 30),
            "SONARR_DELETE_MODE": sonarr_mode,
            "RADARR_SCORE_FILTER_ENABLED": score_enabled,
            "RADARR_MIN_AVG_SCORE": score_min,
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

    log_path = str(LOG_PATH)
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
    is_sonarr = (app_obj.get("type") == "sonarr")

    try:
        result = preview_candidates_sonarr(cfg, app_obj, job) if app_obj.get(
            "type") == "sonarr" else preview_candidates_radarr(cfg, app_obj, job)

        error = result.get("error")
        candidates = result.get("candidates", [])
        cutoff = result.get("cutoff", "")

        if error:
            flash(error, "error")
            return redirect("/jobs")

        score_hdr = "" if is_sonarr else "<th>Score</th>"
        rows = ""
        for c in candidates[:500]:
            if is_sonarr:
                rows += f"""
                  <tr>
                    <td>{c["age_days"]}</td>
                    <td>{safe_html(c.get("title", ""))}</td>
                    <td>{safe_html(str(c.get("year", "")))}</td>
                    <td><code>{safe_html(c.get("added", ""))}</code></td>
                    <td>{safe_html(str(c.get("id", "")))}</td>
                    <td class="muted">{safe_html(c.get("path", "") or "")}</td>
                  </tr>
                """
            else:
                score = c.get("score")
                score_txt = safe_html(score) if score is not None else "—"
                rows += f"""
                  <tr>
                    <td>{c["age_days"]}</td>
                    <td>{score_txt}</td>
                    <td>{safe_html(c.get("title", ""))}</td>
                    <td>{safe_html(str(c.get("year", "")))}</td>
                    <td><code>{safe_html(c.get("added", ""))}</code></td>
                    <td>{safe_html(str(c.get("id", "")))}</td>
                    <td class="muted">{safe_html(c.get("path", "") or "")}</td>
                  </tr>
                """

        app_label = f"{'Sonarr' if app_obj.get('type') == 'sonarr' else 'Radarr'} • {app_obj.get('name', 'App')}"
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
                        {score_hdr}
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
              </div>
              <div class="bd">
                <div class="muted">No runs recorded yet.</div>
              </div>
            </div>
          </div>
        """
        return render_template_string(shell("mediareaparr • Dashboard", "dash", body))

    status_text = str(last_run.get("status") or "").upper()
    avg_score = None
    if isinstance(last_run, dict):
        for k in ("avg_score", "average_score", "avg_score_0_100", "average_score_0_100", "average_score_0_100_int"):
            if k in last_run and last_run.get(k) is not None:
                avg_score = last_run.get(k)
                break
    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Dashboard</h2>
          </div>
          <div class="bd">
            <div class="muted">Last run status: <b>{safe_html(status_text)}</b></div>
            <div class="muted" style="margin-top:6px;">Job: <b>{safe_html(str(last_run.get("job_name", "")))}</b> (<code>{safe_html(str(last_run.get("job_id", "")))}</code>)</div>
            <div class="muted" style="margin-top:6px;">Finished: <code>{safe_html(str(last_run.get("finished_at", "")))}</code></div>
            <div class="muted" style="margin-top:6px;">Candidates: <b>{safe_html(str(last_run.get("candidates_found", 0)))}</b></div>
            {f'<div class="muted" style="margin-top:6px;">Average score: <b>{safe_html(avg_score)}</b></div>' if avg_score is not None else ''}
          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Dashboard", "dash", body))


# ----------------------------
# Status logs
# ----------------------------
def tail_file(path: Path, max_lines: int = 500) -> str:
    try:
        from collections import deque
        dq = deque(maxlen=max_lines)
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                dq.append(line.rstrip("\n"))
        return "\n".join(dq)
    except Exception as e:
        return f"[mediareaparr] Failed to read log file: {e}\n"


@app.get("/status/log")
def status_log():
    # Log file can be overridden with LOG_PATH env var
    path = LOG_PATH
    lines = clamp_int(request.args.get("lines", 500), 50, 5000, 500)
    if not path.exists():
        msg = (
            f"No log file found at: {path}\n\n"
            "Tip: set LOG_PATH to wherever your container writes logs, or pipe stdout to a file.\n"
            "Example (Docker): docker logs mediareaparr > /config/mediareaparr.log\n"
        )
        return Response(msg, mimetype="text/plain; charset=utf-8")
    return Response(tail_file(path, max_lines=lines), mimetype="text/plain; charset=utf-8")


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
                    parts.append(f"{nm} ({typ}, {ok}, url={a.get('url', '')})")
                summary = "; ".join(parts) + (" …" if len(apps_list) > 50 else "")
                rows.append(
                    f"<tr><td><code>{safe_html(k)}</code></td>"
                    f"<td class='muted'>{safe_html(summary) if summary else safe_html(f'[{len(apps_list)} apps]')}</td></tr>"
                )
            elif k == "JOBS":
                jobs = [normalize_job(x) for x in (v or [])]
                parts = []
                for j in jobs[:50]:
                    parts.append(f"{j.get('name', 'Job')} (app_id={j.get('APP_ID', '')}, tag={j.get('TAG_LABEL', '')})")
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
