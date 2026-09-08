#!/usr/bin/env python3
"""Standalone live Codex account-usage reporter for Hermes profiles.

This utility deliberately runs the provider request in a short-lived Hermes
Python subprocess for each profile. Hermes resolves credentials and refreshes
OAuth state relative to ``HERMES_HOME``; keeping that work in a subprocess
prevents one profile's imported auth state from leaking into another.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_LAUNCHER_RE = re.compile(
    r'exec\s+["\'](?P<python>[^"\']+/bin/python)["\']\s+'
    r'["\'](?P<hermes>[^"\']+/hermes)["\']'
)

_CHILD_QUERY = r"""
import hashlib
import json
from datetime import datetime, timezone

import httpx
from agent.account_usage import _codex_backend_urls, _resolve_codex_usage_credentials

try:
    token, base_url, account_id = _resolve_codex_usage_credentials(None, None)
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "codex-cli",
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    with httpx.Client(timeout=15.0) as client:
        response = client.get(_codex_backend_urls(base_url)[0], headers=headers)
        response.raise_for_status()
    payload = response.json() or {}
    rate_limit = payload.get("rate_limit") or {}
    windows = []
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        if window.get("used_percent") is None:
            continue
        windows.append({
            "used_percent": float(window["used_percent"]),
            "reset_at": window.get("reset_at"),
            "limit_window_seconds": window.get("limit_window_seconds"),
        })
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    details = []
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked - use /usage reset to activate")
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float, str)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    print(json.dumps({
        "status": "ok",
        "auth_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "provider": "openai-codex",
        "source": "usage_api",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "plan": str(payload.get("plan_type") or "").replace("_", " ").replace("-", " ").title() or None,
        "windows": windows,
        "details": details,
    }))
except Exception:
    print(json.dumps({
        "status": "unavailable",
        "error": (
            "No live Codex usage data returned (credentials may be unavailable, "
            "the provider request may have failed, or the response may contain "
            "no usage data)."
        ),
    }))
