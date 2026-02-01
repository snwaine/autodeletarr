#!/usr/bin/env python3
"""
MediaReaparr - app.py
Runs cleanup jobs against Radarr/Sonarr based on:
- tag label
- "added" older-than cutoff
- optional Radarr score gate (avg score < threshold)
- delete files + import exclusion options
- Sonarr delete modes

Supports NEW WebUI schema:
- cfg["APPS"] = [{id,type,url,api_key,ok,...}]
- cfg["JOBS"] = [{..., APP_ID: "<app-id>", ...}]

Still supports LEGACY env/global schema:
- RADARR_URL / RADARR_API_KEY / SONARR_URL / SONARR_API_KEY
- jobs with "APP": "radarr"|"sonarr"
"""

import os
import sys
import json
import argparse
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import logging
import threading
import time

# ----------------------------
# Paths
# ----------------------------
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

# ----------------------------
# Logging
# ----------------------------
LOG_DIR = Path(os.environ.get("LOG_DIR", str(CONFIG_DIR / "logs")))
def _dated_log_name(dt: "datetime") -> str:
    # Filename format required: "dd:mm:yyyy mediareaparr.log"
    return dt.strftime("%d-%m-%Y") + " mediareaparr.log"
def current_log_path() -> Path:
    return LOG_DIR / _dated_log_name(datetime.now(timezone.utc))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper().strip()

_LOGGER: Optional[logging.Logger] = None



class TitleCaseFormatter(logging.Formatter):
    def format(self, record):
        try:
            record.levelname = str(record.levelname).title()
        except Exception:
            pass
        if not hasattr(record, "label"):
            record.label = "App"
        return super().format(record)

class DailyDatedFileHandler(logging.Handler):
    """Writes logs to a date-stamped file and rolls over at midnight (UTC)."""
    def __init__(self, log_dir: Path, encoding: str = "utf-8"):
        super().__init__()
        self.log_dir = Path(log_dir)
        self.encoding = encoding
        self._lock = threading.RLock()
        self._date = None
        self._fp = None
        self._open_for(datetime.now(timezone.utc))

    def _path_for(self, dt: "datetime") -> Path:
        return self.log_dir / _dated_log_name(dt)

    def _open_for(self, dt: "datetime"):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        d = dt.strftime("%Y-%m-%d")
        if self._fp:
            try:
                self._fp.close()
            except Exception:
                pass
        self._date = d
        p = self._path_for(dt)
        self._fp = open(p, "a", encoding=self.encoding)

    def rollover_if_needed(self):
        now = datetime.now(timezone.utc)
        d = now.strftime("%Y-%m-%d")
        if d != self._date:
            self._open_for(now)

    def rollover_now(self):
        self._open_for(datetime.now(timezone.utc))

    def emit(self, record):
        try:
            msg = self.format(record)
            with self._lock:
                self.rollover_if_needed()
                self._fp.write(msg + "\n")
                self._fp.flush()
        except Exception:
            self.handleError(record)

def _start_midnight_rollover_thread(handler: DailyDatedFileHandler):
    def _worker():
        while True:
            now = datetime.now(timezone.utc)
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_s = max(1.0, (nxt - now).total_seconds())
            time.sleep(sleep_s)
            try:
                handler.rollover_now()
                p = current_log_path()
                p.parent.mkdir(parents=True, exist_ok=True)
                p.touch(exist_ok=True)
            except Exception:
                pass
    t = threading.Thread(target=_worker, name="mediareaparr-log-rotate", daemon=True)
    t.start()


def setup_logging() -> logging.Logger:
    """Configure rotating file + console logging (singleton)."""
    global _LOGGER
    if _LOGGER is not None:
        try:
            _LOGGER.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
        except Exception:
            _LOGGER.setLevel(logging.INFO)
        return _LOGGER

    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    logger = logging.getLogger("mediareaparr.app")
    logger.propagate = False

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)

    fmt = TitleCaseFormatter("%(asctime)s [%(levelname)s] [%(label)s] [%(message)s]", datefmt="%b %d, %Y, %I:%M:%S %p")

    if not any(isinstance(h, DailyDatedFileHandler) for h in logger.handlers):
        dh = DailyDatedFileHandler(LOG_DIR)
        dh.setFormatter(fmt)
        logger.addHandler(dh)
        _start_midnight_rollover_thread(dh)
    # Console logging disabled (WebUI captures job output and would duplicate lines)

    _LOGGER = logger
    return logger


