"""Multi-user web API around the exporter, meant to be called by a ChatGPT action.

    GET /export?clientId=...&after=YYYY-MM-DD  ->  that user's activities as Markdown

Each user brings their own Strava API app. On the home page they enter their
Client ID + Secret and click "Connect Strava"; the secret and OAuth tokens are
stored per user on the server (data/users/<clientId>/). After that, every request
carries ?clientId=... so the server knows whose tokens to use. The activity cache
is shared across users (Strava activity IDs are globally unique).

If STRAVA_CLIENT_ID / STRAVA_CLIENT_SECRET are set in the environment, that user
is pre-registered and used as the default when no clientId is given.

COROS is also supported via the official COROS MCP (see coros_mcp_export.py):
"Connect COROS" on the home page authorizes with a COROS account (no per-user
client id — one operator account, token stored under data/coros_mcp/), then:

    GET /coros/export[?after=YYYY-MM-DD&before=...&limit=N&detail=0]

Run:
    python server.py            # listens on $PORT (default 8000)
"""

import asyncio
import json
import logging
import os
import secrets
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from aiohttp import web
from dotenv import load_dotenv

from strava_export import (
    AUTHORIZE_URL, SCOPE, DailyLimitReached, RateLimited, StravaAuth, StravaClient,
    load_detail, parse_date, render_activity, render_combined, user_dir,
)
import coros_mcp_export as coros

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
CACHE_DIR = DATA_DIR / "cache" / "strava"    # Strava per-activity cache (shared across users)
COROS_CACHE_DIR = DATA_DIR / "cache" / "coros"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
COROS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
# Migrate the old flat cache (data/cache/*.json) into the per-provider subfolders.
for _f in (DATA_DIR / "cache").glob("activity_*.json"):
    _f.replace(CACHE_DIR / _f.name)
for _f in (DATA_DIR / "cache").glob("coros_*.json"):
    _f.replace(COROS_CACHE_DIR / _f.name)

# COROS is multi-user like Strava, but COROS has no user-supplied client id — so
# each connection gets a generated capability id; its tokens live in
# data/coros_mcp/<id>/ and the export link carries ?clientId=<id> (the id is the key).
COROS_DIR = DATA_DIR / coros.COROS_SUBDIR


def _coros_dir(client_id: str) -> Path:
    """Per-connection COROS token directory (client id sanitized against traversal)."""
    safe = "".join(c for c in str(client_id) if c.isalnum() or c in "-_")
    if not safe:
        raise web.HTTPBadRequest(text="Invalid COROS clientId.")
    return COROS_DIR / safe


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


def _load_athlete(client_id: str) -> dict | None:
    d = user_dir(DATA_DIR, client_id)
    if (d / "athlete.json").exists():
        return json.loads((d / "athlete.json").read_text())
    # Fall back to the athlete embedded in the initial token response, then cache it.
    if (d / "tokens.json").exists():
        athlete = json.loads((d / "tokens.json").read_text()).get("athlete")
        if athlete:
            _save_athlete(client_id, athlete)
            return athlete
    return None


def _save_athlete(client_id: str, athlete: dict) -> None:
    (user_dir(DATA_DIR, client_id) / "athlete.json").write_text(json.dumps(athlete))


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
    """Blocking: fetch from Strava and return (and persist) the combined Markdown.

    Uses wait_on_limit=False so it never blocks the HTTP response. If Strava's
    rate limit is hit mid-export, it returns whatever was fetched so far plus a
    note; cached activities make a follow-up request pick up the rest. Only when
    even the athlete can't be loaded does the limit propagate (handled as 429).
    """
    client = StravaClient(_auth_for(client_id), wait_on_limit=False)
    athlete = client.get_athlete()  # if this is rate-limited, the caller returns 429

    summaries: list[dict] = []
    detailed, sections = [], []
    truncated = False
    try:
        for summary in client.iter_activities(after, before):
            summaries.append(summary)
            if limit and len(summaries) >= limit:
                break
        for summary in summaries:
            detail, zones = load_detail(client, summary, CACHE_DIR, refresh=False,
                                        fetch_zones=bool(summary.get("has_heartrate")))
            detailed.append(detail)
            sections.append(render_activity(detail, zones))
    except (RateLimited, DailyLimitReached):
        truncated = True

    note = ""
    if truncated:
        note = (f"> ⚠️ Partial export — Strava rate limit reached after {len(detailed)} activities. "
                "Run the same request again in a few minutes to fetch the rest "
                "(already-fetched activities are cached).\n\n")
    markdown = note + render_combined(athlete, detailed, sections)
    (user_dir(DATA_DIR, client_id) / "all_activities.md").write_text(markdown)  # latest report per user
    return markdown


