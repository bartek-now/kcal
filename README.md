# kcal

Fetches daily steps and workout calorie data from Garmin Connect.

Each row of output covers one day:

- `weight_kg` — weight from your Garmin scale, taken from the last weigh-in
  of the day if there was more than one; blank if you didn't weigh in that day
- `workout_active_calories` — active calories from tracked workouts, summed
  across all of that day's activities. Garmin's per-activity `calories` field
  is gross (it includes the basal metabolic cost for that activity's
  duration), so this is net of that BMR portion
- `workout_calories` — gross calories Garmin attributes to workouts
  (FYI; includes the BMR portion netted out of `workout_active_calories`)
- `steps` — Garmin's raw daily step total
- `non_workout_steps` — `steps` minus steps attributed to tracked workouts
  (never negative)
- `active_calories` — Garmin's daily active calorie total
- `passive_calories` — Garmin's daily BMR (basal/passive) calorie total

## Setup

```
python -m venv .venv
.venv/Scripts/activate   # or `source .venv/bin/activate` on macOS/Linux
pip install -e .
```

Set your Garmin Connect credentials as environment variables:

```
export GARMIN_EMAIL=you@example.com
export GARMIN_PASSWORD=yourpassword
```

The first login caches a session token under `~/.kcal/garmin_tokens`, so
credentials aren't needed again until that session expires. If your account
has MFA enabled, you'll be prompted for the code on first login.

## Usage

```
kcal fetch --date 2026-07-06
kcal fetch --from 2026-07-01 --to 2026-07-07 --output week.csv
kcal fetch --from 2026-07-01
```

Output is CSV, printed to stdout unless `--output` is given.

Date selection:
- `--date`: a single specific day.
- `--from` and `--to`: an inclusive range.
- `--from` alone: from that day through yesterday.
- neither given: yesterday only (today is skipped since Garmin's daily total
  isn't final until the day is over).
- `--to` without `--from` is an error.

## Tests

```
pip install -e . pytest
pytest
```

## MCP server

`kcal-mcp` runs a stdio MCP server with six tools:

- `get_garmin_daily_stats` (`date`, or `from_date`/`to_date`; defaults to
  yesterday): the same fields as the CSV plus per-workout details. Ranges
  are limited to 120 days.
- `get_garmin_weight` (same date arguments): one compact row per day with a
  weigh-in (`weight_kg`, plus `body_fat_pct`/`muscle_mass_kg` if the scale
  reports them). Ranges up to 366 days, fetched in a single request.
- `get_garmin_day` (`date`, optional `metrics`): several per-day metrics in
  one call (sleep, HRV, resting HR, training readiness/status, ...).
- `list_garmin_endpoints`: every read-only Garmin Connect endpoint and its
  parameters.
- `call_garmin_endpoint` (`endpoint`, `args`): call any of those endpoints,
  e.g. `get_sleep_data` with `{"cdate": "2026-10-05"}`. Only `get_*` methods
  are reachable, so nothing can write or delete. Results over ~50k chars are
  replaced by a notice asking for a narrower request.
- `get_kcal_server_info`: which code the running server loaded and whether
  it's stale (code on disk changed since it started).

`kcal fetch` output is unchanged. Login is non-interactive, so run
`kcal fetch` once in a terminal first to cache the session token.

Claude Desktop / Claude Code config:

```json
{
  "mcpServers": {
    "kcal": {
      "command": "path\\to\\kcal\\.venv\\Scripts\\kcal-mcp.exe"
    }
  }
}
```
