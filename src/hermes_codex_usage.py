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
    totals: dict[str, dict[str, int]] = {}
    for profile in profiles:
        database = profile.home / "state.db"
        if not database.is_file():
            continue
        try:
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
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
        for day, tokens, sessions in rows:
            item = totals.setdefault(str(day), {"tokens": 0, "sessions": 0})
            item["tokens"] += int(tokens or 0)
            item["sessions"] += int(sessions or 0)
    return [
        {"day": day, **totals[day]}
        for day in sorted(totals)
    ]


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
) -> str:
    """Render separate live Codex and local Hermes-codex ASCII charts."""
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
    lines = [codex_title]
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
    lines.append(hermes_title)
    if not history:
        lines.append("No Hermes-codex history")
        return "\n".join(lines)
    maximum = max(int(item.get("tokens", 0) or 0) for item in history) or 1
    for item in history:
        tokens = int(item.get("tokens", 0) or 0)
        sessions = int(item.get("sessions", 0) or 0)
        lines.append(
            f"{item['day']} | "
            f"{_history_bar(tokens, maximum, color=color):<40} "
            f"{tokens:,} tokens ({sessions} sessions)"
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
    if args.today:
        history = load_hermes_history(profiles, day=selected_day)
    else:
        history = load_hermes_history(profiles)
    chart_day_title = (
        f"Hermes-codex local usage for today ({selected_day})" if selected_day else None
    )
    full_output = (
        not args.profile
        and period is None
        and not args.json
        and not args.chart
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    elif args.chart:
        # Charts are intended to be consumed visually, including through the
        # WebUI/pipe that launches this CLI. Keep ANSI colour on by default;
        # --no-color is the explicit machine/logging escape hatch.
        use_color = True if args.color is None else args.color
        print(render_chart(report, history, color=use_color, history_title=chart_day_title))
    elif args.today or full_output:
        use_color = True if args.color is None else args.color
        print(render_chart(report, history, color=use_color, history_title=chart_day_title))
    else:
        print(render_text(report))
    return 0 if all(item.get("status") == "ok" for item in items) else 1


if __name__ == "__main__":
    raise SystemExit(main())