def log_event(level: str, message: str, label: str = "App", exc: Optional[BaseException] = None, **fields: Any) -> None:
    logger = setup_logging()
    suffix = ""
    if fields:
        parts = []
        for k in sorted(fields.keys()):
            v = fields.get(k)
            if v is None:
                continue
            parts.append(f"{k}={v}")
        if parts:
            suffix = " | " + " ".join(parts)
    msg = f"{message}{suffix}"
    lvl = level.upper().strip()
    fn = {
        "DEBUG": logger.debug,
        "INFO": logger.info,
        "WARNING": logger.warning,
        "WARN": logger.warning,
        "ERROR": logger.error,
        "CRITICAL": logger.critical,
    }.get(lvl, logger.info)
    if exc is not None:
        fn(msg, exc_info=exc)
    else:
        fn(msg)


def log_debug(message: str, label: str = "App", **fields: Any) -> None:
    log_event("DEBUG", message, label=label, **fields)


def log_info(message: str, label: str = "App", **fields: Any) -> None:
    log_event("INFO", message, label=label, **fields)


def log_warning(message: str, label: str = "App", **fields: Any) -> None:
    log_event("WARNING", message, label=label, **fields)


def log_error(message: str, label: str = "App", exc: Optional[BaseException] = None, **fields: Any) -> None:
    log_event("ERROR", message, label=label, exc=exc, **fields)


# ----------------------------
# Cleaning summary helpers (Radarr / Sonarr style)
# ----------------------------

def _truncate(items, max_items=5):
    if len(items) <= max_items:
        return items, 0
    return items[:max_items], len(items) - max_items


def log_cleaning_summary(
    *,
    job_name: str,
    app_type: str,
    dry_run: bool,
    radarr_items: list,
    sonarr_items: list,
    max_items: int = 5,
):
    """Write ONE summary line per run using the canonical log format.

    Produces a single line like:
      Feb 01, 2026, 7:26:12 PM [Info] [Radarr Cleaning] job run finished | deleted_count=1 errors=0 ...
    """
    try:
        label = ("Radarr" if app_type == "radarr" else "Sonarr") + " Cleaning"
        if dry_run:
            label += " Dry Run"

        deleted_count = len(radarr_items or []) + len(sonarr_items or [])
        errors = 0

        log_info(
            f"job run finished | job_name={job_name} dry_run={dry_run} deleted_count={deleted_count} errors={errors}",
            label=label,
        )
    except Exception:
        pass

