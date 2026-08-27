"""OpenAI-compatible proxy that puts Statewave memory in front of OpenRouter.

Point any OpenAI/OpenRouter client at this proxy and pass a subject
(``X-Statewave-Subject`` header, or ``statewave_subject`` in the body):

  * before forwarding, a Statewave context bundle for that subject is
    prepended to ``messages`` as a system message;
  * after the reply, the turn is written back as an episode.

No subject on the request means plain pass-through, memory untouched.
Everything else under ``/`` is proxied verbatim, so this is drop-in.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager

import httpx
import jwt
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

log = logging.getLogger("statewave_openrouter")

OPENROUTER_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
STATEWAVE_URL = os.getenv("STATEWAVE_URL", "http://localhost:8000").rstrip("/")
STATEWAVE_KEY = os.getenv("STATEWAVE_API_KEY", "")
# HS256 secret for verifying inbound `X-Statewave-Token` JWTs. Set it and every
# chat call needs a valid token (closes the open-port credit drain), and the
# subject comes from the token `sub` - the client can no longer assert it.
JWT_SECRET = os.getenv("PROXY_JWT_SECRET", "")
# Opt back into trusting a caller-supplied subject: single-tenant deploys with
# no token, or a gateway that authenticates itself but manages many subjects.
TRUST_CLIENT_SUBJECT = os.getenv("STATEWAVE_TRUST_CLIENT_SUBJECT", "").lower() in ("1", "true", "yes")
CONTEXT_TOKENS = int(os.getenv("STATEWAVE_CONTEXT_TOKENS", "1500"))
COMPILE_AFTER_TURN = os.getenv("STATEWAVE_COMPILE_AFTER_TURN", "").lower() in ("1", "true", "yes")
EPISODE_SOURCE = os.getenv("STATEWAVE_EPISODE_SOURCE", "openrouter-proxy")
# Statewave policy rules match on the caller; an absent caller_type is the
# least-privileged one, so always send both.
# `or` not a getenv default: an empty value in a .env file must not become an
# empty caller_id, which reads as "no caller identity" server-side.
CALLER_TYPE = os.getenv("STATEWAVE_CALLER_TYPE") or "openrouter-gateway"
CALLER_ID = os.getenv("STATEWAVE_CALLER_ID") or CALLER_TYPE
TIMEOUT = httpx.Timeout(float(os.getenv("PROXY_TIMEOUT", "120")), connect=10.0)

# Statewave subject/session charset: letters, digits, _ . - : - no "/" or
# whitespace. Both ids share one constraint server-side.
ID_RE = re.compile(r"^[A-Za-z0-9_.\-:]{1,256}$")
# Server-side cap on the retrieval query; over it is a 422, not a truncation.
TASK_MAX = 4000

client = httpx.AsyncClient(timeout=TIMEOUT)
_background: set[asyncio.Task] = set()

# Headers we must not copy from the upstream response: httpx has already
# decoded the body, and Starlette recomputes length/framing itself.
_DROP_RESPONSE_HEADERS = {"content-length", "content-encoding", "transfer-encoding", "connection"}


def _relay(upstream: httpx.Response) -> Response:
    """Rebuild an upstream response, keeping its status and headers.

    Without this both handlers dropped everything but content-type, so every
    ``x-ratelimit-*`` value and the OpenRouter request id vanished at the proxy.
    """
    headers = {k: v for k, v in upstream.headers.items() if k.lower() not in _DROP_RESPONSE_HEADERS}
    return Response(upstream.content, status_code=upstream.status_code, headers=headers)


def _error(status: int, message: str, error_type: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"message": message, "type": error_type}})


@asynccontextmanager
async def _lifespan(_: FastAPI):
    yield
    # Drain before closing: in-flight episode writes are the only place a
    # turn exists before Statewave has it.
    if _background:
        await asyncio.gather(*_background, return_exceptions=True)
    await client.aclose()


app = FastAPI(title="statewave-openrouter", lifespan=_lifespan)


def _spawn(coro) -> None:
    """Run a best-effort side task without blocking the response."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


