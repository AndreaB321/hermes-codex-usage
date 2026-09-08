# hermes-codex-usage

A standalone, manually-run CLI for the live OpenAI Codex account limits available
through Hermes profiles.

## See it first

Run `hermes-codex-usage` to see a live Codex rate-limit snapshot alongside the
local Hermes-codex usage history. Charts are the default output, with colour
enabled automatically. This representative output uses illustrative data so
the shape of the charts is clear:

```text
Subscription quota - authoritative provider data

[bold cyan → violet on navy] Codex rate-limit (live provider snapshot)
default / Weekly [█░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░]
  3% used · 97% remaining • resets 2026-09-11 23:13 BST

Local Hermes telemetry - not an authoritative subscription usage total.

[bold blue → pink on navy] Hermes-codex local usage (last 7 days; input + output tokens)
2026-09-01 | ██████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 6,483,373 tokens (7 sessions)
2026-09-02 | ███████████████░░░░░░░░░░░░░░░░░░░░░░░ 15,983,696 tokens (10 sessions)
2026-09-03 | █████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 9,174,220 tokens (6 sessions)
2026-09-04 | ████████████████████░░░░░░░░░░░░░░░░░░░░ 21,604,885 tokens (13 sessions)
2026-09-05 | ████████████░░░░░░░░░░░░░░░░░░░░░░░░░░░░ 12,738,441 tokens (8 sessions)
2026-09-06 | ████████████████████████████████████████ 42,579,780 tokens (16 sessions)
2026-09-07 | ██████████████████████████░░░░░░░░░░░░░░ 27,318,904 tokens (11 sessions)

Session metrics (cumulative for this period)
model-a: 54,000 tokens • input 45,000 • output 9,000 • cache read 24,000 • cache write 0 • reasoning 4,000 • 20 sessions • 120 API calls
model-b: 16,000 tokens • input 12,000 • output 4,000 • cache read 7,000 • cache write 0 • reasoning 1,000 • 7 sessions • 32 API calls

[bold violet → pink on navy] Hermes-codex model usage (cumulative model-attributed API tokens)
Model bars use Hermes per-model API accounting; the chart above uses session totals.
2026-09-06 | ████████████████████████████████████████ 34,000 tokens (16 sessions)
2026-09-07 | ██████████████████████████░░░░░░░░░░░░░░ 22,000 tokens (11 sessions)

Model metrics (cumulative for this period)
model-a: 42,000 tokens • input 35,000 • output 7,000 • cache read 18,000 • cache write 0 • reasoning 3,000 • 20 sessions • 120 API calls
model-b: 14,000 tokens • input 11,000 • output 3,000 • cache read 6,000 • cache write 0 • reasoning 1,000 • 7 sessions • 32 API calls
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

`--chart` puts the authoritative provider quota first, followed by a grouped
`Local Hermes telemetry` section. The local data is explicitly labelled as
`Local Hermes telemetry - not an authoritative subscription usage total.`

1. **Codex rate-limit** — a percentage meter for each provider window. The
   used portion changes smoothly from green through yellow and orange to red
   as the quota is consumed; the remainder is shown in the same bar. The
   percentage and reset details are printed on the line immediately below the
   bar.
2. **Local Hermes telemetry** — local Hermes accounting, not the Codex
   subscription quota. It contains two subsections:
   **Hermes-codex local usage** — a historical volume chart, with one
   proportional bar per day for `input_tokens + output_tokens`. It uses a
   model-segmented bar when `sessions.model` is available, using the same
   per-model colour convention as the model-usage section. It falls back to a
   blue-to-cyan volume bar when model data is unavailable. The session count is
   shown beside each bar. A `Session metrics` summary follows the chart and
   reports the token, input, output, cache, reasoning, API-call, session and
   available cost fields stored on `sessions`; records without a model are
   shown as `unknown`.
   The default seven-day history is a rolling 168-hour window based on each
   session's `started_at` timestamp, not a fixed set of seven calendar dates.
   The surviving sessions are then grouped by their local calendar date. As a
   result, the oldest visible day can be a partial day and can shrink during
   the day as older sessions fall outside the moving cutoff. For example, a
   run on 8 September in the morning can include more sessions from 1
   September than a run later that afternoon. `--today` is different: it
   selects the current local calendar date.
   **Hermes-codex model usage** — a cumulative model-attributed API-token
   chart. Each day's total bar is segmented by model, with a different colour
   gradient for each model. Metric rows use a shared positional colour sequence
   so corresponding rows match visually even when the two accounting sources
   contain different model sets; the section also reports input, output, cache-read,
   cache-write and reasoning tokens, sessions, API calls and recorded monetary
   costs where available.

Colours are enabled by default for `--chart`, including captured output. Use
`--no-color` for plain text logs, or `--color` to make the choice explicit.
The chart headings are bold and use separate gradients on a controlled
dark-navy background. Model segments receive their own gradients. The explicit
background keeps the headings readable on both light and dark terminal themes.

Each section has a distinct meaning: the first is a live provider snapshot, the
second is local session accounting, and the third is local per-model API
accounting. Model-attributed API totals can differ from session totals because
Hermes records those aggregates through different accounting paths. The Codex
chart is not a retroactive quota history.

Model attribution depends on what Hermes writes to `sessions.model` and
`session_model_usage.model`. If the active model changes during an existing
session but Hermes does not update those fields for the subsequent API calls,
`hermes-codex-usage` cannot infer the change afterwards. Those tokens remain
attributed to the model recorded by Hermes (or to `unknown`) and the new model
will not appear in the local or model metrics for that period. The CLI only
displays model attribution present in the local Hermes databases; it does not
invent or redistribute attribution from the aggregate token totals.

The executable is installed at `~/.local/bin/hermes-codex-usage`.

When the command is run without a period or output selector, it shows the
complete seven-day/live charts. The chart labels include the percentages,
resets, dates, token totals and session counts, so no separate text report is
printed. `--chart` remains the charts-only form (equivalent to the default
visual output), while `--today` shows only today's charts.
