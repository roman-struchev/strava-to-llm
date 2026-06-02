#!/usr/bin/env python3
"""Export all your Strava activities into LLM-friendly Markdown.

Single-file CLI: runs a local OAuth flow once, then fetches every activity with
full detail (splits, heart-rate zones, power, laps, gear, notes) and writes it
as Markdown you can paste or upload into a chat model for training analysis.

Usage:
    python strava_export.py [--after YYYY-MM-DD] [--limit N] [-h]

Strava API docs: https://developers.strava.com/docs/reference/
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
import webbrowser
from collections.abc import Iterator
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests
from dotenv import load_dotenv

log = logging.getLogger("strava_export")

AUTHORIZE_URL = "https://www.strava.com/oauth/authorize"
TOKEN_URL = "https://www.strava.com/oauth/token"
API_BASE = "https://www.strava.com/api/v3"
SCOPE = "activity:read_all"  # read-only, includes private activities
RATE_WINDOW_SECONDS = 15 * 60


class DailyLimitReached(Exception):
    """Raised when Strava's daily request quota is exhausted (resume tomorrow)."""

# Sports measured by pace (time per distance) rather than speed.
FOOT_SPORTS = {"Run", "TrailRun", "VirtualRun", "Walk", "Hike", "Wheelchair"}
SWIM_SPORTS = {"Swim"}


# ==========================================================================
# OAuth: local-redirect flow + token refresh, cached on disk
# ==========================================================================

