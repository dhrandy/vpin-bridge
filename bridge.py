#!/usr/bin/env python3
"""
VPin Bridge - Google TV style API bridge for a pinball cabinet.

Same access pattern as the Google TV bridge:
- Local: http://<docker-host>:8744  (Docker, Dockhand compose, CasaOS)
- Remote: https://<reverse-proxy-host>
  (reverse proxy, same pattern as googletv-bridge)
- Auth: X-Api-Key header, key set in Dockhand Environment as API_KEY,
  and stored in Todd's secure storage as custom.vpin-bridge.
  Todd never sees the raw key, the CLI carries it via surrogate.
- Todd calls this bridge directly, no GitHub polling, no Tailscale.
  The bridge forwards to the cabinet VPin Studio API on the LAN.

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
    Cabinet GET /api/v1/gamestatus, active when gameId > 0
  GET  /api/games/json
    List tables as JSON, from cabinet GET /api/v1/games
    (id + gameDisplayName, for picking a table to play)
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
  POST /api/media?screen=PlayField|Wheel|BackGlass&append=false
    Multipart, field "file" = media file.
    Forwards to POST /api/v1/media/upload/<SCREEN>/<append>
    Screen names are case-sensitive enum names: Wheel, PlayField,
    BackGlass, GameInfo, GameHelp, Topper, DMD, Loading, Menu,
    Other2, Logo, Audio. Default screen is PlayField.
  GET  /api/media/<gameId>
    Frontend media for a table, cabinet GET /api/v1/frontend/media/<id>
  GET  /api/details/<gameId>
    Table details / metadata, cabinet GET /api/v1/frontend/tabledetails/<id>
  POST /api/details/<gameId>
    JSON TableDetails. Cabinet POST /api/v1/frontend/tabledetails/<id>.
    NOT blocked by DRY_RUN.
  GET  /api/playlists
    Cabinet GET /api/v1/playlists
  POST /api/playlists/<playlistId>/<gameId>
    Add a table to a playlist, cabinet PUT /api/v1/playlists/<id>/<gameId>/0.
    NOT blocked by DRY_RUN. Use for the Home playlist, so a table is visible.

VPin endpoints verified Nov 7, 2026 from public VPin Studio source,
main branch, vpin-studio-rest-client. Caveat: main branch, not a
pinned 5.6.1 tag, confirm on the cabinet version before first
real import. DRY_RUN=true (default in compose) refuses all uploads.
"""
import json
import os
import sys
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(os.environ.get("BRIDGE_PORT", "8080"))
CABINET_IP = os.environ.get("CABINET_IP", "")
VPIN_PORT = os.environ.get("VPIN_PORT", "8089")
API_KEY = os.environ.get("API_KEY", "")
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() in ("true", "1", "yes")

def cabinet_base():
    if not CABINET_IP:
        raise RuntimeError("CABINET_IP is not set in Dockhand Environment")
    return f"http://{CABINET_IP}:{VPIN_PORT}"

def cabinet_get(path):
    req = urllib.request.Request(cabinet_base() + path, method="GET")
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

def cabinet_post_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cabinet_base() + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