"""


@dataclass(frozen=True)
class Profile:
    name: str
    home: Path


class ProfileSelectionError(ValueError):
    """Raised when a requested profile cannot be selected."""


def resolve_hermes_root() -> Path:
    """Resolve the Hermes profile root without importing Hermes application code."""
    configured = os.environ.get("HERMES_HOME", "").strip()
    if configured:
        home = Path(configured).expanduser()
        if home.parent.name == "profiles":
            return home.parent.parent
        return home
    return Path.home() / ".hermes"


def _valid_named_profile(path: Path) -> bool:
    return path.is_dir() and _PROFILE_RE.fullmatch(path.name) is not None


def discover_profiles(root: Path | None = None) -> list[Profile]:
    """Discover the default profile and valid named profiles on every call."""
    root = (root or resolve_hermes_root()).expanduser()
    profiles = [Profile("default", root)] if root.is_dir() else []
    profiles_root = root / "profiles"
    if profiles_root.is_dir():
        profiles.extend(
            Profile(entry.name, entry)
            for entry in sorted(profiles_root.iterdir(), key=lambda item: item.name)
            if _valid_named_profile(entry) and entry.name != "default"
        )
    return profiles


def select_profiles(profiles: Iterable[Profile], requested: str | None) -> list[Profile]:
    """Select all discovered profiles or one named profile."""
    available = list(profiles)
    if requested is None:
        return available
    name = requested.strip().lower()
    if not _PROFILE_RE.fullmatch(name) and name != "default":
        raise ProfileSelectionError(
            f"Invalid profile name {requested!r}; expected lowercase letters, numbers, '-' or '_'."
        )
    for profile in available:
        if profile.name == name:
            return [profile]
    raise ProfileSelectionError(f"Profile {requested!r} does not exist under the Hermes profile root.")


def _runtime_from_launcher(launcher: Path) -> tuple[Path, Path] | None:
    try:
        text = launcher.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    match = _LAUNCHER_RE.search(text)
    if not match:
        return None
    python = Path(match.group("python"))
    source = Path(match.group("hermes")).parent
    if python.is_file() and (source / "agent").is_dir():
        return python, source
    return None


def resolve_hermes_runtime() -> tuple[Path, Path]:
    """Find the installed Hermes interpreter and source package."""
    launcher_name = shutil.which("hermes")
    if launcher_name:
        launcher = Path(launcher_name).resolve()
        runtime = _runtime_from_launcher(launcher)
        if runtime:
            return runtime
        if launcher.name == "hermes" and (launcher.parent / "agent").is_dir():
            python = launcher.parent / "venv" / "bin" / "python"
            if python.is_file():
                return python, launcher.parent

    candidates = [Path.home() / ".hermes" / "hermes-agent"]
    for source in candidates:
        python = source / "venv" / "bin" / "python"
        if python.is_file() and (source / "agent").is_dir():
            return python, source
    raise RuntimeError(
        "Could not locate the installed Hermes Python runtime. "
        "Ensure the `hermes` command is available on PATH."
    )


def _run_child(profile: Profile, python: Path, source: Path, timeout: float) -> dict[str, Any]:
    env = os.environ.copy()
    env["HERMES_HOME"] = str(profile.home)
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(source) + os.pathsep + existing_pythonpath if existing_pythonpath else str(source)
    )
    try:
        result = subprocess.run(
            [str(python), "-c", _CHILD_QUERY],
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "unavailable",
            "error": f"Timed out after {timeout:g}s while querying this profile.",
        }
    except OSError as exc:
        return {"status": "unavailable", "error": f"Could not start Hermes runtime: {exc}"}

    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()
        suffix = f" ({detail[-1]})" if detail else ""
        return {
            "status": "unavailable",
            "error": f"Hermes usage query failed with exit status {result.returncode}{suffix}.",
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "status": "unavailable",
            "error": "Hermes usage query returned invalid JSON.",
        }
    if not isinstance(payload, dict):
        return {"status": "unavailable", "error": "Hermes usage query returned an invalid object."}
    return payload


def fetch_profile_snapshot(profile: Profile, *, timeout: float = 30.0) -> dict[str, Any]:
    """Fetch one profile's current usage using Hermes' supported API."""
    python, source = resolve_hermes_runtime()
    return _run_child(profile, python, source, timeout)


def _window_label(limit_window_seconds: Any) -> str:
    """Name a Codex window from the duration supplied by the API."""
    try:
        seconds = int(limit_window_seconds)
    except (TypeError, ValueError):
        return "Rate limit window"
    if seconds == 604800:
        return "Weekly"
    if seconds == 86400:
        return "Daily"
    if seconds == 18000:
        return "Session"
    if seconds >= 86400:
        return f"{seconds // 86400}-day window"
    if seconds >= 3600:
        return f"{seconds // 3600}-hour window"
    return f"{seconds}-second window"


def normalise_codex_windows(raw_windows: Iterable[dict[str, Any] | None]) -> list[dict[str, Any]]:
    """Add truthful labels to raw Codex windows using their durations."""
    windows: list[dict[str, Any]] = []
    for raw in raw_windows:
        if not isinstance(raw, dict):
            continue
        window = dict(raw)
        window["label"] = _window_label(window.get("limit_window_seconds"))
        windows.append(window)
    return windows


def filter_snapshot(snapshot: dict[str, Any], period: str | None) -> dict[str, Any]:
    """Apply an optional provider-window view without inventing calendar usage."""
    result = dict(snapshot)
    if period is None or snapshot.get("status", "ok") != "ok":
        return result
    windows = list(snapshot.get("windows", []))
    if period == "week":
        windows = [window for window in windows if window.get("label") == "Weekly"]
        result["scope"] = "weekly rate-limit allowance"
        result["filter_note"] = (
            "week selects the Codex weekly rate-limit window"
            if windows
            else "week requested, but Codex did not return a Weekly rate-limit window"
        )
    else:
        # Prefer a real provider-reported daily window when one exists. The
        # current Codex response has no Daily window, so retain all returned
        # windows and explain that this is only a live status check.
        daily_windows = [window for window in windows if window.get("label") == "Daily"]
        if daily_windows:
            windows = daily_windows
            result["scope"] = "daily rate-limit window"
            result["filter_note"] = "today selects the Codex daily rate-limit window"
        else:
            # Codex does not expose calendar-day totals. Preserve every live window
            # returned by the provider rather than relabelling a weekly quota as a
            # session or silently returning a fabricated daily figure.
            result["scope"] = "live status checked today"
            result["filter_note"] = (
                "today is not a calendar-day total; showing the live Codex rate-limit "
                "window(s) currently returned by the provider"
            )
    result["windows"] = windows
    result["filter"] = period
    return result


def _normalise_item(profile: Profile, snapshot: dict[str, Any], period: str | None) -> dict[str, Any]:
    if snapshot.get("status", "ok") == "ok":
        snapshot = dict(snapshot)
        snapshot["windows"] = normalise_codex_windows(snapshot.get("windows", []))
    item = {"profile": profile.name, **filter_snapshot(snapshot, period)}
    item.setdefault("status", "unavailable" if item.get("error") else "ok")
    return item


def group_items(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse successful results that used the exact same auth token.

    The child process returns only a one-way fingerprint of the resolved token.
    It is used locally as a grouping key and is never included in the report.
    Failed or un-fingerprinted results are kept separate rather than being
    grouped because identical error messages do not prove shared credentials.
    """
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        profile = str(item.get("profile", ""))
        fingerprint = item.get("auth_fingerprint")
        if item.get("status") == "ok" and isinstance(fingerprint, str) and fingerprint:
            key = ("token", fingerprint)
        else:
            key = ("profile", profile)
        if key not in groups:
            group = dict(item)
            group.pop("profile", None)
            group.pop("auth_fingerprint", None)
            group["profiles"] = [profile]
            groups[key] = group
            order.append(key)
        else:
            groups[key]["profiles"].append(profile)

    result: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        if len(group["profiles"]) > 1:
            group["shared_between"] = list(group["profiles"])
        result.append(group)
    return result


