# hermes-codex-usage

A standalone, manually-run CLI for the live OpenAI Codex account limits available
through Hermes profiles.

## See it first

Run `hermes-codex-usage` to see a live Codex rate-limit snapshot alongside the
local Hermes-codex usage history. Charts are the default output, with colour
enabled automatically. This representative output uses illustrative data so
the shape of both charts is clear:

```text
[bold cyan → violet on navy] Codex rate-limit (live provider snapshot)
default / Weekly [█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  3% used · 97% remaining • resets 2026-09-11 23:13 BST

[bold blue → pink on navy] Hermes-codex local usage (last 7 days; input + output tokens)
2026-09-01 | ██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 6,483,373 tokens (7 sessions)
2026-09-02 | ███████████████░░░░░░░░░░░░░░░░░░░░░░░ 15,983,696 tokens (10 sessions)
2026-09-03 | █████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 9,174,220 tokens (6 sessions)
2026-09-04 | ████████████████████░░░░░░░░░░░░░░░░░░░░ 21,604,885 tokens (13 sessions)
2026-09-05 | ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 12,738,441 tokens (8 sessions)
2026-09-06 | ████████████████████████████████████████ 42,579,780 tokens (16 sessions)
2026-09-07 | ██████████████████████████░░░░░░░░░░░░░░ 27,318,904 tokens (11 sessions)
```

The headings are shown with symbolic markers because GitHub does not render
ANSI escape colours inside Markdown; the command emits the real bold,
gradient ANSI sequences. The Codex percentage and reset information appear on
the line immediately after the bar. Because this is a direct CLI report, it
does not invoke an LLM to compose an answer or consume model tokens, unlike
asking a skill to generate the same report.

## Requirements and installation

- Python 3.10 or newer;
- a working Hermes Agent installation, with its `hermes` launcher available on
  `PATH`;
- an authenticated Hermes `openai-codex` provider for the profiles you want to
  query.

From a checkout:

```bash
python -m venv .venv
.venv/bin/python -m pip install .
hermes-codex-usage --chart
```

The utility does not store credentials. Hermes resolves them from the active
profile's normal credential sources, and the repository deliberately excludes
`.env`, `auth.json`, SQLite databases, session transcripts and runtime logs.

## Usage

```text
hermes-codex-usage
hermes-codex-usage --profile profile-a
hermes-codex-usage --today
hermes-codex-usage --week
hermes-codex-usage --chart
hermes-codex-usage --json
```

With no `--profile`, the command discovers the default Hermes home and every
valid named profile below `profiles/`, then queries each profile independently.
Each query runs in a short-lived Hermes Python subprocess with that profile's
`HERMES_HOME`, so credentials and provider state are resolved afresh on every
execution. It does not run as a daemon, background service, or scheduled job,
and it does not modify Hermes application code.

`--today` shows the charts for today's calendar date only. Hermes
history is filtered by the local calendar date. For Codex, the command checks
the live status now/today; if Codex returns a real `Daily` window, it selects
it. It does **not** invent a calendar-day usage total when that window is
absent: the current API response has only a weekly window, so the command
shows that live window and says so explicitly.
`--week` means "show the weekly allowance" and selects the `Weekly` window when
the provider returns one. If the API does not return a weekly window, the
command says so explicitly. Window labels are derived from the provider's
`limit_window_seconds` field rather than assuming that `primary_window` means
session and `secondary_window` means weekly.

When multiple profiles resolve the exact same authentication token, their
results are collapsed into one block, for example:

```text
Profiles: default, profile-a, profile-b (shared between default, profile-a, profile-b)
```

The token itself and its fingerprint are never printed.

## ASCII charts

`--chart` prints two separate charts:

1. **Codex rate-limit** — a percentage meter for each provider window. The
   used portion changes smoothly from green through yellow and orange to red
   as the quota is consumed; the remainder is shown in the same bar. The
   percentage and reset details are printed on the line immediately below the
   bar.
2. **Hermes-codex local usage** — a historical volume chart, with one
   proportional bar per day for `input_tokens + output_tokens`. It uses a
   separate blue-to-cyan scale because this chart measures volume, not quota
   danger. The session count is shown beside each bar.

Colours are enabled by default for `--chart`, including captured output. Use
`--no-color` for plain text logs, or `--color` to make the choice explicit.
The two chart headings are bold and use separate gradients on a controlled
dark-navy background: cyan-to-violet for Codex and blue-to-pink for
Hermes-codex. The explicit background keeps the headings readable on both
light and dark terminal themes.

Neither chart changes the meaning of the underlying data: the first is a live
provider snapshot and the second is local Hermes accounting. The Codex chart
is not a retroactive quota history.

The executable is installed at `~/.local/bin/hermes-codex-usage`.

When the command is run without a period or output selector, it shows the
complete seven-day/live charts. The chart labels include the percentages,
resets, dates, token totals and session counts, so no separate text report is
printed. `--chart` remains the charts-only form (equivalent to the default
visual output), while `--today` shows only today's charts.
