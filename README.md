# Strava exported to markdown

A small script that downloads all your [Strava](https://www.strava.com)
activities and saves them as Markdown files — so you can drop them into
ChatGPT, Claude, or any other chat and ask questions about your training.

For each activity it grabs the details: per-kilometer splits, heart-rate zones,
power, cadence, elevation, laps, gear and your notes.

## Why Markdown?

Because chat models read Markdown tables and headings well. Pasting raw JSON
into a chat works badly; a tidy Markdown file works great. Then you can just
ask things like *"compare my pace in April and May"* or *"is my heart rate
drifting less on long runs than it used to?"*.

## What you get

After a run you'll have:

```
export/
├── all_activities.md          # everything in one file — upload this one
└── activities/
    ├── 2024-05-12-123456-morning-run.md
    ├── 2024-05-10-123001-tempo-intervals.md
    └── ...
```

`all_activities.md` starts with a summary table of every activity, followed by
the full details of each one. The `activities/` folder has the same thing split
into one file per workout, in case the combined file is too big for your chat.

Here's the summary table at the top of `all_activities.md`:

![The overview table in all_activities.md](screenshots/activities_all.png)

And here's a single activity — metrics, per-kilometer splits, laps and zones:

![A single activity with splits](screenshots/activity.png)

## What you need

- Python 3.10 or newer
- A Strava account
- A free Strava API application (takes two minutes to create — see below)

## Setup

### 1. Create a Strava API application

1. Open <https://www.strava.com/settings/api>.
2. Create an application. Any name and website will do.
3. Set **Authorization Callback Domain** to exactly `localhost`.
4. Copy your **Client ID** and **Client Secret** — you'll need them next.

### 2. Install

```bash
git clone https://github.com/roman-struchev/strava-to-llm.git
cd strava-to-llm

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Add your credentials

Copy the example file and paste in your Client ID and Secret:

```bash
cp .env.example .env
```

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=abcdef0123456789...
```

### 4. Run it

```bash
python strava_export.py
```

The first time, your browser opens and asks you to allow access to your Strava
activities. Click approve — the script catches the response, saves a login token
in `cache/tokens.json`, and starts downloading. After that it logs in on its own,
so you only do this once.

## Using it with a chat model

Upload `export/all_activities.md` to ChatGPT, Claude, etc. (or paste a few files
from `export/activities/`), then ask away. For example:

- *"Summarize my training over the last 4 weeks. Any signs I'm overdoing it?"*
- *"Look at my long runs — is my heart-rate drift getting better?"*
- *"Make a week-by-week mileage table and point out the biggest jumps."*

If your chat has a small context limit, the combined file might be too large —
upload individual files from `export/activities/`, or export just a recent
window with `--after`.

## Options

```text
python strava_export.py [options]

  -o, --output DIR        Where to write the Markdown (default: ./export)
      --cache DIR         Where to keep cached data and your login token (default: ./cache)
      --after YYYY-MM-DD   Only activities on or after this date
      --before YYYY-MM-DD  Only activities before this date
      --limit N           Only the N most recent activities
      --refresh           Download again even if it's already cached
      --summary-only      Quick mode: just the activity list, no splits/zones/laps
      --no-zones          Skip heart-rate/power zones (downloads twice as fast)
      --no-per-activity   Only write the combined file
      --port N            Port for the login redirect (default: 8721)
  -v, --verbose           Show more logging
```

A few handy examples:

```bash
# Quick overview of everything in a few seconds
python strava_export.py --summary-only

# Just this year, fresh data
python strava_export.py --after 2026-01-01 --refresh

# Try it out on your 10 latest activities
python strava_export.py --limit 10
```

## Why it can be slow (and what to do)

The script isn't the bottleneck — Strava is. A new API app is allowed only
**100 requests every 15 minutes** and **1000 per day**. Each activity needs one
or two requests, so if you have a thousand-plus activities, a full export simply
can't finish in a single day. That's a Strava rule, not a bug, and running it in
parallel wouldn't help — you'd just spend the time waiting for the quota anyway.

The script handles this for you:

- **It remembers what it already downloaded.** When the daily limit runs out, it
  stops cleanly, writes the file with whatever it has so far, and tells you to
  run it again later. Next time it skips everything it already has and picks up
  where it left off.
- **It skips unnecessary requests.** Heart-rate zones are only fetched for
  activities that actually have a heart rate. Use `--no-zones` to skip them
  completely and roughly halve the work.
- **There's a fast mode.** `--summary-only` uses just the activity list — about
  six requests total — and finishes in seconds. You lose splits, zones and laps,
  but you get distance, time, pace, average heart rate and elevation for every
  activity. Good for a first look.

If you want it genuinely faster, you can ask Strava to raise your app's limit on
the [API settings page](https://www.strava.com/settings/api) — they usually
grant it for normal personal use.

## Your data stays yours

- Nothing is sent anywhere except to Strava to fetch your own data. The files
  are written to your computer. Whatever you later upload to a chat is your call.
- `.env`, the `cache/` folder (token + downloaded data) and `export/` are all in
  `.gitignore`, so you won't accidentally commit your token or your activities.
- The login token file is saved with `0600` permissions (only you can read it).
- The script only asks for read access (`activity:read_all`, including private
  activities). It never changes anything on your Strava account.