def _format_reset(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            reset = datetime.fromtimestamp(float(value), tz=timezone.utc)
        else:
            reset = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
        return reset.astimezone().strftime("%Y-%m-%d %H:%M %Z")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def render_text(report: dict[str, Any]) -> str:
    lines: list[str] = []
    for index, item in enumerate(report["profiles"]):
        if index:
            lines.append("")
        profiles = item["profiles"]
        if item.get("shared_between"):
            lines.append(
                f"Profiles: {', '.join(profiles)} "
                f"(shared between {', '.join(item['shared_between'])})"
            )
        else:
            lines.append(f"Profile: {profiles[0]}")
        if item.get("status") != "ok":
            lines.append(f"Unavailable: {item.get('error', 'No usage data returned.')}")
            continue
        provider = item.get("provider", "openai-codex")
        plan = item.get("plan")
        lines.append(f"Provider: {provider}" + (f" ({plan})" if plan else ""))
        if item.get("scope"):
            lines.append(f"Scope: {item['scope']}")
        if item.get("filter") == "week" and not item.get("windows"):
            lines.append("Weekly: unavailable (not returned by the Codex API)")
        for window in item.get("windows", []):
            used = window.get("used_percent")
            if used is None:
                text = f"{window.get('label', 'Window')}: unavailable"
            else:
                used_value = max(0, round(float(used)))
                remaining = max(0, round(100 - float(used)))
                text = f"{window.get('label', 'Window')}: {remaining}% remaining ({used_value}% used)"
            reset = _format_reset(window.get("reset_at"))
            if reset:
                text += f" • resets {reset}"
            elif window.get("detail"):
                text += f" • {window['detail']}"
            lines.append(text)
        lines.extend(str(detail) for detail in item.get("details", []))
        if item.get("filter_note"):
            lines.append(f"Note: {item['filter_note']}")
    return "\n".join(lines)


def load_hermes_history(
    profiles: Iterable[Profile],
    *,
    days: int = 7,
    now: float | None = None,
    day: str | None = None,
) -> list[dict[str, Any]]:
    """Read local Hermes session usage for the last ``days`` days.

    This is deliberately separate from the Codex provider snapshot: the state
    database contains Hermes session token accounting, not historical Codex
    rate-limit percentages. Missing or unreadable profile databases are simply
    omitted so a chart remains useful when a profile has no local history.
    """
    if days < 1:
        raise ValueError("days must be at least 1")
    if day is not None:
        try:
            datetime.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("day must be an ISO calendar date") from exc
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    cutoff = current - (days * 86400)
    totals: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        database = profile.home / "state.db"
        if not database.is_file():
            continue
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                session_columns = {
                    str(row[1])
                    for row in connection.execute("PRAGMA table_info(sessions)")
                }
                if "sessions" not in tables or "started_at" not in session_columns:
                    rows = []
                elif "model" in session_columns:
                    connection.row_factory = sqlite3.Row
                    date_expression = 'date(s."started_at", \'unixepoch\', \'localtime\')'
                    model_expression = "COALESCE(NULLIF(s.\"model\", ''), 'unknown')"
                    input_tokens = _model_metric_expression("s", session_columns, "input_tokens")
                    output_tokens = _model_metric_expression("s", session_columns, "output_tokens")
                    estimated, estimated_available = _model_cost_expression(
                        "s", session_columns, "estimated_cost_usd"
                    )
                    actual, actual_available = _model_cost_expression(
                        "s", session_columns, "actual_cost_usd"
                    )
                    if day is None:
                        where = 's."started_at" >= ?'
                        parameters = (cutoff,)
                    else:
                        where = f"{date_expression} = ?"
                        parameters = (day,)
                    query = f"""
                        SELECT
                            {date_expression} AS day,
                            {model_expression} AS model,
                            SUM({input_tokens} + {output_tokens}) AS tokens,
                            SUM({input_tokens}) AS input_tokens,
                            SUM({output_tokens}) AS output_tokens,
                            SUM({_model_metric_expression('s', session_columns, 'cache_read_tokens')}) AS cache_read_tokens,
                            SUM({_model_metric_expression('s', session_columns, 'cache_write_tokens')}) AS cache_write_tokens,
                            SUM({_model_metric_expression('s', session_columns, 'reasoning_tokens')}) AS reasoning_tokens,
                            SUM({_model_metric_expression('s', session_columns, 'api_call_count')}) AS api_calls,
                            COUNT(*) AS sessions,
                            SUM({estimated}) AS estimated_cost_usd,
                            {estimated_available} AS estimated_cost_usd_available,
                            SUM({actual}) AS actual_cost_usd,
                            {actual_available} AS actual_cost_usd_available
                        FROM sessions AS s
                        WHERE {where}
                        GROUP BY 1, 2
                        ORDER BY 1, 2
                    """
                    rows = connection.execute(query, parameters).fetchall()
                else:
                    if day is None:
                        query = """SELECT date(started_at, 'unixepoch', 'localtime') AS day,
                                  COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens,
                                  COUNT(*) AS sessions
                             FROM sessions
                            WHERE started_at >= ?
                            GROUP BY day
                            ORDER BY day"""
                        parameters = (cutoff,)
                    else:
                        query = """SELECT date(started_at, 'unixepoch', 'localtime') AS day,
                                          COALESCE(SUM(input_tokens), 0) + COALESCE(SUM(output_tokens), 0) AS tokens,
                                          COUNT(*) AS sessions
                                     FROM sessions
                                    WHERE date(started_at, 'unixepoch', 'localtime') = ?
                                    GROUP BY day
                                    ORDER BY day"""
                        parameters = (day,)
                    rows = connection.execute(query, parameters).fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            continue
        for row in rows:
            if isinstance(row, sqlite3.Row):
                day_name = str(row["day"])
                item = totals.setdefault(
                    day_name,
                    {"day": day_name, "tokens": 0, "sessions": 0, "_models": {}},
                )
                item["tokens"] += int(row["tokens"] or 0)
                item["sessions"] += int(row["sessions"] or 0)
                model_name = str(row["model"])
                model = item["_models"].setdefault(
                    model_name,
                    {
                        "model": model_name,
                        "tokens": 0,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cache_read_tokens": 0,
                        "cache_write_tokens": 0,
                        "reasoning_tokens": 0,
                        "api_calls": 0,
                        "sessions": 0,
                        "estimated_cost_usd": None,
                        "actual_cost_usd": None,
                    },
                )
                _merge_model_metric(model, row)
            else:
                day_name, tokens, sessions = row
                item = totals.setdefault(
                    str(day_name),
                    {"day": str(day_name), "tokens": 0, "sessions": 0},
                )
                item["tokens"] += int(tokens or 0)
                item["sessions"] += int(sessions or 0)
    history = []
    for day_name in sorted(totals):
        item = totals[day_name]
        models = item.pop("_models", None)
        if models:
            item["models"] = sorted(models.values(), key=lambda value: (-value["tokens"], value["model"]))
        history.append(item)
    return history


def _model_metric_expression(alias: str, columns: set[str], name: str) -> str:
    if name in columns:
        return f'COALESCE({alias}."{name}", 0)'
    return "0"


def _model_cost_expression(alias: str, columns: set[str], name: str) -> tuple[str, str]:
    if name in columns:
        value = f'COALESCE({alias}."{name}", 0)'
        available = f'MAX(CASE WHEN {alias}."{name}" IS NOT NULL THEN 1 ELSE 0 END)'
        return value, available
    return "0", "0"


def _merge_model_metric(target: dict[str, Any], row: sqlite3.Row) -> None:
    for field in (
        "tokens",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "api_calls",
        "sessions",
    ):
        target[field] = target.get(field, 0) + int(row[field] or 0)
    for field in ("estimated_cost_usd", "actual_cost_usd"):
        if row[f"{field}_available"] and float(row[field] or 0.0) != 0.0:
            target[field] = (target.get(field) or 0.0) + float(row[field])


def load_hermes_model_history(
    profiles: Iterable[Profile],
    *,
    days: int = 7,
    now: float | None = None,
    day: str | None = None,
) -> list[dict[str, Any]]:
    """Read daily token and cost metrics grouped by model from local state."""
    if days < 1:
        raise ValueError("days must be at least 1")
    if day is not None:
        try:
            datetime.fromisoformat(day)
        except ValueError as exc:
            raise ValueError("day must be an ISO calendar date") from exc
    current = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    cutoff = current - (days * 86400)
    profiles = list(profiles)
    model_totals: dict[tuple[str, str], dict[str, Any]] = {}

    for profile in profiles:
        database = profile.home / "state.db"
        if not database.is_file():
            continue
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            session_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
            usage_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(session_model_usage)")
            }
            rows: list[sqlite3.Row]
            if (
                "session_model_usage" in tables
                and {"session_id", "model"}.issubset(usage_columns)
                and "started_at" in session_columns
            ):
                estimated, estimated_available = _model_cost_expression(
                    "u", usage_columns, "estimated_cost_usd"
                )
                actual, actual_available = _model_cost_expression(
                    "u", usage_columns, "actual_cost_usd"
                )
                time_expression = 's."started_at"'
                usage_date = f"date({time_expression}, 'unixepoch', 'localtime')"
                where = f"{time_expression} >= ?"
                parameters: tuple[Any, ...] = (cutoff,)
                if day is not None:
                    where = f"{usage_date} = ?"
                    parameters = (day,)
                input_tokens = _model_metric_expression("u", usage_columns, "input_tokens")
                output_tokens = _model_metric_expression("u", usage_columns, "output_tokens")
                session_model = (
                    'NULLIF(s."model", \'\')' if "model" in session_columns else "NULL"
                )
                query = f"""
                    SELECT
                        {usage_date} AS day,
                        COALESCE(NULLIF(u."model", ''), {session_model}, 'unknown') AS model,
                        SUM({input_tokens} + {output_tokens}) AS tokens,
                        SUM({input_tokens}) AS input_tokens,
                        SUM({output_tokens}) AS output_tokens,
                        SUM({_model_metric_expression('u', usage_columns, 'cache_read_tokens')}) AS cache_read_tokens,
                        SUM({_model_metric_expression('u', usage_columns, 'cache_write_tokens')}) AS cache_write_tokens,
                        SUM({_model_metric_expression('u', usage_columns, 'reasoning_tokens')}) AS reasoning_tokens,
                        SUM({_model_metric_expression('u', usage_columns, 'api_call_count')}) AS api_calls,
                        COUNT(DISTINCT u."session_id") AS sessions,
                        SUM({estimated}) AS estimated_cost_usd,
                        {estimated_available} AS estimated_cost_usd_available,
                        SUM({actual}) AS actual_cost_usd,
                        {actual_available} AS actual_cost_usd_available
                    FROM session_model_usage AS u
                    LEFT JOIN sessions AS s ON s."id" = u."session_id"
                    WHERE {where}
                    GROUP BY 1, 2
                    ORDER BY 1, 2
                """
                rows = connection.execute(query, parameters).fetchall()
            elif "sessions" in tables and "started_at" in session_columns:
                estimated, estimated_available = _model_cost_expression(
                    "s", session_columns, "estimated_cost_usd"
                )
                actual, actual_available = _model_cost_expression(
                    "s", session_columns, "actual_cost_usd"
                )
                session_date = "date(s.\"started_at\", 'unixepoch', 'localtime')"
                where = 's."started_at" >= ?'
                parameters = (cutoff,)
                if day is not None:
                    where = f"{session_date} = ?"
                    parameters = (day,)
                model = (
                    "COALESCE(NULLIF(s.\"model\", ''), 'unknown')"
                    if "model" in session_columns
                    else "'unknown'"
                )
                input_tokens = _model_metric_expression("s", session_columns, "input_tokens")
                output_tokens = _model_metric_expression("s", session_columns, "output_tokens")
                query = f"""
                    SELECT
                        {session_date} AS day,
                        {model} AS model,
                        SUM({input_tokens} + {output_tokens}) AS tokens,
                        SUM({input_tokens}) AS input_tokens,
                        SUM({output_tokens}) AS output_tokens,
                        SUM({_model_metric_expression('s', session_columns, 'cache_read_tokens')}) AS cache_read_tokens,
                        SUM({_model_metric_expression('s', session_columns, 'cache_write_tokens')}) AS cache_write_tokens,
                        SUM({_model_metric_expression('s', session_columns, 'reasoning_tokens')}) AS reasoning_tokens,
                        SUM({_model_metric_expression('s', session_columns, 'api_call_count')}) AS api_calls,
                        COUNT(*) AS sessions,
                        SUM({estimated}) AS estimated_cost_usd,
                        {estimated_available} AS estimated_cost_usd_available,
                        SUM({actual}) AS actual_cost_usd,
                        {actual_available} AS actual_cost_usd_available
                    FROM sessions AS s
                    WHERE {where}
                    GROUP BY 1, 2
                    ORDER BY 1, 2
                """
                rows = connection.execute(query, parameters).fetchall()
            else:
                rows = []
        except (OSError, sqlite3.Error):
            rows = []
        finally:
            if connection is not None:
                connection.close()

        for row in rows:
            key = (str(row["day"]), str(row["model"]))
            metric = model_totals.setdefault(
                key,
                {
                    "model": key[1],
                    "tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "api_calls": 0,
                    "sessions": 0,
                    "estimated_cost_usd": None,
                    "actual_cost_usd": None,
                },
            )
            _merge_model_metric(metric, row)

    daily_sessions = {
        item["day"]: item["sessions"]
        for item in load_hermes_history(profiles, days=days, now=now, day=day)
    }
    days_by_name: dict[str, dict[str, Any]] = {}
    for (day_name, _model), metric in sorted(model_totals.items()):
        daily = days_by_name.setdefault(
            day_name,
            {
                "day": day_name,
                "tokens": 0,
                "sessions": daily_sessions.get(day_name, 0),
                "models": [],
            },
        )
        daily["tokens"] += metric["tokens"]
        daily["models"].append(metric)
    for daily in days_by_name.values():
        daily["models"].sort(key=lambda item: (-item["tokens"], item["model"]))
        if not daily["sessions"]:
            daily["sessions"] = sum(item["sessions"] for item in daily["models"])
    return [days_by_name[name] for name in sorted(days_by_name)]


