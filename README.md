# vpin-bridge

An HTTP API bridge for a Visual Pinball cabinet running VPin Studio.

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
   - `DRY_RUN` - start with `true`. While `true`, table uploads
     are refused. Set it to `false` when you are ready to import
     tables for real, then restart the stack.
3. Start the stack. The container pulls `bridge.py` from this
   repo on start and listens on port 8080 inside the container,
   mapped to `8744` on the host.
4. Check health: `GET http://<host>:8744/health` with the
   `X-Api-Key` header. It reports `cabinet_reachable` and
   `game_playing`.

## Updating

Push a new `bridge.py` to `main`, then restart the Dockhand
stack. The container pulls the new file on start. No rebuild,
no image push.

## API

All endpoints, including `/health`, need the `X-Api-Key` header.

| Method | Endpoint | What it does |
| --- | --- | --- |
| GET | `/health` | Bridge up, cabinet reachable, a game playing?, dry-run on? |
| GET | `/api/status` | Cabinet game status, from `GET /api/v1/gamestatus` |
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

## Table import checklist

For every table added, the import is not complete until all of
these are checked:

- Playfield, wheel, backglass, and PuP pack if one exists, and
  help if one exists. Check the pack, and check the Virtual
  Pinball Spreadsheet entry for the table for separate files.
- Wheel style: Davadruix b-variant. Smooth plain chrome ring,
  no rivets, no gold filigree. Playfield inside the disc, title
  below the disc in large bold letters with a black outline,
  overlapping the bottom edge, not across the middle, not
  diagonal. Any wheel made for a table must match that style.
- Display name always capitalized.
- Metadata filled out, and the details page filled out
  (manufacturer, year, type, players, theme, authors, version,
  link to the source file).
- Always make it visible, which means adding it to the Home
  playlist in PinUP Popper.

Note: a table pack's media files are often named plainly
(`borderlands.png`, `playfield.png`), and the importer does not
always pick those up as wheel / playfield media. If a wheel or
playfield is missing after import, upload that file to its
screen with `POST /api/media?screen=Wheel` (or `PlayField`)
and verify it with `GET /api/media/<gameId>`.

## Security notes

- The cabinet IP, the API key, and any other secret live only
  in Dockhand Environment, never in this repo.
- No GitHub token is used anywhere in this setup.
- The bridge never serves the cabinet directly to the internet;
  reach it only through your own reverse proxy, with the API
  key required on every request.
