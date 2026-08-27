"""One runnable check: context in, turn out, pass-through intact.

Both upstreams (Statewave and OpenRouter) are faked with an httpx
MockTransport swapped into the proxy's shared client.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import jwt
import pytest

import statewave_openrouter as sw

CONTEXT = "Known facts:\n- prefers dark roast"
SECRET = "test-secret-padded-to-32-bytes-min!"


def token(sub, secret=SECRET, **claims):
    return jwt.encode({"sub": sub, **claims}, secret, algorithm="HS256")


@pytest.fixture
def calls(monkeypatch):
    """Swap the proxy's upstream client for a recorded fake."""
    seen: list[httpx.Request] = []

    async def sse():
        for line in (
            'data: {"choices":[{"delta":{"content":"dark "}}]}\n\n',
            'data: {"choices":[{"delta":{"content":"roast"}}]}\n\n',
            "data: [DONE]\n\n",
        ):
            yield line.encode()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        path = request.url.path
        raw = request.read()
        if "openrouter" in request.url.host:
            if raw and json.loads(raw).get("stream"):
                return httpx.Response(
                    200, content=sse(), headers={"content-type": "text/event-stream"}
                )
            rl = {"x-ratelimit-remaining": "42", "x-request-id": "or-req-1"}
            if path.endswith("/models"):
                return httpx.Response(200, json={"data": [{"id": "openai/gpt-4o"}]}, headers=rl)
            return httpx.Response(
                200, json={"choices": [{"message": {"content": "dark roast"}}]}, headers=rl
            )
        if path == "/v1/context":
            return httpx.Response(200, json={"assembled_context": CONTEXT})
        if path == "/v1/episodes":
            return httpx.Response(201, json={"id": "ep_1"})
        return httpx.Response(404, json={})

    monkeypatch.setattr(sw, "client", httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return seen


@pytest.fixture
def proxy():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=sw.app), base_url="http://proxy")


@pytest.fixture
def trusted(monkeypatch):
    """Model a single-tenant deploy that trusts the caller-supplied subject."""
    monkeypatch.setattr(sw, "TRUST_CLIENT_SUBJECT", True)


async def drain():
    """Let the fire-and-forget episode writes finish."""
    await asyncio.gather(*list(sw._background))


def sent_to(calls, path):
    return [c for c in calls if c.url.path == path]


def body_of(request):
    return json.loads(request.read())


async def test_context_injected_and_turn_written(calls, proxy, trusted):
    response = await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42", "X-Tenant-ID": "acme"},
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "coffee?"}]},
    )
    await drain()
    assert response.status_code == 200

    context_call = sent_to(calls, "/v1/context")[0]
    assert body_of(context_call) == {
        "subject_id": "user:42",
        "task": "coffee?",
        "max_tokens": 1500,
        "caller_id": "openrouter-gateway",
        "caller_type": "openrouter-gateway",
    }
    assert context_call.headers["X-Tenant-ID"] == "acme"

    forwarded = body_of(sent_to(calls, "/api/v1/chat/completions")[0])
    assert forwarded["messages"] == [
        {"role": "system", "content": CONTEXT},
        {"role": "user", "content": "coffee?"},
    ]

    episode = body_of(sent_to(calls, "/v1/episodes")[0])
    assert episode["subject_id"] == "user:42"
    assert episode["payload"] == {
        "messages": [
            {"role": "user", "content": "coffee?"},
            {"role": "assistant", "content": "dark roast"},
        ],
        "model": "openai/gpt-4o",
    }