class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures the OAuth redirect."""

    def do_GET(self) -> None:  # noqa: N802 (name mandated by the base class)
        query = parse_qs(urlparse(self.path).query)
        self.server.oauth_code = query.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.oauth_error = query.get("error", [None])[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        ok = bool(self.server.oauth_code)  # type: ignore[attr-defined]
        body = ("<h1>Authorized ✔</h1><p>You can close this tab and return to the terminal.</p>"
                if ok else "<h1>Authorization failed</h1><p>Check the terminal.</p>")
        self.wfile.write(f"<html><body>{body}</body></html>".encode("utf-8"))

    def log_message(self, *_args) -> None:  # silence default stderr logging
        pass


class StravaAuth:
    """Obtains and refreshes Strava access tokens, persisting them to disk."""

    def __init__(self, client_id: str, client_secret: str, token_path: Path,
                 port: int = 8721, session: requests.Session | None = None) -> None:
        if not client_id or not client_secret:
            raise SystemExit(
                "Missing STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET.\n"
                "Create an API application at https://www.strava.com/settings/api,\n"
                "then copy .env.example to .env and fill in the values."
            )
        self.client_id = str(client_id)
        self.client_secret = str(client_secret)
        self.token_path = token_path
        self.port = port
        self.redirect_uri = f"http://localhost:{port}"
        self.session = session or requests.Session()

    def get_access_token(self) -> str:
        """Return a valid access token, running the OAuth flow if needed."""
        tokens = self._load()
        if tokens is None:
            log.info("No cached tokens — starting interactive authorization.")
            tokens = self._exchange_code(self._capture_code())
        elif time.time() >= tokens["expires_at"] - 60:  # refresh a minute early
            log.info("Access token expired — refreshing.")
            tokens = self._refresh(tokens["refresh_token"])
        self._save(tokens)
        return tokens["access_token"]

    def _load(self) -> dict | None:
        if not self.token_path.exists():
            return None
        try:
            return json.loads(self.token_path.read_text())
        except (json.JSONDecodeError, ValueError):
            log.warning("Token file is corrupt — re-authorizing.")
            return None

    def _save(self, tokens: dict) -> None:
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(tokens, indent=2))
        self.token_path.chmod(0o600)  # tokens are secrets

    def _capture_code(self) -> str:
        params = {"client_id": self.client_id, "redirect_uri": self.redirect_uri,
                  "response_type": "code", "approval_prompt": "auto", "scope": SCOPE}
        url = f"{AUTHORIZE_URL}?{urlencode(params)}"
        try:
            httpd = HTTPServer(("localhost", self.port), _CallbackHandler)
        except OSError as exc:
            raise SystemExit(f"Cannot bind localhost:{self.port} ({exc}). Free the port or pass --port.")
        httpd.oauth_code = httpd.oauth_error = None  # type: ignore[attr-defined]
        print("\nOpening your browser to authorize access to Strava…")
        print(f"If it does not open, paste this URL manually:\n  {url}\n")
        webbrowser.open(url)
        with httpd:
            httpd.handle_request()  # blocks until the redirect arrives once
        if httpd.oauth_error:  # type: ignore[attr-defined]
            raise SystemExit(f"Strava returned an error: {httpd.oauth_error}")  # type: ignore[attr-defined]
        if not httpd.oauth_code:  # type: ignore[attr-defined]
            raise SystemExit("Did not receive an authorization code from Strava.")
        return httpd.oauth_code  # type: ignore[attr-defined]

    def _exchange_code(self, code: str) -> dict:
        return self._token_request({"code": code, "grant_type": "authorization_code"})

    def _refresh(self, refresh_token: str) -> dict:
        return self._token_request({"refresh_token": refresh_token, "grant_type": "refresh_token"})

    def _token_request(self, extra: dict) -> dict:
        resp = self.session.post(
            TOKEN_URL,
            data={"client_id": self.client_id, "client_secret": self.client_secret, **extra},
            timeout=30,
        )
        if resp.status_code != 200:
            raise SystemExit(f"Token request failed ({resp.status_code}): {resp.text}")
        return resp.json()


# ==========================================================================
# API client: paginating + rate-limit aware
# ==========================================================================

class StravaClient:
    """Authenticated Strava API client with pagination and rate limiting."""

    def __init__(self, auth: StravaAuth, max_retries: int = 5) -> None:
        self.auth = auth
        self.session = auth.session
        self.max_retries = max_retries

    def _get(self, path: str, params: dict | None = None):
        for attempt in range(1, self.max_retries + 1):
            resp = self.session.get(
                f"{API_BASE}{path}",
                headers={"Authorization": f"Bearer {self.auth.get_access_token()}"},
                params=params, timeout=60,
            )
            if resp.status_code == 429:
                limits = self._parse_limits(resp)
                if limits and limits[2] >= limits[3]:  # daily quota exhausted
                    raise DailyLimitReached()
                wait = self._until_window_reset()
                log.warning("Rate limited (attempt %d/%d). Sleeping %dm %ds for the next window.",
                            attempt, self.max_retries, wait // 60, wait % 60)
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                backoff = min(2 ** attempt, 60)
                log.warning("Server error %d. Retrying in %ds.", resp.status_code, backoff)
                time.sleep(backoff)
                continue
            if resp.status_code != 200:
                raise SystemExit(f"GET {path} failed ({resp.status_code}): {resp.text}")
            self._maybe_throttle(resp)
            return resp.json()
        raise SystemExit(f"GET {path} failed after {self.max_retries} retries")

    @staticmethod
    def _parse_limits(resp: requests.Response) -> tuple[int, int, int, int] | None:
        """Return (short_usage, short_limit, daily_usage, daily_limit) from headers."""
        try:
            usage = resp.headers["X-RateLimit-Usage"].split(",")
            limit = resp.headers["X-RateLimit-Limit"].split(",")
            return int(usage[0]), int(limit[0]), int(usage[1]), int(limit[1])
        except (KeyError, ValueError, IndexError):
            return None

    def _maybe_throttle(self, resp: requests.Response) -> None:
        """Stop on daily exhaustion; pause when close to the 15-minute quota."""
        limits = self._parse_limits(resp)
        if not limits:
            return
        short_usage, short_limit, daily_usage, daily_limit = limits
        if daily_usage >= daily_limit:
            raise DailyLimitReached()
        if short_usage >= short_limit - 1:
            wait = self._until_window_reset()
            log.warning("15-min limit reached (%d/%d, daily %d/%d). Sleeping %dm %ds.",
                        short_usage, short_limit, daily_usage, daily_limit, wait // 60, wait % 60)
            time.sleep(wait)

    @staticmethod
    def _until_window_reset() -> int:
        """Seconds until the next quarter-hour boundary Strava resets on."""
        return int(RATE_WINDOW_SECONDS - (time.time() % RATE_WINDOW_SECONDS)) + 1

    def get_athlete(self) -> dict:
        return self._get("/athlete")

    def iter_activities(self, after: int | None, before: int | None, per_page: int = 200) -> Iterator[dict]:
        """Yield summary activities newest-first, transparently paginating."""
        page = 1
        while True:
            params = {"per_page": per_page, "page": page}
            if after is not None:
                params["after"] = after
            if before is not None:
                params["before"] = before
            batch = self._get("/athlete/activities", params=params)
            if not batch:
                return
            yield from batch
            if len(batch) < per_page:
                return
            page += 1

    def get_activity(self, activity_id: int) -> dict:
        return self._get(f"/activities/{activity_id}", params={"include_all_efforts": "false"})

    def get_activity_zones(self, activity_id: int) -> list:
        """Time-in-zone (HR / power) buckets; empty list if unavailable."""
        try:
            data = self._get(f"/activities/{activity_id}/zones")
            return data if isinstance(data, list) else []
        except SystemExit:
            return []


# ==========================================================================
# Markdown formatting
# ==========================================================================

def fmt_distance(meters) -> str:
    return f"{meters / 1000:.2f} km" if meters else "—"


def fmt_duration(seconds) -> str:
    if not seconds:
        return "—"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_pace(speed_mps, sport: str) -> str:
    """Format speed as pace (min/km, min/100m) or km/h depending on the sport."""
    if not speed_mps:
        return "—"
    if sport in SWIM_SPORTS:
        m, s = divmod(int(round(100 / speed_mps)), 60)
        return f"{m}:{s:02d} /100m"
    if sport in FOOT_SPORTS:
        m, s = divmod(int(round(1000 / speed_mps)), 60)
        return f"{m}:{s:02d} /km"
    return f"{speed_mps * 3.6:.1f} km/h"


def fmt_elev(meters) -> str:
    return f"{meters:.0f} m" if meters else "—"


def fmt_date(iso) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return iso


def _num(value, suffix: str = "") -> str:
    return f"{value:g}{suffix}" if value not in (None, "") else "—"


def _hr(value):
    return round(value) if value else None


def slugify(text: str, max_len: int = 60) -> str:
    text = re.sub(r"[^\w\s-]", "", (text or "").strip().lower())
    text = re.sub(r"[\s_-]+", "-", text).strip("-")
    return text[:max_len] or "activity"


def activity_filename(a: dict) -> str:
    """Stable, sortable filename, e.g. ``2024-05-12-123-morning-run.md``."""
    date = (a.get("start_date_local") or "")[:10] or "0000-00-00"
    return f"{date}-{a['id']}-{slugify(a.get('name'))}.md"


def _zones_md(zones: list) -> list[str]:
    out: list[str] = []
    for zone in zones or []:
        buckets = zone.get("distribution_buckets") or []
        if not buckets:
            continue
        total = sum(b.get("time", 0) for b in buckets) or 1
        ztype = zone.get("type", "zone").replace("heartrate", "Heart-rate").title()
        out += [f"### {ztype} zones", "", "| Zone | Range | Time | % |", "|---:|---|---|---:|"]
        for i, b in enumerate(buckets, start=1):
            lo, hi = b.get("min", 0), b.get("max", -1)
            rng = f"{lo}–{hi}" if hi and hi > 0 else f"{lo}+"
            t = b.get("time", 0)
            out.append(f"| Z{i} | {rng} | {fmt_duration(t)} | {100 * t / total:.0f}% |")
        out.append("")
    return out


def _splits_md(a: dict) -> list[str]:
    splits = a.get("splits_metric") or []
    if not splits:
        return []
    sport = a.get("sport_type") or a.get("type") or ""
    out = ["### Splits (per km)", "", "| Km | Time | Pace | Avg HR | Elev Δ |", "|---:|---|---|---:|---:|"]
    for sp in splits:
        ed = sp.get("elevation_difference")
        out.append("| {km} | {time} | {pace} | {hr} | {elev} |".format(
            km=sp.get("split", "?"), time=fmt_duration(sp.get("moving_time")),
            pace=fmt_pace(sp.get("average_speed"), sport), hr=_num(_hr(sp.get("average_heartrate"))),
            elev=_num(round(ed) if ed is not None else None, " m")))
    return out + [""]


def _laps_md(a: dict) -> list[str]:
    laps = a.get("laps") or []
    if len(laps) < 2:  # a single auto-lap == whole activity adds no info
        return []
    sport = a.get("sport_type") or a.get("type") or ""
    out = ["### Laps", "", "| # | Distance | Time | Pace | Avg HR | Max HR |", "|---:|---|---|---|---:|---:|"]
    for lap in laps:
        out.append("| {i} | {dist} | {time} | {pace} | {ahr} | {mhr} |".format(
            i=lap.get("lap_index", "?"), dist=fmt_distance(lap.get("distance")),
            time=fmt_duration(lap.get("moving_time")), pace=fmt_pace(lap.get("average_speed"), sport),
            ahr=_num(_hr(lap.get("average_heartrate"))), mhr=_num(_hr(lap.get("max_heartrate")))))
    return out + [""]


def render_activity(a: dict, zones: list | None = None) -> str:
    """Render one detailed activity (optionally with zone data) to Markdown."""
    sport = a.get("sport_type") or a.get("type") or "Activity"
    lines = [f"## {fmt_date(a.get('start_date_local'))} · {a.get('name') or 'Untitled'} · {sport}", ""]

    gear = a.get("gear")
    facts = [
        ("Distance", fmt_distance(a.get("distance"))),
        ("Moving time", fmt_duration(a.get("moving_time"))),
        ("Elapsed time", fmt_duration(a.get("elapsed_time"))),
        ("Avg pace/speed", fmt_pace(a.get("average_speed"), sport)),
        ("Max pace/speed", fmt_pace(a.get("max_speed"), sport)),
        ("Avg HR", _num(_hr(a.get("average_heartrate")), " bpm")),
        ("Max HR", _num(_hr(a.get("max_heartrate")), " bpm")),
        ("Avg power", _num(_hr(a.get("average_watts")), " W")),
        ("Max power", _num(a.get("max_watts"), " W")),
        ("Avg cadence", _num(_hr(a.get("average_cadence")))),
        ("Elevation gain", fmt_elev(a.get("total_elevation_gain"))),
        ("Calories", _num(a.get("calories"))),
        ("Relative effort", _num(a.get("suffer_score"))),
        ("Gear", gear.get("name") if isinstance(gear, dict) else "—"),
    ]
    lines += [f"- **{label}:** {value}" for label, value in facts if value and value != "—"]

    desc = (a.get("description") or "").strip()
    if desc:
        lines += ["", "**Notes:**", "", "> " + desc.replace("\n", "\n> ")]

    lines += [""] + _zones_md(zones) + _splits_md(a) + _laps_md(a)
    return "\n".join(lines).rstrip() + "\n"


def render_overview(athlete: dict, activities: list[dict]) -> str:
    """Render the summary table that heads the combined export."""
    name = " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")])) or "Athlete"
    lines = [
        f"# Strava export — {name}", "",
        f"Total activities: **{len(activities)}**  ",
        "Distances in km, durations as H:MM:SS, paces per km / per 100 m, speeds in km/h.", "",
        "## Overview", "",
        "| Date | Sport | Name | Distance | Time | Pace/Speed | Avg HR | Elev |",
        "|---|---|---|---|---|---|---:|---:|",
    ]
    for a in activities:
        sport = a.get("sport_type") or a.get("type") or ""
        lines.append("| {date} | {sport} | {name} | {dist} | {time} | {pace} | {hr} | {elev} |".format(
            date=fmt_date(a.get("start_date_local")), sport=sport,
            name=(a.get("name") or "").replace("|", "\\|"), dist=fmt_distance(a.get("distance")),
            time=fmt_duration(a.get("moving_time")), pace=fmt_pace(a.get("average_speed"), sport),
            hr=_num(_hr(a.get("average_heartrate"))), elev=fmt_elev(a.get("total_elevation_gain"))))
    return "\n".join(lines) + "\n"


# ==========================================================================
# Orchestration
# ==========================================================================

def parse_date(value: str | None) -> int | None:
    """Parse ``YYYY-MM-DD`` into a UTC Unix timestamp (or ``None``)."""
    if not value:
        return None
    try:
        dt = datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SystemExit(f"Invalid date '{value}'. Use YYYY-MM-DD.")
    return int(dt.timestamp())


def load_detail(client: StravaClient, summary: dict, cache_dir: Path,
                refresh: bool, fetch_zones: bool = True) -> tuple[dict, list]:
    """Return (detailed_activity, zones), using the on-disk cache when possible."""
    cache_file = cache_dir / f"activity_{summary['id']}.json"
    if cache_file.exists() and not refresh:
        cached = json.loads(cache_file.read_text())
        return cached["detail"], cached.get("zones", [])
    detail = client.get_activity(summary["id"])
    # Zones cost a second request; only worth it when the activity has HR data.
    zones = client.get_activity_zones(summary["id"]) if fetch_zones else []
    cache_file.write_text(json.dumps({"detail": detail, "zones": zones}))
    return detail, zones


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export your Strava activities into LLM-friendly Markdown.")
    p.add_argument("-o", "--output", type=Path, default=Path("export"),
                   help="Output directory for Markdown (default: ./export).")
    p.add_argument("--cache", type=Path, default=Path("cache"),
                   help="Directory for cached JSON and tokens (default: ./cache).")
    p.add_argument("--after", help="Only activities on/after this date (YYYY-MM-DD).")
    p.add_argument("--before", help="Only activities before this date (YYYY-MM-DD).")
    p.add_argument("--limit", type=int, help="Stop after N activities (newest first).")
    p.add_argument("--refresh", action="store_true", help="Re-fetch details even if cached.")
    p.add_argument("--summary-only", action="store_true",
                   help="Skip per-activity requests; use only the activity list (~6 requests, instant). "
                        "No splits/zones/laps.")
    p.add_argument("--no-zones", action="store_true",
                   help="Skip the heart-rate/power zones request per activity (halves API calls).")
    p.add_argument("--no-per-activity", action="store_true", help="Write only the combined file.")
    p.add_argument("--port", type=int, default=8721, help="Local OAuth callback port (default: 8721).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    load_dotenv()

    cache_dir: Path = args.cache
    cache_dir.mkdir(parents=True, exist_ok=True)

    auth = StravaAuth(os.getenv("STRAVA_CLIENT_ID"), os.getenv("STRAVA_CLIENT_SECRET"),
                      token_path=cache_dir / "tokens.json", port=args.port)
    client = StravaClient(auth)

    athlete = client.get_athlete()
    log.info("Authorized as %s %s (id %s).",
             athlete.get("firstname"), athlete.get("lastname"), athlete.get("id"))

    summaries: list[dict] = []
    for summary in client.iter_activities(parse_date(args.after), parse_date(args.before)):
        summaries.append(summary)
        if args.limit and len(summaries) >= args.limit:
            break
    log.info("Found %d activities. Fetching details…", len(summaries))

    out_dir: Path = args.output
    per_dir = out_dir / "activities"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_per_activity:
        per_dir.mkdir(parents=True, exist_ok=True)

    detailed, sections = [], []
    for i, summary in enumerate(summaries, start=1):
        detail, zones = load_detail(client, summary, cache_dir, args.refresh)
        detailed.append(detail)
        md = render_activity(detail, zones)
        sections.append(md)
        if not args.no_per_activity:
            (per_dir / activity_filename(detail)).write_text(md)
        log.info("  [%d/%d] %s", i, len(summaries), detail.get("name", "?"))

    combined = render_overview(athlete, detailed) + "\n---\n\n" + "\n\n---\n\n".join(sections) + "\n"
    combined_path = out_dir / "all_activities.md"
    combined_path.write_text(combined)

    print(f"\n✔ Exported {len(detailed)} activities.")
    print(f"  Combined file: {combined_path}")
    if not args.no_per_activity:
        print(f"  Per-activity:  {per_dir}/ ({len(detailed)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
