# statewave-openrouter

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)

An OpenAI-compatible proxy that sits between your app and
[OpenRouter](https://openrouter.ai) and gives every call
[Statewave](https://github.com/smaramwbc/statewave) memory:

1. **Before** forwarding, it assembles a Statewave context bundle for the
   request's subject and prepends it as a system message.
2. **After** the reply, it writes the turn back as an episode, so the next
   call knows about this one.

Requests without a subject are forwarded untouched - memory is opt-in per
request, not a global mode.

> **Part of the Statewave ecosystem:** [Server](https://github.com/smaramwbc/statewave) · [Python SDK](https://github.com/smaramwbc/statewave-py) · [TypeScript SDK](https://github.com/smaramwbc/statewave-ts) · [Docs](https://github.com/smaramwbc/statewave-docs) · [Website](https://statewave.ai)
>
> 📋 **Issues & feature requests:** tracked centrally on [`smaramwbc/statewave`](https://github.com/smaramwbc/statewave/issues).

---

## Getting started

```bash
pip install -e .
cp .env.example .env        # set OPENROUTER_API_KEY and STATEWAVE_URL
uvicorn statewave_openrouter:app --port 8080
```

Then point any OpenAI-compatible client at the proxy and name a subject:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8080/v1", api_key="sk-or-...")

client.chat.completions.create(
    model="openai/gpt-4o",
    messages=[{"role": "user", "content": "What coffee do I like?"}],
    extra_headers={"X-Statewave-Subject": "user:42"},
)
```

`curl` works the same way:

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "X-Statewave-Subject: user:42" \
  -d '{"model":"openai/gpt-4o","messages":[{"role":"user","content":"What coffee do I like?"}]}'
```

## How a request flows

```
client ──POST /v1/chat/completions──▶ proxy ──POST /v1/context────▶ statewave
                                       │  ◀──assembled_context─────┘
                                       ├──messages = [system(context), ...original]
                                       ├──────────────────────────▶ openrouter
                                       │  ◀──completion────────────┘
                                       ├──POST /v1/episodes───────▶ statewave  (async)
                                    client ◀──completion (verbatim bytes)
```

Streaming works the same way: SSE chunks are relayed byte-for-byte, and the
episode is written once the stream closes.

## Naming the subject

Either of these, header wins:

| Where | Subject | Session (optional) |
| --- | --- | --- |
| Header | `X-Statewave-Subject: user:42` | `X-Statewave-Session: sess_abc` |
| Body | `"statewave_subject": "user:42"` | `"statewave_session": "sess_abc"` |

A subject or session id is 1-256 characters of letters, digits, underscore,
dot, dash or colon - no `/` or whitespace. Anything else gets a `400
statewave_bad_request` from the proxy rather than a 422 from Statewave.

Body fields are stripped before the request reaches OpenRouter. Multi-tenant
Statewave deployments: send `X-Tenant-ID` and it is forwarded, or pin one with
`STATEWAVE_TENANT_ID`.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `OPENROUTER_API_KEY` | - | Fallback key when the caller sends no `Authorization` header |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` | Upstream |
| `OPENROUTER_SITE_URL` / `OPENROUTER_SITE_NAME` | - | OpenRouter attribution (`HTTP-Referer` / `X-Title`) |
| `STATEWAVE_URL` | `http://localhost:8000` | Statewave server |
| `STATEWAVE_API_KEY` | - | Sent as `X-API-Key` |
| `STATEWAVE_TENANT_ID` | - | Default tenant when the caller sends no `X-Tenant-ID` |
| `STATEWAVE_CONTEXT_TOKENS` | `1500` | Token budget for the injected bundle |
| `STATEWAVE_COMPILE_AFTER_TURN` | off | Kick an async compile after each turn |
| `STATEWAVE_EPISODE_SOURCE` | `openrouter-proxy` | `source` on written episodes |
| `STATEWAVE_CALLER_TYPE` | `openrouter-gateway` | Caller class Statewave policy rules match on |
| `STATEWAVE_CALLER_ID` | = `STATEWAVE_CALLER_TYPE` | Caller identity sent on every retrieval |
| `PROXY_TIMEOUT` | `120` | Upstream request timeout (seconds) |

## Requires Statewave >= 1.0.0

Caller identity (`caller_id` / `caller_type`) landed in v0.9.0; v1.0.0 is
the first release where the tenant-config surface is complete. On a tenant
with `require_caller_identity`, retrieval without those fields is a 401; on
one in `policy_mode: enforce`, an absent `caller_type` is the
least-privileged caller and quietly thins the bundle. The proxy always
sends both.

## Failure behaviour

Statewave is an enhancement, never a hard dependency. If context assembly or
the episode write fails, it is logged and the completion still goes through - just without memory for that turn.

## Everything else

Any other path (`/v1/models`, `/v1/credits`, …) is proxied straight to
OpenRouter, so the proxy is a drop-in base URL replacement.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

## License

Apache-2.0. See [LICENSE](LICENSE).
