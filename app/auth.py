"""Local accounts, opaque sessions, CSRF, and login throttling.

* Passwords are scrypt-hashed with a per-user salt. An unknown email still
  pays for one hash so response time does not reveal which emails exist.
* A session token is random and opaque. The server stores only its SHA-256,
  so a leaked session table cannot be replayed. Sessions expire on idle and
  on an absolute limit, whichever comes first.
* State-changing requests need the session's CSRF token in a header. A
  cross-site form can make the browser send the cookie, not the header.
* Invites are one-time, hashed, and expiring. Revoking a user ends every
  session they hold immediately.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass, field

from app.authz import ROLE_CAPABILITIES, ROLE_RESOURCES, Capability, Resource, Role

MIN_PASSWORD_LENGTH = 12
INVITE_SECONDS = 72 * 3600
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


class AuthError(Exception):
    """Deliberately vague: the caller learns only that it did not work."""


def hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(candidate.hex(), digest_hex)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class User:
    email: str
    role: Role
    password_hash: str | None = None
    active: bool = True

    @property
    def resources(self) -> frozenset[Resource]:
        return ROLE_RESOURCES[self.role] if self.active else frozenset()

    @property
    def capabilities(self) -> frozenset[Capability]:
        return ROLE_CAPABILITIES[self.role] if self.active else frozenset()


@dataclass
class Session:
    email: str
    csrf_token: str
    created_at: float
    last_seen: float


@dataclass
class _Invite:
    email: str
    expires_at: float


@dataclass
class _Throttle:
    failures: int = 0
    locked_until: float = 0.0


@dataclass
class AuthService:
    idle_seconds: int
    absolute_seconds: int
    max_failures: int
    lockout_seconds: int
    users: dict[str, User] = field(default_factory=dict)
    _sessions: dict[str, Session] = field(default_factory=dict)
    _invites: dict[str, _Invite] = field(default_factory=dict)
    _throttle: dict[str, _Throttle] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # --- accounts -------------------------------------------------------

    def create_user(self, email: str, role: Role, password: str) -> User:
        user = User(email=email.lower(), role=role, password_hash=hash_password(password))
        self.users[user.email] = user
        return user

    def invite(self, email: str, role: Role, *, now: float | None = None) -> str:
        email = email.lower()
        if email in self.users and self.users[email].password_hash:
            raise AuthError("user already exists")
        token = secrets.token_urlsafe(32)
        with self._lock:
            self.users[email] = User(email=email, role=role)
            self._invites[_digest(token)] = _Invite(email, _now(now) + INVITE_SECONDS)
        return token

    def activate(self, token: str, password: str, *, now: float | None = None) -> User:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise AuthError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        with self._lock:
            invite = self._invites.pop(_digest(token), None)
        if invite is None or invite.expires_at < _now(now):
            raise AuthError("invalid or expired invite")
        user = self.users[invite.email]
        user.password_hash = hash_password(password)
        return user

    def revoke(self, email: str) -> int:
        email = email.lower()
        with self._lock:
            user = self.users.get(email)
            if user is None:
                raise AuthError("unknown user")
            user.active = False
            doomed = [key for key, s in self._sessions.items() if s.email == email]
            for key in doomed:
                del self._sessions[key]
        return len(doomed)

    # --- sessions -------------------------------------------------------

    def login(self, email: str, password: str, *, now: float | None = None) -> tuple[str, Session]:
        email = email.lower()
        moment = _now(now)
        throttle = self._throttle.setdefault(email, _Throttle())
        if throttle.locked_until > moment:
            raise AuthError("invalid credentials")

        user = self.users.get(email)
        ok = verify_password(password, user.password_hash if user and user.password_hash else _DUMMY_HASH)
        if not (ok and user and user.active and user.password_hash):
            throttle.failures += 1
            if throttle.failures >= self.max_failures:
                throttle.locked_until = moment + self.lockout_seconds
                throttle.failures = 0
            raise AuthError("invalid credentials")

        throttle.failures = 0
        token = secrets.token_urlsafe(32)
        session = Session(email, secrets.token_urlsafe(24), moment, moment)
        with self._lock:
            self._sessions[_digest(token)] = session
        return token, session

    def resolve(self, token: str | None, *, now: float | None = None) -> tuple[User, Session]:
        if not token:
            raise AuthError("not signed in")
        moment = _now(now)
        key = _digest(token)
        with self._lock:
            session = self._sessions.get(key)
            if session is None:
                raise AuthError("not signed in")
            expired = (
                moment - session.last_seen > self.idle_seconds
                or moment - session.created_at > self.absolute_seconds
            )
            user = self.users.get(session.email)
            if expired or user is None or not user.active:
                del self._sessions[key]
                raise AuthError("session expired")
            session.last_seen = moment
        return user, session

    def logout(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._sessions.pop(_digest(token), None)

    def session_count(self) -> int:
        return len(self._sessions)


def csrf_ok(session: Session, header_value: str | None) -> bool:
    return bool(header_value) and hmac.compare_digest(session.csrf_token, header_value or "")


def _now(value: float | None) -> float:
    return time.time() if value is None else value
