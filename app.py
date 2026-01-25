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
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
import logging
from logging.handlers import RotatingFileHandler


# ----------------------------
# Paths
# ----------------------------
CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/config"))
CONFIG_PATH = CONFIG_DIR / "config.json"
STATE_PATH = CONFIG_DIR / "state.json"

# ----------------------------
# Logging
# ----------------------------
LOG_PATH = Path(os.environ.get("LOG_PATH", str(CONFIG_DIR / "mediareaparr.log")))

def setup_logging() -> logging.Logger:
    """Configure rotating file + console logging. Returns a logger."""
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    logger = logging.getLogger("mediareaparr")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    # prevent duplicates
    if not any(isinstance(h, RotatingFileHandler) for h in logger.handlers):
        fh = RotatingFileHandler(str(LOG_PATH), maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    return logger

log = setup_logging()

class _Tee:
    def __init__(self, *streams):
        self.streams = streams
    def write(self, s):
        for st in self.streams:
            try:
                st.write(s)
            except Exception:
                pass
        return len(s)
    def flush(self):
        for st in self.streams:
            try:
                st.flush()
            except Exception:
                pass

def setup_stdio_tee() -> None:
    """Mirror stdout/stderr into LOG_PATH so existing print() output is captured."""
    try:
        f = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
    except Exception:
        return
    sys.stdout = _Tee(sys.__stdout__, f)
    sys.stderr = _Tee(sys.__stderr__, f)



# ----------------------------
# Small utils
# ----------------------------
def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(v)
    except Exception:
        return default
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def normalize_bool(v: Any, default: bool) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return default


def parse_iso_date(s: str) -> Optional[datetime]:
    """Parse ISO date/time strings into UTC datetimes."""
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


# Backward-compat helper: older code used parse_radarr_date(...)
# Radarr "added" is ISO so parse_iso_date is correct.
def parse_radarr_date(s: str) -> Optional[datetime]:
    return parse_iso_date(s)


def http_timeout_seconds(cfg: Dict[str, Any]) -> int:
    return clamp_int(cfg.get("HTTP_TIMEOUT_SECONDS", 30), 5, 300, 30)


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
def run_job(cfg: Dict[str, Any], state: Dict[str, Any], job: Dict[str, Any]) -> Dict[str, Any]:
    timeout = http_timeout_seconds(cfg)

    job_id = job["id"]

    # WebUI v2: resolve app via APP_ID in cfg["APPS"]
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
    sonarr_mode = job.get("SONARR_DELETE_MODE", "episodes_only")

    radarr_score_enabled = bool(job.get("RADARR_SCORE_FILTER_ENABLED", False))
    radarr_min_avg_score = int(job.get("RADARR_MIN_AVG_SCORE", 60))

    run_started = now_utc()
    run_state: Dict[str, Any] = {
        "job_id": job_id,
        "job_name": job.get("name", "Job"),
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

            print(f"[mediareaparr] Running Radarr job '{job.get('name')}' ({job_id})")
            print(f"[mediareaparr] RADARR_URL={radarr_url}")
            print(f"[mediareaparr] TAG_LABEL={tag_label} DAYS_OLD={days_old} CUTOFF={cutoff.isoformat()}")
            print(f"[mediareaparr] DRY_RUN={dry_run} DELETE_FILES={delete_files} ADD_IMPORT_EXCLUSION={add_import_exclusion}")
            print(f"[mediareaparr] SCORE_FILTER={radarr_score_enabled} MIN_AVG_SCORE={radarr_min_avg_score}")

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
                    print(f"[mediareaparr] SKIP (score gate) id={movie_id} '{title}' "
                          f"age={age_days} score={avg_score} reason={score_gate_reason}")
                    continue

                if dry_run:
                    print(f"[mediareaparr] DRY-RUN would delete movie id={movie_id} '{title}' ({year}) age={age_days} "
                          f"score={avg_score} path={path}")
                    run_state["deleted"].append({
                        "kind": "movie",
                        "id": movie_id,
                        "title": title,
                        "year": year,
                        "age_days": age_days,
                        "score": avg_score,
                        "path": path,
                        "dry_run": True,
                    })
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                    continue

                try:
                    ok, method = radarr_delete_movie_strict(
                        radarr_url, api_key, timeout, movie_id, delete_files, add_import_exclusion
                    )
                    if not ok:
                        raise RuntimeError("Radarr delete call returned but movie still exists in Radarr")

                    print(f"[mediareaparr] Deleted movie id={movie_id} '{title}' ({year}) "
                          f"age={age_days} score={avg_score} via={method}")
                    run_state["deleted"].append({
                        "kind": "movie",
                        "id": movie_id,
                        "title": title,
                        "year": year,
                        "age_days": age_days,
                        "score": avg_score,
                        "path": path,
                        "dry_run": False,
                    })
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                except Exception as e:
                    err = f"ERROR Radarr deleting id={movie_id} title='{title}': {e}"
                    print(f"[mediareaparr] {err}", file=sys.stderr)
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

            print(f"[mediareaparr] Running Sonarr job '{job.get('name')}' ({job_id})")
            print(f"[mediareaparr] SONARR_URL={sonarr_url}")
            print(f"[mediareaparr] TAG_LABEL={tag_label} DAYS_OLD={days_old} CUTOFF={cutoff.isoformat()}")
            print(f"[mediareaparr] DRY_RUN={dry_run} DELETE_FILES={delete_files} ADD_IMPORT_EXCLUSION={add_import_exclusion}")
            print(f"[mediareaparr] SONARR_DELETE_MODE={sonarr_mode}")

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
                    print(f"[mediareaparr] DRY-RUN candidate series id={series_id} '{title}' ({year}) age={age_days} path={path} mode={sonarr_mode}")
                    run_state["deleted"].append({
                        "kind": "series_candidate",
                        "id": series_id,
                        "title": title,
                        "year": year,
                        "age_days": age_days,
                        "path": path,
                        "mode": sonarr_mode,
                        "dry_run": True,
                    })
                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)
                    continue

                try:
                    if sonarr_mode == "series_whole":
                        sonarr_delete_series(sonarr_url, api_key, timeout, series_id, delete_files, add_import_exclusion)
                        print(f"[mediareaparr] Deleted series (whole) id={series_id} '{title}' ({year}) age={age_days}")
                        run_state["deleted"].append({
                            "kind": "series",
                            "id": series_id,
                            "title": title,
                            "year": year,
                            "age_days": age_days,
                            "path": path,
                            "mode": sonarr_mode,
                            "dry_run": False,
                        })

                    elif sonarr_mode in ("episodes_only", "episodes_then_series_if_empty"):
                        # Delete episode files for the series (if delete_files true),
                        # else do nothing (we won't unmonitor etc. here).
                        if not delete_files:
                            print(f"[mediareaparr] SKIP episode deletion (DELETE_FILES=OFF) series id={series_id} '{title}'")
                        else:
                            eps = sonarr_list_episode_files(sonarr_url, api_key, timeout, series_id)
                            ep_ids = []
                            for ef in eps:
                                try:
                                    ep_ids.append(int(ef.get("id")))
                                except Exception:
                                    continue

                            for ef_id in ep_ids:
                                sonarr_delete_episode_file(sonarr_url, api_key, timeout, ef_id)

                            print(f"[mediareaparr] Deleted {len(ep_ids)} episode file(s) for series id={series_id} '{title}'")

                        if sonarr_mode == "episodes_then_series_if_empty":
                            # Re-check whether any episode files remain
                            remaining = sonarr_list_episode_files(sonarr_url, api_key, timeout, series_id)
                            if not remaining:
                                sonarr_delete_series(sonarr_url, api_key, timeout, series_id, delete_files=False, add_import_excl=add_import_exclusion)
                                print(f"[mediareaparr] Deleted empty series container id={series_id} '{title}' (after episode delete)")
                                run_state["deleted"].append({
                                    "kind": "series_empty_removed",
                                    "id": series_id,
                                    "title": title,
                                    "year": year,
                                    "age_days": age_days,
                                    "path": path,
                                    "mode": sonarr_mode,
                                    "dry_run": False,
                                })
                            else:
                                run_state["deleted"].append({
                                    "kind": "episodes_deleted_only",
                                    "id": series_id,
                                    "title": title,
                                    "year": year,
                                    "age_days": age_days,
                                    "path": path,
                                    "mode": sonarr_mode,
                                    "dry_run": False,
                                    "remaining_episode_files": len(remaining),
                                })
                        else:
                            run_state["deleted"].append({
                                "kind": "episodes_deleted_only",
                                "id": series_id,
                                "title": title,
                                "year": year,
                                "age_days": age_days,
                                "path": path,
                                "mode": sonarr_mode,
                                "dry_run": False,
                            })

                    else:
                        # Shouldn't happen due to normalization
                        print(f"[mediareaparr] Unknown Sonarr mode '{sonarr_mode}', skipping series id={series_id} '{title}'")

                    run_state["deleted_count"] = len(run_state["deleted"])
                    _persist_run(state, job_id, run_state)

                except Exception as e:
                    err = f"ERROR Sonarr processing id={series_id} title='{title}': {e}"
                    print(f"[mediareaparr] {err}", file=sys.stderr)
                    run_state["errors"].append(err)
                    _persist_run(state, job_id, run_state)

        run_state["status"] = "ok" if not run_state["errors"] else "partial"
        return run_state

    except Exception as e:
        run_state["status"] = "error"
        run_state["errors"].append(str(e))
        return run_state

    finally:
        finished = now_utc()
        run_state["finished_at"] = finished.isoformat()
        run_state["duration_seconds"] = int((finished - run_started).total_seconds())
        _persist_run(state, job_id, run_state)


# ----------------------------
# CLI
# ----------------------------
def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--job-id", default="", help="Run a specific job id from config.json")
    args = p.parse_args()

    cfg = load_config()
    state = load_state()

    job_id = (args.job_id or "").strip()

    if not job_id:
        print("[mediareaparr] ERROR: --job-id is required (cron uses it).", file=sys.stderr)
        return 2

    job = find_job_by_id(cfg, job_id)
    if not job:
        print(f"[mediareaparr] ERROR: Job not found: {job_id}", file=sys.stderr)
        return 2

    if not job.get("enabled", False):
        print(f"[mediareaparr] Job is disabled: {job_id} ({job.get('name')})")
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


if __name__ == "__main__":
    raise SystemExit(main())
