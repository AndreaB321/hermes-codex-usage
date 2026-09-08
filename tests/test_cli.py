import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import sys

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

import hermes_codex_usage as cli


def _strip_ansi(text):
    return re.sub(r"\033\[[0-9;]*m", "", text)


@pytest.fixture
def profile_tree(tmp_path):
    root = tmp_path / ".hermes"
    root.mkdir()
    (root / "profiles" / "profile-a").mkdir(parents=True)
    (root / "profiles" / "profile-b").mkdir()
    (root / "profiles" / "not-valid!").mkdir()
    (root / "profiles" / "empty-file").write_text("not a directory")
    return root


def test_discover_profiles_returns_default_and_valid_named_profiles(profile_tree):
    profiles = cli.discover_profiles(profile_tree)

    assert [(item.name, item.home) for item in profiles] == [
        ("default", profile_tree),
        ("profile-a", profile_tree / "profiles" / "profile-a"),
        ("profile-b", profile_tree / "profiles" / "profile-b"),
    ]


def test_select_profiles_rejects_unknown_profile(profile_tree):
    with pytest.raises(cli.ProfileSelectionError, match="does not exist"):
        cli.select_profiles(cli.discover_profiles(profile_tree), "missing")


def test_codex_windows_use_provider_reported_duration_not_primary_secondary_position():
    windows = cli.normalise_codex_windows([
        {
            "used_percent": 2,
            "reset_at": "2026-09-11T22:13:58+00:00",
            "limit_window_seconds": 604800,
        },
        None,
    ])

    assert [window["label"] for window in windows] == ["Weekly"]
    assert windows[0]["limit_window_seconds"] == 604800


def test_filter_snapshot_week_selects_weekly_window_and_reports_missing_data():
    snapshot = {
        "provider": "openai-codex",
        "plan": "Plus",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "windows": [
            {"label": "Weekly", "used_percent": 5.0, "reset_at": None, "detail": None},
        ],
        "details": [],
    }

    filtered = cli.filter_snapshot(snapshot, "week")

    assert [window["label"] for window in filtered["windows"]] == ["Weekly"]
    assert filtered["filter"] == "week"
    assert "weekly" in filtered["filter_note"]


def test_filter_snapshot_today_does_not_claim_calendar_day_total():
    snapshot = {
        "provider": "openai-codex",
        "plan": "Plus",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "windows": [
            {"label": "Weekly", "used_percent": 2.0, "reset_at": None, "detail": None},
        ],
        "details": [],
    }

    filtered = cli.filter_snapshot(snapshot, "today")

    assert [window["label"] for window in filtered["windows"]] == ["Weekly"]
    assert "calendar-day" in filtered["filter_note"]


def test_filter_snapshot_today_prefers_a_provider_daily_window_when_available():
    snapshot = {
        "provider": "openai-codex",
        "plan": "Plus",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "windows": [
            {"label": "Daily", "used_percent": 10.0, "reset_at": None},
            {"label": "Weekly", "used_percent": 2.0, "reset_at": None},
        ],
        "details": [],
    }

    filtered = cli.filter_snapshot(snapshot, "today")

    assert [window["label"] for window in filtered["windows"]] == ["Daily"]
    assert filtered["scope"] == "daily rate-limit window"


def test_format_reset_accepts_codex_unix_timestamp():
    assert cli._format_reset(1789164838) == "2026-09-11 23:13 BST"


def test_group_items_marks_profiles_with_the_same_authentication_token():
    items = [
        {"profile": "default", "status": "ok", "auth_fingerprint": "same", "plan": "Pro"},
        {"profile": "profile-a", "status": "ok", "auth_fingerprint": "same", "plan": "Pro"},
        {"profile": "profile-b", "status": "ok", "auth_fingerprint": "different", "plan": "Pro"},
    ]

    grouped = cli.group_items(items)

    assert grouped[0]["profiles"] == ["default", "profile-a"]
    assert grouped[0]["shared_between"] == ["default", "profile-a"]
    assert "auth_fingerprint" not in grouped[0]
    assert grouped[1]["profiles"] == ["profile-b"]
    assert "shared_between" not in grouped[1]


