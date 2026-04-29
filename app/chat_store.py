import uuid
import json
from datetime import datetime
from app.database import get_db


# ── Chat operations ───────────────────────────────────────────────────────────


def ensure_session(user_id: str, session_id: str):
    """
    Insert session into DB if it doesn't exist.
    Streamlit generates session_id client-side — this makes sure
    it's present in the sessions table before any chat/message FK references it.
    """
    db = get_db()
    try:
        exists = db.execute("SELECT id FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not exists:
            from datetime import datetime, timedelta
            expires_at = (datetime.utcnow() + timedelta(days=7)).isoformat()
            db.execute(
                "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
                (session_id, user_id, expires_at)
            )
            db.commit()
    finally:
        db.close()


def create_chat(user_id: str, session_id: str, mode: str, doc_id: str = None, chat_id: str = None) -> str:
    """
    Create a chat. If chat_id is provided (from Streamlit), use it — but only insert if
    it doesn't already exist in DB. This handles the case where Streamlit generates a UUID
    client-side that hasn't been persisted yet.
    """
    db = get_db()
    try:
        if not chat_id:
            chat_id = str(uuid.uuid4())
        existing = db.execute("SELECT id FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if not existing:
            db.execute(
                "INSERT INTO chats (id, session_id, user_id, doc_id, mode) VALUES (?, ?, ?, ?, ?)",
                (chat_id, session_id, user_id, doc_id, mode)
            )
            db.commit()
        return chat_id
    finally:
        db.close()


def update_chat_title(chat_id: str, title: str):
    db = get_db()
    try:
        db.execute(
            "UPDATE chats SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND title IS NULL",
            (title[:60], chat_id)   # only set on first message
        )
        db.commit()
    finally:
        db.close()


def update_chat_summary(chat_id: str, summary: str):
    db = get_db()
    try:
        db.execute(
            "UPDATE chats SET summary = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (summary, chat_id)
        )
        db.commit()
    finally:
        db.close()


def count_tokens(text: str) -> int:
    """Approximate token count — 1 token ≈ 4 characters (OpenAI/Groq rule of thumb)."""
    return max(1, len(text) // 4)


def save_message(chat_id: str, role: str, content: str, meta: dict = None):
    """
    Save a message with full metadata.
    meta keys: confidence, source_ref, follow_ups, clarifying, not_found, uncertainty
    token_count is computed automatically from content length.
    """
    db = get_db()
    meta = meta or {}
    try:
        db.execute("""
            INSERT INTO messages
                (chat_id, role, content, confidence, source_ref, follow_ups,
                 clarifying, not_found, uncertainty, token_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            chat_id,
            role,
            content,
            meta.get("confidence"),
            meta.get("source_ref"),
            json.dumps(meta.get("follow_ups")) if meta.get("follow_ups") else None,
            json.dumps(meta.get("clarifying")) if meta.get("clarifying") else None,
            int(meta.get("not_found", False)),
            meta.get("uncertainty"),
            count_tokens(content),
        ))
        db.commit()
    finally:
        db.close()


# ── Load all chats for a user on login ───────────────────────────────────────

def load_user_chats(user_id: str, limit: int = 20, offset: int = 0) -> list:
    """
    Load chats for a user with pagination and full message history.
    Called on login to restore chat state in Streamlit.
    limit/offset enable pagination for users with many chats.
    """
    db = get_db()
    try:
        chats = db.execute("""
            SELECT c.id as chat_id, c.mode, c.title, c.summary, c.doc_id,
                   c.created_at, c.updated_at,
                   d.filename, d.doc_type, d.total_pages
            FROM chats c
            LEFT JOIN documents d ON d.id = c.doc_id
            WHERE c.user_id = ?
            ORDER BY c.updated_at DESC
            LIMIT ? OFFSET ?
        """, (user_id, limit, offset)).fetchall()

        result = []
        for chat in chats:
            messages = db.execute("""
                SELECT role, content, confidence, source_ref,
                       follow_ups, clarifying, not_found, uncertainty
                FROM messages
                WHERE chat_id = ?
                ORDER BY created_at ASC
            """, (chat["chat_id"],)).fetchall()

            msg_list = []
            for m in messages:
                msg = {"role": m["role"], "content": m["content"]}
                if m["role"] == "assistant":
                    msg["confidence"]  = m["confidence"]
                    msg["source_ref"]  = m["source_ref"]
                    msg["follow_ups"]  = json.loads(m["follow_ups"])  if m["follow_ups"]  else None
                    msg["clarifying"]  = json.loads(m["clarifying"])  if m["clarifying"]  else None
                    msg["not_found"]   = bool(m["not_found"])
                    msg["uncertainty"] = m["uncertainty"]
                msg_list.append(msg)

            # restore structured summary from JSON if present
            raw_summary = chat["summary"]
            try:
                summary_structured = json.loads(raw_summary) if raw_summary else None
            except Exception:
                summary_structured = None

            result.append({
                "id":                 chat["chat_id"],
                "mode":               chat["mode"],
                "title":              chat["title"],
                "summary":            raw_summary,
                "summary_structured": summary_structured,
                "doc_id":             chat["doc_id"],
                "filename":           chat["filename"],
                "doc_type":           chat["doc_type"],
                "total_pages":        chat["total_pages"],
                "messages":           msg_list,
            })

        return result
    finally:
        db.close()


# ── Document persistence ──────────────────────────────────────────────────────

def save_document(user_id: str, doc_id: str, filename: str, doc_type: str,
                  total_pages: int, total_chunks: int, chunks: list):
    db = get_db()
    try:
        db.execute("""
            INSERT INTO documents (id, user_id, filename, doc_type, total_pages, total_chunks, chunks_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (doc_id, user_id, filename, doc_type, total_pages, total_chunks, json.dumps(chunks)))
        db.commit()
    finally:
        db.close()


def load_document_chunks(doc_id: str) -> list:
    db = get_db()
    try:
        row = db.execute("SELECT chunks_json FROM documents WHERE id = ?", (doc_id,)).fetchone()
        return json.loads(row["chunks_json"]) if row else []
    finally:
        db.close()


def load_user_documents(user_id: str) -> list:
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, filename, doc_type, total_pages, total_chunks FROM documents WHERE user_id = ? ORDER BY uploaded_at DESC",
            (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        db.close()

def delete_chat(chat_id: str, user_id: str) -> bool:
    """
    Delete a chat and all its messages.
    user_id check ensures a user can only delete their own chats.
    Returns True if deleted, False if not found or unauthorized.
    """
    db = get_db()
    try:
        chat = db.execute(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()

        if not chat:
            return False

        # messages are deleted automatically via ON DELETE CASCADE
        db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        db.commit()
        return True
    finally:
        db.close()

def delete_chat(chat_id: str, user_id: str) -> bool:
    """
    Delete a chat and all its messages.
    Verifies ownership before deleting — user can only delete their own chats.
    Returns True if deleted, False if not found or not owned by user.
    """
    db = get_db()
    try:
        row = db.execute(
            "SELECT id FROM chats WHERE id = ? AND user_id = ?",
            (chat_id, user_id)
        ).fetchone()

        if not row:
            return False

        # messages are deleted automatically via ON DELETE CASCADE
        db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        db.commit()
        return True
    finally:
        db.close()


def get_chat_detail(chat_id: str, user_id: str) -> dict:
    """Get full chat detail including all messages and token usage."""
    db = get_db()
    try:
        chat = db.execute("""
            SELECT c.id, c.mode, c.title, c.summary, c.doc_id,
                   c.created_at, c.updated_at,
                   d.filename, d.doc_type, d.total_pages
            FROM chats c
            LEFT JOIN documents d ON d.id = c.doc_id
            WHERE c.id = ? AND c.user_id = ?
        """, (chat_id, user_id)).fetchone()

        if not chat:
            return None

        messages = db.execute("""
            SELECT role, content, confidence, source_ref,
                   follow_ups, clarifying, not_found, uncertainty,
                   token_count, created_at
            FROM messages
            WHERE chat_id = ?
            ORDER BY created_at ASC
        """, (chat_id,)).fetchall()

        total_tokens = db.execute(
            "SELECT COALESCE(SUM(token_count), 0) as total FROM messages WHERE chat_id = ?",
            (chat_id,)
        ).fetchone()["total"]

        msg_list = []
        for m in messages:
            msg = {
                "role":        m["role"],
                "content":     m["content"],
                "token_count": m["token_count"],
                "created_at":  m["created_at"],
            }
            if m["role"] == "assistant":
                msg["confidence"]  = m["confidence"]
                msg["source_ref"]  = m["source_ref"]
                msg["follow_ups"]  = json.loads(m["follow_ups"])  if m["follow_ups"]  else None
                msg["clarifying"]  = json.loads(m["clarifying"])  if m["clarifying"]  else None
                msg["not_found"]   = bool(m["not_found"])
                msg["uncertainty"] = m["uncertainty"]
            msg_list.append(msg)

        return {
            "id":           chat["id"],
            "mode":         chat["mode"],
            "title":        chat["title"],
            "summary":      chat["summary"],
            "doc_id":       chat["doc_id"],
            "filename":     chat["filename"],
            "doc_type":     chat["doc_type"],
            "total_pages":  chat["total_pages"],
            "created_at":   chat["created_at"],
            "updated_at":   chat["updated_at"],
            "total_tokens": total_tokens,
            "messages":     msg_list,
        }
    finally:
        db.close()