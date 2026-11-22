#!/usr/bin/env python3
"""
Stromer collector: polls the Stromer cloud API (same API as the official app "Stromer OMNI"),
stores snapshots in SQLite and shows a small web page to check that it works.

Only reads data. Never sends commands to the bike and never asks the bike for live data
(always cached=true), so the bike's mobile connection is not used by this tool.

Standard library only.
"""
import base64
import hashlib
import html
import http.cookiejar
from html.parser import HTMLParser
import json
import os
import secrets
import sqlite3
import statistics
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.environ.get("DATA_DIR", "/data")
PORT = int(os.environ.get("PORT", "8095"))
POLL_STATE_MINUTES = float(os.environ.get("POLL_STATE_MINUTES", "5"))
POLL_SETTINGS_MINUTES = float(os.environ.get("POLL_SETTINGS_MINUTES", "30"))
POLL_STATISTICS_HOURS = float(os.environ.get("POLL_STATISTICS_HOURS", "6"))
# A ride ends when the bike sends no increasing distance for this long.
RIDE_GAP_MINUTES = float(os.environ.get("RIDE_GAP_MINUTES", "20"))

API = "https://api3.stromer-portal.ch/rapi/mobile/v4.1/"
AUTHORIZE = "https://stromer-portal.ch/mobile/v4/o/authorize/"
TOKEN = "https://stromer-portal.ch/mobile/v4/o/token/"
# Public client id of the official Android app. OAuth 2.0 authorization code with PKCE, no secret.
CLIENT_ID = "4P3VE9rBYdueKQioWb7nv7RJDU8EQsn2wiQaNqhG"
REDIRECT = "stromer://auth"
SCOPE = "bikestatus"
CLIENT_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "de-CH",
    "OsType": "Android",
    "OsVersion": "35",
    "AppVersion": "4.2.6 (2026070902)",
}

DB_PATH = os.path.join(DATA_DIR, "collector.db")
TOKEN_PATH = os.path.join(DATA_DIR, "token.json")

lock = threading.Lock()          # guards token file and pending login
wake = threading.Event()         # "poll now" button
pending_login: dict = {}         # state -> code_verifier


# --------------------------------------------------------------------------------------------
# storage

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS raw (
            id INTEGER PRIMARY KEY, kind TEXT, bike_id INTEGER, fetched_at REAL, ok INTEGER, body TEXT);
        CREATE INDEX IF NOT EXISTS raw_kind ON raw(kind, fetched_at);
        CREATE TABLE IF NOT EXISTS state (
            bike_id INTEGER, rcvts REAL, fetched_at REAL,
            soc REAL, health REAL, total_km REAL, total_s REAL, trip_km REAL, trip_s REAL,
            total_wh REAL, avg_energy REAL, assist REAL, speed REAL,
            battery_temp REAL, motor_temp REAL, locked INTEGER, theft INTEGER,
            PRIMARY KEY (bike_id, rcvts));
        CREATE TABLE IF NOT EXISTS position (
            bike_id INTEGER, timets REAL, rcvts REAL, fetched_at REAL,
            lat REAL, lon REAL, alt REAL, speed REAL,
            PRIMARY KEY (bike_id, timets));
        CREATE TABLE IF NOT EXISTS settings_log (
            bike_id INTEGER, fetched_at REAL, tuning_speed INTEGER, tuning_torque INTEGER,
            tuning_agility INTEGER, recup INTEGER, sensitivity INTEGER, body TEXT);
        CREATE TABLE IF NOT EXISTS daily (
            day TEXT PRIMARY KEY, km REAL, sec REAL, wh REAL, active_days REAL, fetched_at REAL);
        CREATE TABLE IF NOT EXISTS monthly (
            month TEXT PRIMARY KEY, km REAL, sec REAL, wh REAL, active_days REAL, total_days REAL, fetched_at REAL);
        CREATE TABLE IF NOT EXISTS polls (
            fetched_at REAL, kind TEXT, bike_id INTEGER, ok INTEGER, rcvts REAL, message TEXT);
        CREATE TABLE IF NOT EXISTS events (ts REAL, level TEXT, message TEXT);
        """)


def event(level: str, message: str):
    print(f"[{level}] {message}", flush=True)
    with db() as con:
        con.execute("INSERT INTO events VALUES (?,?,?)", (time.time(), level, message))
        con.execute("DELETE FROM events WHERE ts < ?", (time.time() - 30 * 86400,))


def save_raw(kind: str, bike_id, ok: bool, body: str):
    with db() as con:
        con.execute("INSERT INTO raw (kind, bike_id, fetched_at, ok, body) VALUES (?,?,?,?,?)",
                    (kind, bike_id, time.time(), int(ok), body))
        # Keep the last 200 answers per kind; parsed values stay forever.
        con.execute("""DELETE FROM raw WHERE kind = ? AND id NOT IN
                       (SELECT id FROM raw WHERE kind = ? ORDER BY id DESC LIMIT 200)""", (kind, kind))


# --------------------------------------------------------------------------------------------
# OAuth

def load_token():
    try:
        with open(TOKEN_PATH) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def save_token(tok: dict):
    tok = dict(tok)
    tok["expires_at"] = time.time() + float(tok.get("expires_in", 3600)) - 60
    tmp = TOKEN_PATH + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump(tok, f)
    os.replace(tmp, TOKEN_PATH)


def token_request(form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(TOKEN, data=data, headers={"Content-Type": "application/x-www-form-urlencoded",
                                                            "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"Token-Server antwortet {e.code}: {body}") from None


def start_login() -> str:
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    with lock:
        pending_login.clear()
        pending_login[state] = verifier
    return AUTHORIZE + "?" + urllib.parse.urlencode({
        "response_type": "code", "client_id": CLIENT_ID, "redirect_uri": REDIRECT, "scope": SCOPE,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def finish_login(redirect_url: str):
    q = urllib.parse.parse_qs(urllib.parse.urlparse(redirect_url.strip()).query)
    code, state = q.get("code", [None])[0], q.get("state", [None])[0]
    if not code or code == "...":
        raise RuntimeError("In der Adresse fehlt der Code. Die ganze Adresse stromer://auth?code=…&state=… einfügen.")
    with lock:
        verifier = pending_login.pop(state, None)
    if verifier is None:
        raise RuntimeError("Unbekannter oder alter Login-Versuch. Bitte „Login öffnen“ nochmals klicken.")
    tok = token_request({"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT,
                         "client_id": CLIENT_ID, "code_verifier": verifier})
    with lock:
        save_token(tok)
    event("info", "Login erfolgreich, Token gespeichert")
    wake.set()


class _GotCode(Exception):
    def __init__(self, url: str):
        self.url = url


class _StopAtApp(urllib.request.HTTPRedirectHandler):
    """Follows normal redirects, stops at stromer://auth and hands over that address."""

    def http_error_302(self, req, fp, code, msg, headers):
        loc = headers.get("Location", "")
        if loc.startswith(REDIRECT):
            raise _GotCode(loc)
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_302


