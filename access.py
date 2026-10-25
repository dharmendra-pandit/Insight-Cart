# access.py
"""
Access control: who may use the app, and the daily credit limit.

Users sign in with Google through Streamlit's built-in st.login(), configured
in the [auth] section of .streamlit/secrets.toml. Only verified emails on the
allow list ([access] allowed_emails) get in. Credits used are kept in a small
SQLite file (config.ACCESS_DB), keyed by email, so they survive page reloads
and are shared by every tab a user has open.
"""

import sqlite3
import importlib.util
from contextlib import contextmanager
from datetime import datetime

import streamlit as st

import config

AUTH_KEYS = ("redirect_uri", "cookie_secret", "client_id", "client_secret", "server_metadata_url")


# ── Sign-in ───────────────────────────────────────────────────
def _secrets_section(name: str) -> dict:
    try:
        return dict(st.secrets.get(name) or {})
    except Exception:  # no secrets.toml at all
        return {}


def sign_in_problem() -> str | None:
    """Why Google sign-in cannot work yet, or None when it is set up."""
    missing = [k for k in AUTH_KEYS if not str(_secrets_section("auth").get(k) or "").strip()]
    if missing:
        return (
            f"Sign-in is not set up yet: fill in {', '.join(missing)} in the [auth] "
            "section of .streamlit/secrets.toml. See \"Google sign-in\" in README.md."
        )
    if importlib.util.find_spec("authlib") is None:
        return "Sign-in needs the Authlib package. Run: pip install -r requirements.txt"
    return None


def is_allowed(email: str) -> bool:
    """
    Whether an email is on the allow list: listed exactly, or covered by an
    "@domain.com" entry for everyone at that domain.
    """
    email = email.strip().lower()
    if "@" not in email:
        return False
    entries = _secrets_section("access").get("allowed_emails") or []
    allowed = {str(e).strip().lower() for e in entries}
    return email in allowed or "@" + email.rpartition("@")[2] in allowed


# ── Storage ───────────────────────────────────────────────────
@contextmanager
def _db():
    """One transaction: committed on success, rolled back on error."""
    db = sqlite3.connect(config.ACCESS_DB, timeout=10)
    try:
        with db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS usage ("
                "username TEXT, day TEXT, used INTEGER NOT NULL, PRIMARY KEY (username, day))"
            )
            yield db
    finally:
        db.close()


# ── Daily credits ─────────────────────────────────────────────
def _today() -> str:
    return datetime.now(config.LIMIT_TIMEZONE).date().isoformat()


def remaining(username: str) -> int:
    """Credits the user has left today."""
    with _db() as db:
        row = db.execute(
            "SELECT used FROM usage WHERE username = ? AND day = ?", (username, _today())
        ).fetchone()
    return max(config.DAILY_LIMIT_PER_USER - (row[0] if row else 0), 0)


def reserve(username: str) -> str | None:
    """
    Take one of today's credits before an analysis starts, so two tabs cannot
    both use the last one. Returns the day it was taken from (pass it to
    refund()), or None if the daily limit is reached.
    """
    day = _today()
    with _db() as db:
        db.execute("INSERT OR IGNORE INTO usage (username, day, used) VALUES (?, ?, 0)", (username, day))
        cursor = db.execute(
            "UPDATE usage SET used = used + 1 WHERE username = ? AND day = ? AND used < ?",
            (username, day, config.DAILY_LIMIT_PER_USER),
        )
    return day if cursor.rowcount else None


def refund(username: str, day: str) -> None:
    """Give back a credit taken by reserve() for an analysis that failed."""
    with _db() as db:
        db.execute(
            "UPDATE usage SET used = used - 1 WHERE username = ? AND day = ? AND used > 0",
            (username, day),
        )