# ----------------------------
# Config/state IO
# ----------------------------
def load_config() -> Dict[str, Any]:
    cfg: Dict[str, Any] = {
        "APPS": [],
        "JOBS": [],
        "HTTP_TIMEOUT_SECONDS": int(os.environ.get("HTTP_TIMEOUT_SECONDS", "30")),
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    # normalize lists
    if not isinstance(cfg.get("APPS"), list):
        cfg["APPS"] = []
    if not isinstance(cfg.get("JOBS"), list):
        cfg["JOBS"] = []
    return cfg


def load_state() -> Dict[str, Any]:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
        except Exception:
            pass
    return {}


def save_state(state: Dict[str, Any]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def record_run(state: Dict[str, Any], job_id: str, run_state: Dict[str, Any]) -> None:
    """
    Stores:
      state["last_run"] = latest run overall
      state["last_runs"][job_id] = latest run for that job
    """
    if not isinstance(state, dict):
        return
    state["last_run"] = run_state
    if "last_runs" not in state or not isinstance(state.get("last_runs"), dict):
        state["last_runs"] = {}
    state["last_runs"][job_id] = run_state


def _persist_run(state: Dict[str, Any], job_id: str, run_state: Dict[str, Any]) -> None:
    record_run(state, job_id, run_state)
    save_state(state)


# ----------------------------
# Jobs schema
# ----------------------------
SONARR_DELETE_MODES = ("episodes_only", "episodes_then_series_if_empty", "series_whole")


def normalize_job(job: Dict[str, Any]) -> Dict[str, Any]:
    """
    WebUI v2 schema:
      id, name, enabled,
      APP_ID,
      TAG_LABEL, DAYS_OLD,
      SCHED_DAY, SCHED_HOUR,
      DRY_RUN, DELETE_FILES, ADD_IMPORT_EXCLUSION,
      SONARR_DELETE_MODE,
      RADARR_SCORE_FILTER_ENABLED, RADARR_MIN_AVG_SCORE
    """
    j = dict(job or {})

    j["id"] = str(j.get("id") or "").strip()
    j["name"] = str(j.get("name") or "Job").strip()
    j["enabled"] = bool(j.get("enabled", True))

    # WebUI v2 uses APP_ID + cfg["APPS"]; keep legacy "APP" support.
    j["APP_ID"] = str(j.get("APP_ID") or "").strip()
    j["APP"] = str(j.get("APP") or "").strip().lower()
    if j["APP"] and j["APP"] not in ("radarr", "sonarr"):
        j["APP"] = ""

    j["TAG_LABEL"] = str(j.get("TAG_LABEL") or "autodelete30").strip()
    j["DAYS_OLD"] = clamp_int(j.get("DAYS_OLD", 30), 1, 36500, 30)

    j["SCHED_DAY"] = str(j.get("SCHED_DAY") or "daily").lower()
    j["SCHED_HOUR"] = clamp_int(j.get("SCHED_HOUR", 3), 0, 23, 3)

    j["DRY_RUN"] = normalize_bool(j.get("DRY_RUN", True), True)
    j["DELETE_FILES"] = normalize_bool(j.get("DELETE_FILES", True), True)
    j["ADD_IMPORT_EXCLUSION"] = normalize_bool(j.get("ADD_IMPORT_EXCLUSION", False), False)

    mode = str(j.get("SONARR_DELETE_MODE") or "episodes_only").strip().lower()
    if mode not in SONARR_DELETE_MODES:
        mode = "episodes_only"
    j["SONARR_DELETE_MODE"] = mode

    j["RADARR_SCORE_FILTER_ENABLED"] = normalize_bool(j.get("RADARR_SCORE_FILTER_ENABLED", False), False)
    j["RADARR_MIN_AVG_SCORE"] = clamp_int(j.get("RADARR_MIN_AVG_SCORE", 60), 0, 100, 60)

    return j


def list_jobs(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    jobs = cfg.get("JOBS")
    if isinstance(jobs, list) and jobs:
        out = [normalize_job(j) for j in jobs]
        return [j for j in out if j["id"]]

    # Backward compatible: legacy single-job radarr config
    def cfg_get(name: str, default: str) -> str:
        return str(cfg.get(name, os.environ.get(name, default)))

    legacy = {
        "id": "legacy",
        "name": "Legacy Job",
        "enabled": True,
        "APP_ID": "",
        "APP": "radarr",  # legacy mode
        "TAG_LABEL": cfg_get("TAG_LABEL", "autodelete30"),
        "DAYS_OLD": int(cfg_get("DAYS_OLD", "30")),
        "DRY_RUN": cfg_get("DRY_RUN", "true").lower() == "true",
        "DELETE_FILES": cfg_get("DELETE_FILES", "true").lower() == "true",
        "ADD_IMPORT_EXCLUSION": cfg_get("ADD_IMPORT_EXCLUSION", "false").lower() == "true",
        "SCHED_DAY": "daily",
        "SCHED_HOUR": 3,
        "RADARR_SCORE_FILTER_ENABLED": False,
        "RADARR_MIN_AVG_SCORE": 60,
    }
    return [normalize_job(legacy)]


def find_job_by_id(cfg: Dict[str, Any], job_id: str) -> Optional[Dict[str, Any]]:
    job_id = (job_id or "").strip()
    if not job_id:
        return None
    for j in list_jobs(cfg):
        if j.get("id") == job_id:
            return j
    return None


# ----------------------------
# HTTP helpers
# ----------------------------
def api_get(base_url: str, api_key: str, timeout_s: int, path: str):
    url = (base_url or "").rstrip("/") + path
    r = requests.get(url, headers={"X-Api-Key": api_key or ""}, timeout=timeout_s)
    if r.status_code in (401, 403):
        raise PermissionError("Unauthorized (API key incorrect).")
    r.raise_for_status()
    return r.json()


def api_delete(base_url: str, api_key: str, timeout_s: int, path: str):
    url = (base_url or "").rstrip("/") + path
    r = requests.delete(url, headers={"X-Api-Key": api_key or ""}, timeout=timeout_s)
    if r.status_code in (401, 403):
        raise PermissionError("Unauthorized (API key incorrect).")
    # Radarr/Sonarr often returns 200/202/204
    if r.status_code not in (200, 202, 204):
        r.raise_for_status()
    return True


def api_delete_json(base_url: str, api_key: str, timeout_s: int, path: str, payload: Dict[str, Any]):
    url = (base_url or "").rstrip("/") + path
    r = requests.delete(
        url,
        headers={"X-Api-Key": api_key or "", "Content-Type": "application/json"},
        timeout=timeout_s,
        data=json.dumps(payload),
    )
    if r.status_code in (401, 403):
        raise PermissionError("Unauthorized (API key incorrect).")
    if r.status_code not in (200, 202, 204):
        r.raise_for_status()
    return True


def api_post(base_url: str, api_key: str, timeout_s: int, path: str, payload: Dict[str, Any]):
    url = (base_url or "").rstrip("/") + path
    r = requests.post(url, headers={"X-Api-Key": api_key or "", "Content-Type": "application/json"},
                      timeout=timeout_s, data=json.dumps(payload))
    if r.status_code in (401, 403):
        raise PermissionError("Unauthorized (API key incorrect).")
    r.raise_for_status()
    try:
        return r.json()
    except Exception:
        return {}


# ----------------------------
# Ratings / score helpers (Radarr)
# ----------------------------
def _score_to_0_100(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        # 0–10 -> 0–100
        if 0.0 <= f <= 10.0:
            return f * 10.0
        # 0–100 already
        if 0.0 <= f <= 100.0:
            return f
    except Exception:
        return None
    return None


def _avg(values: List[Optional[float]]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def radarr_avg_score_0_100(movie: Dict[str, Any]) -> Optional[float]:
    """
    Radarr returns `movie["ratings"]` as a dict of sources -> {value, votes}.
    Values are usually 0-10 for TMDb, 0-10 for IMDb, 0-100 for others.
    We convert everything to 0-100 then average available.
    """
    ratings = movie.get("ratings") or {}
    if not isinstance(ratings, dict):
        return None

    scores: List[Optional[float]] = []

    for src_obj in ratings.values():
        if not isinstance(src_obj, dict):
            continue
        v = _score_to_0_100(src_obj.get("value"))
        if v is not None:
            scores.append(v)

    return _avg(scores)


# ----------------------------
# Tag maps
# ----------------------------
def radarr_tags_map(base: str, key: str, timeout_s: int) -> Tuple[Dict[str, int], Dict[int, str]]:
    tags = api_get(base, key, timeout_s, "/api/v3/tag")
    label_to_id: Dict[str, int] = {}
    id_to_label: Dict[int, str] = {}
    for t in (tags or []):
        try:
            tid = int(t.get("id"))
            lab = str(t.get("label") or "").strip()
            if lab:
                label_to_id[lab] = tid
                id_to_label[tid] = lab
        except Exception:
            continue
    return label_to_id, id_to_label


def sonarr_tags_map(base: str, key: str, timeout_s: int) -> Tuple[Dict[str, int], Dict[int, str]]:
    tags = api_get(base, key, timeout_s, "/api/v3/tag")
    label_to_id: Dict[str, int] = {}
    id_to_label: Dict[int, str] = {}
    for t in (tags or []):
        try:
            tid = int(t.get("id"))
            lab = str(t.get("label") or "").strip()
            if lab:
                label_to_id[lab] = tid
                id_to_label[tid] = lab
        except Exception:
            continue
    return label_to_id, id_to_label


# ----------------------------
# Sonarr delete operations
# ----------------------------
def sonarr_list_series(base: str, key: str, timeout_s: int) -> List[Dict[str, Any]]:
    return api_get(base, key, timeout_s, "/api/v3/series") or []


def sonarr_list_episode_files(base: str, key: str, timeout_s: int, series_id: int) -> List[Dict[str, Any]]:
    return api_get(base, key, timeout_s, f"/api/v3/episodefile?seriesId={series_id}") or []


def sonarr_delete_episode_file(base: str, key: str, timeout_s: int, episode_file_id: int) -> None:
    api_delete(base, key, timeout_s, f"/api/v3/episodefile/{episode_file_id}")


def sonarr_delete_series(base: str, key: str, timeout_s: int, series_id: int,
                         delete_files: bool, add_import_excl: bool) -> None:
    # Sonarr uses addImportListExclusion
    df = "true" if delete_files else "false"
    ae = "true" if add_import_excl else "false"
    api_delete(base, key, timeout_s, f"/api/v3/series/{series_id}?deleteFiles={df}&addImportListExclusion={ae}")


# ----------------------------
# Radarr delete operations
# ----------------------------
def radarr_list_movies(base: str, key: str, timeout_s: int) -> List[Dict[str, Any]]:
    return api_get(base, key, timeout_s, "/api/v3/movie") or []


def radarr_get_movie(base: str, key: str, timeout_s: int, movie_id: int) -> Optional[Dict[str, Any]]:
    try:
        return api_get(base, key, timeout_s, f"/api/v3/movie/{movie_id}") or {}
    except requests.HTTPError as e:
        # If it's gone, Radarr typically returns 404
        resp = getattr(e, "response", None)
        if resp is not None and resp.status_code == 404:
            return None
        raise


def radarr_delete_movie(base: str, key: str, timeout_s: int, movie_id: int,
                        delete_files: bool, add_import_excl: bool) -> None:
    df = "true" if delete_files else "false"
    ae = "true" if add_import_excl else "false"
    api_delete(base, key, timeout_s, f"/api/v3/movie/{movie_id}?deleteFiles={df}&addImportExclusion={ae}")


def radarr_delete_movie_editor(base: str, key: str, timeout_s: int, movie_id: int,
                               delete_files: bool, add_import_excl: bool) -> None:
    # Bulk/editor delete fallback (some proxies / auth setups behave better with this route)
    payload = {
        "movieIds": [int(movie_id)],
        "deleteFiles": bool(delete_files),
        "addImportExclusion": bool(add_import_excl),
    }
    api_delete_json(base, key, timeout_s, "/api/v3/movie/editor", payload)


def radarr_delete_movie_strict(base: str, key: str, timeout_s: int, movie_id: int,
                               delete_files: bool, add_import_excl: bool) -> Tuple[bool, str]:
    """
    Returns (deleted_ok, method_used)
    """
    # First try the normal delete
    radarr_delete_movie(base, key, timeout_s, movie_id, delete_files, add_import_excl)
    still_there = radarr_get_movie(base, key, timeout_s, movie_id) is not None
    if not still_there:
        return True, "movie/{id}"

    # Fallback to editor delete
    radarr_delete_movie_editor(base, key, timeout_s, movie_id, delete_files, add_import_excl)
    still_there = radarr_get_movie(base, key, timeout_s, movie_id) is not None
    if not still_there:
        return True, "movie/editor"

    return False, "failed"


# ----------------------------
# Runner
# ----------------------------
# ----------------------------
# Runner
# ----------------------------
def run_job(cfg: Dict[str, Any], state: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    job_id = str(job.get("id") or "unknown").strip() or "unknown"
    timeout = http_timeout_seconds(cfg)

    # Ensure normalized keys exist (works for legacy too)
    job = normalize_job(job)

    # Resolve app via APP_ID in cfg["APPS"]
    apps_cfg = cfg.get("APPS") or []
    if not isinstance(apps_cfg, list):
        apps_cfg = []

    app_obj: Optional[Dict[str, Any]] = None
    app_id = str(job.get("APP_ID") or "").strip()
    if app_id:
        for a in apps_cfg:
            if isinstance(a, dict) and str(a.get("id") or "").strip() == app_id:
                app_obj = a
                break

    # Determine mode/type: prefer app_obj.type, else legacy job["APP"]
    app_key = ""
    if isinstance(app_obj, dict):
        app_key = str(app_obj.get("type") or "").strip().lower()
    if app_key not in ("radarr", "sonarr"):
        app_key = str(job.get("APP") or "radarr").strip().lower()
    if app_key not in ("radarr", "sonarr"):
        app_key = "radarr"

    tag_label = job["TAG_LABEL"]
    days_old = int(job["DAYS_OLD"])
    delete_files = bool(job["DELETE_FILES"])
    add_import_exclusion = bool(job["ADD_IMPORT_EXCLUSION"])
    dry_run = bool(job["DRY_RUN"])

    # Button overrides (WebUI can force dry/full without changing saved job)
    try:
        if str(os.environ.get("FORCE_DRY_RUN", "")).strip().lower() in ("1", "true", "yes", "on"):
            dry_run = True
        if str(os.environ.get("FORCE_FULL_RUN", "")).strip().lower() in ("1", "true", "yes", "on"):
            dry_run = False
    except Exception:
        pass

    sonarr_mode = str(job.get("SONARR_DELETE_MODE") or "episodes_only").strip().lower()
    if sonarr_mode not in SONARR_DELETE_MODES:
        sonarr_mode = "episodes_only"

    radarr_score_enabled = bool(job.get("RADARR_SCORE_FILTER_ENABLED", False))
    radarr_min_avg_score = int(job.get("RADARR_MIN_AVG_SCORE", 60))

    run_id = uuid.uuid4().hex[:8]
    run_mode = "dry" if dry_run else "full"

    run_started = now_utc()

    radarr_summary = []
    sonarr_summary = []
    run_state: Dict[str, Any] = {
        "job_id": job_id,
        "job_name": job.get("name", "Job"),
        "run_id": run_id,
        "run_mode": run_mode,
        "app": app_key,
        "app_id": app_id or None,
        "sonarr_delete_mode": sonarr_mode if app_key == "sonarr" else None,
        "started_at": run_started.isoformat(),
        "finished_at": None,
        "duration_seconds": None,
        "status": "running",
        "dry_run": dry_run,
        "delete_files": delete_files,
        "add_import_exclusion": add_import_exclusion,
        "tag": tag_label,
        "days_old": days_old,
        "radarr_score_filter_enabled": radarr_score_enabled if app_key == "radarr" else None,
        "radarr_min_avg_score": radarr_min_avg_score if app_key == "radarr" else None,
        "candidates_found": 0,
        "avg_score": None,  # overall avg score (Radarr only, if scores exist)
        "deleted_count": 0,
        "deleted": [],
        "errors": [],
    }

    _persist_run(state, job_id, run_state)
    log_info(
        "job run started",
        job_id=job_id,
        job_name=job.get("name"),
        run_id=run_id,
        run_mode=run_mode,
        app=app_key,
        app_id=(app_id or None),
    )

    try:
        cutoff = now_utc() - timedelta(days=days_old)

        if app_key == "radarr":
            # Prefer per-app config from WebUI, fallback to legacy env/config
            if isinstance(app_obj, dict):
                radarr_url = str(app_obj.get("url") or "").rstrip("/")
                api_key = str(app_obj.get("api_key") or "").strip()
            else:
                radarr_url = str(cfg.get("RADARR_URL", os.environ.get("RADARR_URL", ""))).rstrip("/")
                api_key = str(cfg.get("RADARR_API_KEY", os.environ.get("RADARR_API_KEY", ""))).strip()

            if not radarr_url:
                raise RuntimeError("RADARR_URL is required (or configure an App in WebUI).")
            if not api_key:
                raise RuntimeError("RADARR_API_KEY is required (or configure an App in WebUI).")
            log_info(f"Running Radarr job '{job.get('name')}' ({job_id})", label="Radarr Cleaning")
            log_debug(f"RADARR_URL={radarr_url}", label="Radarr Connection")
            log_debug(f"TAG_LABEL={tag_label} DAYS_OLD={days_old} CUTOFF={cutoff.isoformat()}", label="Radarr Cleaning")
            log_debug(f"DRY_RUN={dry_run} DELETE_FILES={delete_files} ADD_IMPORT_EXCLUSION={add_import_exclusion}", label="Radarr Cleaning")
            log_debug(f"SCORE_FILTER={radarr_score_enabled} MIN_AVG_SCORE={radarr_min_avg_score}", label="Radarr Cleaning")

            label_to_id, _ = radarr_tags_map(radarr_url, api_key, timeout)
            tag_id = label_to_id.get(tag_label)
            if not tag_id:
                raise RuntimeError(f"Tag '{tag_label}' not found in Radarr. Create it and tag movies first.")

            movies = radarr_list_movies(radarr_url, api_key, timeout)

            candidates: List[Tuple[Dict[str, Any], int]] = []
            for m in movies:
                if tag_id not in (m.get("tags") or []):
                    continue
                added = parse_radarr_date(str(m.get("added") or ""))
                if not added:
                    continue
                if added < cutoff:
                    age_days = int((now_utc() - added).total_seconds() // 86400)
                    candidates.append((m, age_days))

            candidates.sort(key=lambda x: x[1], reverse=True)
            run_state["candidates_found"] = len(candidates)
            _persist_run(state, job_id, run_state)

            overall_scores: List[float] = []

            for m, age_days in candidates:
                movie_id = int(m.get("id"))
                title = str(m.get("title") or "")
                year = m.get("year")
                path = m.get("path")

                avg_score = radarr_avg_score_0_100(m)

                score_gate_blocked = False
                score_gate_reason = None
                if radarr_score_enabled:
                    if avg_score is None:
                        score_gate_blocked = True
                        score_gate_reason = "no_ratings_available"
                    else:
                        overall_scores.append(float(avg_score))
                        if avg_score >= float(radarr_min_avg_score):
                            score_gate_blocked = True
                            score_gate_reason = f"avg_score_{avg_score:.1f}_not_below_{radarr_min_avg_score}"

                if score_gate_blocked:
                    log_debug(
                        f"SKIP (score gate) id={movie_id} '{title}' age={age_days} score={avg_score} reason={score_gate_reason}",
                        label="Radarr Cleaning",
                    )
                    continue

                if dry_run:
                    log_debug(
                        f"DRY-RUN would delete movie id={movie_id} '{title}' ({year}) age={age_days} score={avg_score} path={path}",
                        label="Radarr Cleaning",
                    )
                    radarr_summary.append(f"{title} ({year})")
                    run_state["deleted"].append(
                        {
                            "kind": "movie",
                            "id": movie_id,
                            "title": title,
                            "year": year,
                            "age_days": age_days,
                            "score": avg_score,
                            "path": path,
                            "dry_run": True,
                        }
                    )
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                    continue

                try:
                    ok, method = radarr_delete_movie_strict(
                        radarr_url, api_key, timeout, movie_id, delete_files, add_import_exclusion
                    )
                    if not ok:
                        raise RuntimeError("Radarr delete call returned but movie still exists in Radarr")

                    print(
                        f"Deleted movie id={movie_id} '{title}' ({year}) "
                        f"age={age_days} score={avg_score} via={method}"
                    )
                    radarr_summary.append(f"{title} ({year})")
                    run_state["deleted"].append(
                        {
                            "kind": "movie",
                            "id": movie_id,
                            "title": title,
                            "year": year,
                            "age_days": age_days,
                            "score": avg_score,
                            "path": path,
                            "dry_run": False,
                        }
                    )
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                except Exception as e:
                    err = f"ERROR Radarr deleting id={movie_id} title='{title}': {e}"
                    log_error(str(err), label="App")
                    run_state["errors"].append(err)
                    _persist_run(state, job_id, run_state)

            # publish an overall avg score for dashboard/job cards (if any)
            if radarr_score_enabled and overall_scores:
                run_state["avg_score"] = float(sum(overall_scores) / len(overall_scores))
                _persist_run(state, job_id, run_state)

        else:
            # Sonarr
            if isinstance(app_obj, dict):
                sonarr_url = str(app_obj.get("url") or "").rstrip("/")
                api_key = str(app_obj.get("api_key") or "").strip()
            else:
                sonarr_url = str(cfg.get("SONARR_URL", os.environ.get("SONARR_URL", ""))).rstrip("/")
                api_key = str(cfg.get("SONARR_API_KEY", os.environ.get("SONARR_API_KEY", ""))).strip()

            if not sonarr_url:
                raise RuntimeError("SONARR_URL is required (or configure an App in WebUI).")
            if not api_key:
                raise RuntimeError("SONARR_API_KEY is required (or configure an App in WebUI).")
            log_info(f"Running Sonarr job '{job.get('name')}' ({job_id})", label="Sonarr Cleaning")
            log_debug(f"SONARR_URL={sonarr_url}", label="Sonarr Connection")
            log_debug(f"TAG_LABEL={tag_label} DAYS_OLD={days_old} CUTOFF={cutoff.isoformat()}", label="Sonarr Cleaning")
            log_debug(f"DRY_RUN={dry_run} DELETE_FILES={delete_files} ADD_IMPORT_EXCLUSION={add_import_exclusion}", label="Sonarr Cleaning")
            log_debug(f"SONARR_DELETE_MODE={sonarr_mode}", label="Sonarr Cleaning")

            label_to_id, _ = sonarr_tags_map(sonarr_url, api_key, timeout)
            tag_id = label_to_id.get(tag_label)
            if not tag_id:
                raise RuntimeError(f"Tag '{tag_label}' not found in Sonarr. Create it and tag series first.")

            series_list = sonarr_list_series(sonarr_url, api_key, timeout)

            candidates: List[Tuple[Dict[str, Any], int]] = []
            for s in series_list:
                if tag_id not in (s.get("tags") or []):
                    continue
                added = parse_iso_date(str(s.get("added") or ""))
                if not added:
                    continue
                if added < cutoff:
                    age_days = int((now_utc() - added).total_seconds() // 86400)
                    candidates.append((s, age_days))

            candidates.sort(key=lambda x: x[1], reverse=True)
            run_state["candidates_found"] = len(candidates)
            _persist_run(state, job_id, run_state)

            for s, age_days in candidates:
                series_id = int(s.get("id"))
                title = str(s.get("title") or "")
                year = s.get("year")
                path = s.get("path")

                if dry_run:
                    log_debug(
                        f"DRY-RUN candidate series id={series_id} '{title}' ({year}) age={age_days} path={path} mode={sonarr_mode}",
                        label="Sonarr Cleaning",
                    )
                    run_state["deleted"].append(
                        {
                            "kind": "series_candidate",
                            "id": series_id,
                            "title": title,
                            "year": year,
                            "age_days": age_days,
                            "path": path,
                            "mode": sonarr_mode,
                            "dry_run": True,
                        }
                    )
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                    continue

                try:
                    if sonarr_mode == "series_whole":
                        sonarr_delete_series(sonarr_url, api_key, timeout, series_id, delete_files, add_import_exclusion)
                        log_info(f"Deleted series (whole) id={series_id} '{title}' ({year}) age={age_days}", label="Sonarr Cleaning")
                        run_state["deleted"].append(
                            {
                                "kind": "series",
                                "id": series_id,
                                "title": title,
                                "year": year,
                                "age_days": age_days,
                                "path": path,
                                "mode": sonarr_mode,
                                "dry_run": False,
                            }
                        )

                    elif sonarr_mode in ("episodes_only", "episodes_then_series_if_empty"):
                        if not delete_files:
                            print(f"SKIP episode deletion (DELETE_FILES=OFF) series id={series_id} '{title}'")
                        else:
                            eps = sonarr_list_episode_files(sonarr_url, api_key, timeout, series_id)
                            ep_ids: List[int] = []
                            for ef in eps:
                                try:
                                    ep_ids.append(int(ef.get("id")))
                                except Exception:
                                    continue

                            for ef_id in ep_ids:
                                sonarr_delete_episode_file(sonarr_url, api_key, timeout, ef_id)

                            print(f"Deleted {len(ep_ids)} episode file(s) for series id={series_id} '{title}'")

                            # Summary for Sonarr-style log
                            try:
                                sonarr_summary.append({"series": f"{title} ({year})", "episodes": int(len(ep_ids)), "reason": "Removed empty series"})
                            except Exception:
                                pass

                        if sonarr_mode == "episodes_then_series_if_empty":
                            remaining = sonarr_list_episode_files(sonarr_url, api_key, timeout, series_id)
                            if not remaining:
                                sonarr_delete_series(
                                    sonarr_url,
                                    api_key,
                                    timeout,
                                    series_id,
                                    delete_files=False,
                                    add_import_excl=add_import_exclusion,
                                )
                                print(
                                    f"Deleted empty series container id={series_id} '{title}' "
                                    f"(after episode delete)"
                                )
                                run_state["deleted"].append(
                                    {
                                        "kind": "series_empty_removed",
                                        "id": series_id,
                                        "title": title,
                                        "year": year,
                                        "age_days": age_days,
                                        "path": path,
                                        "mode": sonarr_mode,
                                        "dry_run": False,
                                    }
                                )
                            else:
                                run_state["deleted"].append(
                                    {
                                        "kind": "episodes_deleted_only",
                                        "id": series_id,
                                        "title": title,
                                        "year": year,
                                        "age_days": age_days,
                                        "path": path,
                                        "mode": sonarr_mode,
                                        "dry_run": False,
                                        "remaining_episode_files": len(remaining),
                                    }
                                )
                        else:
                            run_state["deleted"].append(
                                {
                                    "kind": "episodes_deleted_only",
                                    "id": series_id,
                                    "title": title,
                                    "year": year,
                                    "age_days": age_days,
                                    "path": path,
                                    "mode": sonarr_mode,
                                    "dry_run": False,
                                }
                            )

                    else:
                        print(f"Unknown Sonarr mode '{sonarr_mode}', skipping series id={series_id} '{title}'")

                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)

                except Exception as e:
                    err = f"ERROR Sonarr processing id={series_id} title='{title}': {e}"
                    log_error(str(err), label="App")
                    run_state["errors"].append(err)
                    _persist_run(state, job_id, run_state)

        run_state["status"] = "ok" if not run_state["errors"] else "partial"

    except Exception as e:
        run_state["status"] = "error"
        run_state["errors"].append(str(e))

    finally:
        finished = now_utc()
        run_state["finished_at"] = finished.isoformat()
        run_state["duration_seconds"] = int((finished - run_started).total_seconds())
                # Emit Radarr / Sonarr style summary log
        try:
            # Only emit if we have something meaningful to report
            if (app_key == "radarr" and radarr_summary) or (app_key == "sonarr" and sonarr_summary):
                log_cleaning_summary(
                    job_name=job.get("name", "Job"),
                    app_type=app_key,
                    dry_run=dry_run,
                    radarr_items=radarr_summary,
                    sonarr_items=sonarr_summary,
                )
        except Exception:
            pass

        log_info(
            "job run finished",
            job_id=job_id,
            job_name=job.get("name"),
            run_id=run_id,
            run_mode=run_mode,
            status=run_state.get("status"),
            deleted_count=run_state.get("deleted_count"),
            errors=len(run_state.get("errors") or []),
        )
        _persist_run(state, job_id, run_state)

    return run_state


# ----------------------------
# CLI
# ----------------------------
def main() -> int:
    setup_logging()
    try:
        p = argparse.ArgumentParser()
        p.add_argument("--job-id", default="", help="Run a specific job id from config.json")
        args = p.parse_args()

        cfg = load_config()
        state = load_state()

        job_id = (args.job_id or "").strip()

        if not job_id:
            log_error("--job-id is required (cron uses it).")
            log_error("--job-id is required (cron uses it).", label="App")
            return 2

        job = find_job_by_id(cfg, job_id)
        if not job:
            log_error("Job not found", job_id=job_id)
            log_error(f"Job not found: {job_id}", label="App")
            return 2

        if not job.get("enabled", False):
            log_warning("Job is disabled", job_id=job_id, job_name=job.get("name"))
            log_warning(f"Job is disabled: {job_id} ({job.get('name')})", label="App")
            # still record a run so dashboard shows something useful
            run_state = {
                "job_id": job_id,
                "job_name": job.get("name", "Job"),
                "status": "skipped",
                "reason": "disabled",
                "finished_at": now_iso(),
            }
            record_run(state, job_id, run_state)
            save_state(state)
            return 0

        run_job(cfg, state, job)
        return 0

    except Exception as e:
        log_error('fatal error in main', exc=e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