# -- routes -----------------------------------------------------------------

_REPO_URL = "https://github.com/roman-struchev/strava-to-llm"

_GITHUB_LINK = f"""<p><a class="gh" href="{_REPO_URL}" target="_blank" rel="noopener">
  <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"></path>
  </svg>View on GitHub</a></p>"""

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Strava &amp; COROS to LLM</title>
<style>
 body{font-family:system-ui,-apple-system,sans-serif;max-width:760px;margin:56px auto;padding:0 20px;line-height:1.6;color:#1a1a1a}
 h1{font-weight:700;font-size:26px;margin:0 0 6px}
 h2{font-weight:600;margin:0 0 10px}
 .cards{display:flex;flex-wrap:wrap;gap:20px;margin-top:28px}
 .card{flex:1 1 320px;--accent:#fc4c02;border:1px solid #eee;border-radius:14px;padding:22px 22px 26px;box-shadow:0 1px 3px rgba(0,0,0,.04)}
 .card.coros{--accent:#111}
 input{width:100%;padding:11px 12px;margin:6px 0;border:1px solid #ddd;border-radius:8px;font-size:15px;box-sizing:border-box}
 input:focus{outline:none;border-color:var(--accent)}
 .btn{display:inline-block;padding:11px 22px;background:var(--accent);color:#fff;border:0;border-radius:8px;font-size:15px;cursor:pointer;text-decoration:none}
 .btn:hover{filter:brightness(.92)}
 a{color:#fc4c02}
 .card.coros a:not(.btn){color:#111}
 .logout{margin-left:auto;padding:6px 12px;border:1px solid #ddd;border-radius:6px;color:#666;text-decoration:none;font-size:13px;white-space:nowrap}
 .logout:hover{border-color:var(--accent);color:var(--accent)}
 .avatar{width:52px;height:52px;border-radius:50%;object-fit:cover}
 .ex{list-style:none;padding:0;margin:0}
 .ex li{margin:12px 0}
 .ex a{word-break:break-all}
 .copy{margin-left:8px;padding:2px 9px;border:1px solid #ddd;border-radius:6px;background:#fff;color:#666;font-size:12px;cursor:pointer;vertical-align:middle}
 .copy:hover{border-color:var(--accent);color:var(--accent)}
 .copy:disabled{cursor:default;opacity:.7}
 .spinner{display:inline-block;width:10px;height:10px;border:2px solid #ccc;border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;vertical-align:-1px;margin-right:4px}
 @keyframes spin{to{transform:rotate(360deg)}}
 code{background:#f4f4f4;padding:1px 5px;border-radius:4px}
 .muted{color:#888;font-size:14px}
 .gh{display:inline-flex;align-items:center;gap:7px;color:#888;text-decoration:none;font-size:14px;margin-top:44px}
 .gh:hover{color:#1a1a1a}
</style></head><body>
{body}
<script>
function legacyCopy(text){  // for insecure origins (e.g. 0.0.0.0) where navigator.clipboard is absent
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed'; ta.style.left = '-9999px'; ta.style.top = '0';
  document.body.appendChild(ta);
  ta.focus(); ta.select();
  try { ta.setSelectionRange(0, text.length); } catch(e) {}
  let ok = false;
  try { ok = document.execCommand('copy'); } catch(e) { ok = false; }
  ta.remove();
  return ok;
}
async function copyExport(btn, url){
  const orig = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span>Fetching…';
  try{
    const resp = await fetch(url);
    if(!resp.ok) throw new Error('HTTP ' + resp.status);
    const text = await resp.text();
    let ok = false;
    if(navigator.clipboard && window.isSecureContext){
      try { await navigator.clipboard.writeText(text); ok = true; } catch(e) { ok = false; }
    }
    if(!ok) ok = legacyCopy(text);
    btn.textContent = ok ? 'Copied ✓' : 'Copy failed (needs https or localhost)';
  }catch(e){
    btn.textContent = 'Failed: ' + e.message;
  }
  setTimeout(() => { btn.innerHTML = orig; btn.disabled = false; }, 2500);
}
</script>
</body></html>"""


def _example_links(export_url: str) -> str:
    today = date.today()
    sep = "&" if "?" in export_url else "?"
    # (label, url, show a Copy button?) — the label itself is the link; the raw URL isn't shown.
    examples = [
        ("Last activity only", f"{export_url}{sep}limit=1", True),
        ("Last week", f"{export_url}{sep}after={(today - timedelta(days=7)).isoformat()}", False),
        ("This year", f"{export_url}{sep}after={today.year}-01-01", False),
    ]
    items = []
    for label, url, copyable in examples:
        copy_btn = (f'<button type="button" class="copy" onclick="copyExport(this, \'{url}\')">📋 Copy</button>'
                    if copyable else "")
        items.append(f'<li><a href="{url}">{label}</a>{copy_btn}</li>')
    return f'<ul class="ex" style="margin-top:20px;">{"".join(items)}</ul>'


def _strava_card(cid: str, athlete: dict | None, base_url: str) -> str:
    if not cid or not _connected(cid):
        body = f"""<h2>Connect Strava</h2>
      <p>Enter your Strava API app credentials
        (<a href="https://www.strava.com/settings/api" target="_blank">create one here</a> —
        set the callback domain to this server's host):</p>
      <form action="/register" method="post">
        <input name="client_id" placeholder="Client ID" value="{cid}" required>
        <input name="client_secret" placeholder="Client Secret" required>
        <p><button class="btn" type="submit">Connect Strava</button></p>
      </form>"""
        return f'<div class="card">{body}</div>'

    logout = '<a class="logout" href="/logout">Log out</a>'
    if athlete:
        name = " ".join(filter(None, [athlete.get("firstname"), athlete.get("lastname")])) or "—"
        avatar = athlete.get("profile") or athlete.get("profile_medium") or ""
        img = f'<img class="avatar" src="{avatar}" alt="">' if avatar.startswith("http") else ""
        header = f"""<div style="display:flex;align-items:center;gap:14px;">{img}
      <div><div style="font-size:20px;font-weight:600;">{name}</div>
        <div class="muted">Strava · Athlete {athlete.get("id")}</div></div>{logout}</div>"""
    else:
        header = (f'<div style="display:flex;align-items:center;"><div><h2>Connected to Strava</h2>'
                  '<p class="muted">Profile couldn\'t be loaded — the token may need reconnecting.</p>'
                  f"</div>{logout}</div>")
    links = _example_links(f"{base_url}/strava/export?clientId={cid}")
    return f'<div class="card">{header}{links}</div>'


def _coros_card(base_url: str, client_id: str) -> str:
    connected = bool(client_id) and (COROS_DIR / client_id / "mcp_tokens.json").exists()
    if not connected:
        body = """<h2>Connect COROS</h2>
      <p>Connect with your normal COROS account through the official
        <a href="https://coros.com/stories/coros-metrics/c/mcp-testing" target="_blank">COROS MCP</a> —
        no API application needed. You'll be sent to COROS to authorize.</p>
      <p><a class="btn" href="/coros/login">Connect COROS</a></p>
      <p class="muted">Alternatively, authorize once from the terminal:
        <code>python coros_mcp_export.py</code></p>"""
        return f'<div class="card coros">{body}</div>'

    logout = f'<a class="logout" href="/coros/logout?clientId={client_id}">Log out</a>'
    header = (f'<div style="display:flex;align-items:center;">'
              '<div><div style="font-size:20px;font-weight:600;">Connected to COROS</div>'
              '<div class="muted">COROS · via official MCP</div></div>'
              f'{logout}</div>')
    links = _example_links(f"{base_url}/coros/export?clientId={client_id}")
    return f'<div class="card coros">{header}{links}</div>'


async def index(request: web.Request) -> web.Response:
    base_url = _base_url(request)
    new = bool(request.query.get("new"))

    cid = request.query.get("clientId") or request.cookies.get("clientId") or DEFAULT_CLIENT_ID or ""
    athlete = None
    if cid and _connected(cid) and not new:
        athlete = _load_athlete(cid)  # stored at connect time — no API call, no hang
        if athlete is None:  # older session: fetch once (fail-fast on rate limit) and cache
            try:
                athlete = await asyncio.to_thread(
                    lambda: StravaClient(_auth_for(cid), wait_on_limit=False).get_athlete())
                _save_athlete(cid, athlete)
            except (Exception, SystemExit):
                athlete = None

    ccid = request.query.get("clientId") or request.cookies.get("corosClientId") or ""
    strava_card = _strava_card("" if new else cid, athlete, base_url)
    coros_card = _coros_card(base_url, "" if new else ccid)
    body = ('<h1>Export your activities to Markdown for an LLM</h1>'
            '<p class="muted">Connect Strava, COROS, or both. Each export endpoint returns clean '
            'Markdown you can hand to ChatGPT, Claude or any other model.</p>'
            f'<div class="cards">{strava_card}{coros_card}</div>{_GITHUB_LINK}')
    return web.Response(text=_PAGE.replace("{body}", body), content_type="text/html")


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
    error = request.query.get("error")
    if error == "access_denied":
        raise web.HTTPFound("/?new=1")  # user cancelled — quietly back to the login page
    if error or not request.query.get("code"):
        raise web.HTTPBadRequest(text=f"Authorization failed: {error or 'no code returned'}")
    client_id = request.query.get("state")
    if not client_id:
        raise web.HTTPBadRequest(text="Missing state (clientId).")
    tokens = _auth_for(client_id).authorize_with_code(request.query["code"])  # writes tokens.json
    if tokens.get("athlete"):
        _save_athlete(client_id, tokens["athlete"])  # so the profile page needs no API call
    resp = web.HTTPFound("/")
    resp.set_cookie("clientId", client_id, max_age=31536000, httponly=True, samesite="Lax")
    raise resp


async def logout(request: web.Request) -> web.Response:
    """Clear the saved clientId cookie and show the login page."""
    resp = web.HTTPFound("/?new=1")  # ?new=1 forces the login form even if a default user is set
    resp.del_cookie("clientId")
    raise resp


async def export(request: web.Request) -> web.Response:
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
    except (RateLimited, DailyLimitReached) as exc:
        retry = getattr(exc, "retry_after", 900) or 900
        raise web.HTTPTooManyRequests(
            text="Strava rate limit reached. Please try again in a few minutes.",
            headers={"Retry-After": str(retry)})
    except (Exception, SystemExit) as exc:
        raise web.HTTPBadGateway(text=f"Strava request failed: {exc}")
    return web.Response(text=markdown, content_type="text/markdown")


# -- COROS routes (official MCP) --------------------------------------------
# COROS uses OAuth driven by the MCP SDK. We bridge its (redirect_handler,
# callback_handler) pair — designed for a CLI — onto the web request/response
# cycle: /coros/login starts a background connect task, hands the browser the
# authorization URL, and /coros/callback feeds the returned code back to it.

# OAuth state -> {"code_fut": Future, "task": connect Task}. Keyed by state so
# concurrent connections (different users) don't collide.
_coros_pending: dict[str, dict] = {}


def _flatten_error(exc: BaseException | None) -> str:
    """Collapse a (possibly nested) ExceptionGroup into a concise one-line message."""
    if exc is None:
        return "unknown error"
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(_flatten_error(e) for e in exc.exceptions)
    return str(exc)


async def coros_login(request: web.Request) -> web.Response:
    base = _base_url(request)
    parsed = urlparse(base)
    # COROS's OAuth server rejects non-HTTPS callbacks except for loopback hosts.
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1"):
        raise web.HTTPBadRequest(text=(
            f"COROS requires an HTTPS callback for remote hosts like '{parsed.hostname}'. "
            f"Open this page via http://localhost:{parsed.port or 80} (loopback http is allowed), "
            "or serve the app behind HTTPS."))

    client_id = secrets.token_urlsafe(9)  # capability id for this connection (the export key)
    store_dir = COROS_DIR / client_id
    loop = asyncio.get_running_loop()
    auth_url_fut: asyncio.Future = loop.create_future()
    holder: dict = {}

    async def redirect_handler(authorization_url: str) -> None:
        state = parse_qs(urlparse(authorization_url).query).get("state", [None])[0]
        code_fut: asyncio.Future = loop.create_future()
        holder["code_fut"] = code_fut
        if state:
            _coros_pending[state] = {"code_fut": code_fut, "task": holder.get("task")}
        if not auth_url_fut.done():
            auth_url_fut.set_result(authorization_url)

    async def callback_handler() -> tuple[str, str | None]:
        return await holder["code_fut"]

    await coros.resolve_mcp_url()  # adopt COROS's regional endpoint before authorizing
    oauth = coros.make_oauth(store_dir, redirect_uri=f"{base}/coros/callback",
                             redirect_handler=redirect_handler, callback_handler=callback_handler)

    async def _connect() -> None:
        async with coros.mcp_session(oauth) as session:
            await session.list_tools()  # any authenticated call forces token storage

    task = asyncio.create_task(_connect())
    holder["task"] = task  # set synchronously before the loop runs the task
    # Whichever comes first: the authorization URL to redirect to, or a connect failure.
    done, _pending = await asyncio.wait({auth_url_fut, task}, timeout=45,
                                        return_when=asyncio.FIRST_COMPLETED)
    if auth_url_fut in done:
        resp = web.HTTPFound(auth_url_fut.result())
        resp.set_cookie("corosClientId", client_id, max_age=31536000, httponly=True, samesite="Lax")
        raise resp
    if task in done:  # finished before yielding a URL → it errored
        raise web.HTTPBadGateway(text=f"COROS authorization failed: {_flatten_error(task.exception())}")
    raise web.HTTPBadGateway(text="COROS authorization timed out — try again.")


async def coros_callback(request: web.Request) -> web.Response:
    error = request.query.get("error")
    if error == "access_denied":
        raise web.HTTPFound("/?new=1")  # user cancelled
    if error or not request.query.get("code"):
        raise web.HTTPBadRequest(text=f"Authorization failed: {error or 'no code returned'}")
    state = request.query.get("state")
    entry = _coros_pending.pop(state, None)
    if entry is None:
        raise web.HTTPBadRequest(text="Unexpected COROS callback — start again from the home page.")
    if not entry["code_fut"].done():
        entry["code_fut"].set_result((request.query["code"], state))
    task = entry.get("task")
    if task is not None:
        try:  # wait for the background task to exchange the code and store the token
            await asyncio.wait_for(asyncio.shield(task), timeout=30)
        except Exception:
            pass
    raise web.HTTPFound("/")


async def coros_logout(request: web.Request) -> web.Response:
    client_id = request.query.get("clientId") or request.cookies.get("corosClientId")
    if client_id:
        store_dir = _coros_dir(client_id)
        for name in ("mcp_tokens.json", "mcp_client.json"):
            path = store_dir / name
            if path.exists():
                path.unlink()
    resp = web.HTTPFound("/?new=1")
    resp.del_cookie("corosClientId")
    raise resp


async def coros_export(request: web.Request) -> web.Response:
    client_id = request.query.get("clientId")
    if not client_id:
        raise web.HTTPBadRequest(text="clientId is required.")
    store_dir = _coros_dir(client_id)
    if not coros.is_connected(store_dir):
        raise web.HTTPUnauthorized(text="Not connected. Open the home page and connect this COROS clientId first.")
    limit = int(request.query["limit"]) if request.query.get("limit") else None
    detail = request.query.get("detail", "1") not in ("0", "false", "no")  # on by default
    try:
        markdown = await coros.collect_markdown(
            store_dir, after=request.query.get("after"), before=request.query.get("before"),
            limit=limit, tool=request.query.get("tool"), detail=detail,
            cache_dir=COROS_CACHE_DIR, interactive=False)
    except web.HTTPException:
        raise
    except (Exception, SystemExit) as exc:
        raise web.HTTPBadGateway(text=f"COROS request failed: {exc}")
    return web.Response(text=markdown, content_type="text/markdown")


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.post("/register", register),
        web.get("/login", login),
        web.get("/logout", logout),
        web.get("/callback", callback),
        web.get("/export", export),         # legacy alias
        web.get("/strava/export", export),  # uniform with /coros/export
        web.get("/coros/login", coros_login),
        web.get("/coros/logout", coros_logout),
        web.get("/coros/callback", coros_callback),
        web.get("/coros/export", coros_export),
    ])
    return app


if __name__ == "__main__":
    web.run_app(make_app(), port=int(os.getenv("PORT", "8000")))
