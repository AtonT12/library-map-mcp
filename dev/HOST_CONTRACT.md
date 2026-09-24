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
| V→H | request | `ui/initialize` | `{protocolVersion, appCapabilities:{tools, availableDisplayModes}}` |
| H→V | response | — | `{protocolVersion, hostCapabilities, hostInfo?, hostContext}` (or `{code,message}` error) |
| V→H | notification | `ui/notifications/initialized` | `{}` |
| H→V | notification | `ui/notifications/tool-input` | `{arguments}` (required before tool-result) |
| H→V | notification | `ui/notifications/tool-input-partial` | `{arguments}` (0..n, streaming; view ignores) |
| H→V | notification | `ui/notifications/tool-result` | `CallToolResult` (`{content, structuredContent?, _meta?}`) |
| H→V | notification | `ui/notifications/tool-cancelled` | `{reason}` → view shows error bar + retry |
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
Never white-screens on a missing field.

## Order / timing

1. View loads → `ui/initialize` → host responds → view sends `initialized`.
2. Host sends `tool-input` (arguments) then `tool-result` (content +
   structuredContent). Either may arrive first; view renders on
   `tool-result` only.
3. `tool-result` data precedence: `structuredContent` → parse text block
   as JSON → raw text → error bar (never blank iframe).
4. `ui/initialize` unanswered for 3 s → error bar + Retry button
   (re-runs boot; `initialized` is re-sent, ids keep incrementing).

## Pinned values (assumptions to re-verify on Ask)

- `protocolVersion: "2026-01-26"` in `ui/initialize` — cites the spec's own
  `McpUiInitializeResult` example + extension Stable date (NOT the base MCP
  protocol version); the request side is unspecified, kept as best guess.
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
