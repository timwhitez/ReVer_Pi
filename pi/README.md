# ReVer-Pi Pi extension

Standalone extension for [Pi](https://github.com/earendil-works/pi) that routes the agent through a
local ReVer-Pi gateway and exposes the evidence tools the session is entitled to.

```bash
npm ci
npm run check     # typecheck + Node test suite
```

The extension is declared by `pi.extensions` in `package.json` (`./src/index.ts`), so Pi can load this
directory directly; `reverpi pi-run` launches Pi with the same wiring.

It requires two environment variables, normally set by `reverpi pi-run`:

| Variable | Meaning |
|---|---|
| `REVER_GATEWAY_URL` | Gateway base URL (loopback by default) |
| `REVER_SESSION_TOKEN` | Session capability token issued by `reverpi session-create` |

Optional: `REVER_MAX_TOOLS`, `REVER_MAX_TURNS` for task ceilings, and the `REVER_VERIFIER_*` set to
enable the opt-in revalidation tool. See `../docs/configuration.md`.