def test_render_text_puts_shared_profiles_on_the_first_line():
    report = {
        "profiles": [
            {
                "profiles": ["default", "profile-a"],
                "shared_between": ["default", "profile-a"],
                "status": "ok",
                "provider": "openai-codex",
                "plan": "Pro",
                "windows": [],
                "details": [],
                "filter": "today",
                "filter_note": "today is not a calendar-day total",
            }
        ]
    }

    assert cli.render_text(report).splitlines()[0] == (
        "Profiles: default, profile-a (shared between default, profile-a)"
    )


def test_main_json_reports_each_profile_and_returns_success(monkeypatch, profile_tree, capsys):
    snapshots = {
        "default": {
            "provider": "openai-codex",
            "plan": "Plus",
            "fetched_at": "2026-09-06T10:00:00+00:00",
            "windows": [],
            "details": [],
        },
        "profile-a": {
            "provider": "openai-codex",
            "plan": "Pro",
            "fetched_at": "2026-09-06T10:00:01+00:00",
            "windows": [],
            "details": [],
        },
        "profile-b": {
            "provider": "openai-codex",
            "plan": "Plus",
            "fetched_at": "2026-09-06T10:00:02+00:00",
            "windows": [],
            "details": [],
        },
    }
    monkeypatch.setattr(cli, "resolve_hermes_root", lambda: profile_tree)
    monkeypatch.setattr(cli, "fetch_profile_snapshot", lambda profile, **_: snapshots[profile.name])

    assert cli.main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert [item["profiles"] for item in payload["profiles"]] == [
        ["default"],
        ["profile-a"],
        ["profile-b"],
    ]
    assert payload["profiles"][1]["plan"] == "Pro"


def test_main_profile_flag_limits_fetch(monkeypatch, profile_tree, capsys):
    fetched = []
    monkeypatch.setattr(cli, "resolve_hermes_root", lambda: profile_tree)
    monkeypatch.setattr(
        cli,
        "fetch_profile_snapshot",
        lambda profile, **_: fetched.append(profile.name)
        or {
            "provider": "openai-codex",
            "plan": "Plus",
            "fetched_at": "2026-09-06T10:00:00+00:00",
            "windows": [],
            "details": [],
        },
    )

    assert cli.main(["--profile", "profile-a", "--json"]) == 0
    json.loads(capsys.readouterr().out)
    assert fetched == ["profile-a"]


def _create_usage_db(home, rows):
    connection = sqlite3.connect(home / "state.db")
    connection.execute(
        """CREATE TABLE sessions (
            started_at REAL NOT NULL,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0
        )"""
    )
    connection.executemany(
        "INSERT INTO sessions(started_at, input_tokens, output_tokens) VALUES (?, ?, ?)",
        rows,
    )
    connection.commit()
    connection.close()


def test_load_hermes_history_aggregates_selected_profiles(tmp_path):
    default_home = tmp_path / "default"
    named_home = tmp_path / "profile-a"
    default_home.mkdir()
    named_home.mkdir()
    _create_usage_db(default_home, [(1725580800, 100, 20)])
    _create_usage_db(named_home, [(1725580800, 30, 4)])

    history = cli.load_hermes_history(
        [
            cli.Profile("default", default_home),
            cli.Profile("profile-a", named_home),
        ],
        days=7,
        now=1725667200,
    )

    assert history == [{"day": "2024-09-06", "tokens": 154, "sessions": 2}]


