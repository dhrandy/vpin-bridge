# vpin-bridge

![Status: alpha](https://img.shields.io/badge/status-alpha-orange) ![Python: 3.12](https://img.shields.io/badge/python-3.12-blue) ![Deployment: Docker Compose](https://img.shields.io/badge/deploy-Docker_Compose-blue)

An HTTP API bridge for a Visual Pinball cabinet running VPin Studio.

**Alpha software.** Keep the bridge and cabinet private. Test updates on your own setup; API availability can vary with VPin Studio/frontend versions.

It lets a remote assistant (or script) list tables, launch a table,
import a new table, upload media, backglass and PuP packs, fill in
table details, and add a table to a playlist, without opening a
remote desktop session or a VPN tunnel to the cabinet.

The bridge runs on your own server (Docker Compose, Dockhand /
CasaOS) and forwards to the cabinet's VPin Studio Server on the
local network. The cabinet itself is never exposed to the internet.

## How it works

```
assistant / script
  -- X-Api-Key --> vpin-bridge (this repo, port 8744)
  -- HTTP --------> VPin Studio Server on the cabinet (port 8089, LAN only)
```

- `bridge.py` is the whole server. No custom Docker image, no
  Dockerfile, no GitHub token.
- The container uses the stock `python:3.12-slim` image and pulls
  `bridge.py` from this repo on start, so updating is: push a new
  `bridge.py` here, then restart the container.
- Auth is a single API key, `X-Api-Key`, set in the container
  environment. The key is never in this repo.

## Requirements

- A Visual Pinball cabinet (Windows) running VPin Studio Server,
  port 8089, reachable on the local network.
- Windows Firewall must allow port 8089 on the private network.
- A Docker host to run the bridge, for example a NAS running
  CasaOS with Dockhand.
- A reverse proxy in front of the bridge if you want to reach it
  from outside your home network.

## Setup

1. Copy `docker-compose.yml` into a new Dockhand stack. Replace the
   `/path/to/vpin-bridge/...` volume paths with your own host paths.
2. Set the stack Environment values:
   - `CABINET_IP` - the cabinet's LAN IP address. Keep this only
     in Dockhand Environment. Never put it in this repo, in code,
     or in a README.
   - `API_KEY` - a long random API key of your choosing.
   The supplied compose sets `DRY_RUN=true` directly. While true,
   table/ROM uploads are refused, but Play, metadata and playlist
   changes are not blocked. Change that compose entry to false only
   when you are ready for uploads, then restart the stack. It is not
   a read-only mode.
3. Start the stack. The container pulls `bridge.py` from this
   repo on start and listens on port 8080 inside the container,
   mapped to `8744` on the host.
4. Check health: `GET http://<host>:8744/health` with the
   `X-Api-Key` header. It reports `cabinet_reachable` and
   `game_playing`.

For another Compose manager, use its stack editor. From a shell,
save the compose file as `docker-compose.yml`, export `CABINET_IP`
and `API_KEY`, then run `docker compose up -d`. No `.env` file is
supplied or required. The container needs outbound access to GitHub
at startup. Keep the API key out of shell history and public logs.

Copy-paste compose (replace the two host paths):

```yaml
services:
  vpin-bridge:
    image: python:3.12-slim
    container_name: vpin-bridge
    restart: unless-stopped
    environment:
      - BRIDGE_PORT=8080
      - CABINET_IP=${CABINET_IP}
      - VPIN_PORT=8089
      - API_KEY=${API_KEY}
      - DRY_RUN=true
    ports:
      - "8744:8080"
    volumes:
      - /path/to/vpin-bridge/app:/app
      - /path/to/vpin-bridge/data:/data
    command:
      - sh
      - -c
      - |
        python3 -c "import urllib.request; data=urllib.request.urlopen('https://raw.githubusercontent.com/dhrandy/vpin-bridge/main/bridge.py', timeout=30).read(); open('/app/bridge.py','wb').write(data); print('pulled bridge.py,', len(data), 'bytes', flush=True)"
        exec python3 /app/bridge.py
```

## Updating

Push a new `bridge.py` to `main`, then restart the Dockhand
stack. The container pulls the new file on start. No rebuild,
no image push.

## API

All endpoints, including `/health`, need the `X-Api-Key` header.

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/health` | Bridge up, cabinet reachable, a game playing?, dry-run on? |
| GET | `/api/status` | Cabinet game status, including Studio-reported session start when available; not ball-in-play time |
| GET | `/api/analytics` | Read-only bulk cumulative playtime from `/api/v1/alx`. Returns only `gameId` and nonnegative `timePlayedSecs`; at most 10,000 entries / 4 MiB upstream response and a 15-second cabinet timeout. Not last-session duration. |
| GET | `/api/highscores` | Parsed highscore export; current score lists, not a complete every-play log |
| GET | `/api/scorehistory/<gameId>` | Studio's saved highscore-list versions with timestamps, not guaranteed every-play results |
| GET | `/api/thumb/<gameId>` | PlayField image/preview bytes supplied by Studio; the dashboard caches/resizes them |
| GET | `/api/games` | Tables, from `GET /api/v1/export/tables/plain` |
| GET | `/api/games/json` | Tables as JSON, id + display name |
| POST | `/api/play` | `{"gameId": 123}` launches a table. Refuses if a game is already playing. Not blocked by `DRY_RUN`. |
| GET | `/api/media/<gameId>` | Frontend media for a table (wheel, playfield, backglass, help, info) |
| GET | `/api/details/<gameId>` | Table details / metadata |
| POST | `/api/details/<gameId>` | Save table details (JSON TableDetails). Not blocked by `DRY_RUN`. |
| GET | `/api/validations/<gameId>` | Live validation for a table, current issue list (36 = BackGlass missing, 38 = PlayField missing) |
| GET | `/api/reload/<gameId>` | Reload one table, use after uploading media so validation updates |
| GET | `/api/playlists` | Playlists |
| POST | `/api/playlists/<playlistId>/<gameId>` | Add a table to a playlist. Not blocked by `DRY_RUN`. |
| POST | `/api/upload` | Multipart table archive (`.zip` / `.7z` / `.vpa`), fields `mode`, `gameId`, `emuId`, `objectId`, `assetType`. Forwards to the cabinet upload, then runs `/games/process`. Blocked by `DRY_RUN` and while a game is playing. |
| POST | `/api/backglass` | Multipart `.directb2s` file |
| POST | `/api/puppack` | Multipart PuP pack archive |
| POST | `/api/rom?gameId=<id>` | Multipart ROM zip, e.g. gldneye.zip. Blocked by DRY_RUN. |
| POST | `/api/media?screen=<SCREEN>&append=false` | Multipart media file for one screen. Screen names are case-sensitive enum names: `Wheel`, `PlayField`, `BackGlass`, `GameInfo`, `GameHelp`, `Topper`, `DMD`, `Loading`, `Menu`, `Other2`, `Logo`, `Audio`. All-caps screen names (for example `WHEEL`) are rejected by the cabinet with HTTP 400. |

## Dashboard integration

The companion vpin-dashboard has its own admin/viewer accounts, personal hearts, display preferences, instructions and private snapshot database. The bridge API key is a service credential, not a dashboard login. Do not put it into browser code.

The dashboard can collect status every 60 seconds and bulk data every 5 minutes by default, even without an open browser. It defers bulk reads while busy and backs off offline checks to 15 minutes. It does not ask this bridge to download images, launch tables or write cabinet data as part of background collection. Each read can still take the bridge's 15-second cabinet timeout; a caller's shorter timeout does not cancel it.

Deploy the bridge update before the dashboard update to enable cumulative card minutes. Older bridges still let the dashboard display the library, but the minutes field is unavailable. Restart each Compose stack yourself after updating. No additional environment values are required for analytics.

Studio treats a missing backglass/PUP warning as neither backglass nor installed PUP, not a requirement for both. A PUP-equipped table should not be flagged just because it has no directB2S file. This does not promise every table works without further table-specific configuration.

## Security notes

- The cabinet IP, the API key, and any other secret live only
  in Dockhand Environment, never in this repo.
- No GitHub token is used anywhere in this setup.
- The bridge never serves the cabinet directly to the internet;
  reach it only through your own reverse proxy, with the API
  key required on every request.

## AI usage

AI-assisted development was used for this project. The maintainer reviews and decides what ships. Automated tests are not an independent security audit. Do not include keys, cabinet addresses, local paths or private table data in public bug reports.

## Tests

Run `python -m unittest discover -s tests -v` for the synthetic analytics endpoint tests. They use a local fixture server and do not contact a cabinet.

## License

Copyright dhrandy. All rights reserved. No license is granted.