def _usage_colour(percent: float) -> tuple[int, int, int]:
    """Map usage percentage to a smooth green -> yellow -> orange -> red colour."""
    stops = (
        (0.0, (46, 204, 113)),
        (50.0, (255, 193, 7)),
        (80.0, (255, 152, 0)),
        (100.0, (231, 76, 60)),
    )
    value = max(0.0, min(float(percent), 100.0))
    for (start, first), (end, second) in zip(stops, stops[1:]):
        if value <= end:
            ratio = (value - start) / (end - start)
            return tuple(round(a + (b - a) * ratio) for a, b in zip(first, second))
    return stops[-1][1]


def _codex_bar(used_percent: float, *, color: bool, width: int = 40) -> str:
    """Render a single Codex meter; only the used portion receives colour."""
    used = max(0.0, min(float(used_percent), 100.0))
    filled = round(used / 100.0 * width)
    used_chars = "█" * filled
    remaining_chars = "░" * (width - filled)
    if not color or not used_chars:
        return used_chars + remaining_chars
    red, green, blue = _usage_colour(used)
    start = f"\033[38;2;{red};{green};{blue}m"
    return f"{start}{used_chars}\033[0m{remaining_chars}"


def _history_colour(value: float, maximum: float) -> tuple[int, int, int]:
    """Use a blue -> cyan scale for historical volume, not quota severity."""
    ratio = 0.0 if maximum <= 0 else max(0.0, min(float(value) / maximum, 1.0))
    return (59, round(130 + (220 - 130) * ratio), round(246 - (246 - 180) * ratio))


