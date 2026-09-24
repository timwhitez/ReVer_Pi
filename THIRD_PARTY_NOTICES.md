# Third-party notices

ReVer-Pi is MIT-licensed (see `LICENSE`). It depends on the following projects; none are modified,
forked or vendored, and all are installed from their published distributions.

## Runtime — Python (`pyproject.toml`, pinned by `requirements.lock`)

| Package | Version | License |
|---|---|---|
| httpx | 0.28.1 | BSD-3-Clause |
| pydantic | 2.13.4 | MIT |
| PyYAML | 6.0.3 | MIT |
| fastapi | 0.128.2 | MIT |
| uvicorn | 0.48.0 | BSD-3-Clause |
| jsonschema | 4.26.0 | MIT |

## Runtime — Node (`pi/package.json`, pinned by `pi/package-lock.json`)

| Package | Version | License |
|---|---|---|
| @earendil-works/pi-coding-agent | 0.84.2 | MIT |
| @earendil-works/pi-ai | 0.84.2 | MIT |
| typebox | 1.3.7 | MIT |

Pi is an independent project by Earendil Works. ReVer-Pi is a standalone extension for Pi and is not
an official distribution of it.

## Development and optional extras

| Package | Version | License | Used by |
|---|---|---|---|
| pytest | 9.0.2 | MIT | local checks |
| pytest-asyncio | 1.3.0 | Apache-2.0 | async tests |
| scipy | any | BSD-3-Clause | optional numerical cross-checks in the research record |
| typescript | 5.9.3 | Apache-2.0 | `npm run typecheck` |
| @types/node | 22.19.0 | MIT | type definitions |
| harbor | 0.22.0 (optional extra) | see upstream | optional native task runner |

## Research record

`research/` cites published work (SoL-Pi, The Complexity Trap, ACON, TRACE, risk-control and
confidence-sequence literature) by reference only; no third-party code or data is redistributed
there. Development source snapshots used by the research runners are not distributed with this
repository.
