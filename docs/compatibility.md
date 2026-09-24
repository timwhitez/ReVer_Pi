# Compatibility

## Pinned versions

| Component | Version | Notes |
|---|---|---|
| `@earendil-works/pi-coding-agent` | `0.84.2` | Declared in `pi/package.json`; the runtime checks it before starting |
| `@earendil-works/pi-ai` | `0.84.2` | Assistant message stream types |
| `typebox` | `1.3.7` | Tool parameter schemas |
| TypeScript | `5.9.3` | `npm run typecheck` |
| Node.js | `>= 22.19` | `node --experimental-strip-types` runs the extension and its tests |
| Python | `>= 3.11` | Gateway, CLI, studies |
| Python dependencies | `requirements.lock` | `pip install -e .`; `requirements-harbor.lock` adds the optional Harbor extra |

A different Pi version is a compatibility change, not a drop-in upgrade: the extension depends on
Pi's public extension API surface (provider registration, tool registration, event hooks,
`convertToLlm`, message shapes). Re-run the local checks before adopting another release.

## Upstream policy

- Pi is never patched, forked or vendored. `pi/node_modules` is dependency installation only and is
  git-ignored.
- Pi owns authentication, provider URLs, the main model choice and shell behaviour; ReVer-Pi
  supplies a provider *through* Pi's extension API and keeps its own settings separate.
- SoL-Pi and Pi are independent projects; nothing here is copied into or required by them.

## Platform notes

- Loopback binding is the default. Non-loopback binding requires `--allow-network` and is the
  operator's job to isolate.
- The offline verification scripts use an `LD_PRELOAD` guard that needs a C compiler; without one the
  guard is skipped and only the Python/Node checks run.
- The gateway and extension are plain Python/Node; the research runners under `research/` are not
  supported on Windows.