def _history_bar(
    value: float, maximum: float, *, color: bool, width: int = 40
) -> str:
    """Render historical token volume as a proportional bar."""
    bounded_maximum = maximum if maximum > 0 else 1
    filled = round(max(0.0, min(float(value), bounded_maximum)) / bounded_maximum * width)
    used_chars = "█" * filled
    remaining_chars = "░" * (width - filled)
    if not color or not used_chars:
        return used_chars + remaining_chars
    red, green, blue = _history_colour(value, bounded_maximum)
    start = f"\033[38;2;{red};{green};{blue}m"
    return f"{start}{used_chars}\033[0m{remaining_chars}"


_MODEL_PALETTES = (
    ("blue→cyan", ((59, 130, 246), (34, 211, 238))),
    ("green→yellow", ((34, 197, 94), (250, 204, 21))),
    ("violet→pink", ((139, 92, 246), (244, 114, 182))),
    ("orange→red", ((249, 115, 22), (239, 68, 68))),
    ("teal→lime", ((20, 184, 166), (163, 230, 53))),
)


def _model_palette_map(history: list[dict[str, Any]]) -> dict[str, tuple[str, tuple[tuple[int, int, int], ...]]]:
    models = sorted(
        {
            str(model.get("model", "unknown"))
            for item in history
            for model in item.get("models", [])
        }
    )
    return {
        model: _MODEL_PALETTES[index % len(_MODEL_PALETTES)]
        for index, model in enumerate(models)
    }


