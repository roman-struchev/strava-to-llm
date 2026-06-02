# strava-exporter

Export all your [Strava](https://www.strava.com) activities — with full detail —
into clean **Markdown that you can paste or upload into an LLM chat**
(ChatGPT, Claude, Gemini, …) for manual training analysis.

It pulls every activity from your profile, fetches per-activity details
(splits per km, heart-rate zones, power, cadence, elevation, laps, gear, notes),
and writes them as human- and model-readable Markdown.

## Why Markdown?

LLMs parse Markdown tables and headings reliably and cheaply. Instead of
dumping raw JSON into a chat, you get a structured document you can drop in and
ask things like *"compare my pacing in April vs May"* or *"is my aerobic base
improving — look at HR drift across long runs"*.

## Features

- 🔐 **One-click OAuth** — opens your browser, captures the redirect on
  `localhost`, and caches tokens so you only log in once.
- 📋 **Full detail** — distance, moving/elapsed time, pace/speed, avg & max HR,
  power, cadence, elevation gain, calories, gear, description, **per-km splits**,
  **heart-rate / power zone distribution**, and **laps**.
- 📄 **Two outputs** — one combined `all_activities.md` (great for a single
  upload) plus one file per activity under `export/activities/`.
- ⚡ **Caching** — raw API responses are cached on disk, so re-runs are fast and
  don't waste your API quota.
- 🚦 **Rate-limit aware** — respects Strava's 100-requests/15-min limit and
  backs off automatically.
- 🧰 **One file, two dependencies** — a single `strava_export.py` script, no
  database, no server.

## Requirements

- Python 3.10+
- A Strava account and a (free) Strava API application
- Two Python packages: `requests` and `python-dotenv`

## 1. Create a Strava API application

1. Go to **<https://www.strava.com/settings/api>**.
2. Create an application (any name/website works for personal use).
3. Set **Authorization Callback Domain** to exactly: `localhost`
4. Note your **Client ID** and **Client Secret**.

## 2. Install

```bash
git clone https://github.com/roman-struchev/strava-exporter.git
cd strava-exporter

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

## 3. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and fill in your Strava app credentials:

```dotenv
STRAVA_CLIENT_ID=12345
STRAVA_CLIENT_SECRET=abcdef0123456789...
```

## 4. Run

```bash
python strava_export.py
```

On first run a browser window opens asking you to authorize access to your
activities. Approve it; the script catches the redirect, exchanges the code for
tokens, and starts exporting. Tokens are saved in `cache/tokens.json` and
refreshed automatically afterwards.

Output:

```
export/
├── all_activities.md          # overview table + every activity (upload this)
└── activities/
    ├── 2024-05-12-123456-morning-run.md
    ├── 2024-05-10-123001-tempo-intervals.md
    └── ...
```

## 5. Analyze with an LLM

Upload `export/all_activities.md` (or paste a few activity files) into your
chat model and ask away. Example prompts:

- *"Summarize my training load over the last 4 weeks. Any signs of overtraining?"*
- *"Look at my long runs and tell me if my heart-rate drift is improving."*
- *"Build a week-by-week mileage table and flag the biggest jumps."*

> 💡 The combined file can get large. If your chat has a small context window,
> upload individual files from `export/activities/` or use `--after` to export
> a recent window.

## Usage / options

```text
python strava_export.py [options]

  -o, --output DIR        Output directory for Markdown (default: ./export)
      --cache DIR         Directory for cached JSON and tokens (default: ./cache)
      --after YYYY-MM-DD   Only activities on/after this date
      --before YYYY-MM-DD  Only activities before this date
      --limit N           Stop after N activities (newest first)
      --refresh           Re-fetch details even if cached
      --no-per-activity   Write only the combined file
      --port N            Local OAuth callback port (default: 8721)
  -v, --verbose           Verbose logging
```

Examples:

```bash
# Only this year, fresh data
python strava_export.py --after 2026-01-01 --refresh

# Quick test with the 10 most recent activities
python strava_export.py --limit 10
```

## How it works

```
your browser ──auth──▶ Strava OAuth ──tokens──▶ cache/tokens.json
                                                      │ (auto-refreshed)
python strava_export.py                               ▼
   ──▶ GET /athlete/activities (paged)        StravaAuth + StravaClient
   ──▶ GET /activities/{id}                          │  (rate-limited)
   ──▶ GET /activities/{id}/zones                     ▼
                  cache/activity_*.json ──▶ Markdown formatter ──▶ export/*.md
```

Everything lives in one file, `strava_export.py`, split into clearly-labelled
sections: OAuth (`StravaAuth`), the API client (`StravaClient`), the Markdown
formatter, and the CLI orchestration (`main`).

## Privacy & security

- Your data **never leaves your machine** except for the calls you make to the
  Strava API. The exported files are written locally; what you later upload to
  an LLM is your choice.
- `.env`, `cache/` (tokens + raw data) and `export/` are **git-ignored** by
  default — don't commit your tokens or activities.
- Token files are written with `0600` permissions.
- The app requests the `activity:read_all` scope (read-only, including private
  activities). It never writes to or modifies your Strava account.

## Rate limits

Strava's default limits are 100 requests / 15 min and 1000 / day. Each activity
costs ~2 requests (detail + zones). The client throttles and retries
automatically; thanks to caching, only new activities are fetched on re-runs.
Very large histories may pause briefly when a 15-minute window is exhausted.

## License

[MIT](LICENSE) © Roman Struchev

## Disclaimer

This project is not affiliated with or endorsed by Strava. "Strava" is a
trademark of Strava, Inc. Use of the Strava API is subject to the
[Strava API Agreement](https://www.strava.com/legal/api).
