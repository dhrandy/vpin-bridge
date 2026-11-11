#!/usr/bin/env python3
"""
VPin Bridge - private API bridge for a pinball cabinet.

- Local Docker service: http://<docker-host>:8744.
- Keep the service private behind trusted routing.
- Auth: X-Api-Key header, set with the API_KEY environment variable.
- The bridge forwards requests to the VPin Studio API on the LAN.

Cabinet: a Visual Pinball cabinet, VPin Studio Server port 8089, LAN only.
Cabinet IP comes only from env var CABINET_IP, set in Dockhand.
It is never in code, README, repo, logs, or inventory.

Endpoints (all under /api, except /health):
  GET  /health
    {"ok": true, "cabinet_reachable": bool, "game_playing": bool}
  GET  /api/games
    List tables, from cabinet GET /api/v1/export/tables/plain
    (fallback: GET /api/v1/games)
  GET  /api/status
    Cabinet GET /api/v1/gamestatus, active when gameId > 0.
    Adds a "text" field with the current table's display name,
    resolved from the games export, so the dashboard can show a
    live "now playing" banner. When no game is active, "text" is
    "" and "active" is false.
  GET  /api/games/json
    List tables as JSON, from cabinet GET /api/v1/games
    (id + gameDisplayName, for picking a table to play)
  GET  /api/highscores?gameId=<id>
    High scores, from cabinet GET /api/v1/export/highscores/plain.
    Returns {"ok": true, "highscores": [...]} parsed from the plain
    text export (each line one entry, with its score), or
    {"ok": true, "raw": "<text>"} if the text cannot be parsed.
    Each entry also carries the table's lastPlayed from the games
    export (cabinet GET /api/v1/export/tables/plain), and entries
    are sorted by lastPlayed descending, most recently played
    first, so the first entry is the table last played. Tables
    with no lastPlayed fall back to their "modified" date from the
    games export for sorting, so a played table does not sink to
    the bottom just because lastPlayed is blank; tables with
    neither date sort last. Optional gameId filters to one table.
    Each entry also carries "lastScore": the score from the game
    most recently played on that table, detected by comparing the
    table's top-10 score list against the previous fetch (in-memory
    state, lost on bridge restart). "lastScore" is null until a
    genuinely new score appears after the bridge starts, so the
    existing top score is never mislabeled as the last score;
    "lastScoreInitials" and "lastScoreAt" (detection time) ride
    along. One exception: a table with exactly one recorded score
    has its lastScore set to that score on the first fetch, since
    a single recorded score is necessarily the most recent (only)
    score; lastScoreAt is "" for that case because the export
    carries no per-score time. The top-score fields ("score",
    "playerInitials") are unchanged.
  POST /api/play
    JSON {"gameId": <int>}. Launches a table via the frontend,
    cabinet GET /api/v1/frontend/launch/<gameId>.
    Refuses when a game is already playing. NOT blocked by DRY_RUN.
  POST /api/upload
    Multipart form-data, field "file" = table archive (.zip/.7z/.vpa),
    plus fields mode=uploadAndImport|uploadAndReplace, gameId, emuId.
    Forwards to POST /api/v1/games/upload, then
    POST /api/v1/games/process with the returned UploadDescriptor.
    Refuses when a game is playing.
  POST /api/backglass
    Multipart, field "file" = .directb2s, gameId in form.
    Forwards to POST /api/v1/directb2s/upload, assetType=DIRECTB2S
  POST /api/puppack
    Multipart, field "file" = PuP pack archive.
    Forwards to POST /api/v1/puppacks/upload, assetType=PUP_PACK
  POST /api/rom?gameId=<id>
    Multipart, field "file" = ROM zip (e.g. gldneye.zip).
    Forwards to POST /api/v1/assets/<id>/upload/0?assetType=ROM.
    Blocked by DRY_RUN, like the other uploads.
  POST /api/media?screen=PlayField|Wheel|BackGlass&append=false
    Multipart, field "file" = media file.
    Forwards to POST /api/v1/media/upload/<SCREEN>/<append>
    Screen names are case-sensitive enum names: Wheel, PlayField,
    BackGlass, GameInfo, GameHelp, Topper, DMD, Loading, Menu,
    Other2, Logo, Audio. Default screen is PlayField.
  GET  /api/media/<gameId>
    Frontend media for a table, cabinet GET /api/v1/frontend/media/<id>
  GET  /api/thumb/<gameId>
    Playfield picture for a table: streams the cabinet's
    GET /api/v1/media/<id>/PlayField?preview=true, which returns
    the image, or a PNG frame when the PlayField media is a video.
  GET  /api/scorehistory/<gameId>
    Read-only passthrough to cabinet GET /api/v1/games/scorehistory/<id>.
    Timestamped highscore-list snapshots, not a per-play results log.
  GET  /api/details/<gameId>
    Table details / metadata, cabinet GET /api/v1/frontend/tabledetails/<id>
  POST /api/details/<gameId>
    JSON TableDetails. Cabinet POST /api/v1/frontend/tabledetails/<id>.
    NOT blocked by DRY_RUN.
  GET  /api/playlists
    Cabinet GET /api/v1/playlists
  GET  /api/validations/<gameId>
    Live validation for a table, cabinet GET /api/v1/games/validations/<id>.
    Returns the current issue list, for example code 36 means the
    BackGlass image is missing, code 38 means the PlayField is missing.
  GET  /api/reload/<gameId>
    Reload one table, cabinet GET /api/v1/games/reload/<id>. Use after
    uploading media, so the validation list updates.
  POST /api/playlists/<playlistId>/<gameId>
    Add a table to a playlist, cabinet PUT /api/v1/playlists/<id>/<gameId>/0.
    NOT blocked by DRY_RUN. Use for the Home playlist, so a table is visible.

VPin endpoints verified Nov 7, 2026 from public VPin Studio source,
main branch, vpin-studio-rest-client. Caveat: main branch, not a
pinned 5.6.1 tag, confirm on the cabinet version before first
real import. DRY_RUN=true (default in compose) refuses all uploads.
"""
import json
import hmac
import ipaddress
import os
import re
import sys
import threading
import urllib.request
import urllib.error
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # A configured service must not forward credentials or actions elsewhere.
        return None