def _statewave_headers(request: Request) -> dict[str, str]:
    headers = {}
    if STATEWAVE_KEY:
        headers["X-API-Key"] = STATEWAVE_KEY
    tenant = request.headers.get("x-tenant-id") or os.getenv("STATEWAVE_TENANT_ID", "")
    if tenant:
        headers["X-Tenant-ID"] = tenant
    return headers


def _upstream_headers(request: Request) -> dict[str, str]:
    auth = request.headers.get("authorization")
    if not auth and OPENROUTER_KEY:
        auth = f"Bearer {OPENROUTER_KEY}"
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["Authorization"] = auth
    # OpenRouter attribution headers, from the caller or from config.
    for header, env in (("HTTP-Referer", "OPENROUTER_SITE_URL"), ("X-Title", "OPENROUTER_SITE_NAME")):
        value = request.headers.get(header.lower()) or os.getenv(env, "")
        if value:
            headers[header] = value
    return headers


def _last_user_text(body: dict) -> str:
    for message in reversed(body.get("messages") or []):
        if message.get("role") == "user":
            content = message.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):  # multimodal parts
                return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return ""


def _sse_reply(raw: str) -> str:
    """Reconstruct the assistant message from a streamed SSE body."""
    parts = []
    for line in raw.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            parts.append((choice.get("delta") or {}).get("content") or "")
    return "".join(parts)


def _json_reply(payload: dict) -> str:
    for choice in payload.get("choices") or []:
        content = (choice.get("message") or {}).get("content")
        if isinstance(content, str):
            return content
    return ""