def test_load_hermes_history_includes_session_model_metrics(tmp_path):
    home = tmp_path / "default"
    home.mkdir()
    connection = sqlite3.connect(home / "state.db")
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            api_call_count INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL
        );
        INSERT INTO sessions VALUES
            ('one', 1725580800, 'model-a', 100, 20, 10, 2, 5, 3, 0.01, NULL),
            ('two', 1725580800, 'model-b', 30, 4, 6, 1, 2, 1, 0.02, 0.03);
        """
    )
    connection.commit()
    connection.close()

    history = cli.load_hermes_history(
        [cli.Profile("default", home)],
        days=7,
        now=1725667200,
    )

    assert history == [
        {
            "day": "2024-09-06",
            "tokens": 154,
            "sessions": 2,
            "models": [
                {
                    "model": "model-a",
                    "tokens": 120,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 2,
                    "reasoning_tokens": 5,
                    "api_calls": 3,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": None,
                    "sessions": 1,
                },
                {
                    "model": "model-b",
                    "tokens": 34,
                    "input_tokens": 30,
                    "output_tokens": 4,
                    "cache_read_tokens": 6,
                    "cache_write_tokens": 1,
                    "reasoning_tokens": 2,
                    "api_calls": 1,
                    "estimated_cost_usd": 0.02,
                    "actual_cost_usd": 0.03,
                    "sessions": 1,
                },
            ],
        }
    ]


def test_load_hermes_model_history_aggregates_metrics_by_day_and_model(tmp_path):
    home = tmp_path / "default"
    home.mkdir()
    connection = sqlite3.connect(home / "state.db")
    connection.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            started_at REAL NOT NULL,
            model TEXT
        );
        CREATE TABLE session_model_usage (
            session_id TEXT,
            model TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_read_tokens INTEGER,
            cache_write_tokens INTEGER,
            reasoning_tokens INTEGER,
            api_call_count INTEGER,
            estimated_cost_usd REAL,
            actual_cost_usd REAL
        );
        INSERT INTO sessions(id, started_at, model) VALUES
            ('one', 1725580800, 'model-a'),
            ('two', 1725580800, 'model-b');
        INSERT INTO session_model_usage VALUES
            ('one', 'model-a', 100, 20, 10, 2, 5, 3, 0.01, NULL),
            ('two', 'model-b', 30, 4, 6, 1, 2, 1, 0.02, 0.03);
        """
    )
    connection.commit()
    connection.close()

    history = cli.load_hermes_model_history(
        [cli.Profile("default", home)],
        days=7,
        now=1725667200,
    )

    assert history == [
        {
            "day": "2024-09-06",
            "tokens": 154,
            "sessions": 2,
            "models": [
                {
                    "model": "model-a",
                    "tokens": 120,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 2,
                    "reasoning_tokens": 5,
                    "api_calls": 3,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": None,
                    "sessions": 1,
                },
                {
                    "model": "model-b",
                    "tokens": 34,
                    "input_tokens": 30,
                    "output_tokens": 4,
                    "cache_read_tokens": 6,
                    "cache_write_tokens": 1,
                    "reasoning_tokens": 2,
                    "api_calls": 1,
                    "estimated_cost_usd": 0.02,
                    "actual_cost_usd": 0.03,
                    "sessions": 1,
                },
            ],
        }
    ]


def test_render_chart_contains_separate_codex_and_hermes_graphs():
    report = {
        "profiles": [
            {
                "profiles": ["default"],
                "status": "ok",
                "windows": [
                    {"label": "Weekly", "used_percent": 2.0, "reset_at": None}
                ],
            }
        ]
    }

    chart = cli.render_chart(
        report,
        [{"day": "2026-09-06", "tokens": 100, "sessions": 1}],
        model_history=[
            {
                "day": "2026-09-06",
                "tokens": 80,
                "sessions": 1,
                "models": [{"model": "model-a", "tokens": 80, "sessions": 1}],
            }
        ],
    )

    assert "Codex rate-limit" in chart
    assert "Weekly" in chart
    assert "used" in chart
    assert "remaining" in chart
    assert chart.count("Weekly") == 1
    assert "Hermes-codex local usage" in chart
    assert "2026-09-06" in chart
    assert "100 tokens" in chart
    assert "Hermes-codex model usage" in chart
    assert chart.index("Hermes-codex model usage") > chart.index("Hermes-codex local usage")


