"""db.py - tiny SQLite helper. SQLite is built into Python, so nothing to install."""
import os
import sqlite3

# The database file sits next to this script (created automatically on first run).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "travel.db")


def get_conn():
    """Open a new connection. We open one per call because Flask handles requests on different threads."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row  # lets us read columns by name: row["name"]
    return conn


def init_db():
    """Create the tables if they don't exist yet (hotels, bookings, users, conversations, messages)."""
    with get_conn() as conn:
        # hotels: cache of hotels per city (real names from OpenStreetMap, or mock fallback)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS hotels (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                city    TEXT NOT NULL,      -- lowercase city key, e.g. "goa"
                name    TEXT NOT NULL,
                stars   INTEGER NOT NULL,   -- 0 means unknown
                lat     REAL,
                lon     REAL,
                website TEXT,
                address TEXT,
                source  TEXT NOT NULL       -- "osm" (real) or "mock"
            )"""
        )
        # bookings: every demo booking the chatbot makes
        conn.execute(
            """CREATE TABLE IF NOT EXISTS bookings (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ref         TEXT NOT NULL UNIQUE,
                hotel_id    INTEGER NOT NULL,
                guest_name  TEXT NOT NULL,
                check_in    TEXT NOT NULL,  -- YYYY-MM-DD
                check_out   TEXT NOT NULL,
                guests      INTEGER NOT NULL,
                total_price INTEGER NOT NULL,
                created_at  TEXT NOT NULL
            )"""
        )

        # users: people who signed up on the website
        conn.execute(
            """CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                email         TEXT NOT NULL UNIQUE,  -- always stored lowercase, so A@x.com and a@x.com are one account
                password_hash TEXT NOT NULL,         -- a salted hash, NEVER the real password
                created_at    TEXT NOT NULL
            )"""
        )
        # Link each booking to the user who made it. A database created before accounts existed
        # has no user_id column yet, so add it (old bookings keep user_id = NULL and belong to nobody).
        columns = [row["name"] for row in conn.execute("PRAGMA table_info(bookings)")]
        if "user_id" not in columns:
            conn.execute("ALTER TABLE bookings ADD COLUMN user_id INTEGER REFERENCES users(id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bookings_user ON bookings(user_id)")

        # conversations + messages: each logged-in user's saved chats with the assistant
        conn.execute(
            """CREATE TABLE IF NOT EXISTS conversations (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                title      TEXT NOT NULL,        -- first thing the user asked, shortened
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL         -- last message time: newest chat is shown first
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                conversation_id INTEGER NOT NULL REFERENCES conversations(id),
                role            TEXT NOT NULL,   -- "user" or "bot"
                content         TEXT NOT NULL,
                created_at      TEXT NOT NULL
            )"""
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id)")
