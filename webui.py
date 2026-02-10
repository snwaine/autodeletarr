import re
import os
import sys
import subprocess
import json
import threading
import time
import atexit
import signal
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

import traceback
import requests
from flask import (
    Flask, request, redirect, render_template_string,
    flash, get_flashed_messages, send_from_directory, Response, jsonify
)

from utils import (
    env_default,
    clamp_int,
    now_iso,
    parse_iso_date,
    self_test_date_parsing,
    safe_html,
    make_job_id,
    make_app_id,
    checkbox,
    schedule_label,
)

self_test_date_parsing()

"""MediaReaparr WebUI (single-file Flask app)

Organized sections:
  - Imports & constants
  - Logging helpers
  - Config/state IO
  - Schema normalization (Apps/Jobs)
  - Internal scheduler + "Run now"
  - API helpers + preview
  - HTML/CSS template helpers
  - Flask routes
  - Main entrypoint
"""

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

# ----------------------------
# Logging (WebUI log viewer)
# ----------------------------

# Daily log rotation (all logs are named: "dd-mm-yyyy mediareaparr.log")
# LOG_DIR may be set to control where logs are written/read.
DEFAULT_LOG_DIR = Path(os.environ.get("LOG_DIR", "")).expanduser() if os.environ.get("LOG_DIR") else None
if DEFAULT_LOG_DIR is None:
    # Back-compat: if LOG_PATH was provided as a file path, use its parent as LOG_DIR
    _lp = os.environ.get("LOG_PATH", "")
    if _lp:
        try:
            DEFAULT_LOG_DIR = Path(_lp).expanduser().resolve().parent
        except (OSError, RuntimeError):
            DEFAULT_LOG_DIR = None
if DEFAULT_LOG_DIR is None:
    DEFAULT_LOG_DIR = CONFIG_DIR

LOG_FILE_SUFFIX = " mediareaparr.log"


def _today_dd_mm_yyyy() -> str:
    # Use container-local time (honors TZ) so daily files rotate at local midnight
    return datetime.now().strftime("%d-%m-%Y")


def _is_valid_dd_mm_yyyy(s: str) -> bool:
    s = (s or "").strip()
    if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", s):
        return False
    try:
        datetime.strptime(s, "%d-%m-%Y")
        return True
    except ValueError:
        return False


def get_log_path(cfg: Optional[Dict[str, Any]] = None, date_key: Optional[str] = None) -> Path:
    # cfg may still override LOG_DIR/LOG_PATH (but the filename format is fixed)
    log_dir = DEFAULT_LOG_DIR
    try:
        if isinstance(cfg, dict):
            d = str(cfg.get("LOG_DIR") or cfg.get("log_dir") or "").strip()
            if d:
                log_dir = Path(d).expanduser()
            else:
                p = str(cfg.get("LOG_PATH") or cfg.get("log_path") or "").strip()
                if p:
                    try:
                        log_dir = Path(p).expanduser().resolve().parent
                    except (OSError, RuntimeError):
                        pass
    except (OSError, RuntimeError, ValueError):
        pass

    dk = (date_key or "").strip()
    if not _is_valid_dd_mm_yyyy(dk):
        dk = _today_dd_mm_yyyy()

    return Path(log_dir) / f"{dk}{LOG_FILE_SUFFIX}"


def list_log_dates(cfg: Optional[Dict[str, Any]] = None) -> List[str]:
    # Return available dates (dd-mm-yyyy) based on files in LOG_DIR
    try:
        base = get_log_path(cfg).parent
        dates = []
        for p in base.glob(f"*{LOG_FILE_SUFFIX}"):
            name = p.name
            if not name.endswith(LOG_FILE_SUFFIX):
                continue
            dk = name[: -len(LOG_FILE_SUFFIX)]
            if _is_valid_dd_mm_yyyy(dk):
                dates.append(dk)
        # Newest first
        dates.sort(key=lambda s: datetime.strptime(s, "%d-%m-%Y"), reverse=True)
        return dates
    except (OSError, ValueError):
        return []


def tail_file(path: Path, max_lines: int = 500, max_bytes: int = 1024 * 1024) -> str:
    """Return the last N lines of a text file (best-effort, safe for large files)."""
    try:
        p = Path(path)
        if not p.exists() or not p.is_file():
            return f"(log file not found) {p}"
        # Read at most max_bytes from the end
        with p.open("rb") as f:
            try:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - max_bytes), 0)
            except (OSError, ValueError):
                pass
            data = f.read()
        txt = data.decode("utf-8", errors="replace")
        lines = txt.splitlines()
        if max_lines and len(lines) > max_lines:
            lines = lines[-max_lines:]
        return "\n".join(lines)
    except (OSError, TypeError) as e:
        return f"(failed to read log) {e}"


def append_log_line(msg: str, sev: str = "INFO", label: str = "WebUI") -> None:
    """Append a single log line to today's log file in canonical format.

    Format: Feb 01, 2026, 7:26:12 PM [Info] [Label] Message
    """
    try:
        lp = get_log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)

        # Use container-local time (honors TZ env var)
        ts = datetime.now().strftime("%b %d, %Y, %I:%M:%S %p")

        s = (sev or "INFO").strip().upper()
        if s == "WARN":
            s = "WARNING"
        # Title-case severity to match app.py formatter output (Info/Debug/Warning/Error)
        sev_out = s.title()

        lbl = (label or "WebUI").strip()
        with lp.open("a", encoding="utf-8") as _f:
            _f.write(f"{ts} [{sev_out}] [{lbl}] {msg}\n")
    except OSError:
        pass


APP_DIR = Path(__file__).resolve().parent
APP_IMAGES_DIR = APP_DIR / "images"
CONFIG_IMAGES_DIR = CONFIG_DIR / "images"
app = Flask(__name__)


# ----------------------------
# Global 500 handler (prevents blank/opaque 500s)
# ----------------------------


def _wants_json() -> bool:
    # Any API-ish path or explicit JSON accept header
    try:
        p = request.path or ""
        if p.startswith("/api/") or p.startswith("/status/") or p.endswith(".json"):
            return True
        accept = (request.headers.get("Accept") or "").lower()
        return "application/json" in accept
    except RuntimeError:
        return False


@app.errorhandler(Exception)
def handle_unexpected_error(e):
    # Try to log a useful traceback without crashing again
    try:
        tb = traceback.format_exc(limit=25)
        try:
            append_log_line("UNHANDLED EXCEPTION: " + str(e), sev="ERROR", label="WebUI")
            for line in tb.splitlines()[-25:]:
                append_log_line("trace: " + line, sev="ERROR", label="WebUI")
        except OSError:
            pass
    except RuntimeError:
        pass

    if _wants_json():
        return jsonify({"ok": False, "error": str(e)}), 500

    # Minimal HTML error page so the UI doesn't just show "loading"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>MediaReaparr - Error</title>"
        "<style>body{font-family:system-ui;background:#0e0f11;color:#e6e6e6;padding:20px}"
        ".box{max-width:900px;background:#15171b;border:1px solid #2a2d33;border-radius:12px;padding:16px}"
        "pre{white-space:pre-wrap;background:#0b0c0e;border:1px solid #222;border-radius:10px;padding:12px;color:#b8ffcc}"
        "a{color:#7CFFB2}</style></head><body>"
        "<div class='box'><h2>500 - Server Error</h2>"
        "<p>This page failed to render due to an internal server error.</p>"
        "<p><b>Tip:</b> Check container logs (docker logs) for the full traceback.</p>"
        "<h3>Exception</h3><pre>"
        + html_escape(str(e)) +
        "</pre></div></body></html>",
        500,
    )


app.secret_key = os.environ.get("FLASK_SECRET_KEY", "mediareaparr-secret")

# (helpers imported from utils.py)


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
    return u, k


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
    d["DELETE_FILES"] = True
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
            <div class="muted">Dry Run: <b><span id="rn_dry">OFF</span></b> • Job: <b><span id="rn_enabled">Enabled</span></b></div>
          </div>

          <p><b id="rn_msg">Dry Run is OFF — this will perform real actions.</b></p>