_UPSTREAM = urllib.request.build_opener(_NoRedirect())

def upstream_open(request, timeout):
    return _UPSTREAM.open(request, timeout=timeout)


PORT = int(os.environ.get("BRIDGE_PORT", "8080"))
CABINET_IP = os.environ.get("CABINET_IP", "")
VPIN_PORT = os.environ.get("VPIN_PORT", "8089")
API_KEY = os.environ.get("API_KEY", "")
# Wrong-key lockout. Valid keys are never throttled or delayed; only a
# presented key that does not match counts, so scans and health checks are
# unaffected. X-Forwarded-For is trusted only when the direct peer is a
# configured reverse proxy, so clients cannot spoof their identity.
AUTH_MAX_ATTEMPTS = int(os.environ.get("BRIDGE_AUTH_MAX_ATTEMPTS", "5"))
AUTH_WINDOW_SECONDS = int(os.environ.get("BRIDGE_AUTH_WINDOW_SECONDS", "900"))
TRUSTED_PROXIES = [ipaddress.ip_network(x.strip(), strict=False)
                   for x in os.environ.get("BRIDGE_TRUSTED_PROXIES", "").split(",") if x.strip()]
_AUTH_FAILURES = {}
_AUTH_FAILURES_LOCK = threading.Lock()
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes")

# Last-score tracking state (in-memory only, lost on bridge restart).
# _SCORE_BASELINE: gameId -> set of (score, initials) pairs from the
# most recent highscores fetch. _LAST_SCORE: gameId -> the most
# recently detected new score, {"score", "initials", "detectedAt"}.
# A table is baselined on its first appearance; its lastScore stays
# null until a genuinely new score appears on a later fetch, so the
# existing top score is never mislabeled as the last score on
# startup. Guarded by _SCORE_STATE_LOCK: the server is threaded
# and two concurrent highscores fetches must not interleave a
# compare-and-store.
_UPLOAD_LOCK = threading.BoundedSemaphore(1)
MAX_UPLOAD_BYTES = 512 * 1024 * 1024
_CONTROL_LOCK = threading.Lock()
_SCORE_STATE_LOCK = threading.Lock()
_SCORE_BASELINE = {}
_LAST_SCORE = {}

MAX_JSON_RESPONSE = 16 * 1024 * 1024

def bounded_response(response, limit=MAX_JSON_RESPONSE):
    body = response.read(limit + 1)
    if len(body) > limit:
        raise ValueError("Cabinet response exceeds byte limit")
    return body

def cabinet_base():
    if not CABINET_IP:
        raise RuntimeError("CABINET_IP is not set in Dockhand Environment")
    return f"http://{CABINET_IP}:{VPIN_PORT}"

def cabinet_get(path, max_bytes=16*1024*1024):
    req = urllib.request.Request(cabinet_base() + path, method="GET")
    with upstream_open(req, timeout=15) as resp:
        body = bounded_response(resp, max_bytes)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

def cabinet_get_bytes(path, timeout=60):
    """Raw passthrough for cabinet endpoints that return files
    instead of JSON. Returns (content_type, body_bytes)."""
    req = urllib.request.Request(cabinet_base() + path, method="GET")
    with upstream_open(req, timeout=timeout) as resp:
        body=resp.read(32*1024*1024+1)
        if len(body)>32*1024*1024:raise ValueError("Image exceeds byte limit")
        return resp.headers.get("Content-Type", "application/octet-stream"),body

def cabinet_post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cabinet_base() + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with upstream_open(req, timeout=60) as resp:
        body = bounded_response(resp)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

def cabinet_put_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cabinet_base() + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="PUT")
    with upstream_open(req, timeout=60) as resp:
        body = bounded_response(resp)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

def cabinet_delete_json(path):
    req = urllib.request.Request(cabinet_base() + path, method="DELETE")
    with upstream_open(req, timeout=60) as resp:
        body = bounded_response(resp)
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}


