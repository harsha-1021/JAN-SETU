"""
Creates a policymaker login account.

This is a one-time setup script, not an API endpoint on purpose — the
platform shouldn't let just anyone sign themselves up as a policymaker.
Run it once per person who needs access.

Standalone on purpose: does NOT import main.py, so you don't need
SARVAM_API_KEY set just to create a login account.

Usage:
    python create_policymaker.py
"""

import getpass
import hashlib
import secrets
import sqlite3
from datetime import datetime, timezone

DB_PATH = "complaints.db"


def hash_password(password: str, salt: bytes = None) -> tuple:
    if salt is None:
        salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return digest.hex(), salt.hex()


def ensure_users_table() -> None:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            password_salt TEXT NOT NULL,
            created_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()


def main():
    ensure_users_table()

    username = input("Choose a username: ").strip()
    if not username:
        print("Username cannot be empty.")
        return

    password = getpass.getpass("Choose a password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match. Try again.")
        return
    if len(password) < 8:
        print("Use at least 8 characters.")
        return

    password_hash, password_salt = hash_password(password)

    conn = sqlite3.connect(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, password_salt, created_at) VALUES (?, ?, ?, ?)",
            (username, password_hash, password_salt, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print(f"\nAccount created for '{username}'. You can now log in at /dashboard/login.html")
    except sqlite3.IntegrityError:
        print(f"\nA user named '{username}' already exists. Choose a different username.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