async def _fetch_context(subject: str, task: str, session_id: str | None, headers: dict) -> str:
    body = {
        "subject_id": subject,
        "task": (task or "chat")[:TASK_MAX],
        "max_tokens": CONTEXT_TOKENS,
        "caller_id": CALLER_ID,
        "caller_type": CALLER_TYPE,
    }
    if session_id:
        body["session_id"] = session_id
    try:
        response = await client.post(f"{STATEWAVE_URL}/v1/context", json=body, headers=headers)
        response.raise_for_status()
        return response.json().get("assembled_context") or ""
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 401:
            # Not a transient failure: either the API key is wrong, or the tenant
            # requires caller identity and is rejecting ours.
            log.error(
                "statewave rejected the gateway for %s (401) - check STATEWAVE_API_KEY "
                "and STATEWAVE_CALLER_ID/STATEWAVE_CALLER_TYPE",
                subject,
            )
        else:
            log.warning("statewave context fetch failed for %s: %s", subject, exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - memory is an enhancement, never a hard dependency
        log.warning("statewave context fetch failed for %s: %s", subject, exc)
        return ""


async def _write_turn(subject, session_id, user_text, reply, model, headers) -> None:
    episode = {
        "subject_id": subject,
        "source": EPISODE_SOURCE,
        "type": "chat_turn",
        # The compiler reads `messages`; any other shape ingests fine and
        # compiles to an empty string.
        "payload": {
            "messages": [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": reply},
            ],
            "model": model,
        },
    }
    if session_id:
        episode["session_id"] = session_id
    try:
        response = await client.post(f"{STATEWAVE_URL}/v1/episodes", json=episode, headers=headers)
        response.raise_for_status()
        if COMPILE_AFTER_TURN:
            await client.post(
                f"{STATEWAVE_URL}/v1/memories/compile",
                json={"subject_id": subject, "async": True},
                headers=headers,
            )
    except Exception as exc:  # noqa: BLE001 - a failed write must not break the chat
        log.warning("statewave episode write failed for %s: %s", subject, exc)


def _resolve_subject(request: Request, body: dict) -> tuple[str | None, str | None, JSONResponse | None]:
    """Decide which subject this request is allowed to touch.

    Validate any supplied ids, then apply the trust rule: with `JWT_SECRET` set
    the subject comes from a verified token; without it a client-supplied
    subject is honoured only when `TRUST_CLIENT_SUBJECT` is on. Returns
    ``(subject, session_id, error)`` - `error` is a response to return as-is.
    """
    hdr_subject = request.headers.get("x-statewave-subject") or body.pop("statewave_subject", None)
    session_id = request.headers.get("x-statewave-session") or body.pop("statewave_session", None)
    for field, value in (("subject", hdr_subject), ("session", session_id)):
        if value and not ID_RE.fullmatch(value):
            return None, None, _error(
                400,
                f"statewave {field} must be 1-256 chars of letters, digits, "
                "underscore, dot, dash or colon",
                "statewave_bad_request",
            )

    if JWT_SECRET:
        token = request.headers.get("x-statewave-token", "").strip()
        if token[:7].lower() == "bearer ":
            token = token[7:].strip()
        if not token:
            return None, None, _error(
                401, "statewave requires a token in X-Statewave-Token", "statewave_auth_required"
            )
        try:
            claims = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
        except jwt.InvalidTokenError as exc:
            return None, None, _error(401, f"invalid statewave token: {exc}", "statewave_bad_token")
        subject = hdr_subject if (TRUST_CLIENT_SUBJECT and hdr_subject) else claims.get("sub")
        if subject and not ID_RE.fullmatch(subject):
            return None, None, _error(
                400, "statewave token 'sub' is not a valid subject id", "statewave_bad_request"
            )
        return subject, session_id, None

    if hdr_subject and not TRUST_CLIENT_SUBJECT:
        return None, None, _error(
            400,
            "statewave subject supplied but not trusted: set PROXY_JWT_SECRET to derive it "
            "from a token, or STATEWAVE_TRUST_CLIENT_SUBJECT=1 to trust the header",
            "statewave_untrusted_subject",
        )
    return hdr_subject, session_id, None


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()
    subject, session_id, error = _resolve_subject(request, body)
    if error:
        return error
    sw_headers = _statewave_headers(request)
    user_text = _last_user_text(body)

    if subject:
        context = await _fetch_context(subject, user_text, session_id, sw_headers)
        if context:
            messages = body.get("messages") or []
            body["messages"] = [{"role": "system", "content": context}, *messages]

    url = f"{OPENROUTER_URL}/chat/completions"
    headers = _upstream_headers(request)
    model = body.get("model", "")

    if not body.get("stream"):
        upstream = await client.post(url, json=body, headers=headers)
        if subject and upstream.status_code < 400:
            reply = _json_reply(upstream.json())
            _spawn(_write_turn(subject, session_id, user_text, reply, model, sw_headers))
        return _relay(upstream)

    async def relay():
        # ponytail: buffers the full SSE body to rebuild the reply; parse
        # incrementally if replies ever get large enough to matter.
        buffer = bytearray()
        async with client.stream("POST", url, json=body, headers=headers) as upstream:
            async for chunk in upstream.aiter_raw():
                buffer += chunk
                yield chunk
        if subject:
            reply = _sse_reply(bytes(buffer).decode("utf-8", "ignore"))
            if reply:
                _spawn(_write_turn(subject, session_id, user_text, reply, model, sw_headers))

    return StreamingResponse(relay(), media_type="text/event-stream")


@app.get("/health")
async def health():
    """Liveness probe. Never touches OpenRouter, so k8s/ALB checks stay free."""
    return {"status": "ok"}


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def passthrough(path: str, request: Request):
    """Everything else (models, credits, generation) goes straight to OpenRouter."""
    upstream = await client.request(
        request.method,
        f"{OPENROUTER_URL}/{path.removeprefix('v1/')}",
        content=await request.body(),
        headers=_upstream_headers(request),
        params=request.query_params,
    )
    return _relay(upstream)