def _model_segment(count: int, palette: tuple[tuple[int, int, int], ...], *, color: bool) -> str:
    if count <= 0:
        return ""
    blocks = "█" * count
    if not color:
        return blocks
    parts: list[str] = []
    for index, block in enumerate(blocks):
        red, green, blue = _heading_colour(index, count, palette)
        parts.append(f"\033[38;2;{red};{green};{blue}m{block}")
    parts.append("\033[0m")
    return "".join(parts)


def _model_bar(
    models: list[dict[str, Any]],
    maximum: int,
    palettes: dict[str, tuple[str, tuple[tuple[int, int, int], ...]]],
    *,
    color: bool,
    width: int = 40,
) -> str:
    total = sum(int(model.get("tokens", 0) or 0) for model in models)
    if maximum <= 0 or total <= 0:
        return "░" * width
    target = round(total / maximum * width)
    raw_counts = [int(model.get("tokens", 0) or 0) / maximum * width for model in models]
    counts = [int(raw) for raw in raw_counts]
    for index in sorted(range(len(counts)), key=lambda item: raw_counts[item] - counts[item], reverse=True):
        if sum(counts) >= target:
            break
        counts[index] += 1
    segments: list[str] = []
    for model, count in zip(models, counts):
        name = str(model.get("model", "unknown"))
        palette = palettes[name][1]
        segments.append(_model_segment(count, palette, color=color))
    return "".join(segments) + ("░" * max(0, width - sum(counts)))