<p class="muted">If you’re not sure, edit the job and enable <b>Dry Run</b> first.</p>
        </div>
        <div class="mf">
          <button class="btn cancel" type="button" onclick="hideModal('runNowBack')">Cancel</button>
          <form id="runNowFormConfirm" method="post" action="/jobs/run-now" style="margin:0;">
            <input type="hidden" id="runNowJobId" name="job_id" value="">
            <button class="btn bad" type="button" onclick="runNowSubmitConfirm()">Yes, run now</button>
          </form>
        </div>
      </div>
    </div>
    """


# ----------------------------
# Generic confirm modal (replaces browser confirm())
# ----------------------------
def confirm_modal_html() -> str:
    return """
    <div class="modalBack" id="confirmBack" style="display:none;">
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="confirmTitle">
        <div class="mh">
          <h3 id="confirmTitle">Confirm</h3>
          <button class="modalCloseX" type="button" onclick="confirmCancel()" aria-label="Close">×</button>
        </div>
        <div class="mb">
          <p id="confirmMessage" style="margin:0;"></p>
        </div>
        <div class="mf" style="justify-content:flex-end; gap:10px;">
          <button class="btn cancel" type="button" id="confirmCancelBtn" onclick="confirmCancel()">Cancel</button>
          <button class="btn danger" type="button" id="confirmOkBtn" onclick="confirmOk()">OK</button>
        </div>
      </div>
    </div>
    """


def run_now_button_html(job: Dict[str, Any], app_label: str = "App") -> str:
    job = normalize_job(job)

    # Disabled job
    if not job["enabled"]:
        return '<button class="btn" type="button" disabled title="Enable this job to run">Run Now</button>'

    jid = safe_html(job["id"])
    enabled = "true" if bool(job.get("enabled", True)) else "false"
    delete_files = "true" if bool(job.get("DELETE_FILES", True)) else "false"
    app_lbl = safe_html(app_label)

    # 🔹 DRY RUN → execute app.py in dry-run mode (logs like Radarr/Sonarr)
    if job.get("DRY_RUN", True):
        return f'''
          <div style="display:flex; flex-direction:column; gap:8px; align-items:stretch;">
            <form method="post" action="/jobs/run-dry" style="margin:0; display:inline;">
              <input type="hidden" name="job_id" value="{jid}">
              <button class="btn good" type="submit"
                title="Dry Run — no changes will be made (writes to logs)">
                Dry Run
              </button>
            </form>

            <a class="btn" href="/preview?job_id={jid}"
               title="Preview candidates (no changes will be made)">
              Preview
            </a>
          </div>
        '''

    # REAL RUN (confirmation via JS listener; no inline JS)
    return f'''
      <div style="display:flex; flex-direction:column; gap:8px; align-items:stretch;">
        <button class="btn bad" type="button"
          data-action="run-now"
          data-job-id="{jid}"
          data-app-label="{app_lbl}"
          data-dry-run="0"
          data-delete-files="{delete_files}"
          data-enabled="{enabled}">
          Run Now
        </button>

        <a class="btn" href="/preview?job_id={jid}"
           title="Preview candidates (no changes will be made)">
          Preview
        </a>
      </div>
    '''


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
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    # Normalize theme/timeout
    t = (cfg.get("UI_THEME") or "dark").lower()
    cfg["UI_THEME"] = t if t in ("dark", "light") else "dark"
    cfg["HTTP_TIMEOUT_SECONDS"] = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 30), 5, 300, 30)

    apps_list = cfg.get("APPS") or []
    if not isinstance(apps_list, list):
        apps_list = []
    cfg["APPS"] = [normalize_app(a) for a in apps_list]

    jobs = cfg.get("JOBS") or []
    if not isinstance(jobs, list):
        jobs = []
    jobs = [normalize_job(j) for j in jobs]
    cfg["JOBS"] = jobs

    return cfg


def save_config(cfg: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    """Persist state.json (used by internal scheduler + UI status)."""
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except (OSError, TypeError, ValueError) as e:
        try:
            append_log_line(f"save_state failed: {e}")
        except OSError:
            pass


# ----------------------------
# Running job tracking (disables delete while running)
# ----------------------------
def _running_jobs(state: Dict[str, Any]) -> List[str]:
    rj = state.get("RUNNING_JOBS") or []
    if isinstance(rj, list):
        return [str(x) for x in rj if str(x).strip()]
    return []


def is_job_running(job_id: str) -> bool:
    jid = (job_id or "").strip()
    if not jid:
        return False
    state = load_state()
    return jid in set(_running_jobs(state))


def mark_job_running(job_id: str, running: bool) -> None:
    jid = (job_id or "").strip()
    if not jid:
        return
    state = load_state()
    running_jobs = set(_running_jobs(state))
    if running:
        running_jobs.add(jid)
    else:
        running_jobs.discard(jid)
    state["RUNNING_JOBS"] = sorted(running_jobs)
    save_state(state)


# ----------------------------
# Internal scheduler (optional)
# ----------------------------
# Runs jobs inside the WebUI container (no host cron needed).
# Enable/disable via env:
#   INTERNAL_SCHEDULER=1|0  (default: 1)
#   SCHEDULER_TICK_SECONDS=30
# Prevents double-runs using state.json window markers.
INTERNAL_SCHEDULER_ENABLED = env_default("INTERNAL_SCHEDULER", "1").strip() != "0"
SCHEDULER_TICK_SECONDS = clamp_int(env_default("SCHEDULER_TICK_SECONDS", "30"), 5, 3600, 30)
_SCHED_LOCK_PATH = CONFIG_DIR / ".scheduler.lock"


def _weekday_key(dt: datetime) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][dt.weekday()]


def _job_due(job: Dict[str, Any], now: datetime) -> bool:
    if not job.get("enabled", False):
        return False
    day = str(job.get("SCHED_DAY") or "daily").strip().lower()
    try:
        hour = int(job.get("SCHED_HOUR", 3))
    except (TypeError, ValueError):
        hour = 3
    if now.hour != hour:
        return False
    if day != "daily" and day != _weekday_key(now):
        return False
    return True


def _pid_is_running(pid: int) -> bool:
    try:
        if pid <= 0:
            return False
        # Works on Linux + most POSIX
        os.kill(pid, 0)
        return True
    except PermissionError:
        # Process exists but we can't signal it
        return True
    except (OSError, TypeError, ValueError):
        return False


def _release_scheduler_lock() -> None:
    try:
        if _SCHED_LOCK_PATH.exists():
            pid_txt = _SCHED_LOCK_PATH.read_text(encoding="utf-8", errors="ignore").strip()
            try:
                lock_pid = int(pid_txt.splitlines()[0].strip())
            except (ValueError, IndexError):
                lock_pid = None
            # Only remove if we own it (or pid is missing)
            if lock_pid in (None, os.getpid()):
                _SCHED_LOCK_PATH.unlink(missing_ok=True)
    except OSError:
        pass


def _acquire_scheduler_lock() -> bool:
    """Best-effort single-runner guard (helps if you ever run multiple workers).
    Handles stale lock files (e.g. after unclean shutdown).
    """
    try:
        fd = os.open(str(_SCHED_LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return True
    except FileExistsError:
        # Check for stale lock
        try:
            pid_txt = _SCHED_LOCK_PATH.read_text(encoding="utf-8", errors="ignore").strip()
            lock_pid = int(pid_txt.splitlines()[0].strip()) if pid_txt else -1
            if not _pid_is_running(lock_pid):
                _SCHED_LOCK_PATH.unlink(missing_ok=True)
                return _acquire_scheduler_lock()
        except (OSError, ValueError, IndexError):
            pass
        return False
    except OSError:
        # Don't block startup if something odd happens with the lock
        return True


def _scheduler_spawn_job(job_id: str) -> None:
    """Spawn app.py --job-id <id> and stream output into the shared log."""
    try:
        app_py = str((Path(__file__).resolve().parent / "app.py"))
        lp = get_log_path()
        lp.parent.mkdir(parents=True, exist_ok=True)
        f = lp.open("a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, app_py, "--job-id", str(job_id)],
            stdout=f,
            stderr=f,
            close_fds=True,
        )
    except (OSError, ValueError) as e:
        try:
            append_log_line(f"scheduler: failed to spawn job {job_id}: {e}", sev="ERROR", label="Scheduler")
        except OSError:
            pass


def _scheduler_loop() -> None:
    append_log_line(f"scheduler: starting (tick={SCHEDULER_TICK_SECONDS}s)", sev="DEBUG")
    while True:
        try:
            cfg = load_config()
            state = load_state()

            now = datetime.now()  # container-local time (honors TZ env var)
            window = now.strftime("%Y-%m-%d %H")

            last = state.get("scheduler_last") or {}
            if not isinstance(last, dict):
                last = {}

            jobs = cfg.get("JOBS") or []
            if not isinstance(jobs, list):
                jobs = []

            for j in jobs:
                try:
                    jn = normalize_job(j)
                except (TypeError, ValueError):
                    jn = j if isinstance(j, dict) else {}
                jid = str(jn.get("id") or "").strip()
                if not jid:
                    continue
                if not _job_due(jn, now):
                    continue
                if last.get(jid) == window:
                    continue

                last[jid] = window
                state["scheduler_last"] = last
                save_state(state)

                append_log_line(f"scheduler: running job {jid} ({jn.get('name', 'Job')}) window={window}", sev="DEBUG")
                _scheduler_spawn_job(jid)

        except (OSError, ValueError, TypeError, RuntimeError) as e:
            try:
                append_log_line(f"scheduler: loop error: {e}", sev="DEBUG", label="Scheduler")
            except OSError:
                pass

        time.sleep(int(SCHEDULER_TICK_SECONDS))


def _install_scheduler_lock_cleanup() -> None:
    # Ensure the lock doesn't stick around after container stop/restart.
    try:
        atexit.register(_release_scheduler_lock)
    except RuntimeError:
        pass

    def _handler(_signum, _frame):
        try:
            _release_scheduler_lock()
        finally:
            raise SystemExit(0)

    for _sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if _sig is None:
            continue
        try:
            signal.signal(_sig, _handler)
        except (ValueError, OSError, RuntimeError):
            pass


def start_internal_scheduler() -> None:
    if not INTERNAL_SCHEDULER_ENABLED:
        append_log_line("scheduler: disabled via INTERNAL_SCHEDULER=0", sev="DEBUG", label="Scheduler")
        return
    if not _acquire_scheduler_lock():
        append_log_line("scheduler: lock exists, not starting (another worker owns it)", sev="DEBUG")
        return

    _install_scheduler_lock_cleanup()

    t = threading.Thread(target=_scheduler_loop, daemon=True, name="mediareaparr-scheduler")
    t.start()


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
    except (requests.exceptions.RequestException, ValueError):
        # App is offline / timeout / bad gateway / etc.
        return []


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
        except (TypeError, ValueError):
            continue

        if 0 <= v <= 10:
            values.append(v * 10)
        elif 0 <= v <= 100:
            values.append(v)

    if not values:
        return None

    return int(sum(values) / len(values))


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
    <!-- Favicon -->
    <link rel="icon" href="/images/favicon.ico" sizes="any">
    <link rel="shortcut icon" href="/images/favicon.ico">

<style>
  :root{
    --bg:#111827;
    --panel:#1f2937;
    --panel2:#1b2431;
    --muted:#9ca3af;
    --edgehighlight:#9ca3af;
    --rule:#555555;
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
    --scrollbarbackgroundcolor:#595959;
    --BackgroundColor1:#333333;
    --FieldinputColor:#595959;
    --sidebarActiveBackgroundColor:#333333;
    --toolbarBackgroundColor:#262626;
    --pageBackgroundColor:#202020;
    --muted:#9ca3af;
    --edgehighlight:#9ca3af;
    --rule:#555555;
    --text:#f1f5f9;
    --line:#333333;
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
    --scrollbarbackgroundcolor:#595959;
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
    --FieldinputColor:#ffffff;
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
     background:var(--FieldinputColor);
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
     background:var(--FieldinputColor);
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

   /* per-theme highlight colors */
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

  .sbItem{
    display:flex;
    align-items:center;
    justify-content: space-between;
    gap: 10px;
    padding: 20px 30px;
    border: none;
    background: none;
    font-size: 20px;
    text-decoration: none;
    cursor:pointer;
  }

  .sbItem,.sbItem:visited,.sbItem:focus,.sbItem:active{
    color: var(--text);
    text-decoration: none;
    -webkit-tap-highlight-color: transparent;
  }

  .sbItem:hover{
    color: var(--accent);
    text-decoration: none;
  }

  /* Keep active/selected state stable even during mousedown */
  .sbItem.active,.sbItem.active:visited,.sbItem.active:focus,.sbItem.active:active{
    color: var(--accent);
    text-decoration: none;
  }

  .sbItem:active span,.sbItem:focus span{
    color: inherit;
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

.jobs-empty-row {
    display: flex !important;
    align-items: center;
    justify-content: center;
    grid-column: 1 / -1; /* span full jobs grid */
    min-height: 80px;
    width: 100%;
    color: #8fa8a0;
    font-size: 13px;
    text-align: center;
    pointer-events: none;
}
/* Jobs list: keep Edit + Run/Dry buttons side-by-side */
.jobRowActions{
  display: inline-flex;
  align-items: center;
  gap: 8px;
  white-space: nowrap;
}
.jobRowActions form{
  display: inline-flex;
  margin: 0;
}

/* Jobs list: make "Run Now" and "Dry Run" buttons the same size */
.jobRowActions .btn{
  min-width: 32px;            /* keeps 'Run Now' and 'Dry Run' aligned */
  max-width: 65px;
  justify-content: center;
}
  .addJobBar{
    width: 100%;
    border: 3px dashed rgba(255,255,255,.18);
    background: rgba(255,255,255,.03);
    border-radius: 12px;
    min-height: 48px;
    display:flex;
    align-items:center;
    justify-content:center;
    gap: 10px;
    cursor:pointer;
    user-select:none;
    transition: background .18s ease, border-color .18s ease, box-shadow .18s ease, transform .18s ease;
    margin-bottom: 15px;
    }
  .addJobBar.disabled{
    opacity: .55;
    cursor: not-allowed;
    border-style: dashed;
    filter: grayscale(0.2);
  }
  .jobCard.addJobCard.disabled{
    opacity: .55;
    cursor: not-allowed;
    filter: grayscale(0.2);
  }

  /* Sonarr — blue */
  .addJobBar[data-app-type="sonarr"]:hover{
    border-color: #38bdf8;
    background: rgba(56,189,248,.10);
    box-shadow: 0 0 0 3px rgba(56,189,248,.18), var(--shadow);
    transform: translateY(-1px);
  }
  /* Radarr — yellow */
  .addJobBar[data-app-type="radarr"]:hover{
    border-color: #eeb530;
    background: rgba(238,181,48,.14);
    box-shadow: 0 0 0 3px rgba(238,181,48,.22), var(--shadow);
    transform: translateY(-1px);
  }
  body[data-theme="light"] .addJobBar{
    border-color: rgba(0,0,0,.14);
    background: rgba(0,0,0,.02);
  }
  .addJobBar:active{ transform: translateY(0px); }
  .addJobBar .plus{
    font-size: 22px;
    line-height: 1;
    color: var(--muted);
    font-weight: 900;
  }
  .addJobBar .label{
    font-weight: 800;
    color: var(--muted);
  }

  .jobsListWrap{
    border: 1px solid var(--line);
    border-radius: 12px;
    overflow: hidden;
    background: rgba(0,0,0,.12);
    margin-bottom: 20px; /* footer gap at bottom of Jobs list */
  }
  body[data-theme="light"] .jobsListWrap{ background: rgba(0,0,0,.03); }

  .jobsTable{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }
  /* Jobs list view layout:
     - Job name column: 20% (left aligned)
     - Actions column: fixed width (right aligned)
     - All other columns share remaining space evenly
  */
  .jobsTable th:first-child, .jobsTable td:first-child{ width: 16%; text-align: left; }
  .jobsTable th:last-child,  .jobsTable td:last-child{ width: 195px; text-align: right; }
  .jobsTable td.jobsEmptyCell{ text-align: center !important; }
  /* Center "pill" values in their columns (all non-name, non-actions columns) */
  .jobsTable th:not(:first-child):not(:last-child),
  .jobsTable td:not(:first-child):not(:last-child){
    text-align: center;
  }
  .jobsTable th, .jobsTable td{ overflow: hidden; text-overflow: ellipsis; }
  .jobsTable thead th{
    text-transform: uppercase;
    letter-spacing: .08em;
    font-size: 11px;
    color: rgba(255,255,255,.70);
    background: rgba(255,255,255,.06);
    padding: 10px 12px;
    border-bottom: 1px solid var(--line);
    white-space: nowrap;
  }
  body[data-theme="light"] .jobsTable thead th{
    color: rgba(0,0,0,.65);
    background: rgba(0,0,0,.04);
  }
  .jobsTable tbody td{
    padding: 12px 12px;
    border-bottom: 1px solid rgba(255,255,255,.06);
    vertical-align: middle;
  }
  .jobsTable tbody tr:nth-child(odd) td{
  background: rgba(255,255,255,0.02);
  }
    .jobsTable tbody tr:nth-child(even) td{
    background: transparent;
  }
  body[data-theme="light"] .jobsTable tbody tr:nth-child(odd) td{
    background: rgba(0,0,0,0.03);
  }
  body[data-theme="light"] .jobsTable tbody td{
    border-bottom: 1px solid rgba(0,0,0,.06);
  }
  .jobsTable tbody tr:hover td{
    background: rgba(255,255,255,.03);
  }

/* Jobs list: icon-only delete button */
.jobDeleteBtn{
  display:inline-flex;
  align-items:center;
  justify-content:center;
  width: 29px;
  height: 29px;
  border-radius: 10px;
  border-color: #ff00007a;
  background: #ff454554;
  color: #ffffff;
  cursor: pointer;
  padding: 0;
  line-height: 1;
}

.jobDeleteBtn:disabled{
  opacity: .45;
  cursor: not-allowed;
}

/* Jobs list: icon-only edit button */
.btn.iconOnly{
  width: 29px;
  height: 29px;
  padding: 0;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
}

.jobDeleteBtn:hover{ background: rgba(239,68,68,0.22); }
.jobDeleteBtn:active{ background: rgba(239,68,68,0.30); }
.jobDeleteBtn span{ font-size: 16px; }



  .jobPill{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    padding: 5px 12px;
    border-radius: 15px;
    font-size: 12px;
    font-weight: 700;
    border: 1px solid rgba(255,255,255,.18);
    color: rgba(255,255,255,92);
    background: rgba(255,255,255,.06);
    white-space: nowrap;
    cursor:default;
  }
  body[data-theme="light"] .jobPill{
    color: #111827;
    border-color: rgba(0,0,0,.12);
    background: rgba(0,0,0,.04);
  }

  /* App-colored job pills in Jobs → List view */
  .jobPill.radarr{
    border-color: rgba(255, 208, 0, .55);
    background: rgba(249, 189, 49, 30%);
  }
  body[data-theme="light"] .jobPill.radarr{
    border-color: rgba(255, 208, 0, .55);
    background: rgba(249, 189, 49, 30%);
  }

  .jobPill.sonarr{
    border-color: rgba(59, 130, 246, .55);
    background: rgba(0, 161, 254, 30%);
  }
  body[data-theme="light"] .jobPill.sonarr{
    border-color: rgba(59, 130, 246, .55);
    background: rgba(0, 161, 254, 30%);
  }

  .jobPill.muted{
    border-color: #434343;
    background: #272727;
    color: #ffffff;
  }
  body[data-theme="light"] .jobPill.muted{
    border-color: rgba(0,0,0,12);
    background: rgba(0,0,0,04);
    color: rgba(0,0,0,62);
  }

  .jobNameCell{
    display:flex;
    align-items:center;
    gap: 10px;
    min-width: 220px;
  }
  .jobRowIcon{
    width: 18px;
    height: 18px;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    flex: 0 0 auto;
  }
  .jobRowIcon img{
    width: 18px;
    height: 18px;
    display:block;
  }
  body[data-theme="light"] .jobRowIcon{
    border-color: rgba(0,0,0,.35);
  }
  body[data-theme="light"]   .jobRowActions{
    display:flex;
    gap: 10px;
    justify-content:flex-end;
    align-items:center;
    flex-wrap: nowrap;
    white-space: nowrap;
  }
  .jobsTable .btn{
    padding: 8px 14px;
    border-radius: 10px;
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
    box-shadow: 0 0px 28px rgba(0, 0, 0, .55);
  }

  .card .hd h2{
    margin:0;
    font-size: 14px;
    letter-spacing:.2px;
  }

  .card .bd{
    padding: 6px 30px;
    background: var(--pageBackgroundColor);
    min-height: 0;
    overflow: auto;
    flex: 1 1 auto;
  }

  .muted{ color: var(--muted); }
  .btnrow{ display:flex; gap:10px; flex-wrap: wrap; align-items:center; }

  .btn{
    border: 1px solid #4f4e4e;
    background: #272727;
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

  .btn:hover{
    border-color: rgba(34,197,94,.55);
    box-shadow: 0 0 0 3px rgba(34,197,94,.10), 0 10px 22px rgba(0,0,0,.22);
    transform: translateY(-1px);
    text-decoration: none;
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
    border-color: #22c55e8c;
    background: #16a34a54;
  }
  .btn.warn{
    border-color: rgba(245,158,11,.55);
    background: linear-gradient(135deg, rgba(245,158,11,.22), rgba(245,158,11,.08));
  }


  .btn.cancel{
    border-color: rgba(148,163,184,.50);
    background: linear-gradient(135deg, rgba(148,163,184,.18), rgba(148,163,184,.08));
    color: var(--text);
  }
  .btn.cancel:hover{
    border-color: rgba(148,163,184,.70);
    box-shadow: 0 0 0 3px rgba(148,163,184,.18), 0 10px 22px rgba(0,0,0,.22);
  }
  .btn.cancel:active{
    box-shadow: 0 0 0 2px rgba(148,163,184,.14), 0 6px 14px rgba(0,0,0,.18);
  }

.btn.bad{
    border-color: #ff00007a;
    background: #ff454554;
  }

  /* Add instance button (separate from .btn) */
  .addInstanceBtn{
    border: 1px solid rgba(124,255,178,.55);
    background: rgba(124,255,178,.10);
    color: var(--accent);
    padding: var(--btn-py) var(--btn-px);
    font-weight: 700;
    font-size: var(--btn-fs);
    gap: var(--btn-gap);
    border-radius: 8px;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
    transition: box-shadow .18s ease, border-color .18s ease, transform .18s ease, filter .18s ease;
  }
  .addInstanceBtn:hover{
    border-color: rgba(124,255,178,.85);
    box-shadow: 0 0 0 3px rgba(124,255,178,.18), 0 10px 22px rgba(0,0,0,.22);
    transform: translateY(-1px);
    text-decoration: none;
  }
  .addInstanceBtn:active{
    transform: translateY(0);
    box-shadow: 0 0 0 2px rgba(124,255,178,.12), 0 6px 14px rgba(0,0,0,.18);
  }
  body[data-theme="light"] .addInstanceBtn:hover{
    box-shadow: 0 0 0 3px rgba(167,213,65,.16), 0 10px 18px rgba(2,6,23,.10);
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
    background: var(--FieldinputColor);
    color: var(--text);
  }
/* ---- Remove number input arrows (all browsers) ---- */
input[type="number"]::-webkit-inner-spin-button,
input[type="number"]::-webkit-outer-spin-button {
  -webkit-appearance: none;
  margin: 0;
}
input[type="number"] {
  -moz-appearance: textfield;
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
    background: var(--FieldinputColor);
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

    /* Radarr score filter row (match screenshot: compact checkbox + pill number) */
  .field .scoreRow{
    display:flex;
    align-items:center;
    gap:12px;
    flex-wrap:nowrap;
  }
  .field .scoreRow label.check{
    display:flex;
    align-items:center;
    gap:10px;
    padding: 0;
    margin: 0;
  }
  /* Don't scale the checkbox in this row */
  .field .scoreRow label.check input[type=checkbox]{
    transform: none;
  }
  .field .scoreRow .scoreNumInput{
    width: 65px;
    height: 32px;
    min-width: 60px;
    max-width: 65px;
    padding: 10px 10px;
    border-radius: 10px;
    border: 1px solid var(--BackgroundColor1);
    background: var(--FieldinputColor);
    color: var(--text);
    text-align: left;
  }
  /* On very small widths allow wrapping so the number doesn't crush the label */
  @media (max-width: 420px){
    .field .scoreRow{ flex-wrap:wrap; }
    .field .scoreRow .scoreNumInput{ margin-left: 0; }
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
    border-radius: 12px !important;
    overflow:hidden;
    border: 3px solid var(--BackgroundColor1);
    background: var(--BackgroundColor1);
    box-shadow: var(--shadow);
    max-width: none;
    width: 100%;
  }

  .jobName{
    color: var(--text);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .3px;

    max-width: 18ch;
    white-space: nowrap;
    overflow: hidden;
    position: relative;
  }

  .jobName:hover{
    cursor: default;
  }

  /* Only fade the end when the text actually overflows the box */
  .jobName.is-overflowing{
    -webkit-mask-image: linear-gradient(
      to right,
      rgba(0,0,0,1) 80%,
      rgba(0,0,0,0) 100%
    );
    mask-image: linear-gradient(
      to right,
      rgba(0,0,0,1) 80%,
      rgba(0,0,0,0) 100%
    );
  }

  @media (min-width: 700px){ .jobsGrid{ grid-template-columns: repeat(3, minmax(300px, 1fr)); } }
  @media (min-width: 1200px){ .jobsGrid{ grid-template-columns: repeat(4, minmax(300px, 1fr)); gap: 16px; } }
  @media (min-width: 1800px){ .jobsGrid{ grid-template-columns: repeat(5, minmax(300px, 1fr)); gap: 20px; } }

  /* Jobs sections (Sonarr/Radarr) */

  .jobsSections{ display:block; width:100%; }
  .jobsSection{ display:block; width:100%; margin-top: 18px; clear: both; }
  .jobsSectionHeader{
    display:flex; align-items:center; gap: 14px;
    margin: 18px 0 18px;
  }
  .jobsSectionHeader .title{
    font-size: 15px; font-weight: 700; color: var(--text);
  }
  .jobsSectionHeader .rule{
    flex:1; height: 2px; background: var(--muted);
    opacity: .75;
  }


  .jobHeader{
    padding: 12px 12px;
    border-bottom: 1px solid var(--line);
    background: var(--HeaderBackgroundColor);
    display: grid;
    grid-template-columns: 1fr auto;
    align-items: center;
    gap: 10px;
  }

  .jobHeaderLeft{ justify-self: start; min-width: 0; }
  .jobHeaderCenter{ justify-self: center; }
  .jobHeaderRight{ justify-self: end; display:flex; align-items:center; gap:10px; }

  .jobModalIcon {
    width: 1em;
    height: 1em;
    max-width: 1em;
    max-height: 1em;
    flex: 0 0 auto;
    display: inline-block;
    vertical-align: -0.125em;
    object-fit: contain;
  }

  .jobName{
    color: var(--text);
    font-size: 12px;
    font-weight: 800;
    letter-spacing: .3px;
    max-width: 16ch;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .jobTitleRow{
    display:flex;
    align-items:center;
    gap:12px;
    min-width:0;
  }

  .appIcon{
    width:18px;
    height:18px;
    flex:0 0 18px;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    opacity:.95;
  }
  .appIcon img{
    width:30px;
    height:30px;
    display:block;
  }

  /* Apps cards: left stack + icon */
  .appCardLeft{
    display:flex;
    align-items:flex-start;
    gap:12px;
    min-width:0;
  }
  .appCardIcon img{
    width:40px;
    height:40px;
    display:block;
    transition: filter .18s ease, opacity .18s ease, transform .18s ease;
  }

  /* Apps cards: connection state visuals */
  @keyframes reaparrPulse {
    0%   { transform: scale(1);    opacity: .65; }
    50%  { transform: scale(1.04); opacity: 1; }
    100% { transform: scale(1);    opacity: .65; }
  }

  .appCard.is-disconnected .appCardIcon img{
    filter: grayscale(1);
    opacity: .45;
  }
  .appCard.is-checking .appCardIcon img{
    animation: reaparrPulse 1.0s ease-in-out infinite;
    opacity: .85;
  }
  .appCard.is-connected .appCardIcon img{
    filter:none;
    opacity: 1;
  }


  .enableWrap{ display:flex; align-items:center; gap:8px; }
  .enableLbl{ font-size: 12px; color: var(--muted); white-space: nowrap; }

  .jobBody{
    padding: 12px 12px;
    background: var(--BackgroundColor1);
    display: grid;
    grid-template-columns: 1fr 70px;
    gap: 12px;
    align-items: center;
  }

  .jobBody .metaStack{
    justify-content: center;
  }

  .jobBody .metaStack .metaRow:first-child{
    display: none;
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

  .metaStack{ display:flex; flex-direction: column; gap: 8px; font-size: 11px; }
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


/* App card hover accents (match Sonarr blue / Radarr yellow) */
.appCard{
  transition: box-shadow .18s ease, border-color .18s ease, transform .18s ease;
}
.appCard[data-app-type="sonarr"]:hover{
  border-color:#38bdf8;
  box-shadow:0 0 0 1px rgba(56,189,248,.6),0 0 0px rgba(56,189,248,.5),0 0 18px rgba(56,189,248,.35);
}
.appCard[data-app-type="radarr"]:hover{
  border-color:#eeb530;
  box-shadow:0 0 0 1px rgba(238,181,48,.7),0 0 0px rgba(238,181,48,.55),0 0 18px rgba(238,181,48,.4);
}


.appCard{
  width: 300px;
  height: 150px;
  border-radius: 12px !important;
  overflow: hidden;
  border: 1px solid var(--BackgroundColor1);
  background: var(--BackgroundColor1);
  box-shadow: var(--shadow);
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  padding: 0; /* Job cards use section padding (header/body) */
  position: relative;
  user-select: none;
  cursor:pointer;
}

  .appCardTop{
  padding: 12px 12px;
  border-bottom: 1px solid var(--line);
  background: var(--HeaderBackgroundColor);
  display:flex;
  align-items:flex-start;
  justify-content: space-between;
  gap: 12px;
  min-width: 0;
}
/* App cards: match Job card body padding + footer */
.appCard:not(.addAppCard) > div:last-child{
  padding: 10px 12px 12px 12px;
}
.appCard:not(.addAppCard) .appCardLeft{
  padding: 0; /* header already padded */
}
.appCard:not(.addAppCard) .appTitle{
  font-size: 16px;
  font-weight: 700;
}
.appCard:not(.addAppCard) .appSub{
  font-size: 12px;
  opacity: .85;
}

  /* App card body (meta + status) */
  .appCardBody{
    padding: 6px 12px 12px 12px !important; /* tighter to divider */
    display:flex;
    flex-direction: column;
    gap: 10px;
  }
  .appCardBody .appMeta{
    margin: 0;
    padding: 0;
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    line-height: 2.3;
  }
.appTitle{
    font-size: 12px;
    font-weight: 600;
    line-height: 2.4;
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
  .jobsTable td .pill{
    justify-content: center;
  }
  .pill.good{ border-color: rgba(34,197,94,.45); }
  .pill.bad{ border-color: rgba(239,68,68,.55); background: rgba(239,68,68,.10); }

  /* Add App card (match Add Job card styling) */
  .appCard.addAppCard{
    display:flex;
    align-items:center;
    justify-content:center;
    cursor:pointer;
    user-select:none;
    border:4px dashed rgba(255,255,255,.18);
    background: rgba(255,255,255,.03);
    transition: background .18s ease, border-color .18s ease, box-shadow .18s ease, transform .18s ease;
  }
  body[data-theme="light"] .appCard.addAppCard{
    border-color: rgba(0,0,0,.14);
    background: rgba(0,0,0,.02);
  }
  .appCard.addAppCard:hover{
    border-color: rgba(167,213,65,.45);
    background: rgba(167,213,65,.06);
    box-shadow: 0 0 0 3px rgba(167,213,65,.18), var(--shadow);
    transform: translateY(-1px);
  }
  .appCard.addAppCard:active{ transform: translateY(0px); }

  .appCard.addAppCard .addAppCardInner{
    border: none;
    width: auto;
    height: auto;
    display:block;
    text-align:center;
    font-size: 42px;
    color: var(--muted);
    line-height: 1;
  }
  .appCard.addAppCard .addAppCardLabel{
    margin-top: 6px;
    font-weight: 700;
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
    border: 2px solid #717171;
    background: #4d4b4b;
    box-shadow: var(--shadow);
    padding: 14px 14px;
    cursor: pointer;
    user-select: none;
    display: flex;
    flex-direction: column;
    gap: 8px;
    min-height: 110px;
    border-radius: 10px;
  }

  [data-theme="light"] .pickTile{ background: #ffffff; }
  .pickTile:hover{
    border-color: #9f9f9f;
    box-shadow: #9f9f9f;
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
    width: min(540px, 100%);
    border: 3px solid var(--BackgroundColor1);
    background: var(--BackgroundColor1);
    box-shadow: var(--shadow);
    overflow:hidden;
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
    border-bottom: 1px solid var(--edgehighlight);
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
    border-top: 1px solid var(--edgehighlight);
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
    border: 1px solid var(--BackgroundColor1);
    border-radius: 8px;
    background: var(--FieldinputColor);
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

    /* App add/edit footer — equal height buttons */
  .appFooter .btn,
  .appFooterRight .btn{
    height: 30px;              /* single source of truth */
    min-height: 30px;
    padding: 0 14px;           /* horizontal only */
    display: inline-flex;
    align-items: center;
    justify-content: center;
    line-height: 1;
  }

/* ---------------------------
     Settings: header tabs
     --------------------------- */
  .settingsTabs{
    display:flex;
    align-items:flex-end;
    gap: 14px;
    height: 100%;
  }
  .settingsTab{
    background: none;
    border: none;
    padding: 10px 4px;
    font-size: 13px;
    font-weight: 700;
    color: var(--muted);
    cursor: pointer;
    border-bottom: 2px solid transparent;
    letter-spacing: .2px;
  }
  .settingsTab:hover{ color: var(--text); }
  .settingsTab.active{
    color: var(--accent);
    border-bottom-color: var(--accent);
  }
  .settingsPanel{ display:none; }
  .settingsPanel.active{ display:block; }

  /* Log window */
  .logWrap{ margin-top:14px; }
  .logToolbar{ display:flex; gap:10px; align-items:center; justify-content:space-between; margin-bottom:10px; }
  .logBox{
    border: 1px solid var(--line);
    background: rgba(0,0,0,.25);
    border-radius: 14px;
    padding: 12px;
    height: 420px;
    overflow: auto;
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    font-size: 12px;
    line-height: 1.4;
    white-space: pre;
  }

  /* Logs table (Status page) */
  .logTableWrap{
    border: 1px solid var(--line);
    background: rgba(0,0,0,.22);
    border-radius: 12px;
    overflow: hidden;
  }
  .logTableHeader{
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding: 12px 12px;
    border-bottom: 1px solid var(--line);
    background: rgba(255,255,255,.03);
    gap: 12px;
  }
  .logTableHeader .left{ display:flex; flex-direction:column; gap:2px; }
  .logTableHeader .right{ display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
  .logFilterSelect{
    background: rgba(0,0,0,.25);
    border: 1px solid var(--line);
    color: var(--text);
    border-radius: 10px;
    padding: 8px 10px;
    min-width: 170px;
    outline: none;
  }
  .logTable{
    width: 100%;
    border-collapse: separate;
    border-spacing: 0;
    font-size: 13px;
  }
  .logTable thead th{
    text-align:left;
    font-size: 12px;
    letter-spacing: .08em;
    text-transform: uppercase;
    color: rgba(255,255,255,.7);
    padding: 10px 12px;
    background: rgba(255,255,255,.06);
    border-bottom: 1px solid var(--line);
  }
  .logTable tbody td{
    padding: 12px 12px;
    border-bottom: 1px solid rgba(255,255,255,.06);
    vertical-align: middle;
  }
  .logTable tbody tr:hover td{
    background: rgba(255,255,255,.03);
  }
  .logPill{
    display:inline-flex;
    align-items:center;
    justify-content:center;
    height: 22px;
    padding: 0 10px;
    border-radius: 999px;
    font-size: 11px;
    border: 1px solid rgba(255,255,255,.25);
    color: rgba(255,255,255,.85);
    background: rgba(0,0,0,.18);
    letter-spacing: .04em;
  }
  .logPill.info{ border-color: rgba(0,255,120,.55); color: rgba(140,255,200,.95); }
  .logPill.debug{ border-color: rgba(255,255,255,.25); }
  .logPill.warning{ border-color: rgba(255,200,0,.55); color: rgba(255,230,140,.95); }
  .logPill.error{ border-color: rgba(255,80,80,.7); color: rgba(255,190,190,.98); }

  .logLabel{ font-weight: 700; }
  .logMsg{ color: rgba(255,255,255,.92); }
  .logActions{ display:flex; gap:8px; justify-content:flex-end; }
  .iconBtn{
    width: 36px;
    height: 32px;
    border-radius: 8px;
    border: 1px solid rgba(0,0,0,.25);
    background: rgba(255,140,0,.9);
    color: #fff;
    display:inline-flex;
    align-items:center;
    justify-content:center;
    cursor: pointer;
  }
  .iconBtn:hover{ filter: brightness(1.05); }
  .iconBtn:active{ transform: translateY(1px); }

  .logPager{
    display:flex;
    align-items:center;
    justify-content:space-between;
    padding: 10px 12px;
    background: rgba(255,255,255,.05);
    gap: 12px;
  }
  .logPager .muted{ color: rgba(255,255,255,.65); }
  .pagerBtns{ display:flex; gap:10px; align-items:center; }
  .btnTiny{
    padding: 8px 12px;
    border-radius: 10px;
    border: 1px solid var(--line);
    background: rgba(0,0,0,.20);
    color: var(--text);
  }
  .btnTiny:disabled{
    opacity: .45;
    cursor: not-allowed;
  }

  .logMeta{ display:flex; gap:10px; align-items:center; }
  .checkRow{ display:flex; gap:8px; align-items:center; }


  /* --- Job modal: sit BELOW fixed header (no overlap) --- */
  #jobBack{
    align-items: flex-start;
    /* keep the modal below the fixed top header */
    padding-top: calc(var(--top-h) + 18px) !important;
    padding-bottom: 18px !important;
  }
  #jobBack .modal{
    /* fill remaining viewport below header */
    height: calc(100vh - var(--top-h) - 36px);
    max-height: calc(100vh - var(--top-h) - 36px);
  }


  /* Job modal section descriptions */
  .fieldDesc{
    margin-top: 6px;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.35;
  }
  .sectionDesc{
    margin: 2px 0 10px;
    font-size: 12px;
    color: var(--muted);
    line-height: 1.35;
  }



  /* ------------------------------------------------
     True custom scrollbar (JS-driven; no ::-webkit-scrollbar theming)
     ------------------------------------------------ */
  .csWrap{
    display: grid;
    grid-template-columns: 1fr 12px;
    gap: 8px;
    align-items: stretch;
    min-height: 0;
  }

  /* When a scroll area is disabled (no overflow), collapse the bar gap */
  .csWrap.csNoScroll{
    grid-template-columns: 1fr 0px;
    gap: 0px;
  }

  .csViewport{
    min-height: 0;
    overflow: hidden !important; /* we translate content; no native scrollbars */
    position: relative;
    -webkit-overflow-scrolling: auto;
    scrollbar-width: none;
  }

  .csContent{
    will-change: transform;
    /* Add bottom padding so the last item isn't clipped (margins don't count toward scrollHeight) */
    padding-bottom: 16px;
    box-sizing: border-box;
  }

  /* Dropdown menus need less bottom padding */
  .csMenuViewport .csContent{
    padding-bottom: 6px;
  }

  .csBar{
    position: relative;
    width: 12px;
    min-width: 12px;
    padding: 2px;
    opacity: .85;
    transition: opacity .15s ease;
    user-select: none;
    touch-action: none;
  }
  .csWrap:hover .csBar{ opacity: 1; }

  .csTrack{
    position: absolute;
    top: 2px; bottom: 2px; left: 2px; right: 2px;
    border-radius: 999px;
    background: rgba(0,0,0,22);
  }
  body[data-theme="light"] .csTrack{
    background: rgba(0,0,0,10);
    border-color: rgba(0,0,0,12);
  }

  .csThumb{
    position: absolute;
    left: 2px;
    right: 2px;
    top: 2px;
    height: 48px;
    border-radius: 999px;
    background: rgba(167,213,65,55);
    border: 1px solid rgba(167,213,65,65);
    box-shadow: 0 0 0 0 rgba(167,213,65,0);
    cursor: pointer;
    transition: background .12s ease, box-shadow .12s ease, transform .12s ease;
  }

  .csWrap:hover .csThumb{
    background: rgba(167,213,65,72);
  }

  .csThumb:hover,
  .csThumb:active{
    background: rgba(167,213,65,88);
    box-shadow: 0 0 0 0px rgba(167,213,65,18), 0 0 8px rgba(167,213,65,45);
  }

  /* Make the bar blend on dark background using your scrollbar background variable */
  body[data-theme="dark"] .csTrack{
    background: rgba(89,89,89,55);
    border-color: rgba(255,255,255,10);
  }

  /* Reduce visual weight for tiny areas (dropdown menus etc.) */
  .fakeSelectMenu .csBar{ opacity: .65; }
  .fakeSelectMenu:hover .csBar{ opacity: .9; }

  /* Dropdown menus: keep absolute positioning; build scrollbar INSIDE the menu */
  .csMenuHost{
    padding: 0 !important;
    overflow: hidden !important;
  }
  .csMenuHost .csMenuWrap{
    height: auto;
  }
  .csMenuHost .csMenuWrap.csNoScroll{
    grid-template-columns: 1fr 0px;
    gap: 0px;
  }
  .csMenuHost .csMenuWrap{
    grid-template-columns: 1fr 10px;
    gap: 6px;
  }
  .csMenuHost .csMenuBar{
    width: 10px;
    min-width: 10px;
  }
  .csMenuHost .csTrack{
    left: 1px; right: 1px; top: 2px; bottom: 2px;
  }

  /* Dropdown menu scrollbar track override */
  .csMenuHost .csTrack{
    background: #7d7b7b !important;
  }
  .csMenuHost .csThumb{
    left: 1px; right: 1px;
  }
  .csMenuHost .csMenuViewport{
    height: auto;
    max-height: 260px;
    padding: 4px;
    box-sizing: border-box;
  }


  /* Validation: red outline for required empty inputs */
  .invalidField{
    border-color: rgba(239,68,68,0.9) !important;
    box-shadow: 0 0 0 3px rgba(239,68,68,0.25) !important;
  }

</style>

<script>
  function $(id){ return document.getElementById(id); }
  function setVal(id, v){ const el = $(id); if (el) el.value = v; }
  function setChecked(id, v){ const el = $(id); if (el) el.checked = !!v; }

  // -----------------------
  // Custom confirm modal (replaces browser confirm())
  // -----------------------
  window.__CONFIRM_RESOLVE = null;

  function customConfirm(message, opts){
    opts = opts || {};
    const title = opts.title || "Confirm";
    const okText = opts.okText || "OK";
    const cancelText = opts.cancelText || "Cancel";
    const danger = !!opts.danger;

    const titleEl = $("confirmTitle");
    const msgEl = $("confirmMessage");
    const okBtn = $("confirmOkBtn");
    const cancelBtn = $("confirmCancelBtn");

    if (titleEl) titleEl.textContent = title;
    if (msgEl) msgEl.textContent = message || "";

    if (okBtn) okBtn.textContent = okText;
    if (cancelBtn) cancelBtn.textContent = cancelText;

    if (okBtn){
      okBtn.classList.toggle("danger", danger);
      okBtn.classList.toggle("primary", !danger);
    }

    showModal("confirmBack");
    // Focus OK by default for delete confirms, otherwise Cancel
    setTimeout(() => {
      try {
        if (danger && okBtn) okBtn.focus();
        else if (cancelBtn) cancelBtn.focus();
      } catch(e){}
    }, 0);

    return new Promise((resolve) => {
      window.__CONFIRM_RESOLVE = resolve;
    });
  }

  function _confirmFinish(val){
    const r = window.__CONFIRM_RESOLVE;
    window.__CONFIRM_RESOLVE = null;
    hideModal("confirmBack");
    try { if (r) r(!!val); } catch(e){}
  }

  function confirmOk(){ _confirmFinish(true); }
  function confirmCancel(){ _confirmFinish(false); }

   // -----------------------
   // FakeSelect (custom dropdown) - keeps native <select> hidden for form submit
   // -----------------------

  function syncFakeSelect(selectId){
    const native = document.getElementById(selectId);
    const fake = document.querySelector('.fakeSelect[data-for="' + selectId + '"]');
    if (!native || !fake) return;

    const val = fake.querySelector(".fakeSelectValue");
    const cur = native.options[native.selectedIndex];
    if (val) val.textContent = cur ? cur.textContent : "-- Select --";

    const menu = fake.querySelector(".fakeSelectMenu");
    if (menu){
      menu.querySelectorAll(".fakeOpt").forEach(item => {
        const v = item.getAttribute("data-value") || "";
        const sel = (v === (native.value || ""));
        item.classList.toggle("active", sel);
        item.setAttribute("aria-selected", sel ? "true" : "false");
      });
    }
  }

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
       try { if (window.__initMenuScrollbar) window.__initMenuScrollbar(menu); } catch(e){}
       if (btn) btn.setAttribute("aria-expanded", "true");
       if (menu) menu.focus();
       // Ensure dropdown menu gets its internal custom scrollbar once visible
       try { if (window.__initCustomScrollbars) setTimeout(window.__initCustomScrollbars, 0); } catch(e){}
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

       // Init/refresh internal custom scrollbar for this dropdown menu (menu is rebuilt often)
       try { if (window.__initMenuScrollbar) window.__initMenuScrollbar(menu); } catch(e){}
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

  function resetModalScroll(modalEl){
    if (!modalEl) return;
    try { modalEl.scrollTop = 0; modalEl.scrollLeft = 0; } catch(e) {}

    // Common modal body container
    try {
      const mb = modalEl.querySelector(".mb");
      if (mb) { mb.scrollTop = 0; mb.scrollLeft = 0; }
    } catch(e) {}

    // Reset *any* scrollable descendants (nested panels, code blocks, etc.)
    try {
      const all = modalEl.querySelectorAll("*");
      all.forEach((n) => {
        try {
          const cs = getComputedStyle(n);
          const oy = cs.overflowY;
          const ox = cs.overflowX;
          const canY = (oy === "auto" || oy === "scroll") && (n.scrollHeight > n.clientHeight + 1);
          const canX = (ox === "auto" || ox === "scroll") && (n.scrollWidth  > n.clientWidth  + 1);
          if (canY) n.scrollTop = 0;
          if (canX) n.scrollLeft = 0;
        } catch(e) {}
      });
    } catch(e) {}
  }

  function showModal(id){
    const el = $(id);
    if (el) {
      el.style.display = "flex";

      // Reset scroll after it becomes visible (and again next frame for late layout)
      requestAnimationFrame(() => {
        resetModalScroll(el);
        requestAnimationFrame(() => resetModalScroll(el));
      });
    }
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

  function unescHtml(s){
    const ta = document.createElement("textarea");
    ta.innerHTML = (s ?? "").toString();
    return ta.value;
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

  async function maybeCloseAppModal(prefix){
    const back = (prefix === "r") ? $("appBackRadarr") : $("appBackSonarr");
    if (!back || back.style.display !== "flex") {
      hideModal(prefix === "r" ? "appBackRadarr" : "appBackSonarr");
      return;
    }
    appModalUpdateDirty(prefix);
    if (window.__APP_MODAL_DIRTY[prefix]){
      const ok = await customConfirm("Discard changes to this application?", {title:"Discard changes?", okText:"Discard", cancelText:"Keep editing"});
      if (!ok) return;
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

  async function submitDeleteApp(prefix){
    const id = ($("app_id_" + prefix)?.value || "").trim();
    if (!id) return;
    const ok = await customConfirm("Delete this app? Jobs using it must be updated first.", {title:"Delete application?", okText:"Delete", cancelText:"Cancel", danger:true});
    if (!ok) return;
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
    const nameEl = $("app_name_" + prefix);
    const urlEl  = $("app_url_" + prefix);
    const keyEl  = $("app_key_" + prefix);

    const name = ((nameEl?.value || "") + "").trim();
    const url  = ((urlEl?.value || "") + "").trim();
    const key  = ((keyEl?.value || "") + "").trim();

    const testBtn = $("appTestBtn_" + prefix);
    const saveBtn = $("appSaveBtn_" + prefix);
    const testOk = ($("app_test_ok_" + prefix)?.value || "") === "1";

    // Best-effort: only show red outline while the modal is open
    let modalOpen = true;
    try{
      const back = (prefix === "s") ? $("appBackSonarr") : $("appBackRadarr");
      modalOpen = !!(back && window.getComputedStyle(back).display !== "none");
    }catch(e){}

    // Reset visuals
    if (modalOpen){
      [nameEl, urlEl, keyEl].forEach(el => { if (el) el.classList.remove("invalidField"); });
    }
    if (saveBtn) saveBtn.removeAttribute("title");

    // Test gating: require all fields present
    if (testBtn) testBtn.disabled = (name === "" || url === "" || key === "");

    // Save gating: require all fields + Test OK
    let reason = "";
    if (name === "") reason = "App Name is required";
    else if (url === "") reason = "App URL is required";
    else if (key === "") reason = "API Key is required";
    else if (!testOk) reason = "Run Test to enable Save";

    if (saveBtn){
      saveBtn.disabled = (reason !== "");
      if (reason) saveBtn.title = reason;
    }

    if (modalOpen && reason){
      // highlight the first failing field
      if (name === "" && nameEl) nameEl.classList.add("invalidField");
      else if (url === "" && urlEl) urlEl.classList.add("invalidField");
      else if (key === "" && keyEl) keyEl.classList.add("invalidField");
    }
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

  async function maybeCloseJobModal(){
    const back = $("jobBack");
    if (!back || back.style.display !== "flex") {
      hideModal("jobBack");
      return;
    }
    jobModalUpdateDirty();
    if (window.__JOB_MODAL_DIRTY){
      const ok = await customConfirm("Discard changes to this job?", {title:"Discard changes?", okText:"Discard", cancelText:"Keep editing"});
      if (!ok) return;
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


  function setJobAppLabel(appType){
    const el = $("job_app_label");
    const t = (appType || "").toLowerCase();
    if (!el) return;
    if (t === "sonarr") el.textContent = "Sonarr Instances";
    else if (t === "radarr") el.textContent = "Radarr Instances";
    else el.textContent = "App";
  }

  function updateJobAppEmptyState(appType){
    const want = (appType || "").toLowerCase();
    const sel = $("job_app");
    const empty = $("job_app_empty");
    const title = $("job_app_empty_title");
    const msg = $("job_app_empty_msg");
    const saveBtn = $("jobSaveBtn");

    if (!sel || !empty) return;

    const realOpts = Array.from(sel.options || []).filter(o => (o.value || "").toString().trim() !== "");
    const none = (realOpts.length === 0);

    if (none){
      empty.style.display = "";
      if (want === "sonarr"){
        if (title) title.textContent = "Sonarr not configured";
        if (msg) msg.textContent = "No Sonarr instances are configured yet. Add a Sonarr instance in Settings → Apps.";
      } else if (want === "radarr"){
        if (title) title.textContent = "Radarr not configured";
        if (msg) msg.textContent = "No Radarr instances are configured yet. Add a Radarr instance in Settings → Apps.";
      } else {
        if (title) title.textContent = "App not configured";
        if (msg) msg.textContent = "No connected instances are available. Add an instance in Settings → Apps.";
      }

      // disable select & fake
      sel.disabled = true;
      const fake = $("fake_job_app");
      if (fake && fake.__syncDisabled) fake.__syncDisabled();

      // disable save
      if (saveBtn) saveBtn.disabled = true;
    } else {
      empty.style.display = "none";
      sel.disabled = false;
      const fake = $("fake_job_app");
      if (fake && fake.__syncDisabled) fake.__syncDisabled();
      if (saveBtn) saveBtn.disabled = false;
    }
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


  function filterJobAppOptions(preferredType, keepValue){
    // preferredType: "radarr" | "sonarr" | "" (no filter)
    const sel = $("job_app");
    if (!sel) return;

    const want = (preferredType || "").toLowerCase().trim();
    const keep = (keepValue ?? sel.value ?? "").toString();

    // Cache original options once
    if (!sel.__allOptions){
      sel.__allOptions = Array.from(sel.options).map(o => o.cloneNode(true));
    }

    // Rebuild options list from cache
    sel.innerHTML = "";
    for (const opt of sel.__allOptions){
      const v = (opt.value || "").toString();
      if (!v){
        sel.appendChild(opt.cloneNode(true));
        continue;
      }
      if (want === "radarr" || want === "sonarr"){
        const t = (window.__APP_TYPES && window.__APP_TYPES[v]) ? window.__APP_TYPES[v] : "";
        if (t && t !== want) continue; // completely omit
      }
      sel.appendChild(opt.cloneNode(true));
    }

    // Restore selection if possible
    const hasKeep = Array.from(sel.options).some(o => o.value === keep);
    if (hasKeep){
      sel.value = keep;
    } else if (sel.options.length){
      sel.value = sel.options[0].value;
    }

    // Rebuild FakeSelect UI
    const fake = $("fake_job_app");
    if (fake && fake.__rebuild) fake.__rebuild();
    syncFakeSelect("job_app");

    setJobAppLabel(want);
    updateJobAppEmptyState(want);
      updateJobDaysDesc(want);
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


  function updateJobDaysDesc(appIdOrType){
    const el = $("job_days_desc");
    if (!el) return;

    let tpe = (appIdOrType || "").toString().toLowerCase();
    // If an app id was passed, resolve to type
    if (window.__APP_TYPES && window.__APP_TYPES[appIdOrType]){
      tpe = window.__APP_TYPES[appIdOrType];
    }

    if (tpe === "sonarr"){
      el.textContent = "Items must be older than this many days (based on the Added date in Sonarr).";
    } else if (tpe === "radarr"){
      el.textContent = "Items must be older than this many days (based on the Added date in Radarr).";
    } else {
      el.textContent = "Items must be older than this many days (based on the Added date in the selected app).";
    }
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
    const t = (window.__APP_TYPES && window.__APP_TYPES[appId]) ? window.__APP_TYPES[appId] : "";
    if (t) filterJobAppOptions(t, appId);
    rebuildTagOptions(appId, "");
    updateSonarrModeVisibility(appId);
    updateRadarrScoreVisibility(appId);
    updateJobDaysDesc(appId);
    setTimeout(jobModalUpdateDirty, 0);
  }


  function setJobModalTitle(appTypeOrId, isEdit){
    let t = (appTypeOrId || "").toString();
    // appTypeOrId may be an app id; map via __APP_TYPES if available
    if (window.__APP_TYPES && window.__APP_TYPES[t]) t = window.__APP_TYPES[t];
    t = (t || "radarr").toLowerCase();
    const isSonarr = (t === "sonarr");

    const iconEl = $("jobTitleIcon");
    const textEl = $("jobTitleText");

    if (iconEl){
      iconEl.src = isSonarr ? "/images/sonarr_icon.svg" : "/images/radarr_icon.svg";
      iconEl.alt = isSonarr ? "Sonarr" : "Radarr";
    }
    if (textEl){
      textEl.textContent = (isEdit ? "Edit " : "Add ") + (isSonarr ? "Sonarr Job" : "Radarr Job");
    }
  }

async function openNewJob(preferredType){
    const form = $("jobForm");
    if (!form) return;

    form.action = "/jobs/save";
    setVal("job_id", "");
    setVal("job_name", "New Job");

    const appSel = $("job_app");
    const defApp = appSel?.getAttribute("data-default-app") || "";

    // If the requested app type has no connected instances, offer to jump to Apps settings
    const want = (preferredType || "").toString().toLowerCase();
    const typesMap = (window.__APP_TYPES || {});
    const connectedCount = Object.values(typesMap).filter(v => (v || "").toString().toLowerCase() === want).length;

    if ((want === "sonarr" || want === "radarr") && connectedCount === 0){
      const label = (want === "sonarr") ? "Sonarr" : "Radarr";
      const ok = await customConfirm(
        `${label} not configured. Would you like to do this now?`,
        { title: `${label} not configured`, okText: "Go to Apps", cancelText: "Not now" }
      );
      if (ok){
        window.location.href = "/settings?tab=apps";
      }
      return;
    }


    // Filter app list to the section type (Sonarr card shows Sonarr apps, etc.)
    filterJobAppOptions(preferredType, defApp);

    // Prefer first app matching preferredType ("radarr"|"sonarr") if provided
    if (appSel) {
      let picked = "";
      if (preferredType && window.__APP_TYPES) {
        if (defApp && window.__APP_TYPES[defApp] === preferredType) {
          picked = defApp;
        } else {
          for (let i = 0; i < appSel.options.length; i++) {
            const v = appSel.options[i].value;
            if (window.__APP_TYPES[v] === preferredType) { picked = v; break; }
          }
        }
      }
      if (!picked && defApp) picked = defApp;
      if (picked) appSel.value = picked;
      if (appSel.selectedIndex < 0 && appSel.options.length > 0) appSel.selectedIndex = 0;
    }

    const actualApp = appSel ? (appSel.value || defApp) : defApp;
    syncFakeSelect("job_app");

    rebuildTagOptions(actualApp, "");
    updateSonarrModeVisibility(actualApp);
    updateRadarrScoreVisibility(actualApp);

    setVal("job_sonarr_mode", "episodes_only");
    setVal("job_days", "30");
    setVal("job_day", "daily");
    setVal("job_hour", "3");
    setChecked("job_dry", true);
setChecked("job_excl", false);
    setVal("job_enabled", "1");

    // Radarr score filter defaults
    setChecked("job_score_enabled", false);
    setVal("job_score_min", "60");

    setJobModalTitle(actualApp || preferredType, false);
    showModal("jobBack");
    setTimeout(jobModalMarkClean, 0);
    try{ setTimeout(refreshJobSaveButton, 0); }catch(e){}
  }

  function openEditJob(btn){
    const form = $("jobForm");
    if (!form || !btn) return;

    form.action = "/jobs/save";
    setVal("job_id", btn.getAttribute("data-id") || "");
    setVal("job_name", btn.getAttribute("data-name") || "Job");

    const appId = btn.getAttribute("data-app-id") || "";
    setVal("job_app", appId);
    // Limit the App dropdown to the correct type for this job
    const jobType = (window.__APP_TYPES && window.__APP_TYPES[appId]) ? window.__APP_TYPES[appId] : "";
    filterJobAppOptions(jobType, appId);
    syncFakeSelect("job_app");

    // Tag value may be HTML-escaped in attributes (e.g. "&amp;")
    const tag = unescHtml(btn.getAttribute("data-tag") || "");
    rebuildTagOptions(appId, tag);
    syncFakeSelect("job_tag");

    updateSonarrModeVisibility(appId);
    updateRadarrScoreVisibility(appId);

    const smode = btn.getAttribute("data-sonarr-mode") || "episodes_only";
    setVal("job_sonarr_mode", smode);
    syncFakeSelect("job_sonarr_mode");

    setVal("job_days", btn.getAttribute("data-days") || "30");

    setVal("job_day", btn.getAttribute("data-day") || "daily");
    syncFakeSelect("job_day");

    setVal("job_hour", btn.getAttribute("data-hour") || "3");
    syncFakeSelect("job_hour");

    setChecked("job_dry", (btn.getAttribute("data-dry") || "1") === "1");
    setChecked("job_excl", (btn.getAttribute("data-excl") || "0") === "1");

    setVal("job_enabled", (btn.getAttribute("data-enabled") || "1"));
    syncFakeSelect("job_enabled");

    // Radarr score filter
    setChecked("job_score_enabled", (btn.getAttribute("data-score-en") || "0") === "1");
    setVal("job_score_min", btn.getAttribute("data-score-min") || "60");

    setJobModalTitle(jobType || appId, true);
    showModal("jobBack");
    try{ setTimeout(validateJobForm, 0); }catch(e){}
    setTimeout(jobModalMarkClean, 0);
    try{ setTimeout(refreshJobSaveButton, 0); }catch(e){}
  }

  function openRunNowConfirm(jobId, opts){
    opts = opts || {};
    const appLabel = (opts.appLabel || "App");
    const dryRun = !!opts.dryRun;
    const enabled = (opts.enabled === undefined) ? true : !!opts.enabled;

    const hid = $("runNowJobId");
    if (hid) hid.value = jobId || "";

    const elApp = $("rn_app");
    const elDry = $("rn_dry");
    const elEnabled = $("rn_enabled");

    if (elApp) elApp.textContent = appLabel;
    if (elDry) elDry.textContent = dryRun ? "ON" : "OFF";
    if (elEnabled) elEnabled.textContent = enabled ? "Enabled" : "Disabled";

    const msg = $("rn_msg");
    if (msg){
      msg.textContent = dryRun
        ? "Dry Run is ON — preview only (no deletions)."
        : "Dry Run is OFF — this will delete files and items in the app.";
    }
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
    // Jobs page: avoid inline JS by using data-action handlers
    document.addEventListener("click", (e) => {
      const el = e.target.closest("[data-action]");
      if (!el) return;
      const act = el.getAttribute("data-action");
      if (act === "job-add-card") {
        e.preventDefault();
        const t = el.getAttribute("data-app-type") || "";
        openNewJob(t);
      } else if (act === "job-edit") {
        e.preventDefault();
        openEditJob(el);
      } else if (act === "run-now") {
        e.preventDefault();
        const jid = el.getAttribute("data-job-id") || "";
        const appLabel = el.getAttribute("data-app-label") || "App";
        const enabled = (el.getAttribute("data-enabled") === "true" || el.getAttribute("data-enabled") === "1");
        const delFiles = (el.getAttribute("data-delete-files") === "true" || el.getAttribute("data-delete-files") === "1");
        openRunNowConfirm(jid, { appLabel: appLabel, dryRun: false, deleteFiles: delFiles, enabled: enabled });
      }
    });

    // Keyboard activation for the add-job cards
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter" && e.key !== " ") return;
      const el = e.target.closest('[data-action="job-add-card"]');
      if (!el) return;
      e.preventDefault();
      const t = el.getAttribute("data-app-type") || "";
      openNewJob(t);
    });

    // Confirm before deleting a job (Sonarr/Radarr specific + includes job name)
    document.addEventListener("submit", async (e) => {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (!form.classList.contains("jobDeleteForm")) return;
      e.preventDefault();

      const rawJobName = form.getAttribute("data-job-name") || "";
      const jobName = rawJobName ? unescHtml(rawJobName) : "";
      const appType = (form.getAttribute("data-app-type") || "").toLowerCase();
      const appLabel = (appType === "sonarr") ? "Sonarr" : ((appType === "radarr") ? "Radarr" : "App");

      const title = `Delete ${appLabel} job`;
      let msg = jobName ? `Delete ${appLabel} job "${jobName}"?` : `Delete this ${appLabel} job?`;
      msg += " This cannot be undone.";

      const ok = await customConfirm(msg, {title: title, okText:"Delete", cancelText:"Cancel", danger:true});
      if (ok) { try { form.submit(); } catch(err){} }
    });

    // Job name overflow fade (only when overflowing)
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


    // Settings tabs (General / Apps)
    (function(){
      const tabs = document.querySelectorAll(".settingsTab");
      if (!tabs || !tabs.length) return;
      function activate(key){
        tabs.forEach(t => {
          const on = (t.getAttribute("data-tab")||"") === key;
          t.classList.toggle("active", on);
          t.setAttribute("aria-selected", on ? "true" : "false");
        });
        document.querySelectorAll(".settingsPanel").forEach(p => {
          p.classList.toggle("active", p.id === ("settings_tab_" + key));
        });
      }
      tabs.forEach(t => {
        t.addEventListener("click", () => {
          const key = (t.getAttribute("data-tab")||"general");
          activate(key);
        });
      });
      // default
      const qp = new URLSearchParams(location.search || "");
      const presetQ = (qp.get("tab") || "").trim().toLowerCase();
      const presetH = (location.hash || "").replace("#","").trim().toLowerCase();
      const preset = presetQ || presetH;
      if (preset === "apps") activate("apps");
      else activate("general");
    })();

    // -----------------------
    // Apps cards: live connection check (Settings → Apps)
    // - Greyed icon when disconnected
    // - Pulse while checking
    // -----------------------
    let __appsPingTimer = null;

    function isSettingsAppsTabActive(){
      const panel = document.getElementById("settings_tab_apps");
      return !!(panel && panel.classList.contains("active"));
    }

    function setAppCardState(card, state){
      if (!card) return;
      card.classList.remove("is-connected","is-disconnected","is-checking");
      card.classList.add(state);
    }

    async function refreshAppsConnectivity(){
      if (!isSettingsAppsTabActive()) return;

      const cards = Array.from(document.querySelectorAll(".appCard[data-app-id]"));
      if (!cards.length) return;

      // set checking state immediately
      cards.forEach(c => setAppCardState(c, "is-checking"));
      cards.forEach(c => {
        const pill = c.querySelector("[data-pill]");
        if (pill){
          pill.classList.remove("good","bad");
          pill.textContent = "Checking connection…";
        }
      });

      try{
        const resp = await fetch("/apps/ping", { cache: "no-store" });
        const data = await resp.json();
        const st = (data && data.status) ? data.status : {};
        let connectedN = 0;

        cards.forEach(c => {
          const id = c.getAttribute("data-app-id") || "";
          const ok = !!st[id];
          setAppCardState(c, ok ? "is-connected" : "is-disconnected");

          const pill = c.querySelector("[data-pill]");
          if (pill){
            pill.classList.toggle("good", ok);
            pill.classList.toggle("bad", !ok);
            pill.textContent = ok ? "Connected" : "Not Connected";
          }
          if (ok) connectedN += 1;
        });

        const cnt = document.getElementById("appsConnectedCount");
        if (cnt) cnt.textContent = String(connectedN);
      } catch(e){
        // On error, revert to disconnected
        cards.forEach(c => {
          setAppCardState(c, "is-disconnected");
          const pill = c.querySelector("[data-pill]");
          if (pill){
            pill.classList.remove("good");
            pill.classList.add("bad");
            pill.textContent = "Not Connected";
          }
        });
      }
    }

    function startAppsPing(){
      if (__appsPingTimer) return;
      refreshAppsConnectivity();
      __appsPingTimer = setInterval(() => refreshAppsConnectivity(), 30000);
    }

    function stopAppsPing(){
      if (__appsPingTimer) clearInterval(__appsPingTimer);
      __appsPingTimer = null;
    }

    // refresh immediately when user switches to Apps tab
    document.querySelectorAll('.settingsTabs [data-tab="apps"]').forEach(btn => {
      btn.addEventListener("click", () => refreshAppsConnectivity());
    });

    // start background timer (refresh is no-op unless Apps tab is active)
    startAppsPing();


        const host = $("toastHost");
    if (host) setTimeout(() => { try { host.remove(); } catch(e){} }, 6000);
  });

  // ------------------------------------------------
  // True custom scrollbar (no ::-webkit-scrollbar theming)
  // ------------------------------------------------
  (function(){
    const CS_SEL = [
      ".pageContent .grid > .card:only-child > .bd",
      ".modal .mb",
      ".tablewrap",      ".logBox",
      ".logTableBody"
    ].join(",");

    function clamp(v, a, b){ return Math.max(a, Math.min(b, v)); }


    function initCore(viewport, content, wrap, bar, thumb){
      let scrollTop = 0;
      let maxScroll = 0;
      let dragging = false;
      let dragStartY = 0;
      let dragStartScroll = 0;

      function updateMetrics(){
        const vh = viewport.clientHeight || 0;
        const ch = content.scrollHeight || 0;
        maxScroll = Math.max(0, ch - vh);

        scrollTop = clamp(scrollTop, 0, maxScroll);

        if (maxScroll <= 0){
          wrap.classList.add("csNoScroll");
        } else {
          wrap.classList.remove("csNoScroll");
        }

        content.style.transform = "translate3d(0," + (-scrollTop) + "px,0)";

        const trackH = bar.clientHeight - 4;
        const thumbMin = 22;
        const thumbH = (maxScroll <= 0 || ch <= 0) ? trackH : Math.max(thumbMin, Math.round((vh / ch) * trackH));
        thumb.style.height = thumbH + "px";

        const travel = Math.max(0, trackH - thumbH);
        const tt = (maxScroll <= 0) ? 0 : (scrollTop / maxScroll);
        const top = 2 + Math.round(travel * tt);
        thumb.style.top = top + "px";
      }

      function scrollBy(delta){
        if (maxScroll <= 0) return false;
        const next = clamp(scrollTop + delta, 0, maxScroll);
        if (next === scrollTop) return false;
        scrollTop = next;
        updateMetrics();
        return true;
      }

      viewport.addEventListener("wheel", (e) => {
        if (maxScroll <= 0) return;
        const dy = e.deltaY || 0;
        if ((dy < 0 && scrollTop <= 0) || (dy > 0 && scrollTop >= maxScroll)){
          return;
        }
        const handled = scrollBy(dy);
        if (handled){
          e.preventDefault();
          e.stopPropagation();
        }
      }, { passive: false });

      function onMove(e){
        if (!dragging) return;
        const y = (e.touches && e.touches[0]) ? e.touches[0].clientY : e.clientY;
        const trackH = bar.clientHeight - 4;
        const thumbH = thumb.getBoundingClientRect().height;
        const travel = Math.max(0, trackH - thumbH);
        const dy = y - dragStartY;
        const ratio = (travel <= 0) ? 0 : (dy / travel);
        scrollTop = clamp(dragStartScroll + ratio * maxScroll, 0, maxScroll);
        updateMetrics();
        e.preventDefault();
      }
      function onUp(){
        if (!dragging) return;
        dragging = false;
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
      }

      thumb.addEventListener("pointerdown", (e) => {
        if (maxScroll <= 0) return;
        dragging = true;
        thumb.setPointerCapture(e.pointerId);
        dragStartY = e.clientY;
        dragStartScroll = scrollTop;
        document.addEventListener("pointermove", onMove, { passive: false });
        document.addEventListener("pointerup", onUp);
        e.preventDefault();
        e.stopPropagation();
      });

      bar.addEventListener("pointerdown", (e) => {
        if (maxScroll <= 0) return;
        if (e.target === thumb) return;
        const r = bar.getBoundingClientRect();
        const y = e.clientY - r.top - 2;
        const trackH = bar.clientHeight - 4;
        const thumbH = thumb.getBoundingClientRect().height;
        const travel = Math.max(0, trackH - thumbH);
        const tt = clamp((y - (thumbH / 2)) / (travel || 1), 0, 1);
        scrollTop = tt * maxScroll;
        updateMetrics();
        e.preventDefault();
      });

      thumb.addEventListener("keydown", (e) => {
        if (maxScroll <= 0) return;
        const key = e.key || "";
        let handled = false;
        if (key === "ArrowDown"){ handled = scrollBy(32); }
        else if (key === "ArrowUp"){ handled = scrollBy(-32); }
        else if (key === "PageDown"){ handled = scrollBy(viewport.clientHeight * 0.9); }
        else if (key === "PageUp"){ handled = scrollBy(-viewport.clientHeight * 0.9); }
        else if (key === "Home"){ scrollTop = 0; updateMetrics(); handled = true; }
        else if (key === "End"){ scrollTop = maxScroll; updateMetrics(); handled = true; }
        if (handled){ e.preventDefault(); e.stopPropagation(); }
      });

      try{
        const ro = new ResizeObserver(() => updateMetrics());
        ro.observe(viewport);
        ro.observe(content);
      }catch(e){}

      try{
        const mo = new MutationObserver(() => updateMetrics());
        mo.observe(content, { childList: true, subtree: true, characterData: true });
      }catch(e){}

      setTimeout(updateMetrics, 0);
    }

    function initMenu(menuEl){
      if (!menuEl) return;
      // If buildMenu rebuilt the menu (innerHTML reset), csMenuWrap disappears but csInit stays.
      // Allow re-init in that case so the scrollbar doesn't "work once then vanish".
      if (menuEl.dataset.csInit === "1" && menuEl.querySelector(".csMenuWrap")){
        // refresh height constraint
        try{
          const cs = getComputedStyle(menuEl);
          const mh = (cs && cs.maxHeight && cs.maxHeight !== "none") ? cs.maxHeight : "260px";
          const vp = menuEl.querySelector(".csMenuViewport");
          if (vp) vp.style.maxHeight = mh;
        }catch(e){}
        return;
      }

      // only init once visible
      try{
        const cs = getComputedStyle(menuEl);
        if (cs && (cs.display === "none" || cs.visibility === "hidden")) return;
      }catch(e){}

      menuEl.dataset.csInit = "1";
      menuEl.classList.add("csMenuHost");

      // If we've already built the internal structure, bail
      if (menuEl.querySelector(".csMenuWrap")) return;

      const wrap = document.createElement("div");
      wrap.className = "csWrap csMenuWrap";

      const viewport = document.createElement("div");
      viewport.className = "csViewport csMenuViewport";

      const content = document.createElement("div");
      content.className = "csContent";

      // Move existing children into content
      while (menuEl.firstChild) content.appendChild(menuEl.firstChild);
      viewport.appendChild(content);

      const bar = document.createElement("div");
      bar.className = "csBar csMenuBar";
      bar.setAttribute("aria-hidden", "true");

      const track = document.createElement("div");
      track.className = "csTrack";

      const thumb = document.createElement("div");
      thumb.className = "csThumb";
      thumb.tabIndex = 0;

      bar.appendChild(track);
      bar.appendChild(thumb);

      wrap.appendChild(viewport);
      wrap.appendChild(bar);
      menuEl.appendChild(wrap);

      // Ensure viewport has a real height constraint (percentage heights won't resolve on max-height menus)
      try{
        const cs = getComputedStyle(menuEl);
        const mh = (cs && cs.maxHeight && cs.maxHeight !== "none") ? cs.maxHeight : "260px";
        viewport.style.maxHeight = mh;
        viewport.style.height = "auto";
        wrap.style.height = "auto";
      }catch(e){}

      initCore(viewport, content, wrap, bar, thumb);
    }

function initOne(el){
      if (!el || el.dataset.csInit === "1") return;

      // If the element is currently hidden, don't wrap it (wrapping would leave the bar visible)
      // We'll try again when it becomes visible.
      try{
        const cs = getComputedStyle(el);
        if (cs && cs.display === "none") return;
      }catch(e){}

      // Dropdown menus must keep their absolute positioning; use an internal scrollbar instead of wrapping
      if (el.classList && el.classList.contains("fakeSelectMenu")){ initMenu(el); return; }

      el.dataset.csInit = "1";

      const parent = el.parentNode;
      if (!parent) return;

      const wrap = document.createElement("div");
      wrap.className = "csWrap";

      const bar = document.createElement("div");
      bar.className = "csBar";
      bar.setAttribute("aria-hidden", "true");

      const track = document.createElement("div");
      track.className = "csTrack";

      const thumb = document.createElement("div");
      thumb.className = "csThumb";
      thumb.tabIndex = 0;

      bar.appendChild(track);
      bar.appendChild(thumb);

      parent.replaceChild(wrap, el);
      wrap.appendChild(el);
      wrap.appendChild(bar);

      // Turn the original element into a viewport and move its children into csContent
      el.classList.add("csViewport");
      const content = document.createElement("div");
      content.className = "csContent";
      while (el.firstChild) content.appendChild(el.firstChild);
      el.appendChild(content);

      let scrollTop = 0;
      let maxScroll = 0;
      let dragging = false;
      let dragStartY = 0;
      let dragStartScroll = 0;

      function updateMetrics(){
        const vh = el.clientHeight || 0;
        const ch = content.scrollHeight || 0;
        maxScroll = Math.max(0, ch - vh);

        scrollTop = clamp(scrollTop, 0, maxScroll);

        // Toggle bar visibility + spacing
        if (maxScroll <= 0){
          wrap.classList.add("csNoScroll");
        } else {
          wrap.classList.remove("csNoScroll");
        }

        // Apply transform
        content.style.transform = "translate3d(0," + (-scrollTop) + "px,0)";

        // Thumb sizing + position
        const trackH = bar.clientHeight - 4; // padding accounted
        const thumbMin = 26;
        const thumbH = (maxScroll <= 0 || ch <= 0) ? trackH : Math.max(thumbMin, Math.round((vh / ch) * trackH));
        thumb.style.height = thumbH + "px";

        const travel = Math.max(0, trackH - thumbH);
        const t = (maxScroll <= 0) ? 0 : (scrollTop / maxScroll);
        const top = 2 + Math.round(travel * t);
        thumb.style.top = top + "px";
      }

      function scrollBy(delta){
        if (maxScroll <= 0) return false;
        const next = clamp(scrollTop + delta, 0, maxScroll);
        if (next === scrollTop) return false;
        scrollTop = next;
        updateMetrics();
        return true;
      }

      // Wheel / trackpad
      el.addEventListener("wheel", (e) => {
        if (maxScroll <= 0) return;
        const dy = e.deltaY || 0;

        // Let outer scroll areas handle if we're at the ends
        if ((dy < 0 && scrollTop <= 0) || (dy > 0 && scrollTop >= maxScroll)){
          return;
        }

        const handled = scrollBy(dy);
        if (handled){
          e.preventDefault();
          e.stopPropagation();
        }
      }, { passive: false });

      // Thumb drag
      function onMove(e){
        if (!dragging) return;
        const y = (e.touches && e.touches[0]) ? e.touches[0].clientY : e.clientY;
        const trackH = bar.clientHeight - 4;
        const thumbH = thumb.getBoundingClientRect().height;
        const travel = Math.max(0, trackH - thumbH);
        const dy = y - dragStartY;
        const ratio = (travel <= 0) ? 0 : (dy / travel);
        scrollTop = clamp(dragStartScroll + ratio * maxScroll, 0, maxScroll);
        updateMetrics();
        e.preventDefault();
      }
      function onUp(){
        if (!dragging) return;
        dragging = false;
        document.removeEventListener("pointermove", onMove);
        document.removeEventListener("pointerup", onUp);
      }

      thumb.addEventListener("pointerdown", (e) => {
        if (maxScroll <= 0) return;
        dragging = true;
        thumb.setPointerCapture(e.pointerId);
        dragStartY = e.clientY;
        dragStartScroll = scrollTop;
        document.addEventListener("pointermove", onMove, { passive: false });
        document.addEventListener("pointerup", onUp);
        e.preventDefault();
        e.stopPropagation();
      });

      // Track click
      bar.addEventListener("pointerdown", (e) => {
        if (maxScroll <= 0) return;
        if (e.target === thumb) return;
        const r = bar.getBoundingClientRect();
        const y = e.clientY - r.top - 2;
        const trackH = bar.clientHeight - 4;
        const thumbH = thumb.getBoundingClientRect().height;
        const travel = Math.max(0, trackH - thumbH);
        const t = clamp((y - (thumbH / 2)) / (travel || 1), 0, 1);
        scrollTop = t * maxScroll;
        updateMetrics();
        e.preventDefault();
      });

      // Keyboard (when thumb focused)
      thumb.addEventListener("keydown", (e) => {
        if (maxScroll <= 0) return;
        const key = e.key || "";
        let handled = false;
        if (key === "ArrowDown"){ handled = scrollBy(40); }
        else if (key === "ArrowUp"){ handled = scrollBy(-40); }
        else if (key === "PageDown"){ handled = scrollBy(el.clientHeight * 0.9); }
        else if (key === "PageUp"){ handled = scrollBy(-el.clientHeight * 0.9); }
        else if (key === "Home"){ scrollTop = 0; updateMetrics(); handled = true; }
        else if (key === "End"){ scrollTop = maxScroll; updateMetrics(); handled = true; }
        if (handled){ e.preventDefault(); e.stopPropagation(); }
      });

      // Watch for size/content changes
      try{
        const ro = new ResizeObserver(() => updateMetrics());
        ro.observe(el);
        ro.observe(content);
      }catch(e){}

      try{
        const mo = new MutationObserver(() => updateMetrics());
        mo.observe(content, { childList: true, subtree: true, characterData: true });
      }catch(e){}

      // Initial
      setTimeout(updateMetrics, 0);
    }

    function initAll(){
      document.querySelectorAll(CS_SEL).forEach(initOne);
    }

    // expose for pages that inject content dynamically
    window.__initCustomScrollbars = initAll;
    window.__initMenuScrollbar = initMenu;

    document.addEventListener("DOMContentLoaded", () => {
      initAll();
    });

    // Re-init when modals open/close
    document.addEventListener("click", (e) => {
      const t = e.target;
      if (!t) return;
      // common modal open triggers: add job/app, edit buttons etc.
      if (t.closest && (t.closest(".addJobBar") || t.closest(".addAppCard") || t.closest(".jobEditBtn") || t.closest(".btn"))){
        setTimeout(initAll, 0);
      }
    }, true);

  })();

</script>




<script>
/* --- Combined Job Form Validator (UI-only)
   Rules:
   - Job Name required (always)
   - Days Old required (always)
   - If Score Filter is ON -> Score required
   UX:
   - Add .invalidField red outline to empty fields
   - Disable Save and set tooltip explaining the first failing rule
*/
(function(){
  function _isVisible(el){
    return !!(el && el.offsetParent !== null);
  }
  function _isModalOpen(modal){
    if (!modal) return false;
    try{
      return window.getComputedStyle(modal).display !== "none";
    }catch(e){
      return true;
    }
  }

  function validateJobForm(){
    const modal = document.getElementById("jobBack");
    const saveBtn = document.getElementById("jobSaveBtn");
    if (!saveBtn) return true;
    if (modal && !_isModalOpen(modal)) return true; // don't interfere when modal closed

    const nameInput  = document.getElementById("job_name");
    const daysInput  = document.getElementById("job_days");
    const scoreEn    = document.getElementById("job_score_enabled");
    const scoreInput = document.getElementById("job_score_min");

    // reset visuals + tooltip
    [nameInput, daysInput, scoreInput].forEach(el => { if (el) el.classList.remove("invalidField"); });
    saveBtn.removeAttribute("title");

    // 1) Job Name required
    if (nameInput && _isVisible(nameInput) && nameInput.value.trim() === ""){
      saveBtn.disabled = true;
      nameInput.classList.add("invalidField");
      saveBtn.title = "Job Name is required";
      return false;
    }

    // 2) Days Old required
    if (daysInput && _isVisible(daysInput) && daysInput.value.trim() === ""){
      saveBtn.disabled = true;
      daysInput.classList.add("invalidField");
      saveBtn.title = "Days Old is required";
      return false;
    }

    // 3) Score required only when enabled
    const scoreGateOn = !!(scoreEn && !scoreEn.disabled && scoreEn.checked);
    if (scoreGateOn){
      if (scoreInput && _isVisible(scoreInput) && !scoreInput.disabled && scoreInput.value.trim() === ""){
        saveBtn.disabled = true;
        scoreInput.classList.add("invalidField");
        saveBtn.title = "Score is required";
        return false;
      }
    }

    // Also validate open App modals (Apps form fields)
    try{ refreshAppButtons("s"); }catch(e){}
    try{ refreshAppButtons("r"); }catch(e){} 

    // Valid
    saveBtn.disabled = false;
    return true;
  }

  // Expose globally (useful for openNewJob/openEditJob)
  window.validateJobForm = validateJobForm;

  // Live events
  document.addEventListener("input", (e) => {
    const id = e.target && e.target.id;
    if (id === "job_name" || id === "job_days" || id === "job_score_min" || id === "app_name_s" || id === "app_url_s" || id === "app_key_s" || id === "app_name_r" || id === "app_url_r" || id === "app_key_r") validateJobForm();
  });
  document.addEventListener("change", (e) => {
    const id = e.target && e.target.id;
    if (id === "job_score_enabled" || id === "job_app" || id === "app_test_ok_s" || id === "app_test_ok_r") validateJobForm();
  });
  document.addEventListener("keyup", (e) => {
    const id = e.target && e.target.id;
    if (id === "job_name" || id === "job_days" || id === "job_score_min" || id === "app_name_s" || id === "app_url_s" || id === "app_key_s" || id === "app_name_r" || id === "app_url_r" || id === "app_key_r") validateJobForm();
  });

  // Re-check whenever the modal opens/closes
  const modal = document.getElementById("jobBack");
  if (modal && window.MutationObserver){
    const mo = new MutationObserver(() => { try{ validateJobForm(); }catch(e){} });
    mo.observe(modal, { attributes:true, attributeFilter:["style","class"] });
  }

  document.addEventListener("DOMContentLoaded", () => { try{ validateJobForm(); }catch(e){} });
})();
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
          {sb_item("Settings", "/settings", "settings")}
          {sb_item("Logs", "/status", "logs")}
          <div style="height:6px;"></div>
          {theme_btn_sidebar}
        </div>
      </div>
    """

    topbar = """
      <div class="pageHeader">
        <div class="ptIn">
          <div class="pageTopLogo">
            <img src="/images/logo-full.png" alt="MediaReaparr">
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
  {confirm_modal_html()}
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


@app.get("/images/<path:filename>")
def serve_images(filename: str):
    """
    Serve static images from:
      1) /config/images (user overrides)
      2) ./images (bundled defaults)
    """
    filename = (filename or "").strip()
    if not filename or ".." in filename.replace("\\", "/"):
        return "", 404

    def _cache_static(resp):
        # Cache for 1 hour; browsers will revalidate using ETag/Last-Modified.
        try:
            resp.cache_control.public = True
            resp.cache_control.max_age = 3600
            resp.cache_control.must_revalidate = True
        except (AttributeError, TypeError):
            resp.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
        return resp

    # Prefer user-provided overrides in /config/images
    if CONFIG_IMAGES_DIR.exists():
        p = (CONFIG_IMAGES_DIR / filename)
        if p.exists() and p.is_file():
            return _cache_static(send_from_directory(str(CONFIG_IMAGES_DIR), filename))

    # Fall back to bundled images
    if APP_IMAGES_DIR.exists():
        p = (APP_IMAGES_DIR / filename)
        if p.exists() and p.is_file():
            return _cache_static(send_from_directory(str(APP_IMAGES_DIR), filename))

    return "", 404


@app.get("/favicon.ico")
def favicon_redirect():
    # Browsers often request /favicon.ico automatically
    return redirect("/images/favicon.ico", code=302)


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

    # apps list + usage map (so we can reuse Apps UI inside Settings)
    apps_list = [normalize_app(a) for a in (cfg.get("APPS") or [])]
    total_apps = len(apps_list)
    connected_apps = sum(1 for a in apps_list if a.get("ok") and a.get("url") and a.get("api_key"))

    jobs = [normalize_job(j) for j in (cfg.get("JOBS") or [])]
    usage: Dict[str, int] = {}
    for j in jobs:
        aid = str(j.get("APP_ID") or "").strip()
        if not aid:
            continue
        usage[aid] = usage.get(aid, 0) + 1

    def app_card(a: Dict[str, Any]) -> str:
        a = normalize_app(a)
        kind = a.get("type", "radarr")
        title = a.get("name", "App")
        ok = bool(a.get("ok", False))
        url = str(a.get("url") or "")
        app_id = safe_html(a.get("id"))
        card_state = "is-connected" if ok else "is-disconnected"

        href = (url or "").strip()
        if href:
            ext = f"""<a class="appCardLinkBtn" href="{safe_html(href)}" target="_blank" rel="noreferrer" title="Open {safe_html(title)}">
              ↗
            </a>"""
        else:
            ext = """<div class="appCardLinkBtn" title="No URL set" style="opacity:.4; cursor:default;">↗</div>"""

        pill = '<span class="pill good" data-pill>Connected</span>' if ok else '<span class="pill bad" data-pill>Not Connected</span>'
        type_label = "Radarr" if kind == "radarr" else "Sonarr"
        icon_src = "/images/radarr_icon.svg" if kind == "radarr" else "/images/sonarr_icon.svg"

        return f"""
        <div class="appCard {card_state}" data-app-id="{app_id}" data-app-type="{safe_html(kind)}" role="button" tabindex="0"
             onclick="openEditApp('{app_id}')"
             onkeydown="if(event.key==='Enter'||event.key===' '){{
                event.preventDefault(); openEditApp('{app_id}');
             }}"
             title="Configure {safe_html(title)}">
          <div class="appCardTop">
            <div class="appCardLeft">
              <div class="appCardIcon" aria-hidden="true">
                <img src="{safe_html(icon_src)}" alt="">
              </div>
              <div style="min-width:0;">
                <div class="appTitle">{safe_html(title)}</div>
              </div>
            </div>
            {ext}
          </div>
          <div class="appCardBody">
            <div class="appMeta">{safe_html(type_label)} • {safe_html(url or 'No URL')}</div>
            <div>{pill}</div>
          </div>
        </div>
        """

    app_cards = "".join(app_card(a) for a in apps_list)

    add_card = """
      <div class="appCard addAppCard" id="addAppCard" role="button" tabindex="0" title="Add an app">
        <div style="text-align:center;">
          <div class="addAppCardInner">+</div>
          <div class="muted addAppCardLabel">Add App</div>
        </div>
      </div>
    """

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd">
            <div class="settingsTabs" role="tablist" aria-label="Settings tabs">
              <button class="settingsTab active" type="button" data-tab="general" role="tab" aria-selected="true">General</button>
              <button class="settingsTab" type="button" data-tab="apps" role="tab" aria-selected="false">Apps</button>
            </div>
            <div class="btnrow">
</div>
          </div>

          <div class="bd">
            <div class="settingsPanel active" id="settings_tab_general" role="tabpanel">
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
                  App connections are managed in <b>Settings → Apps</b>.
                </div>
              </form>
            </div>

            <div class="settingsPanel" id="settings_tab_apps" role="tabpanel">
              <div class="muted" style="margin-bottom:10px;">
                Connected apps: <b id="appsConnectedCount">{connected_apps}</b> / <b>{total_apps}</b>
              </div>

              <div class="appsGrid" style="margin-top:10px;">
                {add_card}
                {app_cards}
              </div>
            </div>
          </div>
        </div>
      </div>


      {app_modals_html(cfg, usage)}
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
          <button class="btn cancel" type="button" onclick="hideModal('appPickBack')">Cancel</button>
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
                <button class="btn cancel" type="button" onclick="maybeCloseAppModal('r')">Cancel</button>
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
                <button class="btn cancel" type="button" onclick="maybeCloseAppModal('s')">Cancel</button>
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
    # Apps are now managed under Settings → Apps
    return redirect("/settings?tab=apps")


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
    except ValueError:
        return {}


def detect_app_type_from_status(status_payload: Dict[str, Any]) -> Optional[str]:
    if not isinstance(status_payload, dict):
        return None
    candidates = [
        status_payload.get("appName"),
        status_payload.get("applicationName"),
        status_payload.get("name"),
        status_payload.get("instanceName"),
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

    # Basic required fields (mirror client-side validation)
    if not name:
        flash("App Name is required.", "error")
        return redirect("/apps")
    if not url:
        flash("App URL is required.", "error")
        return redirect("/apps")
    if not api_key:
        flash("API Key is required.", "error")
        return redirect("/apps")

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
        system_status = _system_status(url, api_key, int(cfg.get("HTTP_TIMEOUT_SECONDS", 30)))
        detected = detect_app_type_from_status(system_status) or app_type

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
    except (requests.exceptions.RequestException, OSError, ValueError) as e:
        msg = f"{kind} connection failed: {e}"

    if is_ajax:
        return {"ok": False, "message": msg, "detected_type": app_type}

    flash(msg, "error")
    return redirect("/apps")


@app.get("/apps/ping")
def apps_ping():
    """Lightweight connectivity check for Settings → Apps tab.

    Returns: {"status": {"<app_id>": true/false, ...}}
    """
    cfg = load_config()
    apps_list = cfg.get("APPS") or []
    status_by_app: Dict[str, bool] = {}

    # Keep this quick to avoid UI hangs
    timeout = clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 5), 1, 60, 5)

    for a in apps_list:
        a = normalize_app(a or {})
        app_id = str(a.get("id") or "")
        url = str(a.get("url") or "").strip().rstrip("/")
        api_key = str(a.get("api_key") or "").strip()
        if not app_id:
            continue
        if not url or not api_key:
            status_by_app[app_id] = False
            continue
        try:
            r = _system_status(url, api_key, timeout_s=timeout)
            # If we got a JSON back, the request succeeded; treat as connected
            status_by_app[app_id] = True if isinstance(r, dict) else True
        except (requests.exceptions.RequestException, PermissionError, ValueError):
            status_by_app[app_id] = False

    return jsonify({"status": status_by_app})


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

    enabled = checkbox(request.form, "enabled")

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
      <div class="modal" role="dialog" aria-modal="true" aria-labelledby="jobTitleText">
        <div class="mh">
          <div id="jobTitle" class="jobModalTitle">
            <img id="jobTitleIcon" class="jobModalIcon" src="/images/radarr_icon.svg" alt="" />
            <span id="jobTitleText">Add Job</span>
          </div>
          <button class="modalCloseX" type="button" onclick="maybeCloseJobModal()" aria-label="Close">×</button>
        </div>

        <form id="jobForm" method="post" action="/jobs/save" style="margin:0;">
          <div class="mb">
            <input type="hidden" name="job_id" id="job_id" value="">

            <div class="field" style="margin-bottom:12px;">
              <label>Job Name</label>
              <input type="text" name="name" id="job_name" value="New Job" required>
            <div class="fieldDesc">Friendly name shown on the Jobs page (e.g. <b>Weekly Cleanup</b>).</div></div>

            <div class="field" style="margin-bottom:12px;">
              <label id="job_app_label">App</label>
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
               <div class="fieldDesc">Choose which instance this job will run against. Only connected instances are listed.</div>
               <div class="muted" id="job_app_empty" style="margin-top:8px; display:none;">
                 <b id="job_app_empty_title">App not configured</b><br>
                 <span id="job_app_empty_msg">No instances configured.</span>
                 <div style="margin-top:8px;">
                   <a class="addInstanceBtn" href="/settings?tab=apps" onclick="hideModal('jobBack');">Add instance</a>
                 </div>
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
            <div class="fieldDesc">Only items with this tag will be considered for cleanup.</div></div>

            <div class="field" style="margin-bottom:12px;">
              <label>Days Old</label>
              <input type="number" min="1" name="DAYS_OLD" id="job_days" value="30" required>
            <div class="fieldDesc" id="job_days_desc">Items must be older than this many days.</div></div>

            <!-- Radarr-only: score filter (styled like existing fields/checks) -->
             <div class="field" id="radarrScoreField" style="display:none; margin-bottom:12px;">
               <label>Radarr score filter</label>
               <div class="scoreRow">
                 <label class="check scoreInline">
                   <input type="checkbox" id="job_score_enabled" name="RADARR_SCORE_FILTER_ENABLED">
                   <span><b>Delete if score is below</b></span>
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
               <div class="sectionDesc">Optional safety gate for Radarr jobs. When enabled, only movies scoring below your threshold are eligible.</div>
             </div>

            <div class="field" id="sonarrDeleteModeField" style="display:none; margin-bottom:12px;">
              <label>Removal Type</label>
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
               <div class="fieldDesc">Controls how Sonarr removes content when a match is found.</div></div>
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
            <div class="fieldDesc">When this job runs automatically.</div></div>

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
            <div class="fieldDesc">Hour of day (24h clock) for the scheduled run.</div></div>

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
            <div class="fieldDesc">Disabled jobs won’t run on schedule and can’t be run manually.</div></div>

            <div class="checks" style="margin-top:12px;">


              <label class="check">
                <input type="checkbox" id="job_excl" name="ADD_IMPORT_EXCLUSION">
                <div>
                  <div style="font-weight:700;">Add Import Exclusion</div>
                  <div class="muted">Prevents re-import.</div>
                </div>
              </label>

<label class="check">
                <input type="checkbox" id="job_dry" name="DRY_RUN" checked>
                <div>
                  <div style="font-weight:700;">Dry Run</div>
                  <div class="muted">Log only; no deletes.</div>
                </div>
              </label>

<div class="muted" style="margin:2px 12px 6px 12px;">
                <b>Real runs always delete files.</b> Keep <b>Dry Run</b> enabled to preview safely.
              </div>
</div>
          </div>

          <div class="mf">
            <button class="btn cancel" type="button" onclick="maybeCloseJobModal()">Cancel</button>
            <button class="btn primary" id="jobSaveBtn" type="submit">Save Job</button>
          </div>


</form>
      </div>
    </div>
    """
    sonarr_rows = []
    radarr_rows = []
    other_rows = []
    for j0 in cfg["JOBS"]:
        j = normalize_job(j0)
        a = find_app(cfg, j.get("APP_ID"))
        if a:
            app_kind = a.get("type", "radarr")
            app_label = f"{'Radarr' if app_kind == 'radarr' else 'Sonarr'} • {a.get('name', 'App')}"
        else:
            app_kind = "radarr"
            app_label = "Missing app"

        sched = schedule_label(j["SCHED_DAY"], j["SCHED_HOUR"])
        tag_val = j.get("TAG_LABEL") or "—"

        edit_btn = f"""
          <button class="btn jobEditBtn iconOnly"
                  type="button"
                  title="Edit job"
                  aria-label="Edit job"
                  data-action="job-edit"
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
                  data-excl="{'1' if j["ADD_IMPORT_EXCLUSION"] else '0'}">
            🔧
          </button>
        """

        # List view row (table layout)
        # Per app: Sonarr shows Removal Type; Radarr shows Score Below (when enabled)
        days_pill = f'<span class="jobPill {app_kind}">{int(j["DAYS_OLD"])}</span>'
        tag_pill = f'<span class="jobPill {app_kind}">{safe_html(tag_val)}</span>'
        sched_pill = f'<span class="jobPill {app_kind}">{safe_html(sched)}</span>'
        excl_pill = f'<span class="jobPill {app_kind}">{"YES" if j.get("ADD_IMPORT_EXCLUSION") else "NO"}</span>'

        removal_pill = '<span class="jobPill muted">—</span>'
        score_pill = '<span class="jobPill muted">—</span>'

        if a and a.get("type") == "sonarr":
            removal_pill = f'<span class="jobPill sonarr">{safe_html(sonarr_delete_mode_label(j.get("SONARR_DELETE_MODE")))}</span>'
        if a and a.get("type") == "radarr":
            if j.get("RADARR_SCORE_FILTER_ENABLED"):
                score_pill = f'<span class="jobPill radarr">{int(j.get("RADARR_MIN_AVG_SCORE", 60))}</span>'

        # Primary run button for list view (no Preview button here to keep it compact)
        jid2 = safe_html(j["id"])
        enabled2 = "true" if bool(j.get("enabled", True)) else "false"
        delete_files2 = "true" if bool(j.get("DELETE_FILES", True)) else "false"
        app_lbl2 = safe_html(app_label)

        if not j["enabled"]:
            run_btn_list = '<button class="btn" type="button" disabled title="Enable this job to run">Run Now</button>'
        elif j.get("DRY_RUN", True):
            run_btn_list = f'''
              <form method="post" action="/jobs/run-dry" style="margin:0;">
                <input type="hidden" name="job_id" value="{jid2}">
                <button class="btn good" type="submit" title="Dry Run — no changes will be made (writes to logs)">Dry Run</button>
              </form>
            '''
        else:
            run_btn_list = f'''
              <button class="btn bad" type="button"
                data-action="run-now"
                data-job-id="{jid2}"
                data-app-label="{app_lbl2}"
                data-dry-run="0"
                data-delete-files="{delete_files2}"
                data-enabled="{enabled2}">
                Run Now
              </button>
            '''

        row_icon_src = "/images/sonarr_icon.svg" if app_kind == "sonarr" else (
            "/images/radarr_icon.svg" if app_kind == "radarr" else "")
        row_icon_html = f'<img src="{row_icon_src}" alt="" />' if row_icon_src else ""
        job_name_cell = f'''
          <div class="jobNameCell">
            <span class="jobRowIcon" aria-hidden="true">{row_icon_html}</span>
            <span class="jobRowName">{safe_html(j["name"])}</span>
          </div>
        '''

        running2 = is_job_running(j["id"])
        delete_disabled = 'disabled="disabled"' if running2 else ""
        delete_title = "Job is running" if running2 else "Delete job"

        actions_cell = f'''
          <div class="jobRowActions">
            {edit_btn}
            {run_btn_list}
            <form method="post" action="/jobs/delete" style="margin:0;" class="jobDeleteForm"
                  title="{delete_title}" data-app-type="{safe_html(app_kind)}" data-job-name="{safe_html(j["name"])}">
              <input type="hidden" name="job_id" value="{jid2}">
              <button class="jobDeleteBtn" type="submit" aria-label="Delete job" {delete_disabled}>
                <span aria-hidden="true">🗑</span>
              </button>
            </form>
          </div>
        '''

        if app_kind == "sonarr":
            row_html = f'''
              <tr>
                <td>{job_name_cell}</td>
                <td>{tag_pill}</td>
                <td>{days_pill}</td>
                <td>{removal_pill}</td>
                <td>{excl_pill}</td>
                <td>{sched_pill}</td>
                <td style="text-align:right;">{actions_cell}</td>
              </tr>
            '''
            sonarr_rows.append(row_html)
        elif app_kind == "radarr":
            row_html = f'''
              <tr>
                <td>{job_name_cell}</td>
                <td>{tag_pill}</td>
                <td>{days_pill}</td>
                <td>{score_pill}</td>
                <td>{excl_pill}</td>
                <td>{sched_pill}</td>
                <td style="text-align:right;">{actions_cell}</td>
              </tr>
            '''
            radarr_rows.append(row_html)
        else:
            row_html = f'''
              <tr>
                <td>{job_name_cell}</td>
                <td>{tag_pill}</td>
                <td>{days_pill}</td>
                <td><span class="jobPill muted">—</span></td>
                <td>{excl_pill}</td>
                <td>{sched_pill}</td>
                <td style="text-align:right;">{actions_cell}</td>
              </tr>
            '''
            other_rows.append(row_html)

    can_add_job = len(ready_apps) > 0

    hint_html = ""
    if not can_add_job:
        hint_html = """
          <div class="muted" style="margin-top:12px;">
            Add Job is disabled because no connected apps exist.
            Go to <a href="/settings?tab=apps"><b>Apps</b></a>, add an app, run <b>Test</b>, then <b>Save</b>.
          </div>
        """

    other_section_html = ""

    # Always render the "Add Job" UI, even when no jobs exist (empty state)
    add_job_action = 'data-action="job-add-card"' if can_add_job else ''
    add_job_tab = 'tabindex="0"' if can_add_job else 'tabindex="-1" aria-disabled="true"'
    add_job_extra_cls = '' if can_add_job else ' disabled'

    sonarr_empty_row = ''
    if not sonarr_rows:
        sonarr_empty_row = '<tr><td colspan="7" class="muted jobsEmptyCell">No Sonarr jobs yet.</td></tr>'

    sonarr_section_html = f'''
      <div class="jobsSection">
        <div class="jobsSectionHeader"><div class="title">Sonarr Jobs</div><div class="rule"></div></div>
        <div class="jobsViewList jobsView">
          <div class="addJobBar{add_job_extra_cls}" role="button" {add_job_tab}
               {add_job_action} data-app-type="sonarr"
               title="Add Sonarr job">
            <span class="plus">+</span><span class="label">Add Sonarr Job</span>
          </div>
          <div class="jobsListWrap">
            <table class="jobsTable">
              <thead>
                <tr>
                  <th>Job Name</th>
                  <th>Tag</th>
                  <th>If Older Then</th>
                  <th>Removal Type</th>
                  <th>Import Exclusion</th>
                  <th>Schedule</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {sonarr_empty_row}{''.join(sonarr_rows)}
              </tbody>
            </table>
          </div>
        </div>

      </div>
    '''

    radarr_empty_row = ''
    if not radarr_rows:
        radarr_empty_row = '<tr><td colspan="7" class="muted jobsEmptyCell">No Radarr jobs yet.</td></tr>'

    radarr_section_html = f'''
      <div class="jobsSection">
        <div class="jobsSectionHeader"><div class="title">Radarr Jobs</div><div class="rule"></div></div>

        <div class="jobsViewList jobsView">
          <div class="addJobBar{add_job_extra_cls}" role="button" {add_job_tab}
               {add_job_action} data-app-type="radarr"
               title="Add Radarr job">
            <span class="plus">+</span><span class="label">Add Radarr Job</span>
          </div>
          <div class="jobsListWrap">
            <table class="jobsTable">
              <thead>
                <tr>
                  <th>Job Name</th>
                  <th>Tag</th>
                  <th>If Older Then</th>
                  <th>If Score Below</th>
                  <th>Import Exclusion</th>
                  <th>Schedule</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {radarr_empty_row}{''.join(radarr_rows)}
              </tbody>
            </table>
          </div>
        </div>

      </div>
    '''

    body = f"""
      {tags_js}

      <div class="grid">
        <div class="card">
          <div class="hd">
            <h2>Jobs</h2>
            <div class="btnrow">            </div>
          </div>

          <div class="bd">
            <div class="jobsSections">
              {sonarr_section_html}{radarr_section_html}{other_section_html}
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
        score_enabled = checkbox(request.form, "RADARR_SCORE_FILTER_ENABLED")
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
            "DRY_RUN": checkbox(request.form, "DRY_RUN"),
            "DELETE_FILES": True,
            "ADD_IMPORT_EXCLUSION": checkbox(request.form, "ADD_IMPORT_EXCLUSION"),
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

    except (ValueError, TypeError, OSError) as e:
        flash(str(e), "error")
        return redirect("/jobs")


@app.post("/jobs/delete")
def jobs_delete():
    job_id = (request.form.get("job_id") or "").strip()

    if is_job_running(job_id):
        flash("Job is currently running and cannot be deleted.", "error")
        return redirect("/jobs")

    cfg = load_config()
    jobs = [j for j in (cfg.get("JOBS") or []) if str(j.get("id")) != job_id]

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

    # Run app.py immediately in the background and append its output to the shared log file.
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Best-effort: find app.py alongside webui.py; fall back to /app/app.py
    app_py = (Path(__file__).resolve().parent / "app.py")
    if not app_py.exists():
        app_py = Path("/app/app.py")

    cmd = [sys.executable, str(app_py), "--job-id", job_id]
    append_log_line(f"Run Now: launching {cmd!r}")

    try:
        mark_job_running(job_id, True)
        with log_path.open("a", encoding="utf-8") as lf:
            p = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=lf,
                cwd=str(app_py.parent),
                env={**os.environ, "CONFIG_DIR": str(CONFIG_DIR), "LOG_PATH": str(log_path)},
                start_new_session=True,
            )

        def _wait_and_clear():
            try:
                p.wait()
            finally:
                mark_job_running(job_id, False)

        threading.Thread(target=_wait_and_clear, daemon=True).start()

        flash("Run Now started ✔ (watch Status → Logs)", "success")
    except (OSError, ValueError) as e:
        append_log_line(f"Run Now: failed to launch job_id={job_id}: {e}")
        mark_job_running(job_id, False)
        flash(f"Failed to start job: {e}", "error")

    return redirect("/dashboard")


@app.post("/jobs/run-dry")
def jobs_run_dry():
    cfg = load_config()
    job_id = (request.form.get("job_id") or "").strip()
    if not job_id:
        flash("Missing job id.", "error")
        return redirect("/jobs")

    job = find_job(cfg, job_id)
    if not job:
        flash("Job not found.", "error")
        return redirect("/jobs")

    jobn = normalize_job(job)
    if not jobn.get("enabled", False):
        flash("This job is disabled. Enable it before running.", "error")
        return redirect("/jobs")

    # If using WebUI apps, enforce readiness (consistent with Run Now)
    app_id = str(jobn.get("APP_ID") or "").strip()
    if app_id and not is_app_ready(cfg, app_id):
        flash("This job's app is missing or not connected. Fix it in Apps.", "error")
        return redirect("/apps")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    app_py = (Path(__file__).resolve().parent / "app.py")
    if not app_py.exists():
        app_py = Path("/app/app.py")

    cmd = [sys.executable, str(app_py), "--job-id", str(job_id)]
    append_log_line(f"Dry Run: launching {cmd!r}")

    try:
        mark_job_running(job_id, True)
        with log_path.open("a", encoding="utf-8") as lf:
            p = subprocess.Popen(
                cmd,
                stdout=lf,
                stderr=lf,
                cwd=str(app_py.parent),
                env={**os.environ, "CONFIG_DIR": str(CONFIG_DIR), "LOG_PATH": str(log_path), "FORCE_DRY_RUN": "1"},
                start_new_session=True,
            )

        def _wait_and_clear():
            try:
                p.wait()
            finally:
                mark_job_running(job_id, False)

        threading.Thread(target=_wait_and_clear, daemon=True).start()

        flash("Dry Run started ✔ (watch Status → Logs)", "success")
    except (OSError, ValueError) as e:
        append_log_line(f"Dry Run: failed to launch job_id={job_id}: {e}")
        mark_job_running(job_id, False)
        flash(f"Failed to start dry run: {e}", "error")

    return redirect("/dashboard")


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
                    <td>{safe_html(str(c.get("age_days", "")))}</td>
                    <td>{safe_html(c.get("title", ""))}</td>
                    <td>{safe_html(str(c.get("year", "")))}</td>
                    <td><code>{safe_html(c.get("added", ""))}</code></td>
                    <td>{safe_html(str(c.get("id", "")))}</td>
                    <td class="muted">{safe_html(c.get("path", "") or "")}</td>
                  </tr>
                """
            else:
                score = c.get("score")
                score_txt = safe_html(str(score)) if score is not None else "—"
                rows += f"""
                  <tr>
                    <td>{safe_html(str(c.get("age_days", "")))}</td>
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

    except (requests.exceptions.RequestException, ValueError, TypeError) as e:
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
        body = f"""
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
# Status