async def test_no_subject_is_plain_passthrough(calls, proxy):
    await proxy.post(
        "/v1/chat/completions",
        json={"model": "openai/gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()
    assert sent_to(calls, "/v1/context") == []
    assert sent_to(calls, "/v1/episodes") == []
    assert body_of(sent_to(calls, "/api/v1/chat/completions")[0])["messages"] == [
        {"role": "user", "content": "hi"}
    ]


# "/" is in neither charset; subject and session share one constraint server-side.
@pytest.mark.parametrize(
    "headers",
    [
        {"X-Statewave-Subject": "user/42"},
        {"X-Statewave-Subject": "user:42", "X-Statewave-Session": "sess/abc"},
    ],
)
async def test_bad_id_is_rejected_before_any_upstream_call(calls, proxy, headers):
    response = await proxy.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "statewave_bad_request"
    assert calls == []


async def test_long_message_is_truncated_to_the_server_cap(calls, proxy, trusted):
    await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42"},
        json={"model": "x", "messages": [{"role": "user", "content": "x" * 9000}]},
    )
    await drain()
    assert len(body_of(sent_to(calls, "/v1/context")[0])["task"]) == sw.TASK_MAX


async def test_stream_passes_through_verbatim_and_records_reply(calls, proxy, trusted):
    chunks = []
    async with proxy.stream(
        "POST",
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42"},
        json={"model": "x", "stream": True, "messages": [{"role": "user", "content": "coffee?"}]},
    ) as response:
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
    await drain()

    assert b"".join(chunks).endswith(b"data: [DONE]\n\n")
    payload = body_of(sent_to(calls, "/v1/episodes")[0])["payload"]
    assert payload["messages"][1] == {"role": "assistant", "content": "dark roast"}


async def test_statewave_down_still_answers(calls, proxy, monkeypatch, trusted):
    # Statewave answers 404 for every path -> no context, no episode, still a completion.
    monkeypatch.setattr(sw, "STATEWAVE_URL", "http://localhost:8000/gone")
    response = await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42"},
        json={"model": "x", "messages": [{"role": "user", "content": "coffee?"}]},
    )
    await drain()
    assert response.status_code == 200
    forwarded = body_of(sent_to(calls, "/api/v1/chat/completions")[0])
    assert forwarded["messages"] == [{"role": "user", "content": "coffee?"}]


async def test_shutdown_drains_writes_then_closes_client(calls, proxy, trusted):
    # No drain() here on purpose: shutdown is what has to finish the write,
    # and it has to do it before the client is closed under it.
    await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42"},
        json={"model": "x", "messages": [{"role": "user", "content": "coffee?"}]},
    )
    async with sw._lifespan(sw.app):
        pass
    assert sent_to(calls, "/v1/episodes")
    assert sw.client.is_closed


async def test_other_routes_proxy_to_openrouter(calls, proxy):
    response = await proxy.get("/v1/models")
    assert response.status_code == 200
    assert sent_to(calls, "/api/v1/models")


async def test_health_probe_hits_no_upstream(calls, proxy):
    response = await proxy.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert calls == []


async def test_upstream_rate_limit_headers_survive_the_round_trip(calls, proxy):
    # Non-stream chat and plain pass-through both went through _relay.
    chat = await proxy.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    models = await proxy.get("/v1/models")
    for response in (chat, models):
        assert response.headers["x-ratelimit-remaining"] == "42"
        assert response.headers["x-request-id"] == "or-req-1"


# --- M2: subject is derived from a verified token, not asserted by the caller ---


async def test_a_subject_with_no_trust_mode_is_rejected(calls, proxy):
    response = await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:42"},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "statewave_untrusted_subject"
    assert calls == []


async def test_token_required_once_a_secret_is_set(calls, proxy, monkeypatch):
    monkeypatch.setattr(sw, "JWT_SECRET", SECRET)
    response = await proxy.post(
        "/v1/chat/completions",
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "statewave_auth_required"
    assert calls == []


async def test_a_token_signed_with_the_wrong_secret_is_rejected(calls, proxy, monkeypatch):
    monkeypatch.setattr(sw, "JWT_SECRET", SECRET)
    response = await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Token": token("user:42", secret="a-different-secret-also-32-bytes-x!")},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "statewave_bad_token"
    assert calls == []


async def test_forged_subject_header_loses_to_the_token(calls, proxy, monkeypatch):
    monkeypatch.setattr(sw, "JWT_SECRET", SECRET)
    await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "user:99", "X-Statewave-Token": token("user:42")},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()
    assert body_of(sent_to(calls, "/v1/context")[0])["subject_id"] == "user:42"
    assert body_of(sent_to(calls, "/v1/episodes")[0])["subject_id"] == "user:42"


async def test_trusted_gateway_may_still_override_the_token_subject(calls, proxy, monkeypatch):
    monkeypatch.setattr(sw, "JWT_SECRET", SECRET)
    monkeypatch.setattr(sw, "TRUST_CLIENT_SUBJECT", True)
    await proxy.post(
        "/v1/chat/completions",
        headers={"X-Statewave-Subject": "team:7", "X-Statewave-Token": token("gateway:1")},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()
    assert body_of(sent_to(calls, "/v1/context")[0])["subject_id"] == "team:7"
