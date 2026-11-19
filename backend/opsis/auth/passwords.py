"""Password hashing (argon2id) and the password rules."""

from __future__ import annotations

import contextlib
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from opsis.errors import ApiError

MIN_LENGTH = 12
MAX_LENGTH = 200

# argon2id with the library's defaults (RFC 9106, low-memory profile).
_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify(password_hash: str | None, password: str) -> bool:
    """True when `password` matches. Takes the same time whether or not there is
    a hash, so the response time does not reveal which emails have accounts."""
    if not password_hash:
        _verify_dummy(password)
        return False
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return _hasher.hash("opsis-equal-time-for-unknown-accounts")


def _verify_dummy(password: str) -> None:
    with contextlib.suppress(VerificationError, InvalidHashError):
        _hasher.verify(_dummy_hash(), password)


def check_new(password: str, *, email: str = "", path: str = "/password") -> None:
    """Refuse passwords that are too short, too long, or the email itself."""
    problem = None
    if len(password) < MIN_LENGTH:
        problem = f"Use at least {MIN_LENGTH} characters."
    elif len(password) > MAX_LENGTH:
        problem = f"Use at most {MAX_LENGTH} characters."
    elif email and password.strip().lower() == email.strip().lower():
        problem = "The password cannot be your email address."
    elif len(set(password)) < 4:
        problem = "The password is too simple: use more different characters."
    if problem:
        raise ApiError(
            422,
            "weak_password",
            f"Choose another password. {problem}",
            [{"path": path, "message": problem}],
        )
