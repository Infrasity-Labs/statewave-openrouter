# statewave-openrouter - Requirements

Scope: one file (`statewave_openrouter.py`, ~350 lines) that fronts OpenRouter
with Statewave memory. Anything that would be a second service belongs in
`smaramwbc/statewave`, not here.

Status as of 2026-08-26: code is written and tested, **zero commits exist**.
Version 0.1.0, Alpha.

Legend: **Done** = shipped and covered by a test in `test_proxy.py`.
**Gap** = not built. **Won't** = deliberately out of scope.

---

## Functional

| ID | Requirement | Status |
| --- | --- | --- |
| F1 | `POST /v1/chat/completions` with a subject fetches `/v1/context` and prepends the bundle as a system message | Done |
| F2 | After the reply, the turn is written to `/v1/episodes` off the response path | Done |
| F3 | No subject → byte-identical pass-through, zero Statewave calls | Done |
| F4 | Subject/session from `X-Statewave-Subject`/`-Session` header or `statewave_subject`/`_session` body field; header wins; body fields stripped before upstream | Done |
| F5 | Ids not matching `^[A-Za-z0-9_.\-:]{1,256}$` → `400 statewave_bad_request` before any upstream call | Done |
| F6 | Retrieval `task` truncated to 4000 chars (server cap is a 422, not a truncation) | Done |
| F7 | Streaming: SSE relayed byte-for-byte, episode written when the stream closes | Done |
| F8 | Every other path proxied to OpenRouter unchanged | Done |
| F9 | `caller_id` + `caller_type` sent on every retrieval; empty env value must not become an empty caller_id | Done |
| F10 | `X-Tenant-ID` forwarded, or pinned via `STATEWAVE_TENANT_ID` | Done |
| F11 | Optional async compile after each turn (`STATEWAVE_COMPILE_AFTER_TURN`) | Done |
| F12 | `POST /v1/completions` (legacy) is memory-aware | **Gap** - falls through to F8 |
| F13 | `POST /v1/responses` is memory-aware | **Gap** - falls through to F8 |
| F14 | Non-stream path writes an episode even when the reply text is empty; stream path skips it. Pick one. | **Gap** - asymmetry, `chat_completions` vs `relay` |
| F15 | Multi-turn history summarisation, local caching, prompt templating | Won't - Statewave's job |

## Reliability

| ID | Requirement | Status |
| --- | --- | --- |
| R1 | Statewave failure (any exception, any status) never fails the completion - logged, turn proceeds without memory | Done |
| R2 | 401 from Statewave logged as an error naming the two likely causes, not as a transient warning | Done |
| R3 | Shutdown drains in-flight episode writes before closing the httpx client - the turn exists nowhere else | Done |
| R4 | Upstream timeout 120s default, 10s connect | Done |
| R5 | Streaming buffers the whole SSE body to rebuild the reply - memory is O(reply size) per in-flight stream | **Gap** - marked `ponytail:` in source; parse incrementally when replies get large |
| R6 | No retry/backoff against Statewave | Won't - a failed turn is one lost episode, not lost data |

## Security

| ID | Requirement | Status |
| --- | --- | --- |
| S1 | Caller's `Authorization` forwarded verbatim; `OPENROUTER_API_KEY` used only as fallback | Done |
| S2 | **Subject is caller-asserted.** Any client that can reach the proxy can send `X-Statewave-Subject: user:99` and read/write that subject's memory. | **Gap - blocks any deployment where clients are not trusted** |
| S3 | With `OPENROUTER_API_KEY` set and no proxy-level auth, anyone who can reach the port spends the operator's OpenRouter credits | **Gap** - same root cause as S2 |
| S4 | Statewave API key never reaches OpenRouter and vice versa (separate header builders) | Done |
| S5 | Secrets never logged - log lines carry subject ids only | Done |

S2/S3 fix, decided: proxy trusts a bearer token it can verify, and derives the
subject from it. Header-supplied subject is accepted only when the proxy is
started in an explicit trusted-client mode. One env var, one check - not an
auth framework.

## Operations

| ID | Requirement | Status |
| --- | --- | --- |
| O1 | Config entirely via env vars, documented in `.env.example` and README | Done |
| O2 | `GET /health` that does not hit OpenRouter | Done |
| O3 | Upstream response headers relayed (`x-ratelimit-*`, OpenRouter request id) | Done - shared `_relay` builder; stream path still drops them |
| O4 | CI: ruff + pytest on 3.11 | Done |
| O5 | CI matrix covers 3.12 and 3.13 - both claimed in `pyproject.toml` classifiers, neither tested | Done |
| O6 | Startup warns when the Statewave server is older than 1.0.0 (README states the floor; nothing enforces it) | **Gap** |
| O7 | Dockerfile + published image | **Gap** |
| O8 | Published to PyPI, tagged, CHANGELOG | **Gap** |

---

## Timeline

One developer, all dates 2026. Everything below is small: this is a
single-file proxy, not a platform.

| Week | Milestone | Ships |
| --- | --- | --- |
| ~~Wed Aug 26 to Fri Aug 28~~ **done 2026-08-27** | **M0 - exist in git** | Initial commit of the current tree, tag `v0.1.0`, CHANGELOG. O8 minus PyPI. |
| ~~Mon Aug 31 to Fri Sep 4~~ **done 2026-08-27** | **M1 - operable** | O2 `/health`, O3 header relay (shared `_relay` builder), O5 CI matrix. Tests: probe hits no upstream; rate-limit header survives a round trip. Stream path still drops upstream headers - folded into R5's rework. |
| **Mon Sep 7 to Fri Sep 11** | **M2 - trustworthy** | S2 + S3: verified bearer → derived subject, `STATEWAVE_TRUST_CLIENT_SUBJECT` opt-out for single-tenant deploys. Tests: forged subject rejected; trusted mode still honours the header. **Blocks any public deployment - do not ship a hosted instance before this.** |
| **Mon Sep 14 to Fri Sep 18** | **M3 - surface complete** | F12, F13 (`/v1/completions`, `/v1/responses` memory-aware - extract the subject/context/episode logic the three now share), F14 empty-reply symmetry, O6 version warning. |
| **Mon Sep 21 to Fri Sep 25** | **M4 - v1.0.0** | R5 incremental SSE parse, O7 Docker image, PyPI publish, README rewrite against the final surface. Tag `v1.0.0`, drop Alpha classifier. |

Critical path: M0 → M2. M1 and M3 can swap if a deployment target appears first.

Not scheduled: metrics/OpenTelemetry, multi-provider upstreams, an admin API.
Each turns this into a service that needs owning. Add when something concrete
demands it.