def test_render_chart_adds_coloured_session_metrics_after_local_usage():
    report = {"profiles": []}
    history = [
        {
            "day": "2026-09-06",
            "tokens": 154,
            "sessions": 2,
            "models": [
                {
                    "model": "model-a",
                    "tokens": 120,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_tokens": 10,
                    "cache_write_tokens": 2,
                    "reasoning_tokens": 5,
                    "api_calls": 3,
                    "estimated_cost_usd": 0.01,
                    "actual_cost_usd": None,
                    "sessions": 1,
                },
                {
                    "model": "model-b",
                    "tokens": 34,
                    "input_tokens": 30,
                    "output_tokens": 4,
                    "cache_read_tokens": 6,
                    "cache_write_tokens": 1,
                    "reasoning_tokens": 2,
                    "api_calls": 1,
                    "estimated_cost_usd": 0.02,
                    "actual_cost_usd": 0.03,
                    "sessions": 1,
                },
            ],
        }
    ]

    chart = cli.render_chart(report, history, color=True, model_history=[])
    visible = _strip_ansi(chart)

    assert "Session metrics (cumulative for this period)" in visible
    assert "model-a: 120 tokens • input 100 • output 20" in visible
    assert "model-b: 34 tokens • input 30 • output 4" in visible
    assert "\x1b[38;2;59;130;246mm" in chart
    local_line = next(line for line in chart.splitlines() if "2026-09-06 |" in line)
    assert "\x1b[38;2;59;130;246m" in local_line  # model-a: blue → cyan
    assert "\x1b[38;2;34;197;94m" in local_line  # model-b: green → yellow
    assert visible.index("Session metrics") < visible.index("Hermes-codex model usage")


def test_render_chart_shares_model_palette_between_usage_sections():
    chart = cli.render_chart(
        {"profiles": []},
        [
            {
                "day": "2026-09-08",
                "tokens": 100,
                "sessions": 1,
                "models": [{"model": "model-z", "tokens": 100, "sessions": 1}],
            }
        ],
        color=True,
        model_history=[
            {
                "day": "2026-09-08",
                "tokens": 100,
                "sessions": 2,
                "models": [
                    {"model": "model-a", "tokens": 50, "sessions": 1},
                    {"model": "model-z", "tokens": 50, "sessions": 1},
                ],
            }
        ],
    )

    daily_lines = [line for line in chart.splitlines() if "2026-09-08 |" in line]
    assert len(daily_lines) == 2
    assert "\x1b[38;2;34;197;94m" in daily_lines[0]  # model-z: green → yellow
    assert "\x1b[38;2;34;197;94m" in daily_lines[1]


def test_render_chart_shares_metric_row_colours_between_usage_sections():
    history = [
        {
            "day": f"2026-09-0{index}",
            "tokens": (5 - index) * 100,
            "sessions": 1,
            "models": [{"model": f"session-{index}", "tokens": (5 - index) * 100, "sessions": 1}],
        }
        for index in range(1, 5)
    ]
    model_history = [
        {
            "day": f"2026-09-0{index}",
            "tokens": (5 - index) * 100,
            "sessions": 1,
            "models": [{"model": f"model-{index}", "tokens": (5 - index) * 100, "sessions": 1}],
        }
        for index in range(1, 5)
    ]

    chart = cli.render_chart(
        {"profiles": []},
        history,
        color=True,
        model_history=model_history,
    )
    raw_lines = chart.splitlines()
    visible_lines = [_strip_ansi(line) for line in raw_lines]
    session_start = visible_lines.index("Session metrics (cumulative for this period)") + 1
    model_start = visible_lines.index("Model metrics (cumulative for this period)") + 1

    assert raw_lines[session_start + 3].startswith("\x1b[38;2;249;115;22m")
    assert raw_lines[model_start + 3].startswith("\x1b[38;2;249;115;22m")


