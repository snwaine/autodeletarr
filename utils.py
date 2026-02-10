"""mediareaparr utils.py

Shared helpers used by both app.py (runner) and webui.py (Flask UI).

Design goals:
- dependency-light
- safe to import from both contexts
- stable signatures
"""

from __future__ import annotations

import os
import re
import uuid
from html import escape as html_escape
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Mapping


# ----------------------------
# Basic helpers
# ----------------------------
def env_default(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def clamp_int(v: Any, lo: int, hi: int, default: int) -> int:
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return default
    if iv < lo:
        return lo
    if iv > hi:
        return hi
    return iv


def _coerce_bool(v: Any) -> Optional[bool]:
    """Best-effort conversion of common truthy/falsey inputs to bool.
    Returns None if it can't decide.
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    return None


def normalize_bool(v: Any, default: bool = False) -> bool:
    b = _coerce_bool(v)
    return default if b is None else b

def http_timeout_seconds(cfg: Dict[str, Any]) -> int:
    try:
        return int((cfg or {}).get("HTTP_TIMEOUT_SECONDS", 30))
    except (TypeError, ValueError):
        return 30


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def parse_iso_date(s: str) -> Optional[datetime]:
    """Parse an ISO-ish datetime string and return a timezone-aware UTC datetime (or None)."""
    if not s:
        return None
    try:
        ss = str(s).strip()
        if ss.endswith("Z"):
            ss = ss[:-1] + "+00:00"
        # allow date-only strings (YYYY-MM-DD)
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}$", ss):
            ss = ss + "T00:00:00+00:00"
        dt = datetime.fromisoformat(ss)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def self_test_date_parsing() -> None:
    """Tiny self-test to prevent regressions in date parsing.

    Enabled by setting MEDIAREAPARR_SELFTEST=1 (default).
    Set MEDIAREAPARR_SELFTEST=0 to skip.
    """
    if os.environ.get("MEDIAREAPARR_SELFTEST", "1").strip().lower() in ("0", "false", "no", "off"):
        return

    d1 = parse_iso_date("2026-02-01T12:34:56Z")
    assert d1 is not None and d1.tzinfo is not None and d1.utcoffset().total_seconds() == 0

    d2 = parse_iso_date("2026-02-01T12:34:56+01:00")
    assert d2 is not None and d2.hour == 11 and d2.minute == 34 and d2.utcoffset().total_seconds() == 0

    d3 = parse_iso_date("2026-02-01T12:34:56")
    assert d3 is not None and d3.hour == 12 and d3.utcoffset().total_seconds() == 0

    d4 = parse_iso_date("2026-02-01")
    assert d4 is not None and d4.hour == 0 and d4.minute == 0 and d4.utcoffset().total_seconds() == 0

    assert parse_iso_date("not-a-date") is None


# ----------------------------
# IDs + HTML escaping
# ----------------------------
def make_job_id() -> str:
    return uuid.uuid4().hex[:10]


def make_app_id() -> str:
    return uuid.uuid4().hex[:10]



# ----------------------------
# Web form / UI helpers
# ----------------------------
def checkbox(form: Mapping[str, Any], name: str) -> bool:
    """Interpret typical HTML checkbox values from a form mapping."""
    b = _coerce_bool(form.get(name))
    return False if b is None else b

def schedule_label(day_key: str, hour: Any) -> str:
    """Render a compact schedule label used in the UI (e.g., 'Mon • 03:00')."""
    dk = (day_key or "daily").strip().lower()
    names = {
        "daily": "Daily",
        "mon": "Mon",
        "tue": "Tue",
        "wed": "Wed",
        "thu": "Thu",
        "fri": "Fri",
        "sat": "Sat",
        "sun": "Sun",
    }
    day_txt = names.get(dk, "Daily")
    h = clamp_int(hour, 0, 23, 3)
    return f"{day_txt} • {h:02d}:00"

def safe_html(s: Any) -> str:
    return html_escape(str(s or ""), quote=True)


# ----------------------------
# Logging helpers (daily log rotation)
# ----------------------------
LOG_FILE_SUFFIX = " mediareaparr.log"


def _today_dd_mm_yyyy() -> str:
    return now_utc().strftime("%d-%m-%Y")


def _is_valid_dd_mm_yyyy(s: str) -> bool:
    s = (s or "").strip()
    if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", s):
        return False
    try:
        datetime.strptime(s, "%d-%m-%Y")
        return True
    except (TypeError, ValueError):
        return False


def get_log_path(
    *,
    cfg: Optional[Dict[str, Any]] = None,
    default_dir: Optional[Path] = None,
    date_key: Optional[str] = None,
) -> Path:
    """Return the path for the daily MediaReaparr log file.

    Filename format: '<dd-mm-yyyy> mediareaparr.log'
    Directory resolution order:
      1) cfg['LOG_DIR'] / cfg['log_dir']
      2) cfg['LOG_PATH'] / cfg['log_path'] -> parent directory
      3) env LOG_DIR
      4) env LOG_PATH -> parent directory
      5) default_dir (or /config)
    """
    log_dir: Optional[Path] = None

    # cfg overrides
    if isinstance(cfg, dict):
        d = str(cfg.get("LOG_DIR") or cfg.get("log_dir") or "").strip()
        if d:
            log_dir = Path(d).expanduser()
        else:
            p = str(cfg.get("LOG_PATH") or cfg.get("log_path") or "").strip()
            if p:
                try:
                    log_dir = Path(p).expanduser().resolve().parent
                except (OSError, RuntimeError, ValueError, TypeError):
                    log_dir = None

    # env overrides
    if log_dir is None:
        d = os.environ.get("LOG_DIR", "").strip()
        if d:
            log_dir = Path(d).expanduser()
    if log_dir is None:
        p = os.environ.get("LOG_PATH", "").strip()
        if p:
            try:
                log_dir = Path(p).expanduser().resolve().parent
            except (OSError, RuntimeError, ValueError, TypeError):
                log_dir = None

    if log_dir is None:
        log_dir = (default_dir or Path(os.environ.get("CONFIG_DIR", "/config"))).expanduser()

    dk = (date_key or "").strip()
    if not _is_valid_dd_mm_yyyy(dk):
        dk = _today_dd_mm_yyyy()

    return Path(log_dir) / f"{dk}{LOG_FILE_SUFFIX}"


def format_log_line(message: str, *, severity: str = "INFO", label: str = "App", at: Optional[datetime] = None) -> str:
    ts = (at or now_utc()).strftime("%b %d, %Y, %-I:%M:%S %p")
    sev = (severity or "INFO").strip().upper()
    # UI wants [Severity] bracket token; keep title-like (Info/Warning/etc.)? historically you used INFO.
    # Keep upper-case to match earlier work.
    return f"{ts} [{sev}] [{label}] {message}"


def append_log_line(
    message: str,
    *,
    severity: str = "INFO",
    label: str = "App",
    cfg: Optional[Dict[str, Any]] = None,
    default_dir: Optional[Path] = None,
) -> None:
    path = get_log_path(cfg=cfg, default_dir=default_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = format_log_line(message, severity=severity, label=label)
    # newline-safe
    with path.open("a", encoding="utf-8") as f:
        f.write(line.replace("\r", "").replace("\n", " ") + "\n")


if __name__ == "__main__":
    self_test_date_parsing()
    print("utils.py self-test OK")