def _parse_play_date(s):
    """Parse a cabinet date like "Nov 8, 2026, 3:41:42 PM".

    The cabinet writes a narrow no-break space (U+202F) before the
    AM/PM marker, so all whitespace is normalized to a plain space
    first. Returns a datetime, or datetime.min when the value is
    empty or unparseable, so such entries sort last.
    """
    s = "".join(" " if ch.isspace() else ch for ch in (s or "")).strip()
    if not s:
        return datetime.min
    try:
        return datetime.strptime(s, "%b %d, %Y, %I:%M:%S %p")
    except ValueError:
        return datetime.min


def parse_last_played_map(text):
    """Parse the cabinet's games export into a gameId -> dates map.

    The games export is semicolon-delimited CSV, one table per line,
    with a header row that includes `id`, `lastPlayed` and `modified`
    columns. Returns a dict
    {gameId: (lastPlayed string as-is, parsed lastPlayed datetime,
    parsed modified datetime)}.
    The lastPlayed string is "" when the table has never been played.
    The modified datetime is datetime.min when the header has no
    `modified` column or the value is empty/unparseable.
    Returns an empty dict if the header has no id/lastPlayed columns
    or no rows parse, so the caller can fall back to the createdAt
    sort instead of failing.
    """
    last_played = {}
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return last_played
    header = lines[0].rstrip("\r").split(";")
    if "id" not in header or "lastPlayed" not in header:
        return last_played
    id_idx = header.index("id")
    lp_idx = header.index("lastPlayed")
    mod_idx = header.index("modified") if "modified" in header else None
    max_idx = max(i for i in (id_idx, lp_idx, mod_idx) if i is not None)
    for line in lines[1:]:
        fields = line.rstrip("\r").split(";")
        if len(fields) <= max_idx:
            continue
        try:
            game_id = int(fields[id_idx].strip())
        except ValueError:
            continue
        lp = fields[lp_idx].strip()
        mod_dt = datetime.min
        if mod_idx is not None:
            mod_dt = _parse_play_date(fields[mod_idx].strip())
        last_played[game_id] = (lp, _parse_play_date(lp), mod_dt)
    return last_played


def parse_game_names_map(text):
    """Parse the cabinet's games export into a gameId -> display name map.

    The games export is semicolon-delimited CSV, one table per line,
    with a header row that includes `id` and `gameDisplayName`
    columns. Returns a dict {gameId: <gameDisplayName string as-is>}.
    Returns an empty dict if the header has no id/gameDisplayName
    columns or no rows parse.
    """
    names = {}
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return names
    header = lines[0].rstrip("\r").split(";")
    if "id" not in header or "gameDisplayName" not in header:
        return names
    id_idx = header.index("id")
    name_idx = header.index("gameDisplayName")
    max_idx = max(id_idx, name_idx)
    for line in lines[1:]:
        fields = line.rstrip("\r").split(";")
        if len(fields) <= max_idx:
            continue
        try:
            game_id = int(fields[id_idx].strip())
        except ValueError:
            continue
        name = fields[name_idx].strip()
        if name:
            names[game_id] = name
    return names


def _highscore_pairs(fields):
    """All (score, initials) pairs from one highscores export line.

    fields is the semicolon-split line: gameId, name, createdAt,
    then up to ten (initials, score) pairs starting at index 3
    (initials1 at 3, score1 at 4, initials2 at 5, score2 at 6,
    ... initials10 at 21, score10 at 22). Pairs whose score does
    not parse are skipped; an empty initials field is kept as "".
    Returns a list of (score:int, initials:str).
    """
    pairs = []
    for k in range(10):
        i_idx = 3 + 2 * k
        s_idx = 4 + 2 * k
        if s_idx >= len(fields):
            break
        try:
            score = int(fields[s_idx].strip().replace(",", ""))
        except ValueError:
            continue
        pairs.append((score, fields[i_idx].strip()))
    return pairs