def test_render_model_chart_uses_cumulative_bars_and_model_segments():
    model_history = [
        {
            "day": "2026-09-06",
            "tokens": 120,
            "sessions": 2,
            "models": [
                {"model": "model-a", "tokens": 80, "sessions": 1},
                {"model": "model-b", "tokens": 40, "sessions": 1},
            ],
        }
    ]

    chart = cli.render_model_chart(model_history, color=True)
    visible = _strip_ansi(chart)

    assert "Hermes-codex model usage" in visible
    assert "2026-09-06 |" in visible
    assert "120 tokens (2 sessions)" in visible
    assert "model-a" in visible
    assert "model-b" in visible
    assert chart.count("\x1b[38;2;") >= 4
    assert "\x1b[38;2;59;130;246mm" in chart
    title_line = next(
        line for line in chart.splitlines() if "Hermes-codex model usage" in _strip_ansi(line)
    )
    assert title_line.startswith(
        "\x1b[1m\x1b[48;2;15;23;42m\x1b[38;2;139;92;246mH"
    )
    assert "\x1b[38;2;244;114;182m" in title_line


def test_codex_chart_puts_percentage_and_reset_details_on_line_after_bar():
    report = {
        "profiles": [
            {
                "profiles": ["default"],
                "status": "ok",
                "windows": [
                    {
                        "label": "Weekly",
                        "used_percent": 3.0,
                        "reset_at": "2026-09-11T22:13:58+00:00",
                    }
                ],
            }
        ]
    }

    chart = cli.render_chart(report, [], color=False)
    lines = chart.splitlines()

    bar_index = next(index for index, line in enumerate(lines) if line.startswith("default / Weekly ["))
    assert lines[bar_index] == "default / Weekly [█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]"
    assert lines[bar_index + 1] == (
        "  3% used · 97% remaining • resets 2026-09-11 23:13 BST"
    )


def test_render_chart_can_colour_usage_bar_with_green_to_red_gradient():
    report = {
        "profiles": [
            {
                "profiles": ["default"],
                "status": "ok",
                "windows": [
                    {"label": "Weekly", "used_percent": 2.0, "reset_at": None},
                    {"label": "Session", "used_percent": 100.0, "reset_at": None},
                ],
            }
        ]
    }

    chart = cli.render_chart(report, [], color=True)

    assert "\x1b[38;2;54;204;109m" in chart  # low usage: green shade
    assert "\x1b[38;2;231;76;60m" in chart  # high usage: red
    assert chart.count("\x1b[0m") >= 2


def test_chart_titles_are_bold_gradient_text_on_an_explicit_contrasting_background():
    chart = cli.render_chart(
        {
            "profiles": [
                {
                    "profiles": ["default"],
                    "status": "ok",
                    "windows": [{"label": "Weekly", "used_percent": 2.0}],
                }
            ]
        },
        [],
        color=True,
    )

    assert "\033[1m\033[48;2;15;23;42m" in chart
    assert chart.count("\033[38;2;") >= 4
    assert chart.count("\033[0m") >= 2
    visible = _strip_ansi(chart)
    assert "Codex rate-limit" in visible
    assert "Hermes-codex local usage" in visible


def test_chart_titles_have_no_ansi_when_colour_is_disabled():
    chart = cli.render_chart({"profiles": []}, [], color=False)

    assert chart.startswith(
        "Subscription quota - authoritative provider data\n\n"
        "Codex rate-limit (live provider snapshot)"
    )
    assert "Local Hermes telemetry - not an authoritative subscription usage total." in chart
    assert chart.index("Codex rate-limit") < chart.index("Local Hermes telemetry")
    assert "Hermes-codex local usage (last 7 days; input + output tokens)" in chart
    assert "\033[" not in chart


def test_render_chart_colours_hermes_history_as_a_volume_chart():
    report = {"profiles": []}

    chart = cli.render_chart(
        report,
        [
            {"day": "2026-09-05", "tokens": 100, "sessions": 2},
            {"day": "2026-09-06", "tokens": 200, "sessions": 3},
        ],
        color=True,
    )

    history_lines = [line for line in chart.splitlines() if "tokens (" in line]
    assert len(history_lines) == 2
    assert all("\x1b[38;2;" in line for line in history_lines)
    assert "Hermes-codex local usage (last 7 days; input + output tokens)" in _strip_ansi(chart)


