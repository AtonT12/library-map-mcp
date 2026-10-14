# RUNBOOK — Manual testing & local-dev integration with Ask

> Audience: whoever is changing this server and wants to see it work, first on
> its own and then mounted in the Ask platform.
> Scope: **local only**. Nothing here deploys anything.
> Language note: this file is English because `AGENTS.md §Language convention`
> requires everything landed in this repo to be English.

Two independent tracks — do them in order:

| Track | What it proves | Needs |
|---|---|---|
| **A. Local manual testing** (§0–§5) | the server answers, the routes are sane, the view renders | Python venv only |
| **B. Ask local-dev integration** (§6) | the view renders **inside the real Ask chat host** | Docker Desktop + Ask backend + Admin UI |

Track A is a prerequisite for Track B: the container built in §6 runs the same
code you just tested in §1–§5.

Throughout, `<APP>` = `D:\Codes\RITS\map-mcp\daniel-library-mcp` and
`<VENV>` = `D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe`.
**Always call the venv interpreter by absolute path** — a bare `python` on this
machine is not the project interpreter.

---

## 0. One-time setup

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' -m pip install -r requirements.txt
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' scripts\build_static.py
```

```bash
# Git Bash
cd /d/Codes/RITS/map-mcp/daniel-library-mcp
../.venv-daniel/Scripts/python.exe -m pip install -r requirements.txt
../.venv-daniel/Scripts/python.exe scripts/build_static.py
```

- `requirements.txt` pins `mcp==1.26.0` and `Pillow==10.4.0`. Do **not** bump
  `mcp` to 2.x (`FastMCP` is renamed and Streamable HTTP adds a 4 MiB request
  cap). `jsonschema` arrives transitively and is used by the tests — leave it.
- `build_static.py` writes the gitignored `src/static/*.webp`. It has already
  been run (the two `.webp` files exist), but **re-run it after any change to
  `maps/*_base.jpg`**, otherwise the `ui://` resource fails to build.
- `requirements-dev.txt` is intentionally empty: the dev tools use stdlib +
  Pillow only.

---

## 1. Start the server

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
$env:PYTHONPATH = 'src'
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' 'src\mcp_server.py'
# optional overrides, set before launching:
#   $env:PORT = '8010'          # listen here instead of 8000
#   $env:ENABLE_MCP_APPS = '0'  # kill switch: no ui:// resource, legacy tools only
```

```bash
# Git Bash
cd /d/Codes/RITS/map-mcp/daniel-library-mcp
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/mcp_server.py
```

Expect, in order: `assets ok` (this is `selfcheck.verify_assets()`), then the
FastMCP banner, then a listener on `0.0.0.0:8000`.

Behaviour that surprises people:

- Transport is **Streamable HTTP at `/mcp` only** — there is no stdio or SSE
  mode and no flag to switch. Port comes from `PORT` (default `8000`).
- If `selfcheck` finds a missing asset the process prints the list and
  **`exit 1`** — the server never starts. That is deliberate (`Dockerfile`
  relies on the same check at build time).
- A stray `pydantic_settings` warning on startup is harmless.

---

## 2. Talk to it — VS Code MCP client

The client wiring is already committed at
`D:\Codes\RITS\map-mcp\daniel-library-mcp\.vscode\mcp.json`:

```json
{ "servers": { "nyush-library": { "type": "http", "url": "http://127.0.0.1:8000/mcp" } } }
```

With the server running (§1), open this folder in VS Code and run
**`MCP: List Servers`** from the command palette → start/reconnect
`nyush-library` → the 7 tools appear in the tool list.

`D:\Codes\RITS\opencode.json` points at the same URL as a `remote` server. Note
the known limitation (`PROJECT_RECOGNITION §6`): **tools work, the interactive
view does not** — a terminal host ignores `_meta.ui`, which is exactly the
legacy L2 path.

---

## 3. Talk to it — raw JSON-RPC probe

Useful when a client is misbehaving and you want to see the wire.

In PowerShell, `curl` is an alias for `Invoke-WebRequest`; **call `curl.exe`**.
First a dumb reachability check:

```powershell
Test-NetConnection 127.0.0.1 -Port 8000
```

Then the handshake. Streamable HTTP keys a session off the `mcp-session-id`
response header — capture it from step 1 and paste it into steps 2–3.

```powershell
# 1) initialize  (note the mcp-session-id response header)
curl.exe -s -D - -o - -X POST http://127.0.0.1:8000/mcp `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -d '{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"probe\",\"version\":\"0.1\"}}}'

# 2) tell the server we are initialized
curl.exe -s -X POST http://127.0.0.1:8000/mcp `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -H "mcp-session-id: <ID-FROM-STEP-1>" `
  -d '{\"jsonrpc\":\"2.0\",\"method\":\"notifications/initialized\"}'

# 3) list the tools
curl.exe -s -X POST http://127.0.0.1:8000/mcp `
  -H "Content-Type: application/json" `
  -H "Accept: application/json, text/event-stream" `
  -H "mcp-session-id: <ID-FROM-STEP-1>" `
  -d '{\"jsonrpc\":\"2.0\",\"id\":2,\"method\":\"tools/list\",\"params\":{}}'
```

> **UNVERIFIED** (confirm on your machine): the exact `mcp-session-id` behaviour,
> the negotiated `protocolVersion`, and whether responses come back as
> `application/json` or an SSE (`text/event-stream`) body that you have to parse.
> If the probe fights you, skip it — §2 and §5 cover the same ground with less
> pain.

---

## 4. Manual test of the app view (dev harness)

You cannot develop the view inside Ask; `dev/host.html` is the host-side
simulator that completes the `ui://` handshake locally.

### 4.1 Regenerate the preview and serve `dev/`

`dev/preview.html` is a **generated artifact** (gitignored, not on disk until
you build it) and it does **not** auto-update. Re-run this after every change to
`src/view/viewer.js`, `src/view/template.py`, or the base maps:

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' scripts\build_preview.py
```

It prints the byte count and writes `dev/preview.html`, byte-identical to what
the live `ui://nav/library-view.html` resource serves.

Then serve `dev/` **over HTTP** — `fetch()` is blocked on `file://`, so the
"real RoutePlan" button cannot load its fixture otherwise. Use a port other than
8000 so you do not collide with the server:

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp\dev
& 'D:\Codes\RITS\map-mcp\.venv-daniel\Scripts\python.exe' -m http.server 8077
```

### 4.2 Drive it

1. Open `http://127.0.0.1:8077/host.html`.
2. In the **view** dropdown pick `./preview.html (template output)` — the full
   mode. (`view.html (5-0 shell)` is a thin error/status shell, not the real
   template.)
3. Wait for `initialized` in the log — that is the handshake completing.
4. Push a route, either way:
   - click **`tool-result (real RoutePlan)`** (it fetches
     `dev/fixtures/route_n607.json`), or
   - open the console (**host page's** console, not the iframe's) and paste:

     ```js
     const p = await fetch('./fixtures/route_n607.json').then(r=>r.json());
     notify('ui/notifications/tool-result', {content:[{type:'text',text:'review N607'}], structuredContent:p});
     ```

   Within one route you can also use **`tool-input`** to send arguments first,
   and **`tool-result (structured)` / `(text-only)` / `(empty)`** to exercise the
   three failure-ish shapes. **`push host-context-changed`** re-applies the
   theme/`maxHeight` you set in the panel; **`reload view`** reboots the iframe.

### 4.3 Manual checklist

There is **no Playwright** in this project (deliberate) — UI regression is this
list, run by hand after any `viewer.js` change:

| # | Area | What to check |
|---|---|---|
| V4 | Pan / zoom | wheel zoom is centred on the cursor; panning does not drift |
| V5 | Place detail | tapping a station opens the side panel with the right record |
| V6 | Floor tabs | 5F ⇄ 6F switch re-renders the correct base map |
| V7 | Hi-res tiles | zoom past the 0.35 threshold triggers a `get_map_view` tile (800 ms debounce) |
| 3D | Oblique view | off by default; toggling it extrudes slabs, keeps circles round / text upright, and dims the non-current floor to 0.45 |
| L3 | Error bar | the three `tool-result` flavours (structured / text-only / empty) never white-screen; the 3 s handshake timeout shows Retry |
| V9 | Theme | toggle light/dark in the panel; both look right |
| V8 | Height | with `maxHeight = 720`, long content **scrolls inside the frame** instead of being clipped |

---

## 5. Automated test suites

Run from `<APP>`. Suites are plain scripts with `assert` (no pytest).

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
# Git Bash
cd /d/Codes/RITS/map-mcp/daniel-library-mcp
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test_fallbacks.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/test_route_schema.py
PYTHONPATH=src ../.venv-daniel/Scripts/python.exe src/selfcheck.py
node dev/viewer.test.js
```

| Suite | Covers | Notes |
|---|---|---|
| `src/test.py` | route shapes / text parity / accessible / already-there + `render_view` + the wall audit over ~220 routes | **heaviest**; the wall audit is the slow part |
| `src/test_fallbacks.py` | `ENABLE_MCP_APPS=0` subprocess (no resource, no `_meta.ui`) + **4 byte-frozen image lengths** + error paths + a ≤100 px transit-registration probe | the 4 frozen lengths **must not change** unless you intend a re-freeze |
| `src/test_route_schema.py` | `schemas/*.json` snapshots (`--write` to regenerate) + top-1 parity + determinism + boundaries + **starts a real server on port 18083** | it **temporarily renames** `corridors.json` and `maps/5f_base.jpg` and restores them in a `finally`; do not run it concurrently with other data-touching work |
| `src/selfcheck.py` | asset integrity; prints `assets ok` | exit 0 = ok, non-zero = list of problems |
| `node dev/viewer.test.js` | 2D coordinate math / layering / 3D projection / error paths against the real fixture | prints `VIEWER TEST OK` |

Wall-test budgets (env, all default keepers of a known residual set):

```
WALL_CROSSING_BUDGET   # default 0
WALL_CLEARANCE_BUDGET  # default 0
WALL_DETOUR_BUDGET     # default 5 (documented residuals; see PROJECT_RECOGNITION §7)
```

Dev-only auditors (not in CI, offline, stdlib + Pillow):

```powershell
& '..\.venv-daniel\Scripts\python.exe' dev\wall_audit.py all          # profile / coverage / ortho / connect
& '..\.venv-daniel\Scripts\python.exe' dev\vector_compare.py --refine # .ai vector walls vs corridors.json
```

CI (`.github/workflows/test.yaml`) runs the three Python suites + the viewer
test + `docker build .` on py3.11.

---

## 6. Mount into Ask (local dev integration)

The goal: see the `ui://nav/library-view.html` view render **inside the real Ask
chat**. This is the **local dev path** — no registry push, no CI. Ask is at
`D:\Codes\RITS\Ask`.

How mounting works, in one line: an admin **registers an image reference**, the
platform **runs that image as a container**, and the server does nothing until
it is **bound to an experience**. You never edit Ask's source to add a server.

### 6.1 Prerequisites

| Need | How |
|---|---|
| Docker Desktop running (Linux containers) | `docker info` must succeed. |
| Ask's Docker socket reachable from the backend | see the Windows note below |
| Ask infrastructure | `cd D:\Codes\RITS\Ask ; npm install ; docker compose up -d` (db/pgvector, elasticsearch, kibana, redis, unstructured, adminer, smtp-relay), then apply migrations: `npx prisma migrate deploy` |
| Ask backend | `npm run start:dev` (listens on `http://localhost:3000`) |
| Ask Admin UI | `cd D:\Codes\RITS\Ask-Admin-UI ; npm install ; npm run dev` → `http://localhost:5173/mcp-servers` |
| Ask chat UI (for the smoke test) | `cd D:\Codes\RITS\Ask-Roary-UI ; npm install ; npm run dev` |
| An account with `MANAGE_MCP_SERVERS` | Admin UI logs in via Google (`GOOGLE_DOMAIN=nyu.edu`); the permission is granted **only to `super-admin`** (`Ask/src/.../roles-permissions.ts`). Promote yourself with the repo's `prisma/addAdmin.ts` helper if you get a 403. |

> **Windows Docker socket — read this before §6.2.**
> The backend talks to Docker through `dockerode` with
> `socketPath = DOCKER_SOCKET || '/var/run/docker.sock'`
> (`Ask/src/mcp-registry/mcp-runtime.service.ts`). `/var/run/docker.sock` is a
> Linux path and **does not exist on Windows**, so a natively-run backend cannot
> reach Docker until you set a Windows named pipe in `D:\Codes\RITS\Ask\.env`:
>
> ```
> DOCKER_SOCKET=//./pipe/dockerDesktopLinuxEngine
> ```
>
> **UNVERIFIED**: the exact pipe name depends on your Docker Desktop context —
> `docker context inspect` will tell you (`//./pipe/dockerDesktopLinuxEngine`
> for the WSL2 engine vs `//./pipe/docker_engine` for the older one). The
> alternative is to run the Ask backend inside its own container (as in
> production), where the socket is bind-mounted and the Linux default is correct.
>
> Also: **both Vite UIs default to port 5173** and will collide. Start one of
> them with `npm run dev -- --port 5174` (Ask's `FRONTEND_URL` already allows
> both 5173 and 5174).

You do **not** need `docker login ghcr.io` for this path — see the next step.

### 6.2 Build and tag the image

The runtime pulls on Start (`provisionServer` → `pullImage`), **but it falls
back to a locally-built image when the pull fails and a same-named image exists
locally** (`mcp-runtime.service.ts`, the `catch (pullErr)` branch). So a local
build is enough — no registry, no login.

The image reference must satisfy the allowlist prefix (**`ghcr.io/shanghai-rits/`**
by default), or registration is refused before Docker is even touched. Build
under that exact tag:

```powershell
cd D:\Codes\RITS\map-mcp\daniel-library-mcp
docker build -t ghcr.io/shanghai-rits/daniel-library-mcp:dev .
```

Why `:dev` and not `:latest`: it is obviously local, and it keeps the tag free
for a real push later. The build runs `selfcheck.py` and `build_static.py` at
image-build time (see `Dockerfile`), so a broken asset fails the build, not the
first request.

> Do **not** tag it `ghcr.io/atont12/library-map-mcp` (the fork's name) — that
> prefix is not allowlisted and Register would reject it.

### 6.3 Run the Admin UI and sign in

Start the backend and Admin UI from §6.1, then open
`http://localhost:5173/mcp-servers` and sign in with Google. If you see a 403
alert about `MANAGE_MCP_SERVERS`, you are not a super-admin yet.

### 6.4 Register the server

**Admin → MCP Servers → Register MCP Server**. Fill exactly this:

| Field | Value |
|---|---|
| Name | `nyush-library` |
| Description | NYUSH Library visual navigator (routes + interactive floor view) |
| Image | `ghcr.io/shanghai-rits/daniel-library-mcp:dev` |
| Port | `8000` |
| Endpoint path | `/mcp` |
| Environment variables | *(leave empty)* — `PORT` is injected from the Port field, and the app toggle is on by default |
| Secret env | *(leave empty)* |
| Active | on |
| Allowed internet domains (egress) | *(leave empty)* — this server needs no outbound internet |

Saving moves the row through `pending → pulling → running` (the list polls every
5 s). A pull failure is expected here; it should fall through to the local image
and still reach `running`.

### 6.5 Start it and verify

Press **Start** on the row, then check, in order:

1. **Status** becomes `running` and **Last error** stays empty. The start path
   ends with a **30 s** readiness poll — do not call it failed before that
   window closes.
2. **Logs** show `assets ok` and a listener. A container that starts and exits
   usually means a hard-coded port or a missing asset.
3. **Tools** lists all seven: `get_library_map`, `get_library_directions`,
   `search_locations`, `get_route`, `get_location_detail`, `get_library_status`,
   `get_map_view`.
4. App binding: the four structured tools show a cyan **`MCP App`** tag, and
   expanding a row reads `Renders ui://nav/library-view.html in the chat`.
   `get_library_map` / `get_library_directions` have **no** tag (legacy).
   `get_map_view` shows **`app only`**.

If Tools is empty but the container is running, the port or endpoint path is
wrong (`8000` + `/mcp`).

### 6.6 Confirm app settings and bind to an experience

**Edit** on the row:

- **Render MCP Apps** → on. (Off makes the tools text-only on the next message,
  no restart — this is the A/B switch.)
- **Allowed app origins (CSP)** → **empty**. Empty means the view may only talk
  to its own server through the host proxy, which is what we want.

Then **Admin → Experiences → (a row, e.g. `ask-roary`) → MCP Tools → Add →
`nyush-library`**. A registered server does nothing until it is bound; binding
takes effect on the next chat request, no deploy.

### 6.7 Smoke-test in the chat

Open that experience's chat and ask something that forces the tool, e.g.
**"How do I get from the Entrance to N607?"** (a cross-floor route, so you also
exercise the two-image path). Check:

- the tool is actually called and the answer uses real data;
- the view renders **inline** and **scrolls** — with a long leg list and with a
  leg expanded;
- the frame is capped at **720 px**; anything past it must scroll **inside** the
  view, not be clipped;
- **light and dark** both look right;
- a 5F→6F request returns **two floor images**.

### 6.8 A/B the app

Two ways to prove the app layer is what renders it:

- **Restart-time**: set `ENABLE_MCP_APPS=0` (env on the row) and restart → the
  `ui://` resource is not registered, tools return text/images only.
- **No-restart**: Edit → turn **Render MCP Apps** off → next message is
  text-only.

---

## 7. What is still unverified (P1)

`dev/HOST_CONTRACT.md` marks the following as **assumed, not observed** — the
harness cannot simulate them, and **P1 (a real Ask smoke test) has never been
run**:

- Ask's actual `theme` / `containerDimensions.maxHeight` values and whether it
  ever sends `host-context-changed`.
- Whether Ask enforces the declared CSP, and its digest cache for `ui://`
  resources (~10 min, keyed by image digest).
- `ui/message` consent behaviour, `tool-cancelled`, `tool-input-partial`
  streaming, and the `sandbox-proxy-ready` dance.
- The pinned `protocolVersion: "2026-01-26"` in `ui/initialize` (cites the spec's
  own example; the request side is unspecified).

Treat every §6.7 observation as the first real evidence for these.

---

## 8. Troubleshooting

| Symptom | Most likely cause (this server) |
|---|---|
| Registration refused | Image not under `ghcr.io/shanghai-rits/` |
| Start ends `failed`, Last error mentions a pull error | No local image under that exact tag, or Docker unreachable (Windows `DOCKER_SOCKET` wrong) |
| Start ends `failed` ~30 s in, Logs show `asset check failed` | `src/static/*.webp` missing (only if you hand-cut the image; build-time generation covers normal builds) |
| Running, but Tools is empty | Port or endpoint path wrong — must be `8000` + `/mcp` |
| Tools listed, but no cyan tag | The server was started with `ENABLE_MCP_APPS=0` |
| App does not render in chat | `Render MCP Apps` off, or the tool has no `ui://`, or the view bundle was not built |
| App renders but is cut off | The view is not scrolling inside the 720 px ceiling |
| App renders stale after a rebuild | The `ui://` digest cache; a locally-built image has no `RepoDigests`, so whether the cache invalidates is **UNVERIFIED** — restart the server row and retry |
| Admin UI returns 403 | Your account is not `super-admin` |
| `dev/host.html` shows a network error for the fixture | You opened it over `file://`; serve `dev/` over HTTP (§4.1) |
| `curl` in PowerShell does nothing useful | `curl` is aliased to `Invoke-WebRequest`; use `curl.exe` |

---

## 9. Production path (for later) and its current blocker

Production is the same mechanic without the local shortcut: CI pushes the image
to GHCR, then an admin registers the pushed reference.

`.github/workflows/docker-publish.yaml` builds and pushes on `main` / tags.
**Blocker:** the workflow sets `IMAGE_NAME: shanghai-rits/daniel-library-mcp`
in `env`, but the "Resolve image name" step overwrites it with
`github.repository`, lowercased. On this fork that resolves to
**`ghcr.io/atont12/library-map-mcp`**, which is **not** under the default
allowlist prefix — Register would refuse it. Options when you get there:

1. push from the upstream `shanghai-rits` repo, or
2. pin `IMAGE_NAME` in the workflow and drop the override step, or
3. add `ghcr.io/atont12/` to Ask's `MCP_IMAGE_ALLOWLIST` — narrows the
   platform's guardrail, so only for a throwaway environment.

Also remember production runs the backend inside the container network, so
`MCP_PUBLISH_TO_HOST` must be `false` (dev defaults it to `true`).

---

## Appendix — quick reference

| Thing | Value |
|---|---|
| Server entry | `src/mcp_server.py` (Streamable HTTP, `/mcp`, `PORT`) |
| Resource | `ui://nav/library-view.html` (`text/html;profile=mcp-app`) |
| Tools | 7 (see §6.5) |
| Dev harness | `dev/host.html`, `dev/preview.html` (generated), `dev/view.html`, `dev/HOST_CONTRACT.md` |
| Regenerate preview | `python scripts/build_preview.py` |
| Serve harness | `cd dev && python -m http.server 8077` |
| Frozen fixture | `dev/fixtures/route_n607.json` (byte-frozen; do not edit) |
| Schema snapshots | `schemas/*.json` (`test_route_schema.py --write` to regenerate) |
| Register values | image `ghcr.io/shanghai-rits/daniel-library-mcp:dev`, port `8000`, path `/mcp` |
| Ask docs | `Ask/docs/mcp-authoring.md`, `Ask/docs/mcp-app-registration.md` |