class _Forms(HTMLParser):
    """Collects the input fields of the forms on a page."""

    def __init__(self):
        super().__init__()
        self.forms: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.forms.append({"action": a.get("action") or "", "fields": {}, "buttons": []})
        elif self.forms and tag in ("input", "button") and a.get("name"):
            if a.get("type") == "submit" or tag == "button":
                self.forms[-1]["buttons"].append((a["name"], a.get("value", "")))
            else:
                self.forms[-1]["fields"][a["name"]] = a.get("value") or ""


def password_login(username: str, password: str):
    """
    Logs in on stromer-portal.ch like the browser window of the official app: login form
    (with CSRF token and Referer), then the OAuth redirect to stromer://auth?code=...
    The password is used for this one request only, never stored or logged.
    """
    url = start_login()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), _StopAtApp())
    ua = {"User-Agent": "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/131 Mobile Safari/537.36",
          "Accept-Language": "de-CH,de;q=0.9"}

    def submit(page_url: str, form: dict, extra: dict):
        target = urllib.parse.urljoin(page_url, form["action"] or page_url)
        data = urllib.parse.urlencode({**form["fields"], **extra}).encode()
        req = urllib.request.Request(target, data=data, headers={**ua, "Referer": page_url,
                                                                 "Content-Type": "application/x-www-form-urlencoded"})
        return opener.open(req, timeout=30)

    try:
        r = opener.open(urllib.request.Request(url, headers=ua), timeout=30)
        page_url, body = r.geturl(), r.read().decode(errors="replace")
        sent_credentials = False
        for _ in range(3):  # login form, then possibly a consent form
            parser = _Forms()
            parser.feed(body)
            form = next((f for f in parser.forms if "password" in f["fields"]), None)
            if form:
                if sent_credentials:
                    raise RuntimeError("Stromer hat die Anmeldung abgelehnt. E-Mail und Passwort prüfen.")
                r = submit(page_url, form, {"username": username, "password": password})
                sent_credentials = True
            else:
                consent = next((f for f in parser.forms if f["buttons"] or "allow" in f["fields"]), None)
                if not consent:
                    raise RuntimeError("Unerwartete Antwort von Stromer, keine Weiterleitung zur App erhalten.")
                allow = next((b for b in consent["buttons"] if "allow" in b[0].lower()), ("allow", "Authorize"))
                r = submit(page_url, consent, {allow[0]: allow[1]})
            page_url, body = r.geturl(), r.read().decode(errors="replace")
        raise RuntimeError("Keine Weiterleitung zur App erhalten.")
    except _GotCode as got:
        finish_login(got.url)
    except urllib.error.HTTPError as ex:
        if ex.code == 403:
            raise RuntimeError("Stromer meldet 403 (CSRF). Bitte nochmals versuchen.") from None
        raise RuntimeError(f"Stromer antwortet {ex.code}.") from None


def access_token() -> str:
    with lock:
        tok = load_token()
        if not tok:
            raise PermissionError("Nicht angemeldet")
        if time.time() < tok.get("expires_at", 0):
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise PermissionError("Kein Refresh-Token, bitte neu anmelden")
        try:
            new = token_request({"grant_type": "refresh_token", "refresh_token": tok["refresh_token"],
                                 "client_id": CLIENT_ID})
        except RuntimeError as e:
            raise PermissionError(f"Token-Erneuerung fehlgeschlagen, bitte neu anmelden. {e}") from None
        # The server may rotate the refresh token; keep the old one if it does not send a new one.
        new.setdefault("refresh_token", tok["refresh_token"])
        save_token(new)
        return new["access_token"]


# --------------------------------------------------------------------------------------------
# API

class ApiError(Exception):
    pass