def parse_highscores_plain(text, last_played=None):
    """Parse the cabinet's plain-text highscores export into a list.

    The export is semicolon-delimited CSV, one table per line, with
    a header row:
        gameId;gameDisplayName;createdAt;playerInitials1;score1;
        playerInitials2;score2;...;playerInitials10;score10;
    Example data line:
        1;Leprechaun King;Nov 8, 2026, 5:08:08 PM;SCO;1,300,000;
        TTY;1,200,000;WIC;1,100,000;NM1;1,000,000;...

    The header line (starting with "gameId;") is skipped. For each
    data line the top score is score1 (field 4), with the top
    player's initials in field 3.

    The createdAt field (field 2) is the same export-generation time
    on every line, so it cannot order tables by recency. When a
    last_played map (from parse_last_played_map, built from the
    games export) is given, each entry gets a "lastPlayed" field
    (string as-is, "" when never played) and entries are sorted by
    lastPlayed descending, most recently played first. Some tables
    have been played but carry an empty lastPlayed in the games
    export (the cabinet does not always stamp it); for those, the
    table's "modified" date from the games export is used as the
    sort key instead, so a played table does not sink to the bottom
    just because lastPlayed is blank. The displayed "lastPlayed"
    text is never changed by this fallback, only the sort order.
    Entries with neither a lastPlayed nor a modified date sort last.
    Without a map, entries keep the createdAt sort as a fallback.
    Each entry also carries a private "_score_set" frozenset of all
    (score, initials) pairs on its line, for track_last_scores to
    compare against the previous fetch; the handler strips it via
    track_last_scores before the entries are sent. When a line has
    exactly one populated score slot, the entry also carries a
    private "_single_score" (score, initials) pair, so
    track_last_scores can report it as the table's lastScore: a
    single recorded score is necessarily the most recent (only)
    score, so it needs no baseline. Lines with two or more slots
    carry "_single_score" as None.
    Returns a list of
    {"gameId": int, "text": <gameDisplayName>, "score": <score1 int>,
    "playerInitials": <initials1>,
    "createdAt": <createdAt string as-is>,
    "lastPlayed": <lastPlayed string as-is>} dicts, or None if no
    data line parses, so the caller can fall back to returning the
    raw text.
    """
    entries = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("gameId;"):
            continue
        fields = line.split(";")
        if len(fields) < 5:
            continue
        try:
            game_id = int(fields[0].strip())
        except ValueError:
            continue
        name = fields[1].strip()
        if not name:
            continue
        try:
            score = int(fields[4].strip().replace(",", ""))
        except ValueError:
            continue
        created_at = fields[2].strip()
        if last_played:
            lp_str, lp_dt, mod_dt = last_played.get(
                game_id, ("", datetime.min, datetime.min))
            # Prefer the real lastPlayed; fall back to the modified
            # date when lastPlayed is blank (the cabinet does not
            # always stamp it even for played tables).
            sort_key = lp_dt if lp_dt != datetime.min else mod_dt
        else:
            lp_str = ""
            sort_key = _parse_play_date(created_at)
        pairs = _highscore_pairs(fields)
        entries.append({"gameId": game_id, "text": name, "score": score,
                        "playerInitials": fields[3].strip(),
                        "createdAt": created_at, "lastPlayed": lp_str,
                        "_sort_key": sort_key,
                        "_score_set": frozenset(pairs),
                        "_single_score": pairs[0] if len(pairs) == 1 else None})
    if not entries:
        return None
    entries.sort(key=lambda e: e["_sort_key"], reverse=True)
    for entry in entries:
        del entry["_sort_key"]
    return entries


def track_last_scores(entries):
    """Detect newly appeared scores and attach lastScore to entries.

    Compares each entry's full score list (the "_score_set"
    frozenset attached by parse_highscores_plain) against the set
    stored from the previous highscores fetch. On a table's first
    appearance its current set is stored as the baseline and its
    lastScore stays null, so no existing score is mislabeled as
    the last score when the bridge starts (or restarts). On later
    fetches, any (score, initials) pair not in the previous set is
    a new score from a game just played; the newest detected one
    is recorded as that table's lastScore, even when it is not the
    top score. If several new pairs appear in one fetch (several
    games between polls), the smallest new score is recorded, the
    one inserted lowest in the ranking. Each entry gets "lastScore"
    (int, or None), "lastScoreInitials" (str, "" when none) and
    "lastScoreAt" (ISO detection time, "" when none); a lastScore
    once detected persists across later unchanged fetches. The
    "_score_set" scratch field is removed. Exception: a table with
    exactly one recorded score gets that score as its lastScore on
    the first fetch (from the "_single_score" pair attached by
    parse_highscores_plain), since a single recorded score is
    necessarily the most recent (only) score; its lastScoreAt is
    "" because the export carries no per-score time. A detected
    lastScore always wins over the single-score fallback, and
    once a table has two or more scores the fallback no longer
    applies. State is in-memory only and is lost when the bridge
    restarts.
    """
    now = datetime.now().isoformat(timespec="seconds")
    with _SCORE_STATE_LOCK:
        for entry in entries:
            game_id = entry["gameId"]
            current = entry.pop("_score_set", frozenset())
            single = entry.pop("_single_score", None)
            prev = _SCORE_BASELINE.get(game_id)
            if prev is None:
                # First sight of this table: baseline only, never
                # label an existing score as the last score.
                _SCORE_BASELINE[game_id] = set(current)
            else:
                new_pairs = current - prev
                if new_pairs:
                    # Newest detected score; smallest wins the tie
                    # when several games landed between fetches.
                    score, initials = min(new_pairs, key=lambda p: p[0])
                    _LAST_SCORE[game_id] = {
                        "score": score, "initials": initials,
                        "detectedAt": now}
                _SCORE_BASELINE[game_id] = set(current)
            last = _LAST_SCORE.get(game_id)
            if last is None and single is not None:
                # Exactly one recorded score: it is the last score.
                entry["lastScore"] = single[0]
                entry["lastScoreInitials"] = single[1]
                entry["lastScoreAt"] = ""
            elif last is None:
                entry["lastScore"] = None
                entry["lastScoreInitials"] = ""
                entry["lastScoreAt"] = ""
            else:
                entry["lastScore"] = last["score"]
                entry["lastScoreInitials"] = last["initials"]
                entry["lastScoreAt"] = last["detectedAt"]
    return entries

