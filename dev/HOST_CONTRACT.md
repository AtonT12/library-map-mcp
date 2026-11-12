# HOST_CONTRACT.md — MCP Apps wire contract (Step 5-0, corrected)

> Status: method names + shapes below are **spec-verified** against
> SEP-1865 `specification/2026-01-26/apps.mdx` (Stable). Still unverified:
> Ask's actual behavior (P1 skipped). If Ask deviates, update this file
> FIRST, then `viewer.js` — never the other way round.
>
> Correction history: `ui/call-server-tool` never existed — the bridge
> reuses MCP verbs (`tools/call`). Fixed along with `ui/message` shape,
> `host-context-changed` merge, `appCapabilities` fields, and
> `size-changed` params (all verified against the same spec text).

## Transport

JSON-RPC 2.0 over `window.postMessage` between host page and sandboxed
iframe. Requests carry numeric `id`; responses echo it; notifications
have no `id`. `viewer.js` tolerates unknown inbound methods by ignoring
them; unknown fields inside known messages are ignored (optional-read).

## Message catalog (spec-verified names)

| Dir | Kind | Method | Params |
|---|---|---|---|
| V→H | request | `ui/initialize` | `{protocolVersion, appInfo:{name,version}, appCapabilities:{tools, availableDisplayModes}}` — **all three required**; the host validates the params object and rejects a missing `appInfo` with `-32602 invalid_type` |
| H→V | response | — | `{protocolVersion, hostCapabilities, hostInfo?, hostContext}` (or `{code,message}` error) |
| V→H | notification | `ui/notifications/initialized` | `{}` |
| H→V | notification | `ui/notifications/tool-input` | `{arguments}` (required before tool-result) |
| H→V | notification | `ui/notifications/tool-input-partial` | `{arguments}` (0..n, streaming; view ignores) |
| H→V | notification | `ui/notifications/tool-result` | `CallToolResult` (`{content, structuredContent?, _meta?}`) |
| H→V | notification | `ui/notifications/tool-cancelled` | `{reason}` → view shows error bar + Reset |
| V→H | request | `tools/call` | `{name, arguments}` → standard `CallToolResult` (host proxies to server; MUST reject when visibility lacks `"app"`) |
| V→H | request | `resources/read` | available; unused (tiles go through `get_map_view`) |
| V→H | notification | `ui/notifications/size-changed` | `{width, height}` (sent after each render) |
| H→V | notification | `ui/notifications/host-context-changed` | `Partial<HostContext>` **merged** into current context (no wrapper) |
| V→H | request | `ui/message` | `{role:"user", content:{type:"text", text}}` → `{}`; error exits (Step 6); host MAY require consent |
| V→H | notification | `notifications/message` | log-only (no ui/ prefix); unused |
| V→H | request | `ui/open-link` | `{url}`; reserved exit path |
| V→H | request | `ui/update-model-context` | reserved (not used) |
| H→V | request | `ui/resource-teardown` | reserved (not handled; view is stateless per render) |

## hostContext shape (spec subset we use)

```jsonc
{
  "theme": "light",                        // "light" | "dark" | absent
  "containerDimensions": { "maxHeight": 720 }  // whole block may be absent
}
```

Full `HostContext` also defines `toolInfo/styles/displayMode/locale/…`;
`viewer.js` optional-reads only the two above. `host-context-changed`
carries a **partial merged** into state (spec: "View SHOULD merge").
Never white-screens on a missing field. This library app always renders in
**light** mode to match Ask's current chat UI, including when `theme` is
missing or explicitly `dark`. It does not follow `prefers-color-scheme`.
Other host context, including `containerDimensions`, is still merged and
applied normally.

Ask's `resolveHostTheme` can fall back to the OS preference when the page
has no `dark` class. The proposed host fix (Ask-Roary-UI PR #12) was closed
without merging as of 2026-11-12. Keeping this map light fixes the mismatch
from the app side without requiring a host update. If Ask gains a dark UI,
revisit this product policy before enabling host-driven map themes.

## Production UI surface

The shipped `ui://` view contains only the map, floor tabs, the 2D/3D
toggle, Reset, a marker-detail overlay, and actionable error text. It
must not contain `ping`/`status`/`tilt` controls, raw JSON, or a route
step list; the host chat owns the route narrative. `get_location_detail`
takes exactly `{place_id: string}`.

Both 2D and 3D support dragging to pan and scrolling to zoom. The 3D
projection has a fixed oblique angle; it is not an orbit camera. Only start
and destination markers open place details. The 2D initial view includes the
entire floor footprint, with legend areas clipped out. Reset clears both
views' pan/zoom state; resizing preserves the user's relative 3D framing.

## Order / timing

1. View loads → `ui/initialize` → host responds → view sends `initialized`.
2. Host sends `tool-input` (arguments) then `tool-result` (content +
   structuredContent). Either may arrive first; view renders on
   `tool-result` only.
3. `tool-result` data precedence: `structuredContent` → parse text block
   as JSON → raw text → error bar (never blank iframe).
4. `ui/initialize` unanswered for 3 s → error bar + **Reconnect** button
   (re-runs boot). A failed initialize does **not** send `initialized`
   and does not accept a late tool result that would hide the error.
   The normal map control is **Reset** (restores mode/floor/pan/zoom);
   it never re-runs the handshake or reloads the iframe.

## Pinned values (assumptions to re-verify on Ask)

- `protocolVersion: "2026-01-26"` in `ui/initialize` — cites the spec's own
  `McpUiInitializeResult` example + extension Stable date (NOT the base MCP
  protocol version); the request side is unspecified, kept as best guess.
  **Verified 2026-11-11**: the host accepts this value, but it validates the
  *whole* params object — a missing `appInfo` fails with
  `invalid_type path ["params","appInfo"]`. The host's rule comes from the
  `@modelcontextprotocol/ext-apps` bridge schema:
  `{appInfo: Implementation(name,version), appCapabilities: object, protocolVersion: string}`,
  all required. `dev/browser_test.py` and `dev/host.html` now enforce the same
  rule so a missing field fails locally instead of in the real host.
- `APP_MIME_TYPE = text/html;profile=mcp-app`, `EXTENSION_ID =
  io.modelcontextprotocol/ui` (spec-verified).
- `ui://` resource digest caching (~10 min, unverified Ask behavior).
- Sandbox proxy (`sandbox-proxy-ready` dance) and `tool-input-partial`
  streaming: acknowledged, not simulated; the view ignores both safely.

## Dev harness coverage (`dev/host.html`)

Simulates: initialize response (theme/maxHeight adjustable, omittable),
tool-input, three tool-result flavors (structured / text-only / empty),
`host-context-changed` push (partial, spec shape), `tools/call` echo,
`ui/message` accept-and-display, initialized-guard warnings. Does NOT
simulate: Ask's real field values, CSP, digest cache, consent denials,
`tool-cancelled`, or failure modes — those can only be observed on Ask.
