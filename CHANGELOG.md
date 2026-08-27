# Changelog

All notable changes to this project are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versions follow SemVer.

## [Unreleased]

### Added
- `GET /health` liveness probe that never contacts OpenRouter (O2).
- CI now runs on Python 3.11, 3.12 and 3.13 (O5).

### Changed
- Non-stream and pass-through responses relay all upstream headers
  (`x-ratelimit-*`, OpenRouter request id, …) via a shared `_relay` builder,
  instead of keeping only `content-type` (O3). The streaming path is unchanged.

## [0.1.0] - 2026-08-27

Initial release. Single-file OpenAI-compatible proxy that fronts OpenRouter with
Statewave memory.

### Added
- `POST /v1/chat/completions`: fetches a Statewave context bundle for the request
  subject, prepends it as a system message, and writes the turn back as an
  episode off the response path. Streaming and non-streaming both supported.
- Subject/session via `X-Statewave-Subject` / `X-Statewave-Session` headers or
  `statewave_subject` / `statewave_session` body fields (header wins, body fields
  stripped before upstream).
- No subject: byte-identical pass-through with zero Statewave calls.
- Everything else under `/` proxied to OpenRouter verbatim.
- Caller identity (`caller_id` + `caller_type`) on every retrieval; tenant
  forwarding via `X-Tenant-ID` or pinned `STATEWAVE_TENANT_ID`.
- Optional async compile after each turn (`STATEWAVE_COMPILE_AFTER_TURN`).
- Statewave failures never fail the completion; shutdown drains in-flight
  episode writes before closing the HTTP client.

[Unreleased]: https://github.com/smaramwbc/statewave-openrouter/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/smaramwbc/statewave-openrouter/releases/tag/v0.1.0