def valid_id(value, allow_zero=False):
    return isinstance(value,str) and re.fullmatch(r'[0-9]{1,10}',value) is not None and (0 if allow_zero else 1) <= int(value) <= 2147483647


def client_ip(handler):
    peer = (getattr(handler, 'client_address', None) or ('', 0))[0]
    try:
        peer_ip = ipaddress.ip_address(peer)
    except ValueError:
        return peer
    if not any(peer_ip in net for net in TRUSTED_PROXIES):
        return peer
    chain = [x.strip() for x in handler.headers.get("X-Forwarded-For", "").split(",") if x.strip()]
    for candidate in reversed(chain):
        try:
            hop = ipaddress.ip_address(candidate)
        except ValueError:
            break
        if any(hop in net for net in TRUSTED_PROXIES):
            continue
        return candidate
    return peer


def auth_throttled(ip):
    now = datetime.now().timestamp()
    with _AUTH_FAILURES_LOCK:
        count, until = _AUTH_FAILURES.get(ip, (0, 0))
        return (count >= AUTH_MAX_ATTEMPTS and now < until), max(0, int(until - now))


def record_auth_failure(ip):
    global _AUTH_FAILURES
    now = datetime.now().timestamp()
    with _AUTH_FAILURES_LOCK:
        if len(_AUTH_FAILURES) > 10000:
            _AUTH_FAILURES = {k: v for k, v in _AUTH_FAILURES.items() if v[1] > now}
        count, until = _AUTH_FAILURES.get(ip, (0, 0))
        if now >= until:
            count, until = 0, now + AUTH_WINDOW_SECONDS
        _AUTH_FAILURES[ip] = (count + 1, until)


def clear_auth_failures(ip):
    with _AUTH_FAILURES_LOCK:
        _AUTH_FAILURES.pop(ip, None)


