# kcal

Fetches daily steps and workout calorie data from Garmin Connect.

Garmin's daily step total already includes steps taken during step-based
workouts (runs, walks, hikes). This tool subtracts those workout steps back
out, so you get:

- `total_steps` — Garmin's raw daily total
- `workout_steps` — steps attributed to activities that reported a step count
- `non_workout_steps` — `total_steps` minus `workout_steps` (never negative)
- `total_calories` / `active_calories` / `bmr_calories` — Garmin's daily summary

Calories get a similar split, with one wrinkle: Garmin's per-activity
`calories` field is **gross** — it includes the basal metabolic (BMR) cost
for that activity's duration — while the day summary's `active_calories` is
already **net** of BMR. Comparing them directly (e.g. a long hike's gross
calories vs. the day's net active calories) can make a single workout look
like it burned more than the whole day's active total. So:

- `workout_calories` — gross calories Garmin attributes to workouts (matches
  what the Garmin app shows per activity)
- `workout_active_calories` — workout calories net of BMR; this is the part
  actually comparable to (and subtracted from) `active_calories`
- `non_workout_active_calories` — `active_calories` minus
  `workout_active_calories` (never negative)

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
kcal fetch --from 2026-07-01 --to 2026-07-07 --format csv --output week.csv
kcal fetch --from 2026-07-01
kcal fetch --format json
```

`--format` is `table` (default), `json`, or `csv`.

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
