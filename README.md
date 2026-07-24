# kcal

Fetches daily steps and workout calorie data from Garmin Connect.

Each row of output covers one day:

- `workout_active_calories` — active calories from tracked workouts, summed
  across all of that day's activities. Garmin's per-activity `calories` field
  is gross (it includes the basal metabolic cost for that activity's
  duration), so this is net of that BMR portion
- `workout_calories` — gross calories Garmin attributes to workouts
  (FYI; includes the BMR portion netted out of `workout_active_calories`)
- `steps` — Garmin's raw daily step total
- `non_workout_steps` — `steps` minus steps attributed to tracked workouts
  (never negative)
- `estimated_step_calories` — a rough calorie estimate from
  `non_workout_steps`, using a flat 0.04 kcal/step rule of thumb (~40 kcal
  per 1000 steps), independent of Garmin's own calorie figures. Workout steps
  are excluded since those calories are already covered by
  `workout_active_calories`
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