def api_get(path: str, params: dict | None = None):
    url = API + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={**CLIENT_HEADERS, "Authorization": "Bearer " + access_token()})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            body = r.read().decode()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 401:
            with lock:
                tok = load_token()
                if tok:
                    save_token({**tok, "expires_in": -60})  # force a refresh on the next call
        raise ApiError(f"HTTP {e.code} bei {path}: {body[:200]}") from None
    except OSError as e:
        raise ApiError(f"Keine Verbindung ({path}): {e}") from None
    obj = json.loads(body)
    if isinstance(obj, dict) and "result" in obj and str(obj["result"]).lower() not in ("ok", "queued"):
        raise ApiError(f"Server meldet {obj['result']} bei {path}")
    data = obj.get("data", obj) if isinstance(obj, dict) else obj
    return data, body


def first(data):
    if isinstance(data, list):
        return data[0] if data else {}
    return data or {}


def num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def poll_bikes() -> list:
    data, body = api_get("bike/")
    save_raw("bikes", None, True, body)
    return data if isinstance(data, list) else [data]


def poll_state(bike_id: int):
    data, body = api_get(f"bike/{bike_id}/state/", {"cached": "true"})
    s = first(data)
    save_raw("state", bike_id, True, body)
    rcvts = num(s.get("rcvts"))
    with db() as con:
        con.execute("INSERT INTO polls VALUES (?,?,?,?,?,?)", (time.time(), "state", bike_id, 1, rcvts, None))
        if rcvts:
            con.execute("""INSERT OR IGNORE INTO state VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                bike_id, rcvts, time.time(), num(s.get("battery_SOC")), num(s.get("battery_health")),
                num(s.get("total_distance")), num(s.get("total_time")), num(s.get("trip_distance")),
                num(s.get("trip_time")), num(s.get("total_energy_consumption")),
                num(s.get("average_energy_consumption")), num(s.get("assistance_level")), num(s.get("bike_speed")),
                num(s.get("battery_temp")), num(s.get("motor_temp")),
                int(bool(s.get("lock_flag"))), int(bool(s.get("theft_flag")))))


def poll_position(bike_id: int):
    data, body = api_get(f"bike/{bike_id}/position/", {"cached": "true"})
    p = first(data)
    save_raw("position", bike_id, True, body)
    timets = num(p.get("timets")) or num(p.get("rcvts"))
    if timets and p.get("latitude") is not None:
        with db() as con:
            con.execute("INSERT OR IGNORE INTO position VALUES (?,?,?,?,?,?,?,?)", (
                bike_id, timets, num(p.get("rcvts")), time.time(), num(p.get("latitude")),
                num(p.get("longitude")), num(p.get("altitude")), num(p.get("speed"))))


def poll_settings(bike_id: int):
    # The server accepts only the field groups of the official app; mixing groups gives "syntax_error".
    s, bodies = {}, []
    for fields in ("tuning_speed,tuning_torque,tuning_agility", "recup_level_user_offset,user_torque_sensitivity"):
        data, body = api_get(f"bike/{bike_id}/settings/", {"fields": fields})
        s.update(first(data))
        bodies.append(json.loads(body))
    body = json.dumps(bodies)
    save_raw("settings", bike_id, True, body)
    row = (s.get("tuning_speed"), s.get("tuning_torque"), s.get("tuning_agility"),
           s.get("recup_level_user_offset"), s.get("user_torque_sensitivity"))
    with db() as con:
        last = con.execute("""SELECT tuning_speed, tuning_torque, tuning_agility, recup, sensitivity
                              FROM settings_log WHERE bike_id = ? ORDER BY fetched_at DESC LIMIT 1""",
                           (bike_id,)).fetchone()
        if last is None or tuple(last) != row:
            con.execute("INSERT INTO settings_log VALUES (?,?,?,?,?,?,?,?)", (bike_id, time.time(), *row, body))
            if last is not None:
                event("info", f"Einstellung geändert: Tuning {row[0]}/{row[1]}/{row[2]}")


def poll_statistics():
    end = datetime.now()
    start = end - timedelta(days=400)
    data, body = api_get("bike/statistics/", {"start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"),
                                              "resolution": "days"})
    save_raw("statistics_days", None, True, body)
    rows = data if isinstance(data, list) else [data]
    with db() as con:
        for r in rows:
            day = str(r.get("start") or "")[:10].replace("-", "")
            if len(day) == 8:
                con.execute("INSERT OR REPLACE INTO daily VALUES (?,?,?,?,?,?)", (
                    day, num(r.get("km")), num(r.get("sec")), num(r.get("wh")), num(r.get("active_days")), time.time()))
    try:
        _, body = api_get("bike/statistics/all/")
        save_raw("statistics_all", None, True, body)
    except ApiError as e:
        event("warn", str(e))


def poll_info(bike_id: int):
    """Service booklet (incl. dealer and warranty) and display settings; change rarely."""
    _, body = api_get(f"bike/{bike_id}/service_info/")
    save_raw("service_info", bike_id, True, body)
    fields = "auto_lock_mode,auto_power_off_time,date_format,distance_unit,language,speed_unit,clock_format"
    _, body = api_get(f"bike/{bike_id}/settings/", {"fields": fields})
    save_raw("settings_units", bike_id, True, body)


def poll_months():
    """Monthly statistics since the first record (the server has data from about 2022)."""
    end = datetime.now()
    data, body = api_get("bike/statistics/", {"start": "20150101", "end": end.strftime("%Y%m%d"), "resolution": "months"})
    save_raw("statistics_months", None, True, body)
    rows = data if isinstance(data, list) else [data]
    with db() as con:
        for r in rows:
            m = str(r.get("start") or "")[:6]
            if len(m) == 6:
                con.execute("INSERT OR REPLACE INTO monthly VALUES (?,?,?,?,?,?,?)", (
                    m, num(r.get("km")), num(r.get("sec")), num(r.get("wh")), num(r.get("active_days")),
                    num(r.get("total_days")), time.time()))
    try:
        _, body = api_get("bike/statistics/extra_data/", {"end": end.strftime("%Y%m%d"), "resolution": "days"})
        save_raw("statistics_extra", None, True, body)
    except ApiError as e:
        event("warn", str(e))


def poller():
    last = {"state": 0.0, "settings": 0.0, "statistics": 0.0}
    bikes: list = []
    while True:
        now = time.time()
        try:
            if not load_token():
                pass  # wait for login
            else:
                if not bikes or now - last["statistics"] >= POLL_STATISTICS_HOURS * 3600:
                    bikes = poll_bikes()
                    poll_statistics()
                    poll_months()
                    for b in bikes:
                        poll_info(b.get("bikeid"))
                    last["statistics"] = now
                if now - last["state"] >= POLL_STATE_MINUTES * 60:
                    for b in bikes:
                        bid = b.get("bikeid")
                        poll_state(bid)
                        poll_position(bid)
                    last["state"] = now
                if now - last["settings"] >= POLL_SETTINGS_MINUTES * 60:
                    for b in bikes:
                        poll_settings(b.get("bikeid"))
                    last["settings"] = now
        except PermissionError as e:
            event("error", str(e))
        except ApiError as e:
            event("warn", str(e))
            with db() as con:
                con.execute("INSERT INTO polls VALUES (?,?,?,?,?,?)", (time.time(), "error", None, 0, None, str(e)))
        except Exception:  # keep the thread alive
            event("error", traceback.format_exc(limit=3))
        if wake.wait(30):
            wake.clear()
            last = {k: 0.0 for k in last}


# --------------------------------------------------------------------------------------------
# analysis

def rides(bike_id: int, days: int = 30) -> list:
    """Rides from the counters: consecutive uploads with increasing distance."""
    with db() as con:
        rows = [dict(r) for r in con.execute(
            "SELECT * FROM state WHERE bike_id = ? AND rcvts > ? ORDER BY rcvts",
            (bike_id, time.time() - days * 86400))]
        settings = [dict(r) for r in con.execute(
            "SELECT * FROM settings_log WHERE bike_id = ? ORDER BY fetched_at", (bike_id,))]
        positions = [dict(r) for r in con.execute(
            "SELECT * FROM position WHERE bike_id = ? ORDER BY timets", (bike_id,))]
    out, current = [], None
    for prev, cur in zip(rows, rows[1:]):
        moved = (cur["total_km"] or 0) > (prev["total_km"] or 0) + 0.05
        if moved:
            # Same ride if the previous increase is recent; otherwise a new ride starts at the last standstill value.
            if current and current["end"] is prev and cur["rcvts"] - prev["rcvts"] <= RIDE_GAP_MINUTES * 60:
                current["end"] = cur
                current["uploads"] += 1
            else:
                current = {"start": prev, "end": cur, "uploads": 1}
                out.append(current)
    result = []
    for r in out:
        s, e = r["start"], r["end"]
        km = (e["total_km"] or 0) - (s["total_km"] or 0)
        wh = (e["total_wh"] - s["total_wh"]) if e["total_wh"] is not None and s["total_wh"] is not None else None
        soc = (s["soc"] - e["soc"]) if s["soc"] is not None and e["soc"] is not None else None
        tun = next((t for t in reversed(settings) if t["fetched_at"] <= s["rcvts"] + 60), None)

        def near(ts):
            best = min(positions, key=lambda p: abs(p["timets"] - ts), default=None)
            return best if best and abs(best["timets"] - ts) < 3600 else None

        result.append({
            "start": s["rcvts"], "end": e["rcvts"], "km": km, "wh": wh, "soc_used": soc,
            "wh_per_km": wh / km if wh is not None and km > 0.5 else None,
            "pct_per_km": soc / km if soc is not None and km > 0.5 else None,
            "minutes": (e["rcvts"] - s["rcvts"]) / 60, "uploads": r["uploads"],
            "tuning": f"{tun['tuning_speed']}/{tun['tuning_torque']}/{tun['tuning_agility']}" if tun else None,
            "from": near(s["rcvts"]), "to": near(e["rcvts"]),
        })
    return list(reversed(result))


def upload_stats(bike_id: int, hours: int) -> dict:
    with db() as con:
        ts = [r[0] for r in con.execute("SELECT rcvts FROM state WHERE bike_id = ? AND rcvts > ? ORDER BY rcvts",
                                        (bike_id, time.time() - hours * 3600))]
        polls = con.execute("SELECT COUNT(*) FROM polls WHERE kind = 'state' AND fetched_at > ?",
                            (time.time() - hours * 3600,)).fetchone()[0]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    return {"uploads": len(ts), "polls": polls,
            "median_min": statistics.median(gaps) / 60 if gaps else None,
            "min_min": min(gaps) / 60 if gaps else None,
            "max_h": max(gaps) / 3600 if gaps else None}


# --------------------------------------------------------------------------------------------
# web

def fmt_ts(ts) -> str:
    return datetime.fromtimestamp(ts).strftime("%d.%m.%Y %H:%M") if ts else "–"


def fmt_num(v, d=0, unit="") -> str:
    if v is None:
        return "–"
    s = f"{v:,.{d}f}".replace(",", "'")
    return f"{s} {unit}".strip()


def ago(ts) -> str:
    if not ts:
        return "–"
    d = time.time() - ts
    if d < 120:
        return "gerade eben"
    if d < 7200:
        return f"vor {int(d // 60)} min"
    if d < 172800:
        return f"vor {int(d // 3600)} h"
    return f"vor {int(d // 86400)} Tagen"


def e(v) -> str:
    return html.escape(str(v)) if v is not None else "–"


CSS = """
:root{--bg:#f5f6f7;--card:#fff;--fg:#15191d;--muted:#5a646e;--line:#dde2e6;--accent:#b38600;--ok:#1e8a50;--bad:#c62828}
@media (prefers-color-scheme:dark){:root{--bg:#0e1114;--card:#181d22;--fg:#e6e8ea;--muted:#9aa4ae;--line:#2a3138;--accent:#f5b700;--ok:#2fb36d;--bad:#e5484d}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,-apple-system,Segoe UI,sans-serif}
header{padding:18px 16px;display:flex;align-items:center;gap:12px;max-width:1100px;margin:auto}
header h1{font-size:20px;margin:0;flex:1}main{max-width:1100px;margin:auto;padding:0 16px 40px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px;margin:12px 0}
.card h2{font-size:15px;margin:0 0 10px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.tile{background:var(--bg);border-radius:12px;padding:10px 12px}.tile b{display:block;font-size:20px}
.tile span{font-size:12px;color:var(--muted);text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:14px}th,td{padding:6px 8px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}td.n,th.n{text-align:right}.scroll{overflow-x:auto}
.ok{color:var(--ok)}.bad{color:var(--bad)}.muted{color:var(--muted)}
button,.btn{background:var(--accent);color:#1b1400;border:0;border-radius:10px;padding:9px 14px;font:inherit;font-weight:600;cursor:pointer;text-decoration:none;display:inline-block}
input[type=text]{width:100%;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit}
pre{white-space:pre-wrap;word-break:break-all;font-size:12px;max-height:420px;overflow:auto;background:var(--bg);padding:10px;border-radius:10px}
canvas{max-height:260px}
"""


def page(title: str, body: str) -> bytes:
    return f"""<!doctype html><html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{e(title)}</title>
<style>{CSS}</style></head><body>
<header><h1>Stromer Datensammler</h1><a class="btn" href="/">Mein Stromer</a><a class="btn" href="/daten">Daten</a><a class="btn" href="/login">Login</a></header>
<main>{body}</main></body></html>""".encode()


def dashboard() -> str:
    tok = load_token()
    with db() as con:
        last_poll = con.execute("SELECT MAX(fetched_at) FROM polls WHERE ok = 1").fetchone()[0]
        errors = con.execute("SELECT COUNT(*) FROM polls WHERE ok = 0 AND fetched_at > ?",
                             (time.time() - 86400,)).fetchone()[0]
        bikes_raw = con.execute("SELECT body FROM raw WHERE kind = 'bikes' AND ok = 1 ORDER BY id DESC LIMIT 1").fetchone()
        events_rows = con.execute("SELECT * FROM events ORDER BY ts DESC LIMIT 15").fetchall()
        daily_rows = con.execute("SELECT * FROM daily WHERE km > 0 ORDER BY day DESC LIMIT 60").fetchall()
    bikes = []
    if bikes_raw:
        d = json.loads(bikes_raw[0])
        d = d.get("data", d) if isinstance(d, dict) else d
        bikes = d if isinstance(d, list) else [d]

    status = (f'<span class="ok">angemeldet</span>' if tok else '<span class="bad">nicht angemeldet</span> – <a href="/login">jetzt anmelden</a>')
    out = [f"""<div class="card"><h2>Status</h2><div class="grid">
<div class="tile"><span>Stromer-Zugang</span><b>{status}</b></div>
<div class="tile"><span>Letzte Abfrage</span><b>{ago(last_poll)}</b></div>
<div class="tile"><span>Fehler 24 h</span><b class="{'bad' if errors else 'ok'}">{errors}</b></div>
<div class="tile"><span>Intervall</span><b>{fmt_num(POLL_STATE_MINUTES)} min</b></div>
</div><form method="post" action="/poll" style="margin-top:12px"><button>Jetzt abfragen</button></form></div>"""]

    for b in bikes:
        bid = b.get("bikeid")
        with db() as con:
            st = con.execute("SELECT * FROM state WHERE bike_id = ? ORDER BY rcvts DESC LIMIT 1", (bid,)).fetchone()
            pos = con.execute("SELECT * FROM position WHERE bike_id = ? ORDER BY timets DESC LIMIT 1", (bid,)).fetchone()
            tun = con.execute("SELECT * FROM settings_log WHERE bike_id = ? ORDER BY fetched_at DESC LIMIT 1", (bid,)).fetchone()
        name = b.get("nickname") or b.get("bikemodel") or f"Bike {bid}"
        u24, u7 = upload_stats(bid, 24), upload_stats(bid, 24 * 7)
        out.append(f"""<div class="card"><h2>{e(name)} · {e(b.get('bikemodel'))}</h2><div class="grid">
<div class="tile"><span>Akku</span><b>{fmt_num(st['soc'] if st else None, 0, '%')}</b></div>
<div class="tile"><span>Kilometerstand</span><b>{fmt_num(st['total_km'] if st else None, 1, 'km')}</b></div>
<div class="tile"><span>Energiezähler</span><b>{fmt_num(st['total_wh'] if st else None, 0, 'Wh')}</b></div>
<div class="tile"><span>Ø Verbrauch (Bike)</span><b>{fmt_num(st['avg_energy'] if st else None)}</b></div>
<div class="tile"><span>Daten vom Bike</span><b>{ago(st['rcvts'] if st else None)}</b></div>
<div class="tile"><span>Position</span><b>{ago(pos['timets'] if pos else None)}</b></div>
<div class="tile"><span>Tuning Stufe 2</span><b>{f"{tun['tuning_speed']}/{tun['tuning_torque']}/{tun['tuning_agility']}" if tun else '–'}</b></div>
<div class="tile"><span>Schloss</span><b>{('gesperrt' if st['locked'] else 'offen') if st else '–'}</b></div>
</div></div>
<div class="card"><h2>Wie oft schickt das Bike Daten?</h2>
<p class="muted">Neue Datenstände vom Bike (Zeitstempel rcvts). Grundlage für die Frage, ob die Cloud für eine Auswertung pro Fahrt reicht.</p>
<table><tr><th>Zeitraum</th><th class="n">Abfragen</th><th class="n">neue Stände</th><th class="n">Abstand Median</th><th class="n">kürzester</th><th class="n">längste Lücke</th></tr>
<tr><td>24 Stunden</td><td class="n">{u24['polls']}</td><td class="n">{u24['uploads']}</td><td class="n">{fmt_num(u24['median_min'], 1, 'min')}</td><td class="n">{fmt_num(u24['min_min'], 1, 'min')}</td><td class="n">{fmt_num(u24['max_h'], 1, 'h')}</td></tr>
<tr><td>7 Tage</td><td class="n">{u7['polls']}</td><td class="n">{u7['uploads']}</td><td class="n">{fmt_num(u7['median_min'], 1, 'min')}</td><td class="n">{fmt_num(u7['min_min'], 1, 'min')}</td><td class="n">{fmt_num(u7['max_h'], 1, 'h')}</td></tr>
</table></div>
<div class="card"><h2>Verlauf 7 Tage</h2><canvas id="chart-{bid}"></canvas></div>""")

        rs = rides(bid)
        rows = "".join(f"""<tr><td>{fmt_ts(r['start'])}</td><td class="n">{fmt_num(r['minutes'])} min</td><td class="n">{fmt_num(r['km'], 1)}</td>
<td class="n">{fmt_num(r['wh'])}</td><td class="n">{fmt_num(r['wh_per_km'], 1)}</td><td class="n">{fmt_num(r['soc_used'])}</td>
<td class="n">{fmt_num(r['pct_per_km'], 2)}</td><td class="n">{r['uploads']}</td><td>{e(r['tuning'])}</td>
<td class="muted">{_place(r['from'])} → {_place(r['to'])}</td></tr>""" for r in rs)
        out.append(f"""<div class="card"><h2>Erkannte Fahrten (30 Tage)</h2>
<p class="muted">Aus den Zählern: Kilometerstand steigt zwischen zwei Datenständen. „Stände“ = wie viele Datenstände während der Fahrt ankamen.</p>
<div class="scroll"><table><tr><th>Start</th><th class="n">Dauer</th><th class="n">km</th><th class="n">Wh</th><th class="n">Wh/km</th>
<th class="n">Akku %</th><th class="n">%/km</th><th class="n">Stände</th><th>Tuning</th><th>Von → nach</th></tr>
{rows or '<tr><td colspan="10" class="muted">Noch keine Fahrt erkannt.</td></tr>'}</table></div></div>""")

    drows = "".join(f"""<tr><td>{r['day'][6:8]}.{r['day'][4:6]}.{r['day'][:4]}</td><td class="n">{fmt_num(r['km'], 1)}</td>
<td class="n">{fmt_num((r['sec'] or 0) / 60)} min</td><td class="n">{fmt_num(r['wh'])}</td>
<td class="n">{fmt_num(r['wh'] / r['km'] if r['wh'] and r['km'] else None, 1)}</td>
<td class="n">{fmt_num(r['km'] / (r['sec'] / 3600) if r['sec'] else None, 1)}</td></tr>""" for r in daily_rows)
    out.append(f"""<div class="card"><h2>Tagesstatistik vom Server (letzte 60 Fahrtage)</h2><div class="scroll"><table>
<tr><th>Tag</th><th class="n">km</th><th class="n">Fahrzeit</th><th class="n">Wh</th><th class="n">Wh/km</th><th class="n">Ø km/h</th></tr>
{drows or '<tr><td colspan="6" class="muted">Noch keine Daten.</td></tr>'}</table></div></div>""")

    erows = "".join(f"<tr><td>{fmt_ts(r['ts'])}</td><td class=\"{'bad' if r['level'] == 'error' else 'muted'}\">{e(r['level'])}</td>"
                    f"<td style='white-space:normal'>{e(r['message'])}</td></tr>" for r in events_rows)
    out.append(f"""<div class="card"><h2>Protokoll</h2><table>{erows or '<tr><td class="muted">Leer</td></tr>'}</table>
<p><a href="/raw?kind=state">Rohdaten Zustand</a> · <a href="/raw?kind=position">Position</a> · <a href="/raw?kind=settings">Einstellungen</a> ·
<a href="/raw?kind=statistics_all">Statistik gesamt</a> · <a href="/raw?kind=bikes">Bikes</a></p></div>""")

    out.append("""<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.6/dist/chart.umd.min.js"></script>
<script>
document.querySelectorAll('canvas[id^=chart-]').forEach(async c => {
  const id = c.id.slice(6);
  const d = await (await fetch('/api/series?bike=' + id)).json();
  const fmt = t => new Date(t * 1000).toLocaleString('de-CH', {weekday:'short', hour:'2-digit', minute:'2-digit'});
  new Chart(c, {type: 'line', data: {labels: d.t.map(fmt), datasets: [
    {label: 'Akku %', data: d.soc, yAxisID: 'y', borderColor: '#f5b700', pointRadius: 1, tension: .2},
    {label: 'km seit Start', data: d.km, yAxisID: 'y2', borderColor: '#3e8ef7', pointRadius: 1, stepped: true},
    {label: 'Wh seit Start', data: d.wh, yAxisID: 'y2', borderColor: '#2fb36d', pointRadius: 1, stepped: true}]},
    options: {animation: false, interaction: {mode: 'index', intersect: false},
      scales: {y: {min: 0, max: 100, title: {display: true, text: '%'}}, y2: {position: 'right', grid: {display: false}}}}});
});
</script>""")
    return "".join(out)


def _place(p) -> str:
    if not p:
        return "?"
    return f'<a href="https://www.openstreetmap.org/?mlat={p["lat"]:.5f}&mlon={p["lon"]:.5f}#map=16/{p["lat"]:.5f}/{p["lon"]:.5f}">{p["lat"]:.3f}, {p["lon"]:.3f}</a>'


def latest(kind: str, bike_id=None):
    """Data of the newest stored answer of one kind (envelope removed, settings groups merged)."""
    with db() as con:
        if bike_id is None:
            r = con.execute("SELECT body, fetched_at FROM raw WHERE kind = ? AND ok = 1 ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
        else:
            r = con.execute("SELECT body, fetched_at FROM raw WHERE kind = ? AND bike_id = ? AND ok = 1 ORDER BY id DESC LIMIT 1",
                            (kind, bike_id)).fetchone()
    if not r:
        return None
    obj = json.loads(r["body"])
    parts = obj if kind == "settings" and isinstance(obj, list) else [obj]
    merged = None
    for part in parts:
        d = part.get("data", part) if isinstance(part, dict) else part
        if kind in ("bikes", "statistics_days", "statistics_months"):
            return d
        d = first(d)
        merged = {**(merged or {}), **d} if isinstance(d, dict) else d
    return merged


def bikes_list() -> list:
    d = latest("bikes")
    return (d if isinstance(d, list) else [d]) if d else []


def overview(bike_id: int) -> dict:
    with db() as con:
        track = [dict(r) for r in con.execute(
            "SELECT timets, lat, lon, alt FROM position WHERE bike_id = ? AND timets > ? ORDER BY timets",
            (bike_id, time.time() - 7 * 86400))]
        last_poll = con.execute("SELECT MAX(fetched_at) FROM polls WHERE ok = 1").fetchone()[0]
        today = datetime.now()
        week = (today - timedelta(days=today.weekday())).strftime("%Y%m%d")
        periods = {}
        for key, since in (("week", week), ("month", today.strftime("%Y%m01")), ("year", today.strftime("%Y0101"))):
            r = con.execute("SELECT SUM(km), SUM(sec), SUM(wh), COUNT(*) FROM daily WHERE day >= ? AND km > 0", (since,)).fetchone()
            periods[key] = {"km": r[0], "sec": r[1], "wh": r[2], "days": r[3]}
    bike = next((b for b in bikes_list() if b.get("bikeid") == bike_id), {})
    return {
        "bike": bike, "state": latest("state", bike_id), "position": latest("position", bike_id),
        "settings": latest("settings", bike_id), "units": latest("settings_units", bike_id),
        "service": latest("service_info", bike_id), "all": latest("statistics_all"),
        "extra": latest("statistics_extra"), "track": track, "periods": periods,
        "last_poll": last_poll, "logged_in": load_token() is not None,
    }


def stats_payload(bike_id: int) -> dict:
    with db() as con:
        months = [dict(r) for r in con.execute("SELECT * FROM monthly ORDER BY month")]
        days = [dict(r) for r in con.execute("SELECT * FROM daily WHERE day >= ? ORDER BY day",
                                             ((datetime.now() - timedelta(days=90)).strftime("%Y%m%d"),))]
        # Years from the monthly statistics (complete since the first record), best day from the daily data.
        years = [dict(r) for r in con.execute(
            "SELECT substr(month,1,4) AS year, SUM(km) km, SUM(sec) sec, SUM(wh) wh, SUM(active_days) days, "
            "(SELECT MAX(km) FROM daily WHERE substr(day,1,4) = substr(month,1,4)) best "
            "FROM monthly WHERE km > 0 GROUP BY year ORDER BY year")]
    return {"months": months, "days": days, "years": years, "all": latest("statistics_all"),
            "extra": latest("statistics_extra"), "rides": rides(bike_id, 60)}


def series(bike_id: int) -> dict:
    with db() as con:
        rows = con.execute("SELECT rcvts, soc, total_km, total_wh FROM state WHERE bike_id = ? AND rcvts > ? ORDER BY rcvts",
                           (bike_id, time.time() - 7 * 86400)).fetchall()
    km0 = next((r["total_km"] for r in rows if r["total_km"] is not None), 0)
    wh0 = next((r["total_wh"] for r in rows if r["total_wh"] is not None), 0)
    return {"t": [r["rcvts"] for r in rows], "soc": [r["soc"] for r in rows],
            "km": [(r["total_km"] - km0) if r["total_km"] is not None else None for r in rows],
            "wh": [(r["total_wh"] - wh0) if r["total_wh"] is not None else None for r in rows]}


def login_page(message: str = "") -> str:
    return f"""<div class="card"><h2>Bei Stromer anmelden</h2>
{f'<p class="bad">{e(message)}</p>' if message else ''}
<form method="post" action="/login-password" autocomplete="on">
<p><input type="text" name="username" placeholder="E-Mail des Stromer-Kontos" autocomplete="username"></p>
<p><input type="password" name="password" placeholder="Passwort" autocomplete="current-password"
   style="width:100%;padding:10px;border-radius:10px;border:1px solid var(--line);background:var(--bg);color:var(--fg);font:inherit"></p>
<p><button>Anmelden</button></p></form>
<p class="muted">mynas meldet sich damit einmal bei stromer-portal.ch an, wie das Login-Fenster der offiziellen App.
Das Passwort wird nicht gespeichert und nicht protokolliert, nur der Zugangs-Token bleibt auf mynas ({e(TOKEN_PATH)}).
Die Verbindung zu dieser Seite ist unverschlüsselt (HTTP im Heimnetz).</p></div>
<details class="card"><summary>Ohne Passwort: Code von Hand kopieren</summary>{manual_login()}</details>"""


def manual_login() -> str:
    url = start_login()
    return f"""<div>
<p class="muted">Stromer schickt nach dem Login an <code>stromer://auth?code=…</code>. Der Browser kann das nicht öffnen,
es passiert scheinbar nichts. Die Adresse steckt aber im Netzwerk-Protokoll. Bist du bei Stromer schon angemeldet,
kommt gar kein Login-Formular, nur dieser Sprung.</p>
<ol>
<li>In Chrome oder Edge einen <b>neuen Tab</b> öffnen.</li>
<li>Dort <b>F12</b> drücken, Tab <b>„Netzwerk“</b> wählen, Haken bei <b>„Log beibehalten“</b> (Preserve log).</li>
<li>Diese Adresse kopieren und im neuen Tab in die Adresszeile einfügen:
<input type="text" id="authurl" readonly value="{e(url)}" onclick="this.select()">
<p><button type="button" onclick="navigator.clipboard.writeText(document.getElementById('authurl').value);this.textContent='Kopiert'">Adresse kopieren</button></p></li>
<li>Falls ein Formular kommt: bei Stromer anmelden. Das Passwort gibst du nur auf stromer-portal.ch ein.</li>
<li>Im Netzwerk-Protokoll den Eintrag <b>„authorize/?response_type=…“</b> mit Status <b>302</b> anklicken.
Unter „Antwort-Header“ steht <b>location: stromer://auth?code=…</b>. Diesen ganzen Wert kopieren.</li>
<li>Innerhalb von etwa 60 Sekunden hier einfügen:</li></ol>
<form method="post" action="/login"><input type="text" name="url" placeholder="stromer://auth?code=...&state=...">
<p><button>Anmelden</button></p></form>
<p class="muted">Jedes Laden dieser Seite erzeugt eine neue Adresse; immer die aktuelle verwenden.
Der Zugang (Token) bleibt auf mynas in {e(TOKEN_PATH)} und wird nie angezeigt.</p></div>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code: int, body: bytes, ctype="text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, to: str):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                with open(os.path.join(os.path.dirname(__file__), "static", "index.html"), "rb") as f:
                    self.send(200, f.read())
            elif u.path == "/daten":
                self.send(200, page("Stromer Datensammler", dashboard()))
            elif u.path == "/api/bikes":
                self.send(200, json.dumps(bikes_list()).encode(), "application/json")
            elif u.path == "/api/overview":
                self.send(200, json.dumps(overview(int(q["bike"][0]))).encode(), "application/json")
            elif u.path == "/api/stats":
                self.send(200, json.dumps(stats_payload(int(q["bike"][0]))).encode(), "application/json")
            elif u.path == "/login":
                self.send(200, page("Login", login_page()))
            elif u.path == "/api/series":
                self.send(200, json.dumps(series(int(q["bike"][0]))).encode(), "application/json")
            elif u.path == "/raw":
                kind = q.get("kind", ["state"])[0]
                with db() as con:
                    r = con.execute("SELECT fetched_at, body FROM raw WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)).fetchone()
                pretty = json.dumps(json.loads(r["body"]), indent=2, ensure_ascii=False) if r else "Noch keine Daten."
                self.send(200, page("Rohdaten", f'<div class="card"><h2>{e(kind)} · {fmt_ts(r["fetched_at"] if r else None)}</h2><pre>{e(pretty)}</pre></div>'))
            elif u.path == "/health":
                self.send(200, b"ok", "text/plain")
            else:
                self.send(404, page("Nicht gefunden", "<p>Nicht gefunden.</p>"))
        except Exception:
            self.send(500, page("Fehler", f"<pre>{e(traceback.format_exc(limit=3))}</pre>"))

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        form = urllib.parse.parse_qs(self.rfile.read(length).decode())
        if self.path == "/login-password":
            try:
                password_login(form.get("username", [""])[0].strip(), form.get("password", [""])[0])
                self.redirect("/")
            except Exception as ex:
                self.send(200, page("Login", login_page(str(ex))))
        elif self.path == "/login":
            try:
                finish_login(form.get("url", [""])[0])
                self.redirect("/")
            except Exception as ex:
                self.send(200, page("Login", login_page(str(ex))))
        elif self.path == "/poll":
            wake.set()
            time.sleep(3)
            self.redirect("/daten")
        else:
            self.send(404, b"")


def main():
    init_db()
    event("info", f"Gestartet, Abfrage alle {POLL_STATE_MINUTES:g} min")
    threading.Thread(target=poller, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
