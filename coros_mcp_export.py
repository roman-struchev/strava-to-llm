#!/usr/bin/env python3
"""Export your COROS activities into LLM-friendly Markdown — via the official COROS MCP.

Unlike the Strava exporter, this does NOT talk to a REST API with your own
client_id/secret. It connects to COROS's official hosted **MCP server**
(regional, e.g. https://mcpeu.coros.com/mcp) and authorizes with your normal
COROS account through the browser (OAuth, one time; token cached under
data/coros_mcp/). COROS's tools answer in ready-made human-readable prose, so the
export keeps that text verbatim and adds a parsed Overview table on top:

    # COROS export
    ## Overview     — one row per activity (date, sport, distance, time, pace, HR…)
    ## Activities   — each activity in full: its summary, then getActivityDetail
                      prose, then lap tables (manual/button laps + 1 km splits)

Usage:
    python coros_mcp_export.py                     # authorize (first run), then export
    python coros_mcp_export.py --after 2026-01-01 --limit 20
    python coros_mcp_export.py --no-detail         # summary only (skip detail + laps)

Diagnostics: --list-tools (tools + schemas), --raw (dump a tool result),
--tool <name> --json '{…}' (call a specific tool with exact arguments).

Requires the MCP Python SDK:  pip install mcp
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import webbrowser
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

log = logging.getLogger("coros_mcp_export")


def _require_mcp():
    """Import the MCP SDK on demand, with a friendly hint if it's missing.

    Kept lazy so the pure rendering/normalization helpers below can be imported
    and tested without the SDK installed.
    """
    try:
        from pydantic import AnyUrl
        from mcp.client.auth import OAuthClientProvider
        from mcp.client.session import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken
    except ImportError as exc:
        raise SystemExit(
            "The MCP Python SDK is required for the COROS integration.\n"
            "Install it with:  pip install mcp\n"
            f"(import error: {exc})"
        )
    return (AnyUrl, OAuthClientProvider, ClientSession, streamablehttp_client,
            OAuthClientInformationFull, OAuthClientMetadata, OAuthToken)

# COROS routes each account to a regional MCP endpoint. The consolidated
# https://mcp.coros.com/mcp returns a 401 whose resource metadata points at the
# regional host, and the MCP SDK requires the URL we connect+authorize against to
# match that regional resource. Default is the EU endpoint (mcpeu.coros.com);
# override for another region via COROS_MCP_URL or --mcp-url (the correct host is
# named in the "Protected resource ..." error if it ever mismatches).
MCP_URL = os.getenv("COROS_MCP_URL") or "https://mcpeu.coros.com/mcp"
_region_resolved = False
DEFAULT_PORT = 8722
COROS_SUBDIR = "coros_mcp"  # under the data dir: OAuth tokens + reports live here
CALL_TIMEOUT = timedelta(seconds=180)  # a wide querySportRecords range can take ~20s+
DEFAULT_CONCURRENCY = 6  # max in-flight MCP tool calls (detail/laps run in parallel)

# Tool names aren't documented here; auto-pick the one that most looks like a
# "list my workouts/activities" tool. Overridable with --tool.
TOOL_KEYWORDS = ("workout", "activit", "sport", "exercise", "training", "record")
PREFER_KEYWORDS = ("list", "query", "get", "search", "history")


# ==========================================================================
# OAuth plumbing for the MCP SDK
# ==========================================================================

class _FileTokenStorage:
    """Persist OAuth tokens + dynamic client registration under data/coros_mcp/.

    Structurally satisfies the MCP SDK's ``TokenStorage`` protocol (duck-typed,
    so no import of the SDK is needed just to define it).
    """

    def __init__(self, directory: Path) -> None:
        self._tokens = directory / "mcp_tokens.json"
        self._client = directory / "mcp_client.json"

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        if self._tokens.exists():
            return OAuthToken.model_validate_json(self._tokens.read_text())
        return None

    async def set_tokens(self, tokens) -> None:
        self._tokens.parent.mkdir(parents=True, exist_ok=True)
        self._tokens.write_text(tokens.model_dump_json())
        self._tokens.chmod(0o600)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        if self._client.exists():
            return OAuthClientInformationFull.model_validate_json(self._client.read_text())
        return None

    async def set_client_info(self, client_info) -> None:
        self._client.parent.mkdir(parents=True, exist_ok=True)
        self._client.write_text(client_info.model_dump_json())
        self._client.chmod(0o600)


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot handler that captures the OAuth redirect (code + state)."""

    def do_GET(self) -> None:  # noqa: N802 (name mandated by the base class)
        query = parse_qs(urlparse(self.path).query)
        self.server.auth_code = query.get("code", [None])[0]  # type: ignore[attr-defined]
        self.server.auth_state = query.get("state", [None])[0]  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body><h1>Authorized &#10004;</h1>"
                         b"<p>You can close this tab and return to the terminal.</p></body></html>")

    def log_message(self, *_args) -> None:  # silence default stderr logging
        pass


