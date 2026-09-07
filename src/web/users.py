"""Database-backed web users with roles and PBKDF2 password hashing.

Roles:
  - admin:  full access (generate, regenerate, manage users and billing).
  - viewer: read-only (view reports, download PDFs).
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets

from .. import db

log = logging.getLogger(__name__)

ROLES = ("admin", "viewer")
_PBKDF2_ROUNDS = 240_000


# --------------------------------------------------------------------------- #
# Password hashing (stdlib only; no external dependency)
# --------------------------------------------------------------------------- #
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), _PBKDF2_ROUNDS
    ).hex()
    return f"pbkdf2_sha256${_PBKDF2_ROUNDS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$", 3)
    except ValueError:
        return False
    if algo != "pbkdf2_sha256":
        return False
    computed = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt), int(rounds)
    ).hex()
    return hmac.compare_digest(computed, digest)


def normalize_role(role: str | None) -> str:
    role = (role or "").strip().lower()
    return role if role in ROLES else "viewer"


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def list_users(cfg: dict | None = None) -> list[dict]:
    try:
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT username, role, created_at FROM app_users ORDER BY username"
            )
            rows = cur.fetchall()
    except db.DatabaseNotConfigured:
        return []
    except Exception:  # noqa: BLE001
        log.exception("users: list failed")
        return []
    return [{"username": u, "role": r, "created_at": c} for u, r, c in rows]


def get_user(cfg: dict | None, username: str) -> dict | None:
    try:
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT username, password_hash, role FROM app_users WHERE username=%s",
                (username,),
            )
            row = cur.fetchone()
    except db.DatabaseNotConfigured:
        return None
    except Exception:  # noqa: BLE001
        log.exception("users: get failed")
        return None
    if not row:
        return None
    return {"username": row[0], "password_hash": row[1], "role": row[2]}


def count_users(cfg: dict | None = None) -> int:
    try:
        with db.connect(cfg) as conn, conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM app_users")
            return int(cur.fetchone()[0])
    except db.DatabaseNotConfigured:
        return 0
    except Exception:  # noqa: BLE001
        log.exception("users: count failed")
        return 0


def add_user(cfg: dict | None, username: str, password: str, role: str) -> None:
    username = (username or "").strip()
    if not username or not password:
        raise ValueError("Username and password are required")
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app_users (username, password_hash, role)
            VALUES (%s,%s,%s)
            ON CONFLICT (username) DO UPDATE SET
                password_hash=EXCLUDED.password_hash, role=EXCLUDED.role
            """,
            (username, hash_password(password), normalize_role(role)),
        )
        conn.commit()
    log.info("users: upserted %s (role=%s)", username, normalize_role(role))


def delete_user(cfg: dict | None, username: str) -> None:
    with db.connect(cfg) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM app_users WHERE username=%s", ((username or "").strip(),))
        conn.commit()
    log.info("users: deleted %s", username)


def bootstrap_admin() -> tuple[str, str]:
    """Env-configured fallback admin, usable when app_users is empty."""
    return (
        os.environ.get("BASIC_AUTH_USER", "admin"),
        os.environ.get("BASIC_AUTH_PASSWORD", ""),
    )
