"""Authentication and role-based access control.

Production mode (default): every request carries `Authorization: Bearer hdf_...`.
Keys are random 256-bit secrets shown once at issuance; only their SHA-256 is
stored. Each key maps to a User with a role and optional firm/expert linkage.

Dev mode (`HDF_AUTH_DISABLED=1`): the actor is taken from the `X-Actor-Id`
header with admin privileges — for local development and the test suite only.

Cold start: `POST /bootstrap` creates the first admin user + key, and only
works while the users table is empty.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .models import ApiKey, Role, User

KEY_PREFIX = "hdf_"


@dataclass
class Actor:
    id: str
    name: str
    role: str
    firm_id: str | None = None
    expert_id: str | None = None

    @property
    def is_internal(self) -> bool:
        return self.role in (Role.ADMIN.value, Role.OPS.value)


def generate_key() -> tuple[str, str, str]:
    """Returns (plaintext_key, prefix, sha256_hash). Plaintext is never stored."""
    secret = secrets.token_urlsafe(32)
    key = f"{KEY_PREFIX}{secret}"
    return key, key[:8], hash_key(key)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def issue_key(db: Session, user: User) -> str:
    key, prefix, digest = generate_key()
    db.add(ApiKey(user_id=user.id, prefix=prefix, key_hash=digest))
    db.flush()
    return key


def _resolve(db: Session, authorization: str | None, x_actor_id: str | None) -> Actor:
    if settings.auth_disabled:
        return Actor(id=x_actor_id or "dev-admin", name=x_actor_id or "dev-admin",
                     role=Role.ADMIN.value)
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer API key")
    token = authorization.removeprefix("Bearer ").strip()
    row = db.execute(
        select(ApiKey, User)
        .join(User, ApiKey.user_id == User.id)
        .where(ApiKey.key_hash == hash_key(token), ApiKey.active.is_(True),
               User.active.is_(True))
    ).first()
    if row is None:
        raise HTTPException(401, "invalid or revoked API key")
    api_key, user = row
    api_key.last_used_at = datetime.now(timezone.utc)
    return Actor(id=user.id, name=user.name, role=user.role,
                 firm_id=user.firm_id, expert_id=user.expert_id)


def make_actor_dependency(get_db):
    def current_actor(db: Session = Depends(get_db),
                      authorization: str | None = Header(None),
                      x_actor_id: str | None = Header(None)) -> Actor:
        return _resolve(db, authorization, x_actor_id)
    return current_actor


def require_roles(current_actor, *roles: str):
    allowed = set(roles)

    def dependency(actor: Actor = Depends(current_actor)) -> Actor:
        if actor.role not in allowed:
            raise HTTPException(403, f"role '{actor.role}' is not permitted for this action")
        return actor
    return dependency


def assert_firm_access(actor: Actor, firm_id: str) -> None:
    """Clients may only touch their own firm; internal roles see everything."""
    if actor.is_internal:
        return
    if actor.role == Role.CLIENT.value and actor.firm_id == firm_id:
        return
    raise HTTPException(403, "not permitted for this firm")