def _run_callback_server(port: int) -> tuple[str, str | None]:
    httpd = HTTPServer(("localhost", port), _CallbackHandler)
    httpd.auth_code = httpd.auth_state = None  # type: ignore[attr-defined]
    with httpd:
        httpd.handle_request()  # blocks until the redirect arrives once
    code = httpd.auth_code  # type: ignore[attr-defined]
    if not code:
        raise SystemExit("Did not receive an authorization code from COROS.")
    return code, httpd.auth_state  # type: ignore[attr-defined]


def is_connected(store_dir: Path) -> bool:
    """True if OAuth tokens for the COROS MCP already exist under ``store_dir``."""
    return (store_dir / "mcp_tokens.json").exists()


def make_oauth(store_dir: Path, *, port: int = DEFAULT_PORT, redirect_uri: str | None = None,
               redirect_handler=None, callback_handler=None, interactive: bool = True):
    """Build an MCP OAuth provider.

    - CLI: leave the handlers as ``None`` and ``interactive=True`` — it opens a
      browser and captures the redirect on ``localhost:port``.
    - Web server: pass a public ``redirect_uri`` plus your own ``redirect_handler``
      / ``callback_handler`` that drive the flow through the browser.
    - Export with already-stored tokens: ``interactive=False`` so a missing/expired
      token fails cleanly instead of trying to pop a browser on the server.
    """
    (AnyUrl, OAuthClientProvider, _ClientSession, _streamable,
     _OAuthClientInformationFull, OAuthClientMetadata, _OAuthToken) = _require_mcp()
    redirect_uri = redirect_uri or f"http://localhost:{port}/callback"

    if redirect_handler is None:
        if interactive:
            async def redirect_handler(authorization_url: str) -> None:
                print("\nOpening your browser to authorize access to COROS…")
                print(f"If it does not open, paste this URL manually:\n  {authorization_url}\n")
                webbrowser.open(authorization_url)
        else:
            async def redirect_handler(authorization_url: str) -> None:
                raise SystemExit("COROS is not connected — authorize first "
                                 "(run `python coros_mcp_export.py`).")
    if callback_handler is None:
        if interactive:
            async def callback_handler() -> tuple[str, str | None]:
                return await asyncio.to_thread(_run_callback_server, port)
        else:
            async def callback_handler() -> tuple[str, str | None]:
                raise SystemExit("COROS is not connected — authorize first.")

    return OAuthClientProvider(
        # Must match the regional resource COROS advertises, incl. the /mcp path.
        server_url=MCP_URL,
        client_metadata=OAuthClientMetadata(
            client_name="strava-to-llm COROS exporter",
            redirect_uris=[AnyUrl(redirect_uri)],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
        storage=_FileTokenStorage(store_dir),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


async def resolve_mcp_url() -> str:
    """Auto-detect COROS's regional MCP endpoint and point ``MCP_URL`` at it.

    COROS advertises the canonical (regional) resource — e.g. mcpeu.coros.com — in
    its protected-resource metadata, and the MCP SDK's OAuth flow refuses to run if
    the URL we use doesn't match it. We read that metadata once and adopt it, so a
    stale/region-mismatched configured URL still works. Cached after the first call.
    """
    global MCP_URL, _region_resolved
    if _region_resolved:
        return MCP_URL
    import httpx
    parsed = urlparse(MCP_URL)
    well_known = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-protected-resource{parsed.path}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15) as client:
            resp = await client.get(well_known)
        resource = resp.json().get("resource") if resp.status_code == 200 else None
        if resource and str(resource) != MCP_URL:
            log.info("COROS region resolved: %s → %s", MCP_URL, resource)
            MCP_URL = str(resource)
    except Exception as exc:
        log.warning("COROS region resolution failed (%s); using %s", exc, MCP_URL)
    _region_resolved = True
    return MCP_URL


@asynccontextmanager
async def mcp_session(oauth):
    """Open an initialized MCP client session to the COROS server."""
    (_AnyUrl, _Provider, ClientSession, streamablehttp_client,
     _CI, _CM, _Tok) = _require_mcp()
    async with streamablehttp_client(MCP_URL, auth=oauth) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


# ==========================================================================
# Tool discovery + result parsing
# ==========================================================================

def _print_tools(tools: list) -> None:
    print(f"\n{len(tools)} tool(s) exposed by the COROS MCP server:\n")
    for t in tools:
        print(f"• {t.name}")
        if t.description:
            print(f"    {t.description.strip().splitlines()[0]}")
        schema = getattr(t, "inputSchema", None) or {}
        props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
        if props:
            required = set(schema.get("required") or [])
            for name, spec in props.items():
                typ = spec.get("type", "?") if isinstance(spec, dict) else "?"
                mark = " (required)" if name in required else ""
                print(f"      - {name}: {typ}{mark}")
        print()


def _resolve_tool(tools: list, wanted: str | None):
    if wanted:
        for t in tools:
            if t.name == wanted:
                return t
        raise SystemExit(f"No tool named {wanted!r}. Run --list-tools to see what's available.")
    # Auto-pick: score by activity keyword + a bump for list/query verbs.
    best, best_score = None, 0
    for t in tools:
        name = t.name.lower()
        score = sum(kw in name for kw in TOOL_KEYWORDS)
        if score:
            score = score * 2 + sum(kw in name for kw in PREFER_KEYWORDS)
        if score > best_score:
            best, best_score = t, score
    if best is None:
        raise SystemExit("Could not guess a workouts tool. Run --list-tools and pass --tool <name>.")
    log.info("Auto-selected tool %r (use --tool to override).", best.name)
    return best


def _to_yyyymmdd(value: str | None, default: str) -> str:
    """COROS wants dates as yyyyMMdd; accept either that or YYYY-MM-DD."""
    return value.replace("-", "") if value else default


def _sport_records_args(after, before, limit) -> dict:
    """Full argument set for COROS' ``querySportRecords`` (all its fields are
    marked required). Wide ranges + all sport types so nothing is filtered out;
    the caller trims to --limit afterwards."""
    today = datetime.now().strftime("%Y%m%d")
    return {
        "startDate": _to_yyyymmdd(after, "20100101"),
        "endDate": _to_yyyymmdd(before, today),
        "sportTypeCodes": [65535],       # 65535 == all sports (per the tool's docs)
        "minDistanceKm": 0,
        "maxDistanceKm": 100000,
        "minDurationMinutes": 0,
        "maxDurationMinutes": 1000000,
        "maxAveragePace": "",            # empty == no pace filter
        "locationKeyword": "",
        "limit": limit or 500,
    }


def _build_arguments(tool, args: argparse.Namespace) -> dict:
    """Decide the arguments for a tool call.

    Precedence: explicit --json > a known tool's hand-tuned defaults > a generic
    best-effort mapping of --after/--before/--limit onto the tool's own schema.
    """
    if args.json:
        try:
            return json.loads(args.json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--json is not valid JSON: {exc}")
    if tool.name == "querySportRecords":
        return _sport_records_args(args.after, args.before, args.limit)

    schema = getattr(tool, "inputSchema", None) or {}
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    out: dict = {}
    for name in props:
        low = name.lower()
        if args.after and any(w in low for w in ("startdate", "after", "fromdate", "since")):
            out[name] = args.after
        elif args.before and any(w in low for w in ("enddate", "before", "until")):
            out[name] = args.before
        elif args.limit and low in ("limit", "size", "count", "pagesize"):
            out[name] = args.limit
    return out


def _dump_raw(result) -> None:
    """Print the entire CallToolResult (content blocks + structuredContent) for mapping."""
    try:
        data = result.model_dump(mode="json")  # CallToolResult is a pydantic model
    except Exception:
        data = {
            "isError": getattr(result, "isError", None),
            "structuredContent": getattr(result, "structuredContent", None),
            "content": [getattr(b, "text", repr(b)) for b in (getattr(result, "content", None) or [])],
        }
    print(json.dumps(data, indent=2, ensure_ascii=False, default=str))


def _extract_payload(result) -> Any:
    """Pull JSON (or text) out of an MCP CallToolResult."""
    if getattr(result, "isError", False):
        raise SystemExit(f"COROS MCP tool returned an error: {result.content}")
    structured = getattr(result, "structuredContent", None)
    if structured:
        return structured
    texts = [getattr(b, "text", "") for b in (result.content or []) if getattr(b, "type", "") == "text"]
    joined = "\n".join(t for t in texts if t)
    try:
        # COROS wraps its prose as a JSON string with real newlines inside, which
        # standard json.loads rejects (control chars) — strict=False allows them.
        return json.loads(joined, strict=False)
    except (json.JSONDecodeError, ValueError):
        return joined


# ==========================================================================
# Output: COROS tools return human-readable prose (ideal for feeding an LLM),
# so we pass it through verbatim. The only parsing we do is pulling
# (labelId, sportType) out of the summary, so we can optionally fetch each
# activity's full detail via getActivityDetail.
# ==========================================================================

_ID_RE = re.compile(r"LabelId:\s*(\S+)\s*\|\s*SportType:\s*(\d+)")
_REC_HEAD = re.compile(r"^\s*\d+\.\s+(.*?)\s+[—-]\s+(\d{4}-\d{2}-\d{2})\s*$", re.M)
_TIME_WINDOW_RE = re.compile(r"startTimestamp=(\d+)\s*\|\s*endTimestamp=(\d+)")


def _humanize_timestamps(text: str) -> str:
    """Replace raw ``startTimestamp=… | endTimestamp=…`` epochs with a readable
    UTC clock range — LLMs read that far more reliably than Unix seconds."""
    def repl(m: re.Match) -> str:
        start = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        end = datetime.fromtimestamp(int(m.group(2)), tz=timezone.utc)
        if start.date() == end.date():
            return f"{start:%Y-%m-%d %H:%M}–{end:%H:%M} UTC"
        return f"{start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC"
    return _TIME_WINDOW_RE.sub(repl, text or "")


# Fields dropped from COROS prose as low-value for analysis.
_STRIP_LABELS = ("Calories", "Max Power", "Max Cadence")


def _strip_fields(text: str) -> str:
    """Remove unwanted metrics from COROS prose, both as full ``Label: value`` lines
    and as inline ``| Label: value`` segments (e.g. in the summary's pace line)."""
    kept = [ln for ln in (text or "").splitlines()
            if not any(re.match(rf"\s*{re.escape(lbl)}\s*:", ln, re.I) for lbl in _STRIP_LABELS)]
    out = "\n".join(kept)
    for lbl in _STRIP_LABELS:
        out = re.sub(rf"\s*\|\s*{re.escape(lbl)}\s*:[^|\n]*", "", out, flags=re.I)
    return out


def _grab(block: str, pattern: str) -> str:
    """First capture group of ``pattern`` in ``block`` (up to a | or newline), or ''."""
    m = re.search(pattern, block)
    return m.group(1).strip() if m else ""


def _parse_records(summary_text: str) -> list[dict]:
    """Split the querySportRecords prose into per-activity records. Each carries
    the verbatim block ``text`` and ``label_id``/``sport_type`` (to fetch detail
    and laps), plus string fields for the Overview table — kept exactly as COROS
    formats them (no unit conversion; a missing field just yields '')."""
    heads = list(_REC_HEAD.finditer(summary_text or ""))
    records = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(summary_text)
        block = summary_text[h.start():end].rstrip()
        m = _ID_RE.search(block)
        records.append({
            "text": block,
            "label_id": m.group(1) if m else None,
            "sport_type": int(m.group(2)) if m else None,
            "sport": h.group(1).strip(),
            "date": h.group(2),
            "location": _grab(block, r"Location:\s*([^|\n]+)"),
            "distance": _grab(block, r"Distance:\s*([^|\n]+)"),
            "time": _grab(block, r"Duration:\s*([^|\n]+)"),
            "pace": _grab(block, r"Average (?:Pace|Speed):\s*([^|\n]+)"),
            "hr": _grab(block, r"Avg HR:\s*([^|\n]+)"),
        })
    return records


def _overview_table(records: list[dict]) -> str:
    """Render a Markdown Overview table from parsed records."""
    lines = [
        "## Overview", "",
        f"Total activities: **{len(records)}**", "",
        "| Date | Sport | Location | Distance | Time | Pace/Speed | Avg HR |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in records:
        cells = [r["date"], r["sport"], r["location"], r["distance"], r["time"], r["pace"], r["hr"]]
        lines.append("| " + " | ".join((c or "—").replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


async def _call_payload(session, name: str, arguments: dict):
    """Call a tool and return its payload (a dict for JSON tools, str for prose)."""
    log.info("COROS call %s %s", name, json.dumps(arguments, ensure_ascii=False) if arguments else "{}")
    result = await session.call_tool(name, arguments, read_timeout_seconds=CALL_TIMEOUT)
    return _extract_payload(result)


async def _call_text(session, name: str, arguments: dict) -> str:
    """Call a tool and return its content as text (COROS tools answer in prose)."""
    payload = await _call_payload(session, name, arguments)
    return payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False, indent=2)


def _lap_clock(value) -> str | None:
    try:
        s = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _lap_pace(value) -> str | None:
    clock = _lap_clock(value)  # avgPace is seconds per km
    return f"{clock} /km" if clock else None


# (field name in COROS lap data, column header, formatter). Distances are in cm.
_LAP_COLUMNS = [
    ("lapIndex", "#", lambda v: str(int(v))),
    ("distance", "Distance", lambda v: f"{float(v) / 100000:.2f} km"),
    ("time", "Time", _lap_clock),
    ("avgPace", "Pace", _lap_pace),
    ("avgHr", "Avg HR", lambda v: str(int(v))),
    ("maxHr", "Max HR", lambda v: str(int(v))),
    ("avgPower", "Power", lambda v: f"{int(v)} W"),
    ("avgCadence", "Cadence", lambda v: str(int(v))),
    ("avgStrideLength", "Stride", lambda v: f"{float(v) / 100:.2f} m"),
    ("elevGain", "Elev+", lambda v: f"{float(v):.0f} m"),
]


def _lap_group_label(group: dict) -> str:
    """Name a lap group from its data: manual (button) laps have irregular
    distances; auto laps repeat a fixed distance."""
    laps = group.get("laps") or []
    dists = [lap.get("distance", 0) for lap in laps]
    middles = dists[:-1]  # ignore the last (usually a partial) lap
    if middles and len(set(middles)) == 1:
        km = middles[0] / 100000
        return f"Auto laps ({km:g} km)" if km else "Auto laps"
    return "Manual laps"


def _laps_table(laps: list[dict]) -> list[str]:
    """Markdown table rows for one group's laps (columns kept only if they carry data)."""
    cols = [(n, h, f) for (n, h, f) in _LAP_COLUMNS
            if n in ("lapIndex", "distance", "time") or any(lap.get(n) for lap in laps)]
    rows = ["| " + " | ".join(h for _, h, _ in cols) + " |",
            "|" + "|".join("---" for _ in cols) + "|"]
    for lap in laps:
        cells = []
        for name, _h, fmt in cols:
            value = lap.get(name)
            try:
                cells.append(fmt(value) if value not in (None, "") else "—")
            except (TypeError, ValueError):
                cells.append("—")
        rows.append("| " + " | ".join(cells) + " |")
    return rows


def _render_laps(payload) -> str:
    """Render COROS lap JSON into Strava-style Markdown tables (empty str if none).

    Skips the whole-activity 'total' group (redundant with the detail block) and
    orders manual (button) laps first, then auto laps by distance."""
    if not isinstance(payload, dict):
        return ""
    groups = [(_lap_group_label(g), g) for g in (payload.get("lapGroups") or [])
              if len(g.get("laps") or []) > 1]  # >1 lap: drop the single "total" lap
    manual = [(lbl, g) for lbl, g in groups if lbl == "Manual laps"]
    autos = sorted((lg for lg in groups if lg[0] != "Manual laps"),
                   key=lambda lg: (lg[1].get("laps") or [{}])[0].get("distance", 1 << 60))
    # Manual (button) laps first, then only the finest auto splits (drop coarse 5/10 km).
    chosen = manual + autos[:1]
    blocks: list[str] = []
    for label, group in chosen:
        blocks += [f"### {label}", "", *_laps_table(group["laps"]), ""]
    return "\n".join(blocks).rstrip()


async def _activity_raw(session, sem: asyncio.Semaphore, args: dict, label_id: str,
                        cache_dir: Path | None, refresh: bool):
    """Return (raw detail text, raw lap payload) for one activity, from the on-disk
    cache when possible. COROS activities are immutable, so a labelId is cached
    forever (like the Strava exporter caches activity detail)."""
    cache_file = (cache_dir / f"coros_{label_id}.json") if cache_dir else None
    if cache_file and not refresh and cache_file.exists():
        try:
            cached = json.loads(cache_file.read_text())
            return cached.get("detail"), cached.get("laps")
        except (json.JSONDecodeError, OSError):
            pass

    async def _detail():
        async with sem:
            return await _call_text(session, "getActivityDetail", args)

    async def _laps():
        async with sem:
            return await _call_payload(session, "queryActivityLapData", args)

    detail_res, laps_res = await asyncio.gather(_detail(), _laps(), return_exceptions=True)
    detail_raw = None if isinstance(detail_res, BaseException) else detail_res
    laps_payload = None if isinstance(laps_res, BaseException) else laps_res
    if isinstance(detail_res, BaseException):
        log.warning("  getActivityDetail failed for %s: %s", label_id, detail_res)
    if isinstance(laps_res, BaseException):
        log.warning("  queryActivityLapData failed for %s: %s", label_id, laps_res)

    # Cache only a fully successful fetch, so a transient error is retried next time.
    if cache_file and not isinstance(detail_res, BaseException) and not isinstance(laps_res, BaseException):
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps({"detail": detail_raw, "laps": laps_payload}, ensure_ascii=False))
        except OSError:
            pass
    return detail_raw, laps_payload


async def _activity_section(session, sem: asyncio.Semaphore, rec: dict,
                            cache_dir: Path | None = None, refresh: bool = False) -> list[str]:
    """Markdown for one activity: its summary, then its detail + laps (cached by labelId)."""
    section = ["---", "", rec["text"].strip(), ""]
    if not rec["label_id"]:
        return section
    args = {"labelId": rec["label_id"], "sportType": rec["sport_type"]}
    detail_raw, laps_payload = await _activity_raw(session, sem, args, rec["label_id"], cache_dir, refresh)

    if detail_raw:
        section += [_strip_fields(_humanize_timestamps(detail_raw)).strip(), ""]
    laps_md = _render_laps(laps_payload) if laps_payload else ""
    if laps_md:
        section += [laps_md, ""]
    return section


async def gather_export(session, *, tool=None, after=None, before=None, limit=None,
                        json_args=None, detail=True, concurrency=DEFAULT_CONCURRENCY,
                        cache_dir: Path | None = None, refresh: bool = False) -> str:
    """Build the export: an Overview table, then each activity in full (summary,
    detail, laps). Per-activity detail/lap calls run concurrently (capped by
    ``concurrency``) and are cached by labelId under ``cache_dir``."""
    tools = (await session.list_tools()).tools
    chosen = _resolve_tool(tools, tool)
    ns = argparse.Namespace(after=after, before=before, limit=limit, json=json_args)
    arguments = _build_arguments(chosen, ns)
    summary = _strip_fields(_humanize_timestamps(await _call_text(session, chosen.name, arguments)))

    records = _parse_records(summary)
    if limit:
        records = records[:limit]

    parts = ["# COROS export", ""]
    if not records:  # couldn't parse the prose — emit it verbatim and stop
        return "\n".join(parts + ["## Activities", "", summary.strip()]).rstrip() + "\n"

    if detail:  # fetch detail + laps for all activities concurrently, in order
        log.info("Fetching detail + laps for %d activities (concurrency %d)…", len(records), concurrency)
        sem = asyncio.Semaphore(max(1, concurrency))
        section_lists = await asyncio.gather(
            *(_activity_section(session, sem, r, cache_dir, refresh) for r in records))
    else:
        section_lists = [["---", "", r["text"].strip(), ""] for r in records]

    parts += [_overview_table(records), "", "---", "", "## Activities", ""]
    for section in section_lists:
        parts += section
    return "\n".join(parts).rstrip() + "\n"


async def collect_markdown(store_dir: Path, *, after=None, before=None, limit=None,
                           tool=None, json_args=None, detail=True, concurrency=DEFAULT_CONCURRENCY,
                           cache_dir: Path | None = None, refresh: bool = False,
                           interactive: bool = False, redirect_uri: str | None = None,
                           write_files: bool = True) -> str:
    """Connect (with stored tokens) and return the COROS export as Markdown/text.

    Used by both the CLI and the web server. With ``interactive=False`` a missing
    token raises instead of opening a browser — the server authorizes separately.
    """
    await resolve_mcp_url()
    oauth = make_oauth(store_dir, redirect_uri=redirect_uri, interactive=interactive)
    async with mcp_session(oauth) as session:
        markdown = await gather_export(session, tool=tool, after=after, before=before,
                                       limit=limit, json_args=json_args, detail=detail,
                                       concurrency=concurrency, cache_dir=cache_dir, refresh=refresh)
    if write_files:
        store_dir.mkdir(parents=True, exist_ok=True)
        (store_dir / "coros_all_activities.md").write_text(markdown)
    return markdown


async def run(args: argparse.Namespace) -> int:
    store_dir: Path = args.data / COROS_SUBDIR
    await resolve_mcp_url()
    oauth = make_oauth(store_dir, port=args.port, interactive=True)

    async with mcp_session(oauth) as session:
        tools = (await session.list_tools()).tools
        log.info("Connected to COROS MCP — %d tools available.", len(tools))
        if args.list_tools:
            _print_tools(tools)
            return 0
        if args.raw:  # call the chosen tool once and dump the raw result for inspection
            tool = _resolve_tool(tools, args.tool)
            arguments = _build_arguments(tool, args)
            log.info("COROS call %s %s", tool.name, json.dumps(arguments, ensure_ascii=False) if arguments else "{}")
            result = await session.call_tool(tool.name, arguments, read_timeout_seconds=CALL_TIMEOUT)
            _dump_raw(result)
            return 0
        markdown = await gather_export(session, tool=args.tool, after=args.after,
                                       before=args.before, limit=args.limit,
                                       json_args=args.json, detail=args.detail,
                                       concurrency=args.concurrency,
                                       cache_dir=args.data / "cache" / "coros", refresh=args.refresh)

    store_dir.mkdir(parents=True, exist_ok=True)
    combined_path = store_dir / "coros_all_activities.md"
    combined_path.write_text(markdown)
    print(f"\n✔ Exported COROS activities → {combined_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Export your COROS activities to Markdown via the official COROS MCP.")
    p.add_argument("--data", type=Path, default=Path("data"),
                   help="Root data directory for tokens and reports (default: ./data).")
    p.add_argument("--after", help="Only activities on/after this date (YYYY-MM-DD or yyyyMMdd).")
    p.add_argument("--before", help="Only activities before this date (YYYY-MM-DD or yyyyMMdd).")
    p.add_argument("--limit", type=int, help="Keep at most N activities (newest first).")
    p.add_argument("--no-detail", dest="detail", action="store_false",
                   help="Skip per-activity detail + laps (default: fetch them). Detail adds "
                        "2 calls per activity — use --after/--limit for large histories.")
    p.set_defaults(detail=True)
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help=f"Max parallel MCP calls when fetching detail/laps (default: {DEFAULT_CONCURRENCY}).")
    p.add_argument("--refresh", action="store_true",
                   help="Re-fetch per-activity detail/laps even if cached (cache lives in <data>/cache/).")
    p.add_argument("--tool", help="Exact MCP tool name to call (default: auto-pick querySportRecords).")
    p.add_argument("--json", help="Exact tool arguments as a JSON object (overrides --after/--before/--limit).")
    p.add_argument("--list-tools", action="store_true", help="List the server's tools and their schemas, then exit.")
    p.add_argument("--raw", action="store_true", help="Print the raw tool result as JSON, then exit.")
    p.add_argument("--mcp-url", help=f"COROS MCP endpoint (default: {MCP_URL}; EU: https://mcpeu.coros.com/mcp). "
                                     "Can also be set via the COROS_MCP_URL env var.")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Local OAuth callback port (default: {DEFAULT_PORT}).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    if args.mcp_url:
        global MCP_URL
        MCP_URL = args.mcp_url
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