def test_chart_defaults_to_colour_even_when_stdout_is_not_a_tty(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_hermes_root", lambda: Path("/tmp/unused"))
    monkeypatch.setattr(cli, "discover_profiles", lambda: [cli.Profile("default", Path("/tmp"))])
    monkeypatch.setattr(
        cli,
        "fetch_profile_snapshot",
        lambda profile, **_: {
            "provider": "openai-codex",
            "windows": [{"used_percent": 20.0, "limit_window_seconds": 604800}],
            "details": [],
        },
    )
    monkeypatch.setattr(cli, "load_hermes_history", lambda profiles, **_: [])

    assert cli.main(["--chart"]) == 0
    assert "\x1b[38;2;" in capsys.readouterr().out


def test_render_chart_is_plain_text_when_colour_is_disabled():
    report = {
        "profiles": [
            {
                "profiles": ["default"],
                "status": "ok",
                "windows": [{"label": "Weekly", "used_percent": 2.0}],
            }
        ]
    }

    chart = cli.render_chart(report, [], color=False)

    assert "\x1b[" not in chart


def test_render_chart_marks_missing_codex_window_and_empty_history():
    report = {"profiles": [{"profiles": ["default"], "status": "ok", "windows": []}]}

    chart = cli.render_chart(report, [])

    assert "Codex rate-limit" in chart
    assert "unavailable" in chart
    assert "No Hermes-codex history" in chart


def test_today_option_selects_today():
    args = cli.build_parser().parse_args(["--today"])

    assert args.today is True


def test_today_main_shows_today_chart_without_duplicate_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_profiles", lambda: [cli.Profile("default", Path("/tmp"))])
    monkeypatch.setattr(cli, "current_local_day", lambda: "2026-09-06")
    monkeypatch.setattr(
        cli,
        "fetch_profile_snapshot",
        lambda profile, **_: {
            "provider": "openai-codex",
            "windows": [{"used_percent": 20.0, "limit_window_seconds": 604800}],
            "details": [],
        },
    )
    monkeypatch.setattr(
        cli,
        "load_hermes_history",
        lambda profiles, **kwargs: (
            [{"day": "2026-09-06", "tokens": 100, "sessions": 2}]
            if kwargs.get("day") == "2026-09-06"
            else []
        ),
    )

    assert cli.main(["--today", "--no-color"]) == 0
    output = capsys.readouterr().out

    assert "Codex rate-limit" in output
    assert "Hermes-codex local usage for today (2026-09-06)" in output
    assert "2026-09-06 |" in output
    assert "Provider:" not in output
    assert "Scope:" not in output
    assert "across 2 sessions" not in output
    assert "\x1b[" not in output


def test_no_arguments_shows_all_charts_without_duplicate_text(monkeypatch, capsys):
    monkeypatch.setattr(cli, "discover_profiles", lambda: [cli.Profile("default", Path("/tmp"))])
    monkeypatch.setattr(
        cli,
        "fetch_profile_snapshot",
        lambda profile, **_: {
            "provider": "openai-codex",
            "windows": [{"used_percent": 20.0, "limit_window_seconds": 604800}],
            "details": [],
        },
    )
    monkeypatch.setattr(
        cli,
        "load_hermes_history",
        lambda profiles, **kwargs: [
            {"day": "2026-09-05", "tokens": 50, "sessions": 1},
            {"day": "2026-09-06", "tokens": 100, "sessions": 2},
        ],
    )

    assert cli.main(["--no-color"]) == 0
    output = capsys.readouterr().out

    assert "Codex rate-limit" in output
    assert "Hermes-codex local usage" in output
    assert "2026-09-05 |" in output
    assert "2026-09-06 |" in output
    assert "Provider:" not in output
    assert "usage history" not in output
    assert "across 1 session" not in output