@app.get("/status/log")
def status_log():
    cfg = load_config()
    lines = clamp_int(request.args.get("lines", 500), 10, 5000, 500)
    date_key = (request.args.get("date") or "").strip()
    log_path = get_log_path(cfg, date_key=date_key)
    txt = tail_file(log_path, max_lines=lines)
    return Response(txt, mimetype="text/plain; charset=utf-8")


@app.get("/status/log/dates")
def status_log_dates():
    cfg = load_config()
    dates = list_log_dates(cfg)
    selected = (request.args.get("selected") or "").strip()
    if not _is_valid_dd_mm_yyyy(selected):
        selected = _today_dd_mm_yyyy()
    if selected and selected not in dates:
        dates = [selected] + dates
    return jsonify({
        "ok": True,
        "dates": dates,
        "selected": selected,
        "log_dir": str(get_log_path(cfg, date_key=selected).parent),
        "log_path": str(get_log_path(cfg, date_key=selected)),
    })


# ----------------------------
@app.get("/status")
def status():
    cfg = load_config()

    cfg_for_view = dict(cfg)
    cfg_for_view["APPS"] = []
    for a in (cfg.get("APPS") or []):
        aa = normalize_app(a)
        aa["api_key"] = "***" if aa.get("api_key") else ""
        cfg_for_view["APPS"].append(aa)

    body = f"""
      <div class="grid">
        <div class="card">
          <div class="hd"><h2>Logs</h2></div>
          <div class="bd">

            <div class="logTableWrap">
              <div class="logTableHeader">
                <div class="left">
                  <div><b>Log Viewer</b> <span class="muted">(from file)</span></div>
                  <div class="muted">Path: <code id="logPathCode">{safe_html(str(get_log_path(cfg)))}</code></div>
                </div>

                <div class="right">
                  <select id="logDateSelect" class="logFilterSelect" style="min-width:160px" onchange="onLogDateChange()">
                    <option value="">Loading…</option>
                  </select>

                  <select id="logLevelFilter" class="logFilterSelect" onchange="applyLogFilters()">
                    <option value="">All severities</option>
                    <option value="DEBUG">Debug</option>
                    <option value="INFO" selected>Info</option>
                    <option value="WARNING">Warning</option>
                    <option value="ERROR">Error</option>
                  </select>

                  <select id="logPageSize" class="logFilterSelect" style="min-width:140px" onchange="applyLogFilters(true)">
                    <option value="10">Display 10</option>
                    <option value="25">Display 25</option>
                    <option value="50">Display 50</option>
                    <option value="100">Display 100</option>
                  </select>

                  <button class="btn" type="button" onclick="reloadLogs()">Refresh</button>
                  <label class="checkRow muted" title="Auto-refresh every 3s">
                    <input type="checkbox" id="logAuto" onchange="toggleLogAuto()"> Auto
                  </label>
                </div>
              </div>

              <div style="overflow:auto; max-height: 560px;">
                <table class="logTable">
                  <thead>
                    <tr>
                      <th style="width: 250px;">Timestamp</th>
                      <th style="width: 120px;">Severity</th>
                      <th style="width: 220px;">Label</th>
                      <th>Message</th>
                      <th style="width: 92px;"></th>
                    </tr>
                  </thead>
                  <tbody id="logTbody">
                    <tr><td colspan="5" class="muted">Loading…</td></tr>
                  </tbody>
                </table>
              </div>

              <div class="logPager">
                <div class="muted" id="logPagerMeta">—</div>
                <div class="pagerBtns">
                  <button class="btnTiny" id="logPrevBtn" onclick="pagePrev()" disabled>Previous</button>
                  <button class="btnTiny" id="logNextBtn" onclick="pageNext()" disabled>Next</button>
                </div>
              </div>
            </div>

            <script>
              let __logTimer = null;
              let __logAll = [];
              let __logFiltered = [];
              let __logPage = 1;

              function esc(s){{
                return (s ?? "").toString()
                  .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
                  .replaceAll('"',"&quot;").replaceAll("'","&#39;");
              }}

              function pillClass(sev){{
                const s = (sev||"").toUpperCase();
                if (s === "INFO") return "info";
                if (s === "WARNING" || s === "WARN") return "warning";
                if (s === "ERROR") return "error";
                return "debug";
              }}

              function parseLine(line){{
                // Regex-free parser that supports:
                // 1) 2026-01-30 21:10:35,123 [INFO] [webui] message
                // 2) 2026-01-30 21:10:35 [INFO] [webui] message
                // 3) 2026-01-30 21:10:35 [webui] [INFO] message
                // 4) 2026-01-30 21:10:35 INFO webui: message
                const s0 = (line || "").toString();
                const s = s0.trim();
                if (!s) return null;

                function isDigit(ch) {{
                  return ch >= "0" && ch <= "9";
                }}

                // Parse leading timestamp.
                // Supported:
                // - Mon DD, YYYY, H:MM:SS AM/PM   (canonical, e.g. "Feb 01, 2026, 7:26:12 PM")
                // - YYYY-MM-DD HH:MM:SS(,mmm)
                // - DD-MM-YYYY HH:MM(:SS)?(,mmm)?
                let ts = "";
                let pos = 0;

                // Mon DD, YYYY, H:MM:SS AM/PM
                // (We keep this regex-free by slicing up to the first " [" token.)
                const bracketAt = s.indexOf(" [");
                if (bracketAt > 0) {{
                  const cand = s.substring(0, bracketAt).trim();
                  const mon = cand.length >= 3 ? cand.substring(0, 3) : "";
                  const months = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
                  if (months.indexOf(mon) !== -1) {{
                    // Basic shape check: "Mon DD, YYYY, ..."
                    // e.g. "Feb 01, 2026, 7:26:12 PM"
                    const c6 = cand.length > 6 ? cand[6] : "";
                    const c12 = cand.length > 12 ? cand[12] : "";
                    if (c6 === "," && c12 === ",") {{
                      ts = cand;
                      pos = bracketAt;
                    }}
                  }}
                }}

                // YYYY-MM-DD HH:MM:SS
                if (!ts && s.length >= 19 && s[4] === "-" && s[7] === "-" && (s[10] === " " || s[10] === "T") && s[13] === ":" && s[16] === ":") {{
                  let tsLen = 19;
                  if (s.length >= 23 && (s[19] === "," || s[19] === ".") && isDigit(s[20]) && isDigit(s[21]) && isDigit(s[22])) {{
                    tsLen = 23;
                  }}
                  ts = s.substring(0, tsLen);
                  pos = tsLen;
                }}
                // DD-MM-YYYY HH:MM or DD-MM-YYYY HH:MM:SS
                else if (!ts && s.length >= 16 && isDigit(s[0]) && isDigit(s[1]) && s[2] === "-" && isDigit(s[3]) && isDigit(s[4]) && s[5] === "-" && isDigit(s[6]) && isDigit(s[7]) && isDigit(s[8]) && isDigit(s[9]) && s[10] === " " && isDigit(s[11]) && isDigit(s[12]) && s[13] === ":" && isDigit(s[14]) && isDigit(s[15])) {{
                  let tsLen = 16; // DD-MM-YYYY HH:MM
                  // optional :SS
                  if (s.length >= 19 && s[16] === ":" && isDigit(s[17]) && isDigit(s[18])) {{
                    tsLen = 19;
                  }}
                  // optional ,mmm
                  if (s.length >= tsLen + 4 && (s[tsLen] === "," || s[tsLen] === ".") && isDigit(s[tsLen+1]) && isDigit(s[tsLen+2]) && isDigit(s[tsLen+3])) {{
                    tsLen = tsLen + 4;
                  }}
                  ts = s.substring(0, tsLen);
                  pos = tsLen;
                }}

                let rest = (pos ? s.substring(pos) : s).trim();

                // Collect bracket tokens at start: [something] [something] ...
                const tokens = [];
                while (rest.startsWith("[")) {{
                  const end = rest.indexOf("]");
                  if (end <= 1) break;
                  tokens.push(rest.substring(1, end));
                  rest = rest.substring(end + 1).trim();
                  if (tokens.length >= 3) break;
                }}

                // Determine severity + label from tokens or from leading words
                let sev = "";
                let label = "";

                function normSev(x) {{
                  const u = (x || "").toUpperCase();
                  if (u === "WARN") return "WARNING";
                  return u;
                }}
                function isSev(x) {{
                  const u = normSev(x);
                  return u === "DEBUG" || u === "INFO" || u === "WARNING" || u === "ERROR";
                }}

                if (tokens.length >= 1 && isSev(tokens[0])) {{
                  sev = normSev(tokens[0]);
                  if (tokens.length >= 2) label = tokens[1];
                }} else if (tokens.length >= 2 && isSev(tokens[1])) {{
                  // [label] [SEV]
                  label = tokens[0];
                  sev = normSev(tokens[1]);
                }} else {{
                  // No bracketed severity; try first word
                  const sp = rest.indexOf(" ");
                  const first = (sp === -1 ? rest : rest.substring(0, sp));
                  if (isSev(first)) {{
                    sev = normSev(first);
                    rest = (sp === -1) ? "" : rest.substring(sp + 1).trim();
                    // Optional label as next word before colon
                    const sp2 = rest.indexOf(" ");
                    const cand = (sp2 === -1 ? rest : rest.substring(0, sp2));
                    if (cand && cand.length <= 48) {{
                      // treat as label if followed by ":" later or if it's a simple tag
                      const colon = rest.indexOf(":");
                      if (colon > 0 && colon < 80) {{
                        label = rest.substring(0, colon).trim();
                        rest = rest.substring(colon + 1).trim();
                      }} else if (cand && cand.indexOf(":") === -1) {{
                        // if next token looks like a label and message continues
                        label = cand;
                        rest = (sp2 === -1) ? "" : rest.substring(sp2 + 1).trim();
                      }}
                    }}
                  }}
                }}

                if (!sev) {{
                  // Best-effort severity scan without regex
                  const u = s.toUpperCase();
                  if (u.includes("ERROR")) sev = "ERROR";
                  else if (u.includes("WARNING") || u.includes("WARN")) sev = "WARNING";
                  else if (u.includes("DEBUG")) sev = "DEBUG";
                  else sev = "INFO";
                }}

                // If label still empty, try "label: message" pattern at start of rest
                if (!label) {{
                  const idx = rest.indexOf(":");
                  if (idx > 0 && idx < 64) {{
                    label = rest.slice(0, idx).trim();
                    rest = rest.slice(idx + 1).trim();
                  }}
                }}

                return {{
                  ts: ts || "—",
                  sev: sev,
                  label: label || "—",
                  msg: rest || "",
                  raw: s
                }};
              }}

              function copyText(txt){{
                try {{
                  navigator.clipboard.writeText(txt || "");
                }} catch(e) {{
                  // fallback
                  const ta = document.createElement("textarea");
                  ta.value = txt || "";
                  document.body.appendChild(ta);
                  ta.select();
                  document.execCommand("copy");
                  ta.remove();
                }}
              }}

              function renderPage(){{
                const tbody = document.getElementById("logTbody");
                const pageSize = parseInt(document.getElementById("logPageSize").value || "10", 10);
                const total = __logFiltered.length;
                const pages = Math.max(1, Math.ceil(total / pageSize));
                if (__logPage > pages) __logPage = pages;
                if (__logPage < 1) __logPage = 1;

                const start = ( __logPage - 1 ) * pageSize;
                const end = Math.min(total, start + pageSize);
                const slice = __logFiltered.slice(start, end);

                if (!slice.length) {{
                  tbody.innerHTML = `<tr><td colspan="5" class="muted">No log entries match the filter.</td></tr>`;
                }} else {{
                  tbody.innerHTML = slice.map((e, idx) => {{
                    const sev = (e.sev || "DEBUG").toUpperCase();
                    const cls = pillClass(sev);
                    const label = e.label || "—";
                    const ts = e.ts || "—";
                    const msg = e.msg || "";
                    const raw = e.raw || msg;
                    return `
                      <tr>
                        <td>${{esc(ts)}}</td>
                        <td><span class="logPill ${{cls}}">${{esc(sev)}}</span></td>
                        <td class="logLabel">${{esc(label)}}</td>
                        <td class="logMsg">${{esc(msg)}}</td>
                        <td>
                          <div class="logActions">
                            <button class="iconBtn" title="Copy message" onclick="copyText(${{JSON.stringify(msg)}})">
                              ⧉
                            </button>
                            <button class="iconBtn" title="Copy full line" onclick="copyText(${{JSON.stringify(raw)}})">
                              ⎘
                            </button>
                          </div>
                        </td>
                      </tr>`;
                  }}).join("");
                }}

                document.getElementById("logPagerMeta").textContent =
                  `Showing ${{total ? (start+1) : 0}} to ${{end}} of ${{total}} results`;

                const prev = document.getElementById("logPrevBtn");
                const next = document.getElementById("logNextBtn");
                prev.disabled = (__logPage <= 1);
                next.disabled = (__logPage >= pages);
              }}

              function applyLogFilters(resetPage){{
                const sev = (document.getElementById("logLevelFilter").value || "").toUpperCase();
                __logFiltered = __logAll.filter(e => {{
                  if (!sev) return true;
                  const s = (e.sev || "").toUpperCase();
                  return s === sev || (sev==="WARNING" && s==="WARN");
                }});
                if (resetPage) __logPage = 1;
                renderPage();
              }}

              function pagePrev(){{ __logPage -= 1; renderPage(); }}
              function pageNext(){{ __logPage += 1; renderPage(); }}


              function loadLogDates(){{
                const sel = document.getElementById("logDateSelect");
                if (!sel) return Promise.resolve();
                const current = (sel.value || "");
                return fetch("/status/log/dates?selected=" + encodeURIComponent(current || ""), {{cache:"no-store"}})
                  .then(r => r.json())
                  .then(j => {{
                    if (!j || !j.ok) return;
                    const dates = Array.isArray(j.dates) ? j.dates : [];
                    const selected = j.selected || "";
                    sel.innerHTML = "";
                    for (const d of dates) {{
                      const opt = document.createElement("option");
                      opt.value = d;
                      opt.textContent = d;
                      sel.appendChild(opt);
                    }}
                    if (selected) sel.value = selected;
                    const p = document.getElementById("logPathCode");
                    if (p && j.log_path) p.textContent = j.log_path;
                  }})
                  .catch(() => {{}});
              }}

              function onLogDateChange(){{
                __logPage = 1;
                reloadLogs();
              }}

function reloadLogs(){{
                // Pull a large tail by default (keeps things fast even for big files)
                const lines = 5000;
                const dk = (document.getElementById("logDateSelect") && document.getElementById("logDateSelect").value) || "";
                const url = "/status/log?lines=" + lines + (dk ? ("&date=" + encodeURIComponent(dk)) : "");
                fetch(url, {{cache:"no-store"}})
                  .then(r => r.text())
                  .then(t => {{
                    const linesArr = (t || "").split(String.fromCharCode(10)).map(x => x.replace(String.fromCharCode(13), "")).filter(Boolean);
                    __logAll = linesArr.map(parseLine).reverse(); // newest first like your screenshot
                    applyLogFilters(true);
                  }})
                  .catch(err => {{
                    const tbody = document.getElementById("logTbody");
                    tbody.innerHTML = `<tr><td colspan="5" class="muted">Failed to load logs: ${{esc(err && err.message ? err.message : err)}}</td></tr>`;
                  }});
              }}

              function toggleLogAuto(){{
                const on = document.getElementById("logAuto").checked;
                if (on) {{
                  if (__logTimer) clearInterval(__logTimer);
                  __logTimer = setInterval(() => reloadLogs(), 3000);
                }} else {{
                  if (__logTimer) clearInterval(__logTimer);
                  __logTimer = null;
                }}
              }}

              // initial load
              (function initLogs(){{
                // set default page size to 10 (matches screenshot)
                document.getElementById("logPageSize").value = "10";
                loadLogDates().then(() => reloadLogs());
              }})();
            </script>

          </div>
        </div>
      </div>
    """
    return render_template_string(shell("mediareaparr • Logs", "logs", body))


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEBUI_PORT", "7575")))
    args = parser.parse_args()
    start_internal_scheduler()

    app.run(host=args.host, port=args.port)

# ----------------------------
# Log line parsing (canonical format)
# ----------------------------
_CANONICAL_LOG_RE = re.compile(
    r'^(?P<ts>[A-Z][a-z]{2} \d{2}, \d{4}, \d{2}:\d{2}:\d{2} (?:AM|PM)) '
    r'\[(?P<sev>[^]]+)] '
    r'\[(?P<label>[^]]+)] '
    r'(?P<msg>.*)$'
)


def parse_log_line(line: str):
    s = line.rstrip("\n")
    m = _CANONICAL_LOG_RE.match(s)
    if m:
        return {
            "timestamp": m.group("ts"),
            "severity": m.group("sev").title(),
            "label": m.group("label"),
            "message": m.group("msg"),
        }
    # fallback: return whole line as message
    return {
        "timestamp": "",
        "severity": "Info",
        "label": "App",
        "message": s,
    }
