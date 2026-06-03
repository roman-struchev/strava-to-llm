"""Multi-user web API around the exporter, meant to be called by a ChatGPT action.

    GET /activities?clientId=...&after=YYYY-MM-DD  ->  that user's activities as Markdown

Each user brings their own Strava API app. On the home page they enter their
Client ID + Secret and click "Connect Strava"; the secret and OAuth tokens are
stored per user on the server (data/users/<clientId>/). After that, every request
carries ?clientId=... so the server knows whose tokens to use. The activity cache
is shared across users (Strava activity IDs are globally unique).

If STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET are set in the environment, that user
is pre-registered and used as the default when no clientId is given.

Run:
    python server.py            # listens on $PORT (default 8000)
"""

import asyncio
import json
import os
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

from aiohttp import web
from dotenv import load_dotenv

from strava_export import (
    AUTHORIZE_URL, SCOPE, DailyLimitReached, StravaAuth, StravaClient,
    load_detail, parse_date, render_activity, render_combined, user_dir,
)

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CACHE_DIR = DATA_DIR / "cache"  # shared across users
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# -- per-user storage -------------------------------------------------------

def _save_secret(client_id: str, client_secret: str) -> None:
    d = user_dir(DATA_DIR, client_id)
    d.mkdir(parents=True, exist_ok=True)
    path = d / "secret.json"
    path.write_text(json.dumps({"client_id": str(client_id), "client_secret": client_secret}))
    path.chmod(0o600)


def _load_secret(client_id: str) -> dict | None:
    path = user_dir(DATA_DIR, client_id) / "secret.json"
    return json.loads(path.read_text()) if path.exists() else None


def _connected(client_id: str | None) -> bool:
    return bool(client_id) and (user_dir(DATA_DIR, client_id) / "tokens.json").exists()


def _auth_for(client_id: str) -> StravaAuth:
    secret = _load_secret(client_id)
    if not secret:
        raise web.HTTPBadRequest(text="Unknown clientId — register it on the home page first.")
    try:
        return StravaAuth(secret["client_id"], secret["client_secret"],
                          token_path=user_dir(DATA_DIR, client_id) / "tokens.json")
    except SystemExit as exc:
        raise web.HTTPInternalServerError(text=str(exc))


# Pre-register the env user, if configured, and use it as the default.
DEFAULT_CLIENT_ID = os.getenv("STRAVA_CLIENT_ID") or None
if DEFAULT_CLIENT_ID and os.getenv("STRAVA_CLIENT_SECRET"):
    _save_secret(DEFAULT_CLIENT_ID, os.getenv("STRAVA_CLIENT_SECRET"))


def _base_url(request: web.Request) -> str:
    """Public base URL, honouring a reverse proxy's forwarding headers."""
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    return f"{proto}://{host}"


# -- export -----------------------------------------------------------------

def _collect(client_id: str, after: int | None, before: int | None, limit: int | None) -> str:
    """Blocking: fetch from Strava and return (and persist) the combined Markdown."""
    client = StravaClient(_auth_for(client_id))
    athlete = client.get_athlete()
    summaries: list[dict] = []
    for summary in client.iter_activities(after, before):
        summaries.append(summary)
        if limit and len(summaries) >= limit:
            break
    detailed, sections = [], []
    try:
        for summary in summaries:
            detail, zones = load_detail(client, summary, CACHE_DIR, refresh=False,
                                        fetch_zones=bool(summary.get("has_heartrate")))
            detailed.append(detail)
            sections.append(render_activity(detail, zones))
    except DailyLimitReached:
        pass  # Strava's daily quota is spent — return what we managed to fetch.
    markdown = render_combined(athlete, detailed, sections)
    (user_dir(DATA_DIR, client_id) / "all_activities.md").write_text(markdown)  # latest report per user
    return markdown


