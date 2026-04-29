from dotenv import load_dotenv
load_dotenv()


from fastapi import FastAPI, Depends
from pydantic import BaseModel

from app.database import init_db
from app.auth import create_user, login_user, get_current_user
from app.chat_store import load_user_chats, load_user_documents, delete_chat, get_chat_detail

from general_question import router as general_router
from rag_question import router as rag_router

# init DB on startup
init_db()

app = FastAPI(title="Bot-GPT Assistant API")

app.include_router(general_router)
app.include_router(rag_router)


# ── Auth models ───────────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    email:    str
    password: str
    name:     str

class LoginRequest(BaseModel):
    email:    str
    password: str


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.post("/auth/register", tags=["auth"])
def register(req: RegisterRequest):
    """Register a new user."""
    user = create_user(req.email, req.password, req.name)
    return {"message": "Account created successfully.", "user": user}


@app.post("/auth/login", tags=["auth"])
def login(req: LoginRequest):
    """Login and get a session_id to use in all subsequent requests."""
    return login_user(req.email, req.password)


@app.get("/auth/me", tags=["auth"])
def me(user: dict = Depends(get_current_user)):
    """Get current logged-in user info."""
    return {"user_id": user["user_id"], "email": user["email"], "name": user["name"]}


# ── On-login data load ────────────────────────────────────────────────────────

@app.get("/conversations/{chat_id}", tags=["user"])
def get_conversation(chat_id: str, user: dict = Depends(get_current_user)):
    """Get full conversation history by ID including token usage per message."""
    detail = get_chat_detail(chat_id, user["user_id"])
    if not detail:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return detail


@app.delete("/conversations/{chat_id}", tags=["user"])
def delete_conversation(chat_id: str, user: dict = Depends(get_current_user)):
    """
    Delete a conversation and all its messages.
    Only the owner can delete their own conversation.
    """
    deleted = delete_chat(chat_id, user["user_id"])
    if not deleted:
        raise HTTPException(status_code=404, detail="Conversation not found or not yours.")
    return {"message": "Conversation deleted successfully.", "chat_id": chat_id}


@app.get("/user/chats", tags=["user"])
def get_user_chats(
    user: dict = Depends(get_current_user),
    limit: int = 20,
    offset: int = 0
):
    """
    Load all chats for the logged-in user with pagination.
    limit: number of chats per page (default 20)
    offset: number of chats to skip (default 0)
    """
    return {"chats": load_user_chats(user["user_id"], limit=limit, offset=offset)}


@app.get("/user/documents", tags=["user"])
def get_user_documents(user: dict = Depends(get_current_user)):
    """List all documents uploaded by this user."""
    return {"documents": load_user_documents(user["user_id"])}


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["health"])
def health():
    return {"status": "ok"}