def _sum_model_metrics(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for item in history:
        for model in item.get("models", []):
            name = str(model.get("model", "unknown"))
            total = totals.setdefault(
                name,
                {
                    "model": name,
                    "tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 0,
                    "api_calls": 0,
                    "sessions": 0,
                    "estimated_cost_usd": None,
                    "actual_cost_usd": None,
                },
            )
            for field in (
                "tokens",
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "api_calls",
                "sessions",
            ):
                total[field] += int(model.get(field, 0) or 0)
            for field in ("estimated_cost_usd", "actual_cost_usd"):
                if model.get(field) is not None:
                    total[field] = (total[field] or 0.0) + float(model[field])
    return sorted(totals.values(), key=lambda item: (-item["tokens"], item["model"]))


def _render_metric_summary(
    history: list[dict[str, Any]],
    *,
    heading: str,
    color: bool,
    palettes: dict[str, tuple[str, tuple[tuple[int, int, int], ...]]] | None = None,
) -> list[str]:
    lines = [heading]
    # Metric rows use their shared position in each section so corresponding
    # rows remain visually aligned even when the two accounting sources report
    # different model sets or sort orders.
    for index, model in enumerate(_sum_model_metrics(history)):
        metrics = [
            f"input {model['input_tokens']:,}",
            f"output {model['output_tokens']:,}",
            f"cache read {model['cache_read_tokens']:,}",
            f"cache write {model['cache_write_tokens']:,}",
            f"reasoning {model['reasoning_tokens']:,}",
            f"{model['sessions']} sessions",
            f"{model['api_calls']} API calls",
        ]
        if model["estimated_cost_usd"] is not None:
            metrics.append(f"estimated ${model['estimated_cost_usd']:.2f}")
        if model["actual_cost_usd"] is not None:
            metrics.append(f"actual ${model['actual_cost_usd']:.2f}")
        model_name = _colour_model_name(
            model["model"], _MODEL_PALETTES[index % len(_MODEL_PALETTES)][1], color=color
        )
        lines.append(f"{model_name}: {model['tokens']:,} tokens • " + " • ".join(metrics))
    return lines


def render_model_chart(
    history: list[dict[str, Any]],
    *,
    color: bool = False,
    title: str | None = None,
    palettes: dict[str, tuple[str, tuple[tuple[int, int, int], ...]]] | None = None,
) -> str:
    """Render cumulative token bars segmented by model plus useful metrics."""
    model_title = title or "Hermes-codex model usage (last 7 days; cumulative model-attributed API tokens)"
    lines = [
        _colour_heading(
            model_title,
            color=color,
            palette=((139, 92, 246), (244, 114, 182)),
        )
    ]
    if not history:
        lines.append("No Hermes-codex model history")
        return "\n".join(lines)
    palettes = palettes or _model_palette_map(history)
    lines.append("Model bars use Hermes per-model API accounting; the chart above uses session totals.")
    maximum = max(int(item.get("tokens", 0) or 0) for item in history) or 1
    for item in history:
        tokens = int(item.get("tokens", 0) or 0)
        sessions = int(item.get("sessions", 0) or 0)
        lines.append(
            f"{item['day']} | "
            f"{_model_bar(item.get('models', []), maximum, palettes, color=color):<40} "
            f"{tokens:,} tokens ({sessions} sessions)"
        )
    lines.append("")
    lines.extend(
        _render_metric_summary(
            history,
            heading="Model metrics (cumulative for this period)",
            color=color,
            palettes=palettes,
        )
    )
    return "\n".join(lines)


def _heading_colour(index: int, length: int, palette: tuple[tuple[int, int, int], ...]) -> tuple[int, int, int]:
    """Interpolate a heading colour across a small, terminal-safe palette."""
    if length <= 1:
        return palette[0]
    position = index / (length - 1)
    segment_count = len(palette) - 1
    segment = min(int(position * segment_count), segment_count - 1)
    local = position * segment_count - segment
    first, second = palette[segment], palette[segment + 1]
    return tuple(round(a + (b - a) * local) for a, b in zip(first, second))


def _colour_model_name(
    text: str,
    palette: tuple[tuple[int, int, int], ...],
    *,
    color: bool,
) -> str:
    if not color:
        return text
    parts: list[str] = []
    for index, character in enumerate(text):
        red, green, blue = _heading_colour(index, len(text), palette)
        parts.append(f"\033[38;2;{red};{green};{blue}m{character}")
    parts.append("\033[0m")
    return "".join(parts)


def _colour_heading(text: str, *, color: bool, palette: tuple[tuple[int, int, int], ...]) -> str:
    """Render bold gradient text on an explicit dark background.

    The explicit background is intentional: a foreground colour alone cannot
    be guaranteed to contrast with both light and dark terminal themes.
    """
    if not color:
        return text
    background = "\033[48;2;15;23;42m"  # controlled navy, independent of terminal theme
    parts = [f"\033[1m{background}"]
    for index, character in enumerate(text):
        red, green, blue = _heading_colour(index, len(text), palette)
        parts.append(f"\033[38;2;{red};{green};{blue}m{character}")
    parts.append("\033[0m")
    return "".join(parts)


def current_local_day() -> str:
    """Return today's calendar date in the local timezone."""
    return datetime.now().astimezone().date().isoformat()


def render_chart(
    report: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    color: bool = False,
    history_title: str | None = None,
    model_history: list[dict[str, Any]] | None = None,
    model_title: str | None = None,
) -> str:
    """Render live Codex, local Hermes and model metric charts."""
    codex_title = _colour_heading(
        "Codex rate-limit (live provider snapshot)",
        color=color,
        palette=((34, 211, 238), (96, 165, 250), (167, 139, 250)),
    )
    hermes_title = _colour_heading(
        history_title or "Hermes-codex local usage (last 7 days; input + output tokens)",
        color=color,
        palette=((96, 165, 250), (129, 140, 248), (244, 114, 182)),
    )
    lines = ["Subscription quota - authoritative provider data", "", codex_title]
    rendered_window = False
    for item in report.get("profiles", []):
        profile_label = ", ".join(item.get("profiles", [])) or "profile"
        if item.get("status") != "ok":
            lines.append(f"{profile_label}: unavailable")
            continue
        for window in item.get("windows", []):
            used = window.get("used_percent")
            if used is None:
                lines.append(f"{profile_label} / {window.get('label', 'Window')}: unavailable")
                rendered_window = True
                continue
            used_value = max(0.0, min(float(used), 100.0))
            label = window.get("label", "Window")
            remaining = 100.0 - used_value
            lines.append(f"{profile_label} / {label} [{_codex_bar(used_value, color=color)}]")
            text = f"  {used_value:g}% used · {remaining:g}% remaining"
            reset = _format_reset(window.get("reset_at"))
            if reset:
                text += f" • resets {reset}"
            lines.append(text)
            rendered_window = True
    if not rendered_window and all(item.get("status") == "ok" for item in report.get("profiles", [])):
        lines.append("unavailable (no Codex rate-limit window returned)")

    lines.append("")
    lines.append("Local Hermes telemetry - not an authoritative subscription usage total.")
    lines.append("")
    lines.append(hermes_title)
    palettes = _model_palette_map(history + (model_history or []))
    if not history:
        lines.append("No Hermes-codex history")
    else:
        maximum = max(int(item.get("tokens", 0) or 0) for item in history) or 1
        for item in history:
            tokens = int(item.get("tokens", 0) or 0)
            sessions = int(item.get("sessions", 0) or 0)
            if item.get("models"):
                bar = _model_bar(item["models"], maximum, palettes, color=color)
            else:
                bar = _history_bar(tokens, maximum, color=color)
            lines.append(
                f"{item['day']} | "
                f"{bar:<40} "
                f"{tokens:,} tokens ({sessions} sessions)"
            )

    if any(item.get("models") for item in history):
        lines.append("")
        lines.extend(
            _render_metric_summary(
                history,
                heading="Session metrics (cumulative for this period)",
                color=color,
                palettes=palettes,
            )
        )

    lines.append("")
    lines.extend(
        render_model_chart(
            model_history or [],
            color=color,
            title=model_title,
            palettes=palettes,
        ).splitlines()
    )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hermes-codex-usage",
        description="Fetch live OpenAI Codex account usage for Hermes profiles.",
    )
    parser.add_argument("--profile", "-p", metavar="NAME", help="query only this Hermes profile")
    period = parser.add_mutually_exclusive_group()
    period.add_argument(
        "--today",
        dest="today",
        action="store_true",
        help="show today's charts only; Codex has no calendar-day total",
    )
    period.add_argument(
        "--week",
        action="store_const",
        const="week",
        dest="period",
        help="show the Codex weekly rate-limit allowance",
    )

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    output.add_argument(
        "--chart",
        action="store_true",
        help="show the live Codex quota and coloured seven-day Hermes-codex charts (use --no-color for plain text)",
    )
    colour = parser.add_mutually_exclusive_group()
    colour.add_argument(
        "--color",
        dest="color",
        action="store_true",
        default=None,
        help="force ANSI colours in --chart output",
    )
    colour.add_argument(
        "--no-color",
        dest="color",
        action="store_false",
        help="disable ANSI colours in --chart output",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    period = "today" if args.today else args.period
    try:
        profiles = select_profiles(discover_profiles(), args.profile)
    except ProfileSelectionError as exc:
        print(f"hermes-codex-usage: {exc}", file=sys.stderr)
        return 2
    if not profiles:
        message = f"No Hermes profiles found under {resolve_hermes_root()}"
        if args.json:
            print(json.dumps({"generated_at": datetime.now(timezone.utc).isoformat(), "profiles": [], "error": message}))
        else:
            print(message, file=sys.stderr)
        return 2

    items: list[dict[str, Any]] = []
    for profile in profiles:
        try:
            snapshot = fetch_profile_snapshot(profile)
        except Exception as exc:
            snapshot = {"status": "unavailable", "error": str(exc)}
        items.append(_normalise_item(profile, snapshot, period))

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "filter": period,
        "profiles": group_items(items),
    }
    selected_day = current_local_day() if args.today else None
    full_output = (
        not args.profile
        and period is None
        and not args.json
        and not args.chart
    )
    show_charts = args.today or args.chart or full_output
    if args.today:
        history = load_hermes_history(profiles, day=selected_day)
    else:
        history = load_hermes_history(profiles)
    model_history = (
        load_hermes_model_history(profiles, day=selected_day)
        if args.today
        else load_hermes_model_history(profiles)
        if show_charts
        else []
    )
    chart_day_title = (
        f"Hermes-codex local usage for today ({selected_day})" if selected_day else None
    )
    model_day_title = (
        f"Hermes-codex model usage for today ({selected_day}; cumulative model-attributed API tokens)"
        if selected_day
        else None
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    elif args.chart:
        # Charts are intended to be consumed visually, including through the
        # WebUI/pipe that launches this CLI. Keep ANSI colour on by default;
        # --no-color is the explicit machine/logging escape hatch.
        use_color = True if args.color is None else args.color
        print(
            render_chart(
                report,
                history,
                color=use_color,
                history_title=chart_day_title,
                model_history=model_history,
                model_title=model_day_title,
            )
        )
    elif args.today or full_output:
        use_color = True if args.color is None else args.color
        print(
            render_chart(
                report,
                history,
                color=use_color,
                history_title=chart_day_title,
                model_history=model_history,
                model_title=model_day_title,
            )
        )
    else:
        print(render_text(report))
    return 0 if all(item.get("status") == "ok" for item in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