def cabinet_put_json(path, payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(cabinet_base() + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="PUT")
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = resp.read()
        try:
            return json.loads(body.decode("utf-8"))
        except Exception:
            return {"raw": body.decode("utf-8", "replace")}

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _auth(self):
        if not API_KEY:
            return False
        return self.headers.get("X-Api-Key") == API_KEY

    def _send(self, code, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if not self._auth():
            return self._send(401, {"ok": False, "error": "unauthorized"})
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
                                            "error": str(e), "dry_run": DRY_RUN})
            if path == "/api/status":
                return self._send(200, cabinet_get("/api/v1/gamestatus"))
            if path == "/api/games/json":
                return self._send(200, cabinet_get("/api/v1/games"))
            if path == "/api/playlists":
                return self._send(200, cabinet_get("/api/v1/playlists"))
            parts = [p for p in path.split("/") if p]
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "media":
                try:
                    return self._send(200, cabinet_get(f"/api/v1/frontend/media/{parts[2]}"))
                except Exception:
                    return self._send(200, cabinet_get(f"/api/v1/media/{parts[2]}"))
            if len(parts) == 3 and parts[0] == "api" and parts[1] == "details":
                return self._send(200, cabinet_get(f"/api/v1/frontend/tabledetails/{parts[2]}"))
            if path == "/api/games":
                try:
                    return self._send(200, cabinet_get("/api/v1/export/tables/plain"))
                except Exception:
                    return self._send(200, cabinet_get("/api/v1/games"))
            return self._send(404, {"ok": False, "error": "not found"})
        except Exception as e:
            return self._send(500, {"ok": False, "error": str(e)})

    def do_POST(self):
        if not self._auth():
            return self._send(401, {"ok": False, "error": "unauthorized"})
        path = urlparse(self.path).path
        query = parse_qs(urlparse(self.path).query)
        # Play is not an upload, DRY_RUN does not block it.
        if path == "/api/play":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8") or "{}")
                game_id = int(payload.get("gameId"))
            except Exception as e:
                return self._send(400, {"ok": False, "error": f"gameId required: {e}"})
            try:
                status = cabinet_get("/api/v1/gamestatus")
                if int(status.get("gameId", -1)) > 0:
                    return self._send(409, {"ok": False, "error": "a game is already playing"})
            except Exception as e:
                return self._send(502, {"ok": False, "error": f"cabinet unreachable: {e}"})
            try:
                launched = cabinet_get(f"/api/v1/frontend/launch/{game_id}")
                return self._send(200, {"ok": True, "gameId": game_id, "launched": launched})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
        # Details and playlist changes are not uploads, DRY_RUN does not block them.
        parts = [p for p in path.split("/") if p]
        if len(parts) == 3 and parts[0] == "api" and parts[1] == "details":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length) if length else b"{}"
                payload = json.loads(body.decode("utf-8") or "{}")
                result = cabinet_post_json(f"/api/v1/frontend/tabledetails/{parts[2]}", payload)
                return self._send(200, {"ok": True, "result": result})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "playlists":
            try:
                result = cabinet_put_json(f"/api/v1/playlists/{parts[2]}/{parts[3]}/0", {})
                return self._send(200, {"ok": True, "result": result})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
        if DRY_RUN:
            return self._send(403, {"ok": False, "error": "dry-run mode, uploads disabled. Set DRY_RUN=false in Dockhand to allow imports."})
        try:
            status = cabinet_get("/api/v1/gamestatus")
            if int(status.get("gameId", -1)) > 0:
                return self._send(409, {"ok": False, "error": "a game is playing, import refused"})
        except Exception as e:
            return self._send(502, {"ok": False, "error": f"cabinet unreachable: {e}"})
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""
        content_type = self.headers.get("Content-Type", "")
        # Proxy the multipart body straight to the cabinet. Todd's CLI
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
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return self._send(e.code, {"ok": False, "error": e.read().decode("utf-8", "replace")[:500]})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
            if path == "/api/upload":
                # Second step: process the upload descriptor.
                try:
                    processed = cabinet_post_json("/api/v1/games/process", result)
                    return self._send(200, {"ok": True, "upload": result, "processed": processed})
                except Exception as e:
                    return self._send(200, {"ok": True, "upload": result, "processed_error": str(e)})
            return self._send(200, {"ok": True, "result": result})
        if path == "/api/media":
            screen = query.get("screen", ["PlayField"])[0]
            append = query.get("append", ["false"])[0]
            try:
                req = urllib.request.Request(
                    cabinet_base() + f"/api/v1/media/upload/{screen}/{append}",
                    data=body, headers={"Content-Type": content_type}, method="POST")
                with urllib.request.urlopen(req, timeout=300) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
                return self._send(200, {"ok": True, "result": result})
            except Exception as e:
                return self._send(502, {"ok": False, "error": str(e)})
        return self._send(404, {"ok": False, "error": "not found"})

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
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
