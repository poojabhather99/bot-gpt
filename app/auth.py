import uuid
import bcrypt
from datetime import datetime, timedelta
from fastapi import HTTPException, Header
from app.database import get_db


SESSION_EXPIRE_DAYS = 7


# ── Password helpers ──────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


# ── User operations ───────────────────────────────────────────────────────────

def create_user(email: str, password: str, name: str) -> dict:
    db = get_db()
    try:
        existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="Email already registered.")

        user_id = str(uuid.uuid4())
        db.execute(
            "INSERT INTO users (id, email, password_hash, name) VALUES (?, ?, ?, ?)",
            (user_id, email, hash_password(password), name)
        )
        db.commit()
        return {"id": user_id, "email": email, "name": name}
    finally:
        db.close()


def login_user(email: str, password: str) -> dict:
    db = get_db()
    try:
        user = db.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        if not user or not verify_password(password, user["password_hash"]):
            raise HTTPException(status_code=401, detail="Invalid email or password.")

        # create new session
        session_id = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(days=SESSION_EXPIRE_DAYS)
        db.execute(
            "INSERT INTO sessions (id, user_id, expires_at) VALUES (?, ?, ?)",
            (session_id, user["id"], expires_at)
        )
        # update last login
        db.execute("UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?", (user["id"],))
        db.commit()

        return {
            "session_id": session_id,
            "user_id":    user["id"],
            "name":       user["name"],
            "email":      user["email"],
        }
    finally:
        db.close()


def get_current_user(session_id: str = Header(..., alias="X-Session-ID")) -> dict:
    """
    Dependency — validates session_id from request header.
    Inject into any route that requires auth:
        user = Depends(get_current_user)
    """
    if not session_id:
        raise HTTPException(status_code=401, detail="Missing session ID.")

    db = get_db()
    try:
        row = db.execute("""
            SELECT s.id as session_id, s.user_id, s.expires_at,
                   u.email, u.name
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = ?
        """, (session_id,)).fetchone()

        if not row:
            raise HTTPException(status_code=401, detail="Invalid session.")

        if datetime.fromisoformat(row["expires_at"]) < datetime.utcnow():
            raise HTTPException(status_code=401, detail="Session expired. Please log in again.")

        return {
            "user_id":    row["user_id"],
            "session_id": row["session_id"],
            "email":      row["email"],
            "name":       row["name"],
        }
    finally:
        db.close()