class Handler(BaseHTTPRequestHandler):
    server_version = "vpin-bridge"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def end_headers(self):
        self.send_header("Cache-Control", "private, no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        super().end_headers()

    def log_message(self, *args):
        pass

    def _auth(self):
        # True when authorized. Sends the 401 or 429 reply itself otherwise.
        # A valid key always passes immediately, even during a lockout.
        if API_KEY and hmac.compare_digest(self.headers.get("X-Api-Key", "").encode(), API_KEY.encode()):
            clear_auth_failures(client_ip(self))
            return True
        if API_KEY and self.headers.get("X-Api-Key"):
            ip = client_ip(self)
            limited, retry_after = auth_throttled(ip)
            if limited:
                self._send(429, {"ok": False, "error": "Too many failed attempts. Try again later.", "retryAfterSeconds": retry_after})
                return False
            record_auth_failure(ip)
        self._send(401, {"ok": False, "error": "unauthorized"})
        return False

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth():
            return
        path = urlparse(self.path).path
        try:
            if path == "/health":
                try:
                    status = cabinet_get("/api/v1/gamestatus")
                    playing = int(status.get("gameId", -1)) > 0
                    return self._send(200, {"ok": True, "cabinet_reachable": True,
                                            "game_playing": playing, "dry_run": DRY_RUN})
                except Exception as e:
                    return self._send(200, {"ok": True, "cabinet_reachable": False,
                                            "error": "Cabinet request unavailable", "dry_run": DRY_RUN})
            if path == "/api/status":
                status = cabinet_get("/api/v1/gamestatus")
                # Add the current table's display name so the dashboard
                # can show a live "now playing" banner. The name comes
                # from the games export; when no game is active the
                # text is "" and active is false, so the banner hides.
                game_id = -1
                try:
                    game_id = int(status.get("gameId", -1))
                except (TypeError, ValueError):
                    game_id = -1
                text = ""
                if game_id > 0:
                    try:
                        games = cabinet_get("/api/v1/export/tables/plain")
                        games_text = games.get("raw") if isinstance(games, dict) else None
                        if games_text:
                            text = parse_game_names_map(games_text).get(game_id, "")
                    except Exception:
                        text = ""
                if isinstance(status, dict):
                    status = dict(status)
                    status["text"] = text
                    if game_id <= 0:
                        status["active"] = False
                return self._send(200, status)
            if path == "/api/analytics":
                # Read-only bulk totals; never expose frontend paths or raw metadata.
                summary = cabinet_get("/api/v1/alx", max_bytes=4 * 1024 * 1024)
                if not isinstance(summary, dict) or not isinstance(summary.get("entries"), list):
                    return self._send(502, {"ok": False, "error": "Analytics unavailable"})
                if len(summary["entries"]) > 10000:
                    return self._send(502, {"ok": False, "error": "Analytics exceeds row limit"})
                entries = []
                for entry in summary["entries"]:
                    if not isinstance(entry, dict):
                        continue
                    game_id, seconds = entry.get("gameId"), entry.get("timePlayedSecs")
                    if type(game_id) is int and 0 < game_id <= 2147483647 and type(seconds) is int and 0 <= seconds <= 2147483647:
                        entries.append({"gameId": game_id, "timePlayedSecs": seconds})
                return self._send(200, {"ok": True, "entries": entries})
            if path == "/api/games/json":
                return self._send(200, cabinet_get("/api/v1/games"))
            if path == "/api/playlists":
                return self._send(200, cabinet_get("/api/v1/playlists"))
            parts = [p for p in path.split("/") if p]
            if len(parts)==3 and parts[0]=='api' and parts[1] in ('validations','reload','media','thumb','scorehistory','details') and not valid_id(parts[2]):
                return self._send(400,{"ok":False,"error":"Valid table id required"})

            if len(parts) == 3 and parts[0] == "api" and parts[1] == "validations":
                return self._send(200, cabinet_get(f"/api/v1/games/validations/{parts[2]}"))
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "reload":
                return self._send(200, cabinet_get(f"/api/v1/games/reload/{parts[2]}"))
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "media":
                try:
                    return self._send(200, cabinet_get(f"/api/v1/frontend/media/{parts[2]}"))
                except Exception:
                    return self._send(200, cabinet_get(f"/api/v1/media/{parts[2]}"))
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "thumb":
                # Playfield picture for a table. VPin Studio serves the
                # media bytes itself and extracts a PNG frame when the
                # PlayField media is a video (preview=true), so no file
                # access on the cabinet is needed.
                try:
                    ctype, body = cabinet_get_bytes(f"/api/v1/media/{parts[2]}/PlayField?preview=true")
                except urllib.error.HTTPError as e:
                    return self._send(e.code, {"ok": False, "error": f"cabinet HTTP {e.code}"})
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "scorehistory":
                try:
                    game_id = int(parts[2])
                    if game_id <= 0:
                        raise ValueError()
                except ValueError:
                    return self._send(400, {"ok": False, "error": "bad table id"})
                try:
                    return self._send(200, cabinet_get(f"/api/v1/games/scorehistory/{game_id}"))
                except urllib.error.HTTPError as e:
                    return self._send(e.code, {"ok": False, "error": f"cabinet HTTP {e.code}"})
                except Exception:
                    return self._send(502, {"ok": False, "error": "score history unavailable"})
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "details":
                return self._send(200, cabinet_get(f"/api/v1/frontend/tabledetails/{parts[2]}"))
            if path == "/api/games":
                try:
                    return self._send(200, cabinet_get("/api/v1/export/tables/plain"))
                except Exception:
                    return self._send(200, cabinet_get("/api/v1/games"))
            if path == "/api/highscores":
                query = parse_qs(urlparse(self.path).query)
                game_id = query.get("gameId", [None])[0]
                cabinet_path = "/api/v1/export/highscores/plain"
                if game_id is not None and not valid_id(game_id):
                    return self._send(400,{"ok":False,"error":"Valid table id required"})
                if game_id:
                    cabinet_path += f"?gameId={game_id}"
                try:
                    result = cabinet_get(cabinet_path)
                except Exception as e:
                    return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
                text = result.get("raw") if isinstance(result, dict) else None
                if text is None:
                    return self._send(200, {"ok": True, "highscores": result})
                # The highscores export's createdAt is the same export
                # time on every line, so it cannot order by recency.
                # Pull the games export for the real per-table
                # lastPlayed and sort by that instead. If the games
                # fetch fails, fall back to the createdAt sort.
                last_played = None
                try:
                    games = cabinet_get("/api/v1/export/tables/plain")
                    games_text = games.get("raw") if isinstance(games, dict) else None
                    if games_text:
                        last_played = parse_last_played_map(games_text) or None
                except Exception:
                    last_played = None
                entries = parse_highscores_plain(text, last_played)
                if entries is None:
                    return self._send(200, {"ok": True, "raw": text})
                track_last_scores(entries)
                return self._send(200, {"ok": True, "highscores": entries})
            return self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            return self._send(500, {"ok": False, "error": "Cabinet request unavailable"})

    def _cabinet_control(self, path):
        if not _CONTROL_LOCK.acquire(blocking=False):
            return self._send(409, {"ok": False, "error": "Another cabinet action is in progress"})
        try:
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                game_id = payload.get("gameId")
                if type(game_id) is not int or not 0 < game_id <= 2147483647:
                    raise ValueError()
                if path == "/api/exit" and (set(payload) != {"gameId", "started"} or not isinstance(payload["started"], str)):
                    raise ValueError()
            except Exception:
                return self._send(400, {"ok": False, "error": "Valid session or table identity required"})
            try:
                status = cabinet_get("/api/v1/gamestatus")
                if not isinstance(status, dict) or type(status.get("gameId")) is not int:
                    raise ValueError()
            except Exception:
                return self._send(502, {"ok": False, "error": "Cabinet status unavailable; no action sent"})
            if path == "/api/play":
                if status["gameId"] > 0:
                    return self._send(409, {"ok": False, "error": "A game is already playing"})
                try:
                    launched = cabinet_get(f"/api/v1/frontend/launch/{game_id}")
                    if launched is not True:
                        return self._send(502, {"ok": False, "error": "Studio did not confirm the launch request"})
                    return self._send(200, {"ok": True, "gameId": game_id, "launched": True})
                except Exception:
                    return self._send(502, {"ok": False, "error": "Launch outcome unknown; check cabinet before retrying"})
            try:
                observed = datetime.fromisoformat(str(status.get("started", "")).replace("Z", "+00:00"))
                expected = datetime.fromisoformat(payload["started"].replace("Z", "+00:00"))
                same_start = observed.utcoffset() is not None and expected.utcoffset() is not None and observed == expected
            except (ValueError, TypeError):
                same_start = False
            if status["gameId"] != game_id or not same_start:
                return self._send(409, {"ok": False, "error": "Reported session changed; no exit sent"})
            try:
                # This is a mutation despite Studio exposing it as GET. Never retry it.
                accepted = cabinet_get("/api/v1/frontend/terminate")
                if accepted is not True:
                    return self._send(502, {"ok": False, "error": "Studio did not confirm the exit request; check cabinet"})
                return self._send(200, {"ok": True, "exitRequested": True, "idleConfirmed": False})
            except Exception:
                return self._send(502, {"ok": False, "error": "Exit outcome unknown; check cabinet before retrying"})
        finally:
            _CONTROL_LOCK.release()

    def do_POST(self):
        if not self._auth():
            return
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        if self.headers.get("Transfer-Encoding"):
            return self._send(400,{"ok":False,"error":"Transfer encoding not supported"})
        try:
            length=int(self.headers.get("Content-Length","0"))
        except ValueError:
            return self._send(400,{"ok":False,"error":"Invalid request length"})
        limit = MAX_UPLOAD_BYTES if path in ('/api/upload','/api/backglass','/api/puppack','/api/media','/api/rom') else 200000
        if length<0 or length>limit:
            return self._send(413,{"ok":False,"error":"Request body exceeds limit"})
        if path in ('/api/play','/api/exit') or path.startswith('/api/details/'):
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self._send(415,{"ok":False,"error":"JSON content required"})
        if path in ("/api/play", "/api/exit"):
            return self._cabinet_control(path)
        # Details and playlist changes are not uploads, DRY_RUN does not block them.
        parts = [p for p in path.split("/") if p]
        if len(parts)==3 and parts[:2]==['api','details'] and not valid_id(parts[2]):
            return self._send(400,{"ok":False,"error":"Valid table id required"})
        if len(parts)==4 and parts[:2]==['api','playlists'] and not (valid_id(parts[2],allow_zero=True) and valid_id(parts[3])):
            return self._send(400,{"ok":False,"error":"Valid playlist and table ids required"})

        if len(parts) == 3 and parts[0] == "api" and parts[1] == "details":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8") or "{}")
                if not isinstance(payload,dict):
                    raise ValueError("JSON object required")
                result = cabinet_post_json(f"/api/v1/frontend/tabledetails/{parts[2]}", payload)
                return self._send(200, {"ok": True, "result": result})
            except (ValueError,UnicodeError):
                return self._send(400,{"ok":False,"error":"Valid JSON object required"})
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        if path == "/api/playlists/save":
            if self.headers.get('Content-Type','').split(';')[0].strip()!='application/json':
                return self._send(415,{"ok":False,"error":"JSON content required"})
            try:
                length = int(self.headers.get("Content-Length", 0))
                if not 0 < length <= 4096:
                    raise ValueError()
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict) or not set(payload) <= {"id", "name"} or "name" not in payload:
                    raise ValueError()
                name = payload["name"]
                if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120 or any(ord(c) < 32 for c in name):
                    raise ValueError()
                playlist_id = payload.get("id", -1)
                if type(playlist_id) is not int or playlist_id < -1 or playlist_id == 0 or playlist_id > 2147483647:
                    raise ValueError()
            except (ValueError, UnicodeError):
                return self._send(400, {"ok": False, "error": "Provide a playlist name and optional playlist id"})
            try:
                if playlist_id > 0:
                    existing = cabinet_get("/api/v1/playlists")
                    known = existing if isinstance(existing, list) else []
                    if not any(isinstance(p, dict) and p.get("id") == playlist_id for p in known):
                        return self._send(404, {"ok": False, "error": "Playlist not found"})
                # Only id and name are forwarded: no SQL playlists, flags or
                # media fields can be set through this route.
                result = cabinet_post_json("/api/v1/playlists/save", {"id": playlist_id, "name": name.strip()})
                return self._send(200, {"ok": True, "result": result})
            except Exception:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "playlists":
            try:
                result = cabinet_put_json(f"/api/v1/playlists/{parts[2]}/{parts[3]}/0", {})
                return self._send(200, {"ok": True, "result": result})
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        if path not in ('/api/upload','/api/backglass','/api/puppack','/api/media','/api/rom'):
            return self._send(404,{"ok":False,"error":"not found"})
        if path=='/api/rom' and not valid_id(query.get('gameId',[''])[0]):
            return self._send(400,{"ok":False,"error":"Valid table id required"})
        if path=='/api/media' and (query.get('screen',['PlayField'])[0] not in ('PlayField','Wheel','BackGlass','GameInfo','GameHelp','Topper','DMD','Loading','Menu','Other2','Logo','Audio') or query.get('append',['false'])[0] not in ('true','false')):
            return self._send(400,{"ok":False,"error":"Invalid media screen or append flag"})
        if not _UPLOAD_LOCK.acquire(blocking=False):
            return self._send(409,{"ok":False,"error":"Another upload is in progress"})
        try:
            return self._upload_request(path,query)
        finally:
            _UPLOAD_LOCK.release()

    def _upload_request(self,path,query):
        if DRY_RUN:
            return self._send(403, {"ok": False, "error": "dry-run mode, uploads disabled. Set DRY_RUN=false in Dockhand to allow imports."})
        try:
            status = cabinet_get("/api/v1/gamestatus")
            if not isinstance(status,dict) or type(status.get("gameId")) is not int:
                return self._send(503,{"ok":False,"error":"Cabinet state unknown; import refused"})
            if status["gameId"] > 0:
                return self._send(409, {"ok": False, "error": "a game is playing, import refused"})
        except Exception as e:
            return self._send(502, {"ok": False, "error": "Cabinet unavailable; no upload sent"})
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        # Proxy the multipart body straight to the cabinet. The upload client
        # already includes the required fields (mode, gameId, emuId,
        # objectId, assetType) in the multipart, the bridge just forwards.
        targets = {
            "/api/upload": "/api/v1/games/upload",
            "/api/backglass": "/api/v1/directb2s/upload",
            "/api/puppack": "/api/v1/puppacks/upload",
        }
        if path in targets:
            try:
                req = urllib.request.Request(
                    cabinet_base() + targets[path], data=body,
                    headers={"Content-Type": content_type}, method="POST")
                with upstream_open(req, timeout=300) as resp:
                    result = json.loads(bounded_response(resp).decode("utf-8"))
            except urllib.error.HTTPError as e:
                return self._send(e.code, {"ok": False, "error": "Cabinet rejected the upload"})
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
            if path == "/api/upload":
                # Second step: process the upload descriptor.
                try:
                    processed = cabinet_post_json("/api/v1/games/process", result)
                    return self._send(200, {"ok": True, "upload": result, "processed": processed})
                except Exception as e:
                    return self._send(200, {"ok": True, "upload": result, "processed_error": "Processing outcome unknown; check cabinet before retrying"})
            return self._send(200, {"ok": True, "result": result})
        if path == "/api/media":
            screen = query.get("screen", ["PlayField"])[0]
            append = query.get("append", ["false"])[0]
            try:
                req = urllib.request.Request(
                    cabinet_base() + f"/api/v1/media/upload/{screen}/{append}",
                    data=body, headers={"Content-Type": content_type}, method="POST")
                with upstream_open(req, timeout=300) as resp:
                    result = json.loads(bounded_response(resp).decode("utf-8"))
                return self._send(200, {"ok": True, "result": result})
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        if path == "/api/rom":
            game_id = query.get("gameId", [None])[0]
            if not game_id:
                return self._send(400, {"ok": False, "error": "gameId query param required"})
            try:
                req = urllib.request.Request(
                    cabinet_base() + f"/api/v1/assets/{game_id}/upload/0?assetType=ROM",
                    data=body, headers={"Content-Type": content_type}, method="POST")
                with upstream_open(req, timeout=300) as resp:
                    result = json.loads(bounded_response(resp).decode("utf-8"))
                return self._send(200, {"ok": True, "result": result})
            except urllib.error.HTTPError as e:
                return self._send(e.code, {"ok": False, "error": "Cabinet rejected the upload"})
            except Exception as e:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_DELETE(self):
        if not self._auth():
            return
        parts = [p for p in urlparse(self.path).path.split("/") if p]
        # Only removing a table from a playlist is exposed. Playlist
        # deletion and every other cabinet DELETE stay unreachable.
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "playlists" and valid_id(parts[2], allow_zero=True) and valid_id(parts[3]):
            try:
                result = cabinet_delete_json(f"/api/v1/playlists/{parts[2]}/{parts[3]}")
                return self._send(200, {"ok": True, "result": result})
            except Exception:
                return self._send(502, {"ok": False, "error": "Cabinet request unavailable"})
        return self._send(404, {"ok": False, "error": "not found"})

class LimitedServer(ThreadingHTTPServer):
    """Bound slow-client threads independently of the upload memory cap."""
    daemon_threads = True
    request_queue_size = 16
    slots = threading.BoundedSemaphore(16)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        try:
            super().process_request(request,client_address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request,client_address)
        finally:
            self.slots.release()


if __name__ == "__main__":
    if not API_KEY:
        print("vpin-bridge refusing to start: API_KEY is not set in Dockhand Environment", flush=True)
        sys.exit(1)
    # Create /data folders on start, so no manual folder creation is needed.
    for sub in ("approved", "staging", "done", "failed", "backups", "inventory", "logs", "state"):
        try:
            os.makedirs(f"/data/{sub}", exist_ok=True)
        except Exception:
            pass
    print(f"vpin-bridge listening on :{PORT}, dry_run={DRY_RUN}", flush=True)
    LimitedServer(("0.0.0.0", PORT), Handler).serve_forever()