# -- routes -----------------------------------------------------------------

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>Strava to LLM</title></head>
<body style="font-family: system-ui, sans-serif; max-width: 640px; margin: 48px auto; line-height: 1.6;">
<h1>Strava&nbsp;&rarr;&nbsp;LLM</h1>
{body}
</body></html>"""


def _login_page(cid: str) -> str:
    body = f"""<p>Connect your Strava account to export your activities.</p>
    <p>Enter your Strava API app credentials
      (<a href="https://www.strava.com/settings/api" target="_blank">create one here</a> —
      set the callback domain to this server's host):</p>
    <form action="/register" method="post">
      <p><input name="client_id" placeholder="Client ID" value="{cid}" required style="width:100%; padding:8px;"></p>
      <p><input name="client_secret" placeholder="Client Secret" required style="width:100%; padding:8px;"></p>
      <p><button type="submit" style="padding:10px 18px; background:#fc4c02; color:#fff;
         border:0; border-radius:6px; cursor:pointer;">Connect Strava</button></p>
    </form>"""
    return _PAGE.format(body=body)


def _profile_page(cid: str, athlete: dict | None, base_url: str) -> str:
    week_ago = (date.today() - timedelta(days=7)).isoformat()
    example = f"{base_url}/activities?clientId={cid}&after={week_ago}"
    if athlete:
        name = " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")])) or "—"
        location = ", ".join(filter(None, [athlete.get("city"), athlete.get("country")])) or "—"
        weight = f'{athlete.get("weight")} kg' if athlete.get("weight") else "—"
        rows = [("Name", name), ("Athlete ID", athlete.get("id")), ("Client ID", cid),
                ("Location", location), ("Sex", athlete.get("sex") or "—"), ("Weight", weight)]
        table = "".join(f"<tr><td style='color:#666; padding:2px 16px 2px 0'>{k}</td>"
                        f"<td><b>{v}</b></td></tr>" for k, v in rows)
        who = f"<table>{table}</table>"
    else:
        who = "<p>Connected, but the profile couldn't be loaded — the token may need reconnecting.</p>"
    body = f"""<p>✅ Connected to Strava.</p>
    {who}
    <p style="margin-top:24px;">Example — your last week of training:</p>
    <p><a href="{example}">{example}</a></p>
    <p style="color:#666;">Change the <code>after</code> date for other ranges.
      <a href="/?new=1">Connect a different account</a>.</p>"""
    return _PAGE.format(body=body)


async def index(request: web.Request) -> web.Response:
    cid = request.query.get("clientId") or DEFAULT_CLIENT_ID or ""
    if cid and _connected(cid) and not request.query.get("new"):
        try:
            athlete = await asyncio.to_thread(lambda: StravaClient(_auth_for(cid)).get_athlete())
        except (Exception, SystemExit):
            athlete = None
        return web.Response(text=_profile_page(cid, athlete, _base_url(request)), content_type="text/html")
    return web.Response(text=_login_page(cid), content_type="text/html")


async def register(request: web.Request) -> web.Response:
    form = await request.post()
    client_id = (form.get("client_id") or "").strip()
    client_secret = (form.get("client_secret") or "").strip()
    if not client_id or not client_secret:
        raise web.HTTPBadRequest(text="Both Client ID and Client Secret are required.")
    _save_secret(client_id, client_secret)
    raise web.HTTPFound(f"/login?clientId={client_id}")


async def login(request: web.Request) -> web.Response:
    """Send the user to Strava to authorize access (activity:read_all)."""
    client_id = request.query.get("clientId") or DEFAULT_CLIENT_ID
    if not client_id:
        raise web.HTTPBadRequest(text="clientId is required.")
    if not _load_secret(client_id):
        raise web.HTTPBadRequest(text="Unknown clientId — register it on the home page first.")
    params = {
        "client_id": client_id,
        "redirect_uri": f"{_base_url(request)}/callback",
        "response_type": "code",
        "approval_prompt": "auto",
        "scope": SCOPE,
        "state": client_id,  # so /callback knows which user this is
    }
    raise web.HTTPFound(f"{AUTHORIZE_URL}?{urlencode(params)}")


async def callback(request: web.Request) -> web.Response:
    """Strava redirects here with a code; exchange it and store tokens for this user."""
    if request.query.get("error") or not request.query.get("code"):
        raise web.HTTPBadRequest(text=f"Authorization failed: {request.query.get('error') or 'no code'}")
    client_id = request.query.get("state")
    if not client_id:
        raise web.HTTPBadRequest(text="Missing state (clientId).")
    _auth_for(client_id).authorize_with_code(request.query["code"])  # writes data/users/<id>/tokens.json
    raise web.HTTPFound(f"/?clientId={client_id}")


async def activities(request: web.Request) -> web.Response:
    client_id = request.query.get("clientId") or DEFAULT_CLIENT_ID
    if not client_id:
        raise web.HTTPBadRequest(text="clientId is required.")
    if not _connected(client_id):
        raise web.HTTPUnauthorized(text="Not connected. Open the home page and connect this clientId first.")
    try:
        after = parse_date(request.query.get("after"))
        before = parse_date(request.query.get("before"))
    except SystemExit as exc:
        raise web.HTTPBadRequest(text=str(exc))
    limit = int(request.query["limit"]) if request.query.get("limit") else None

    try:
        markdown = await asyncio.to_thread(_collect, client_id, after, before, limit)
    except web.HTTPException:
        raise
    except (Exception, SystemExit) as exc:
        raise web.HTTPBadGateway(text=f"Strava request failed: {exc}")
    return web.Response(text=markdown, content_type="text/markdown")


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.post("/register", register),
        web.get("/login", login),
        web.get("/callback", callback),
        web.get("/activities", activities),
        web.get("/health", health),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(os.getenv("PORT", "8000")))
