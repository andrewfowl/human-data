"""`Idempotency-Key` support for mutating requests (Stripe-style semantics).

A client that sends `Idempotency-Key: <opaque string>` on POST/PUT/PATCH/DELETE
gets at-most-once execution for that key: a retry of the same request replays
the stored response (marked `Idempotent-Replayed: true`) instead of running the
handler again.

- Keys are scoped to the caller's credential, so two tenants can never collide
  on, or read, each other's stored responses.
- Reusing a key with a different method, path, query or body is a client bug
  and returns 422 rather than silently replaying the wrong response.
- A retry that arrives while the first attempt is still running gets 409 with
  `Retry-After`. An attempt that has held the key longer than
  `HDF_IDEMPOTENCY_LOCK_SECONDS` is presumed crashed and may be taken over.
- Only deterministic outcomes are stored: 2xx and 4xx, except auth, conflict
  and throttling responses (401/403/408/409/425/429). 5xx and exceptions
  release the key so the client can retry.
- Records expire after `HDF_IDEMPOTENCY_TTL_HOURS` and are purged by the cron
  sweep.

Limitation: the stored response commits in its own transaction after the
handler's. A process crash in that window leaves the key in progress; after the
lock timeout a retry re-executes. Handlers that must be exactly-once under
crashes should also dedupe on a natural key (as the Stripe webhook inbox does).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import db as database
from .config import settings
from .models import IdempotencyRecord

HEADER = "idempotency-key"
MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
EXEMPT_PATHS = {"/billing/stripe/webhook", "/healthz"}  # webhook dedupes on event id
UNSTORED_STATUSES = {401, 403, 408, 409, 425, 429}
MAX_KEY_LENGTH = 255

IN_PROGRESS = "in_progress"
COMPLETED = "completed"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)  # sqlite returns naive


def _sha256(*parts: bytes) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(hashlib.sha256(p).digest())  # length-safe framing
    return h.hexdigest()


def principal_for(headers: Headers, client_host: str | None) -> str:
    authorization = headers.get("authorization")
    if authorization:
        return "cred:" + hashlib.sha256(authorization.encode()).hexdigest()
    actor = headers.get("x-actor-id")
    if settings.auth_disabled:
        return f"dev:{actor or 'dev-admin'}"
    return f"anon:{client_host or 'unknown'}"


def is_storable(status: int) -> bool:
    return status < 500 and status not in UNSTORED_STATUSES


def _valid_key(key: str) -> bool:
    return 0 < len(key) <= MAX_KEY_LENGTH and all(0x20 <= ord(c) < 0x7F for c in key)


def _begin(record_id: str, method: str, path: str, request_hash: str):
    """Claims the key. Returns ("proceed"|"replay"|"in_progress"|"mismatch", record|None)."""
    now = _now()
    ttl = timedelta(hours=settings.idempotency_ttl_hours)
    lock = timedelta(seconds=settings.idempotency_lock_seconds)
    with database.session() as s:
        rec = s.get(IdempotencyRecord, record_id)
        if rec is not None and now - _aware(rec.created_at) > ttl:
            s.delete(rec)
            s.commit()
            rec = None
        if rec is None:
            s.add(IdempotencyRecord(id=record_id, method=method, path=path,
                                    request_hash=request_hash, state=IN_PROGRESS,
                                    created_at=now))
            try:
                s.commit()
            except IntegrityError:  # a concurrent attempt claimed it first
                s.rollback()
                return "in_progress", None
            return "proceed", None
        if rec.request_hash != request_hash:
            return "mismatch", None
        if rec.state == COMPLETED:
            s.expunge(rec)
            return "replay", rec
        if now - _aware(rec.created_at) < lock:
            return "in_progress", None
        # Stale claim: the earlier attempt presumably crashed. Exactly one retry wins.
        taken = s.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id,
                   IdempotencyRecord.state == IN_PROGRESS,
                   IdempotencyRecord.created_at == rec.created_at)
            .values(created_at=now)
        ).rowcount
        s.commit()
        return ("proceed", None) if taken == 1 else ("in_progress", None)


def _complete(record_id: str, status: int, content_type: str | None, body: bytes) -> None:
    with database.session() as s:
        s.execute(
            update(IdempotencyRecord)
            .where(IdempotencyRecord.id == record_id)
            .values(state=COMPLETED, status_code=status, content_type=content_type,
                    response_body=body.decode("utf-8", errors="replace"))
        )
        s.commit()


def _release(record_id: str) -> None:
    with database.session() as s:
        s.execute(delete(IdempotencyRecord).where(IdempotencyRecord.id == record_id,
                                                  IdempotencyRecord.state == IN_PROGRESS))
        s.commit()


def purge_expired(session_factory=database.session) -> int:
    cutoff = _now() - timedelta(hours=settings.idempotency_ttl_hours)
    with session_factory() as s:
        n = s.execute(delete(IdempotencyRecord)
                      .where(IdempotencyRecord.created_at < cutoff)).rowcount
        s.commit()
        return n or 0


async def _send_json(send: Send, status: int, body: bytes, extra: list | None = None) -> None:
    headers = [(b"content-type", b"application/json"),
               (b"content-length", str(len(body)).encode())] + (extra or [])
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["method"] not in MUTATING_METHODS
                or scope["path"] in EXEMPT_PATHS):
            return await self.app(scope, receive, send)
        headers = Headers(scope=scope)
        key = headers.get(HEADER)
        if key is None:
            return await self.app(scope, receive, send)
        if not _valid_key(key):
            return await _send_json(
                send, 400,
                b'{"detail":"Idempotency-Key must be 1-255 printable ASCII characters"}')

        chunks: list[bytes] = []
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunks.append(message.get("body", b""))
            more = message.get("more_body", False)
        body = b"".join(chunks)

        client_host = scope["client"][0] if scope.get("client") else None
        record_id = _sha256(principal_for(headers, client_host).encode(), key.encode())
        request_hash = _sha256(scope["method"].encode(), scope["path"].encode(),
                               scope.get("query_string", b""), body)

        outcome, rec = await run_in_threadpool(_begin, record_id, scope["method"],
                                               scope["path"], request_hash)
        if outcome == "replay":
            stored = (rec.response_body or "").encode()
            await send({"type": "http.response.start", "status": rec.status_code,
                        "headers": [(b"content-type",
                                     (rec.content_type or "application/json").encode()),
                                    (b"content-length", str(len(stored)).encode()),
                                    (b"idempotent-replayed", b"true")]})
            await send({"type": "http.response.body", "body": stored})
            return
        if outcome == "mismatch":
            return await _send_json(
                send, 422,
                b'{"detail":"Idempotency-Key was already used with a different request"}')
        if outcome == "in_progress":
            return await _send_json(
                send, 409,
                b'{"detail":"a request with this Idempotency-Key is still in progress"}',
                [(b"retry-after", b"1")])

        replayed = False

        async def replay_receive() -> Message:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        start: Message | None = None
        out: list[bytes] = []

        async def capture(message: Message) -> None:
            nonlocal start
            if message["type"] == "http.response.start":
                start = message
            elif message["type"] == "http.response.body":
                out.append(message.get("body", b""))

        try:
            await self.app(scope, replay_receive, capture)
        except BaseException:
            await run_in_threadpool(_release, record_id)
            raise

        assert start is not None
        status = start["status"]
        response_body = b"".join(out)
        if is_storable(status):
            content_type = Headers(raw=start.get("headers", [])).get("content-type")
            await run_in_threadpool(_complete, record_id, status, content_type, response_body)
        else:
            await run_in_threadpool(_release, record_id)
        await send(start)
        await send({"type": "http.response.body", "body": response_body})
