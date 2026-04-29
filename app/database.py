import sqlite3
import os


def get_db_path():
    """Resolve DB path at call time so .env is always loaded first."""
    return os.getenv("DB_PATH", "qa_assistant.db")


def get_db():
    """Get a database connection with row factory for dict-like access."""
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    path = get_db_path()
    print(f"[DB] Initializing database at: {path}")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    cursor = conn.cursor()

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id           TEXT PRIMARY KEY,
            email        TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name         TEXT NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login   TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS sessions (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at   TIMESTAMP NOT NULL
        );

        CREATE TABLE IF NOT EXISTS documents (
            id           TEXT PRIMARY KEY,
            user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            filename     TEXT NOT NULL,
            doc_type     TEXT NOT NULL,
            total_pages  INTEGER NOT NULL,
            total_chunks INTEGER NOT NULL,
            chunks_json  TEXT NOT NULL,
            uploaded_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS chats (
            id           TEXT PRIMARY KEY,
            session_id   TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
            user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            doc_id       TEXT REFERENCES documents(id) ON DELETE SET NULL,
            mode         TEXT NOT NULL DEFAULT 'general',
            title        TEXT,
            summary      TEXT,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS messages (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id      TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            role         TEXT NOT NULL,
            content      TEXT NOT NULL,
            confidence   TEXT,
            source_ref   TEXT,
            follow_ups   TEXT,
            clarifying   TEXT,
            not_found    INTEGER DEFAULT 0,
            uncertainty  TEXT,
            token_count  INTEGER DEFAULT 0,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_sessions_user   ON sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_chats_user      ON chats(user_id);
        CREATE INDEX IF NOT EXISTS idx_chats_session   ON chats(session_id);
        CREATE INDEX IF NOT EXISTS idx_messages_chat   ON messages(chat_id);
        CREATE INDEX IF NOT EXISTS idx_documents_user  ON documents(user_id);
    """)

    conn.commit()
    conn.close()