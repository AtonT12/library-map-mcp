# RUNBOOK — Manual testing & local-dev integration with Ask

> Audience: whoever is changing this server and wants to see it work — standalone first, then mounted in Ask.
> Scope: **local only**. Nothing here deploys anything.
> Rewritten 2026-10-14 from a full A+B pass on this machine; steps marked *verified* were executed in that pass.
> Language note: English because `AGENTS.md §Language convention` requires everything landed in this repo to be English.

Two independent tracks — do them in order; A is a prerequisite for B (B runs the same code you just tested):

| Track | What it proves | Needs |
|---|---|---|
| **A. Standalone** (§1–§5) | the server answers, routes are sane, the view renders in the dev harness | Python venv only |
| **B. Ask local-dev** (§6–§14) | the view renders **inside the real Ask chat host** | Docker Desktop + Ask backend + Admin/Roary UIs |

Throughout: `<APP>` = `D:\Codes\RITS\map-mcp\daniel-library-mcp`, `<VENV>` = `D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe`.
**Always call the venv interpreter by absolute path** — bare `python` on this machine is not the project interpreter.

---

# Track A — standalone

## 1. One-time setup

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' -m pip install -r requirements.txt
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' scripts\build_static.py
```

```bash
cd /d/Codes/RITS/map-mcp/daniel-library-mcp
../.venv-daniel/Scripts/python.exe -m pip install -r requirements.txt
../.venv-daniel/Scripts/python.exe scripts/build_static.py
```

- `requirements.txt` pins `mcp==1.26.0` (do **not** bump — 2.x renames FastMCP and adds a 4 MiB cap) + `Pillow==10.4.0`. `jsonschema` arrives transitively and the tests use it — leave it. `requirements-dev.txt` is intentionally empty (dev tools are stdlib + Pillow).
- `build_static.py` writes gitignored `src/static/*.webp`. Re-run after **any** change to `maps/*_base.jpg`, otherwise the `ui://` resource fails to build.

## 2. Start the server

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
$env:PYTHONPATH = 'src'
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' 'src\mcp_server.py'
# optional, set before launching:
#   $env:PORT = '8010'           # listen here instead of 8000
#   $env:ENABLE_MCP_APPS = '0'  # kill switch: no ui:// resource, legacy tools only
```

Expect, in order: the harmless `pydantic_settings` warning, FastMCP banner, listener on `0.0.0.0:8000`. Note: a successful startup prints **no** `assets ok` — the selfcheck is silent when it passes (the literal `assets ok` line only appears when you run `selfcheck.py` directly); on failure the server prints `asset check failed, refusing to start:` and **exits 1**. If you launch detached, redirect **stderr too** — the asset-failure list prints there, not stdout.

Behaviour worth knowing: transport is Streamable HTTP at `/mcp` only (no stdio/SSE, no flag); a stray `pydantic_settings` warning on startup is harmless; a missing asset makes the process print the list and **exit 1** (deliberate — the Dockerfile relies on the same check at build time).

## 3. Client probes (pick one)

**VS Code** (fastest): `.vscode/mcp.json` already points at `http://127.0.0.1:8000/mcp`. With the server running, `MCP: List Servers` → reconnect `nyush-library` → confirm the 7 tools. `D:\Codes\RITS\opencode.json` points at the same URL — known limitation: tools work, the interactive view does not (terminal hosts ignore `_meta.ui`; that is the legacy L2 path).

**Raw JSON-RPC** (see the wire): responses arrive as SSE — parse the `data:` lines. The session keys off the `mcp-session-id` response header captured from step 1. In PowerShell, `curl` is an alias for `Invoke-WebRequest` — call **`curl.exe`**.

```powershell
Test-NetConnection 127.0.0.1 -Port 8000
# 1) initialize — note the mcp-session-id response header
curl.exe -s -D - -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"probe\",\"version\":\"0.1\"}}}'
# 2) initialized notification
curl.exe -s -o NUL -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "mcp-session-id: <ID-FROM-STEP-1>" -d '{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}'
# 3) tools/list
curl.exe -s -X POST http://127.0.0.1:8000/mcp -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" -H "mcp-session-id: <ID-FROM-STEP-1>" -d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}'
```

Verified outcome: negotiates `2025-06-18`; `resources/list` contains `ui://nav/library-view.html` with empty CSP domains; `tools/call get_route {"destination":"N607"}` returns two legs (5F→6F) with a Central-Stairs transit.

## 4. Manual app-view harness (dev, no Ask)

`dev/host.html` is the host-side simulator that completes the `ui://` handshake locally. You cannot develop the view inside Ask, so any `viewer.js` change ends here:

1. Regenerate the preview (**generated artifact**, never auto-updates — re-run after any change to `src/view/viewer.js`, `src/view/template.py`, or the base maps):

   ```powershell
   cd D:\Codes\RITS\map-mcp\daniel-library-mcp
   & 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' scripts\build_preview.py
   ```

   It prints the byte count and writes `dev/preview.html`, byte-identical to what the live `ui://` resource serves.
2. Serve `dev/` **over HTTP on a port other than 8000** (`fetch()` is blocked on `file://`, so the fixture button needs HTTP):

   ```powershell
   cd D:\Codes\RITS\map-mcp\daniel-library-mcp\dev
   & 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' -m http.server 8077
   ```
3. Open `http://127.0.0.1:8077/host.html` → **view** dropdown → `./preview.html (template output)` (full mode; `view.html` is a thin error shell) → wait for `initialized` in the log.
4. Push a route: click **`tool-result (real RoutePlan)`** (fetches `dev/fixtures/route_n607.json`) or paste in the **host page's** console (not the iframe's):

   ```js
   const p = await fetch('./fixtures/route_n607.json').then(r=>r.json());
   notify('ui/notifications/tool-result', {content:[{type:'text',text:'review N607'}], structuredContent:p});
   ```

   In-session extras: `tool-input` sends arguments first; `tool-result (structured)/(text-only)/(empty)` exercise the failure-ish shapes; `push host-context-changed` re-applies theme/`maxHeight`; `reload view` reboots the iframe.

Manual checklist — there is **no Playwright** in this project (deliberate); this list *is* the UI regression, run by hand after any `viewer.js` change:

| # | Area | What to check |
|---|---|---|
| V4 | Pan / zoom | wheel zoom centred on cursor; pan doesn't drift |
| V5 | Place detail | tapping a station opens the side panel with the right record |
| V6 | Floor tabs | 5F ⇄ 6F switch re-renders the correct base map |
| V7 | Hi-res tiles | zoom past the 0.35 threshold triggers a `get_map_view` tile (800 ms debounce) |
| 3D | Oblique | off by default; extrudes slabs, circles stay round / text upright, non-current floor dims to 0.45 |
| L3 | Error bar | the three `tool-result` flavours never white-screen; 3 s handshake timeout shows Retry |
| V9 | Theme | light/dark both look right |
| V8 | Height | `maxHeight = 720`: long content **scrolls inside the frame**, not clipped |

## 5. Automated suites

Run from `<APP>`. Plain scripts with `assert` (no pytest).

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
$env:PYTHONPATH = 'src'
& '..\.venv-daniel\Scripts\python.exe' src\test.py
& '..\.venv-daniel\Scripts\python.exe' src\test_fallbacks.py
& '..\.venv-daniel\Scripts\python.exe' src\test_route_schema.py
& '..\.venv-daniel\Scripts\python.exe' src\selfcheck.py
node dev\viewer.test.js
```

```bash
cd /d/Codes/RITS/map-mcp/daniel-library-mcp
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test_fallbacks.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test_route_schema.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/selfcheck.py
node dev/viewer.test.js
```

Gotchas: `test.py` is the heaviest (wall audit over ~220 routes); wall budgets are env vars keeping documented residuals — `WALL_CROSSING_BUDGET=0`, `WALL_CLEARANCE_BUDGET=0`, `WALL_DETOUR_BUDGET=6`. `test_fallbacks.py` includes **4 byte-frozen image lengths** — they must not change unless you intend a re-freeze. `test_route_schema.py` starts a real server on port **18083** and **temporarily renames** `corridors.json` + `maps/5f_base.jpg` (restored in `finally`) — never run it concurrently with other data-touching work. CI (`.github/workflows/test.yaml`) runs the three Python suites + the viewer test + **two independent route auditors** + `docker build` on py3.11.

The two auditors exist because a test that shares its implementation with the code under test cannot catch a shared mistake — they compute the same properties a second way and fail on any disagreement:

| Tool | What it checks independently |
|---|---|
| `dev\xcheck_raster.py` | Wall crossings, computed on a full-resolution Pillow mask instead of the router's vector geometry. Any disagreement between the two methods fails the run. |
| `dev\route_quality.py` | The four user-facing questions (see `ROUTE_QUALITY_SPEC.md`): **reachable** (free-space BFS — can a person get there at all), **walkable** (every segment of every route, including the two doorway connectors), **efficient** (route length vs the *shortest walkable path*, not vs a straight line through walls), **points** (is every marker somewhere a person can stand). ~50 s. |

`dev\wall_audit.py all` is a third tool, for data inspection rather than gating: it profiles each wall segment against the ink in the base map (phantom / displaced / long bare runs) and reports spine orthogonality, coverage and sealed pockets.

---

# Track B — mount into Ask (local dev: no registry push, no CI)

Mechanic in one line: an admin registers an image reference → the platform runs it as a container → it does nothing until bound to an experience. Never edit Ask's source to add a server. Ask lives at `D:\Codes\RITS\Ask` (+ `Ask-Admin-UI`, `Ask-Roary-UI`).

## 6. Prerequisites (including the Windows Docker socket fix)

| Need | Check / action |
|---|---|
| Docker Desktop (Linux containers) running | `docker info` succeeds |
| Three repos installed | `node_modules` present in `Ask`, `Ask-Admin-UI`, `Ask-Roary-UI` (`npm install` in each) |
| **`Ask/.env` → `DOCKER_SOCKET` fixed** | A natively-run backend talks to Docker via dockerode at `DOCKER_SOCKET` (default `/var/run/docker.sock`, which **does not exist on Windows**). Find your engine pipe: `docker context inspect` → Endpoints.docker.Host; on desktop-linux/WLS2 set **`DOCKER_SOCKET=//./pipe/dockerDesktopLinuxEngine`** (*verified*). Restart the backend after editing `.env`. |

No `docker login ghcr.io` needed — when the pull fails, the runtime falls back to a same-named local image.

## 7. Infrastructure up

```powershell
cd D:\Codes\RITS\Ask
docker compose up -d        # db(pgvector), elasticsearch, kibana, redis, unstructured, adminer, smtp-relay
npx prisma migrate deploy   # no-op when the db is current
npm run start:dev           # Nest backend → http://localhost:3000 ("Nest application successfully started")
```

Then the two UIs — both Vite apps default to 5173, so the second **must** move:

```powershell
cd D:\Codes\RITS\Ask-Admin-UI ; npm run dev                 # Admin UI → http://localhost:5173
cd D:\Codes\RITS\Ask-Roary-UI ; npm run dev -- --port 5174  # chat UI   → http://localhost:5174
```

Minimal working subset for this smoke path (*verified*): `db`, `redis`, `elasticsearch`. kibana/adminer/smtp-relay/unstructured are optional (and `unstructured` is a slow multi-GB pull if uncached). `FRONTEND_URL` already admits both 5173 and 5174.

## 8. Build the MCP image locally

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
docker build -t ghcr.io/shanghai-rits/daniel-library-mcp:dev .
```

The tag must sit under the allowlist prefix `ghcr.io/shanghai-rits/` (`MCP_IMAGE_ALLOWLIST`), or registration is refused before Docker is touched. Use `:dev`, not `:latest` — obviously local, and keeps the prod tag free. Do **not** tag `ghcr.io/atont12/…` (the fork name; not allowlisted). The build runs `selfcheck.py` + `build_static.py` at build time, so broken assets fail the build, not the first request.

## 9. Auth — browser or scripted

Browser: open `http://localhost:5173`, sign in with Google (`GOOGLE_DOMAIN=nyu.edu`). A 403 alert about `MANAGE_MCP_SERVERS` means the account lacks the role — grant it with `npx ts-node prisma/addAdmin.ts <your-email>` (from `D:\Codes\RITS\Ask`; the account must already exist in the db).

Scripted (for §11/§13): guest login is enabled (`GUEST_LOGIN_ENABLED=true` in `Ask/.env`):

```powershell
$g = Invoke-RestMethod -Method Post -Uri "http://localhost:3000/auth/guest" -ContentType "application/json" -Body "{}"
$g.access_token   # ~15 min expiry — re-mint on "Invalid or expired token"
```

Each `/auth/guest` call creates a fresh guest user. Management endpoints need super-admin, so promote once: decode the email from the JWT's middle base64 segment, then run `prisma/addAdmin.ts <guest-email>`. Chat endpoints only need a valid token (plus the referrer rule below).

## 10. Register the server

Admin UI → **MCP Servers → Register MCP Server** (or `POST /mcp-servers`). Fill exactly:

| Field | Value |
|---|---|
| Name | `nyush-library` |
| Description | NYUSH Library visual navigator (routes + interactive floor view) |
| Image | `ghcr.io/shanghai-rits/daniel-library-mcp:dev` |
| Port | `8000` |
| Endpoint path | `/mcp` |
| Environment variables | *(empty)* — `PORT` is injected from the Port field; the app toggle defaults on |
| Secret env | *(empty)* |
| Active | on |
| Allowed internet domains (egress) | *(empty)* — this server needs no outbound internet |

Saving drives the row `pending → pulling → running` (polls every 5 s). The pull failure is expected and falls through to the local image (*verified*).

## 11. Start, verify, bind

Press **Start** (or `POST /mcp-servers/:id/start`). Wait out the **30 s** readiness window, then check in order (*all verified*):

1. Status `running`, Last error empty; Logs show `assets ok` + `Uvicorn running on http://0.0.0.0:8000`.
2. Tools lists all seven: `get_library_map`, `get_library_directions`, `search_locations`, `get_route`, `get_location_detail`, `get_library_status`, `get_map_view`.
3. App binding tags: the four structured tools carry the cyan **MCP App** tag (`Renders ui://nav/library-view.html in the chat`); the two legacy ones have none; `get_map_view` shows **app only**. API equivalent: `GET /mcp-servers/:id/apps` returns the one `ui://` resource.
4. **Bind**: Edit the row — Render MCP Apps on (on by default), Allowed app origins (CSP) **empty** (the view may only talk to its own server through the host proxy — desired). Then Admin → Experiences → pick a row (e.g. `ask-roary`) → MCP Tools → Add → `nyush-library`. Binding takes effect on the next chat request; no deploy.

## 12. Smoke test — in the browser

Open the experience's chat (`http://localhost:5174`) and ask **"How do I get from the Entrance to N607?"** (cross-floor — exercises the two-image path). Check: the tool is really called (answer uses real data); the view renders **inline** and scrolls within its **720 px** ceiling (frame scrolls, not clips); light **and** dark themes; a 5F→6F trip shows two floor maps.

**Gotcha found in the 2026-10-14 pass**: for natural phrasing the model prefers the legacy `get_library_directions` (returns image markdown, no widget). The app layer engages when a **structured** tool (`get_route` & co.) is the one called — steer the prompt accordingly (§13).

## 13. Smoke test — scripted REST (app-layer evidence)

Repeats the P1 question without a browser. Two guards shape the calls: guest tokens require `?referrer=<index_name>` on chat start (the experience's `guest_mode` flag must be on), and guest tokens expire in ~15 minutes.

```powershell
# 1) auth (a plain guest suffices for chat; promote per §9 only for management APIs)
$g = Invoke-RestMethod -Method Post -Uri "http://localhost:3000/auth/guest" -ContentType "application/json" -Body "{}"
$t = "Bearer $($g.access_token)"

# 2) start chat — explicit tool steering makes the app path fire
$body = '{"prompt":"Call the get_route tool now (not any other library tool), with destination \"N607\".","conversationPairId":"smoke-1"}'
$sid = (Invoke-RestMethod -Method Post -Uri "http://localhost:3000/chat/start?referrer=ask-roary" -ContentType "application/json" -Headers @{Authorization=$t} -Body $body).chatSessionId

# 3) wait for completion, then drain the stream
while ((Invoke-RestMethod "http://localhost:3000/chat/status/$sid" -Headers @{Authorization=$t}).status -eq 'ONGOING') { Start-Sleep 2 }
curl.exe -s -N "http://localhost:3000/chat/stream/$sid" -H "Authorization: $t" -o chat_stream.sse
```

Then assert against `chat_stream.sse` (*all observed*):

- An **`event: appWidget`** frame appeared carrying `instanceId` + `resourceUri: ui://nav/library-view.html`. Absence = the model picked a legacy tool — a routing choice, not an app failure (still a legitimate L2 result).
- `GET /mcp-apps/instances/<instanceId>` (same token — authorisation is anchored on the user who created the instance) returns the rendered bundle: `html` **byte-identical to `dev/preview.html`**, `result.structuredContent` populated, `csp` domains empty.
- The answer text invites the user to the interactive map instead of re-narrating the widget (the server injects that hint — confirm it is present).

This sequence is the repeatable P1 evidence: Ask created the instance, proxied the resource, and handed the view its RoutePlan.

## 14. A/B the app layer

- **No restart (primary)**: `PATCH /mcp-servers/:id` with `{"appsEnabled": false}` (admin token), or toggle **Render MCP Apps** off in the Admin UI → the *next* message is text-only, no `appWidget`. Re-enable to restore. *Verified both directions.*
- **Restart variant**: set `ENABLE_MCP_APPS=0` in the row's env and restart → the `ui://` resource is not registered at all; tools return text/images only. This is the L1 kill-switch, asserted byte-exact by `test_fallbacks.py`.

---

## 15. What is still unverified (P1 watch list)

`dev/HOST_CONTRACT.md` marks these items **assumed, not observable from the harness**. After the 2026-10-14 pass, the split stands:

- ✅ app instance creation + `appWidget` SSE event in the real chat stream
- ✅ instance HTML proxied byte-for-byte from the locally built image (pull-fallback path works)
- ✅ no-restart A/B via `appsEnabled`
- ⬜ Ask's actual `theme` / `containerDimensions.maxHeight` values and whether it ever sends `host-context-changed`
- ⬜ CSP enforcement and the `ui://` digest cache (~10 min, keyed by image digest; a locally built image has no `RepoDigests`, so invalidation on rebuild is unproven)
- ⬜ `ui/message` consent behaviour, `tool-cancelled`, `tool-input-partial` streaming, the `sandbox-proxy-ready` dance
- ⬜ the pinned `protocolVersion: "2026-01-26"` in `ui/initialize` (cites the spec's own example; the request side is unspecified)

Treat every §12 observation made in the browser as first-hand evidence for these.

## 16. Troubleshooting

| Symptom | Most likely cause (this server / this stack) |
|---|---|
| Registration refused | image not under the `ghcr.io/shanghai-rits/` allowlist prefix |
| Start ends `failed` with a pull error | no local image under that exact tag, or backend can't reach Docker (`DOCKER_SOCKET` pipe wrong / daemon down) |
| Running, but Tools empty | port or endpoint path wrong — must be `8000` + `/mcp` |
| Tools listed, but no cyan tags | server started with `ENABLE_MCP_APPS=0`, or patched `appsEnabled:false` |
| Container starts and exits, logs say `asset check failed` | `src/static/*.webp` missing (only if the image was hand-cut; normal builds generate it) |
| App renders stale after a rebuild | `ui://` digest cache; locally built image has no `RepoDigests` — restart the server row and retry |
| Chat gets images but **no widget** | model chose legacy `get_library_directions` — steer the prompt to a structured tool (§12/§13) |
| Scripted smoke: `"Invalid or expired token"` | guest token lifetime is ~15 min — re-mint `/auth/guest` |
| `dev/host.html` shows a network error for the fixture | opened over `file://` — serve `dev/` over HTTP (§4) |
| `curl` in PowerShell does nothing useful | it is aliased to `Invoke-WebRequest` — use `curl.exe` |

## 17. Production path (later) and its current blocker

Production is the same mechanic without the local shortcut: CI pushes the image to GHCR, then an admin registers the pushed reference. Blocker: `.github/workflows/docker-publish.yaml` sets `IMAGE_NAME` in `env`, but the "Resolve image name" step overwrites it with lowercased `github.repository` — on this fork that resolves to `ghcr.io/atont12/library-map-mcp`, which is **not** under the allowlist prefix. Options when you get there: (1) push from the upstream `shanghai-rits` repo; (2) pin `IMAGE_NAME` in the workflow and drop the override step; (3) add `ghcr.io/atont12/` to Ask's `MCP_IMAGE_ALLOWLIST` — narrows the platform's guardrail, throwaway environments only. Also remember production runs the backend inside the container network, so `MCP_PUBLISH_TO_HOST` must be `false` (dev defaults it to `true`).

## Appendix — quick reference

| Thing | Value |
|---|---|
| Server entry | `src/mcp_server.py` (Streamable HTTP, `/mcp`, `PORT`) |
| Resource | `ui://nav/library-view.html` (`text/html;profile=mcp-app`) |
| Tools | 7 (see §11) |
| Dev harness | `dev/host.html`, `dev/preview.html` (generated), `dev/view.html`, `dev/HOST_CONTRACT.md` |
| Regenerate preview | `<VENV> scripts\build_preview.py` |
| Serve harness | `cd dev && <VENV> -m http.server 8077` |
| Frozen fixture | `dev/fixtures/route_n607.json` (byte-frozen; do not edit) |
| Schema snapshots | `schemas/*.json` (`test_route_schema.py --write` to regenerate) |
| Register values | image `ghcr.io/shanghai-rits/daniel-library-mcp:dev`, port `8000`, path `/mcp` |
| Ask docs | `Ask/docs/mcp-authoring.md`, `Ask/docs/mcp-app-registration.md` |
