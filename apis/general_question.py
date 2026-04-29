from dotenv import load_dotenv
import os
load_dotenv()

from fastapi import APIRouter, FastAPI, Depends
from pydantic import BaseModel
from groq import Groq
from typing import Optional
import json
from app.auth import get_current_user
from app.chat_store import create_chat, save_message, update_chat_title, update_chat_summary, ensure_session

router = APIRouter(prefix="/general", tags=["general"])

client = Groq(api_key=os.getenv("GROQ_API_KEY"))

SYSTEM_PROMPT = """You are a precise, honest Q&A assistant.

CORE RULES:
1. NEVER fabricate facts, statistics, dates, names, or sources.
2. If you are unsure or lack knowledge about something, clearly say so.
3. If the question is vague or ambiguous, DO NOT guess. Instead, ask 1-2 clarifying questions.
4. If the question has multiple interpretations, list them and ask which one the user means.
5. Keep answers concise and structured. Use bullet points for lists, steps, or comparisons.
6. After your answer, suggest 2-3 relevant follow-up questions the user might want to explore.

IMPORTANT: Always respond in this exact JSON format and nothing else:
{
  "answer": "your answer here",
  "confidence": "high | medium | low",
  "clarifying_questions": ["question 1", "question 2"] or null,
  "follow_up_questions": ["question 1", "question 2", "question 3"] or null,
  "uncertainty_note": "mention if unsure" or null
}
"""

MAX_HISTORY_MESSAGES = 10

# { session_id: { chat_id: { "history": [...], "summary": "..." } } }
sessions: dict = {}


class QuestionRequest(BaseModel):
    session_id: str
    chat_id: str
    question: str

class QuestionResponse(BaseModel):
    session_id: str
    chat_id: str
    answer: str
    confidence: str
    clarifying_questions: Optional[list[str]] = None
    follow_up_questions: Optional[list[str]] = None
    uncertainty_note: Optional[str] = None


def get_chat(session_id: str, chat_id: str) -> dict:
    sessions.setdefault(session_id, {})
    sessions[session_id].setdefault(chat_id, {"history": [], "summary": None})
    return sessions[session_id][chat_id]


def summarize_history(history: list) -> str:
    history_text = "\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in history
    )
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0.3,
        max_tokens=512,
        messages=[{
            "role": "user",
            "content": (
                "Summarize the following conversation in 3-5 sentences, "
                "keeping all key facts, decisions, and context needed to "
                "continue the conversation:\n\n"
                f"{history_text}"
            ),
        }],
    )
    return response.choices[0].message.content


def get_messages(session_id: str, chat_id: str) -> list:
    chat = get_chat(session_id, chat_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if chat["summary"]:
        messages.append({
            "role": "system",
            "content": f"Summary of earlier conversation:\n{chat['summary']}"
        })
    messages.extend(chat["history"])
    return messages


def update_history(session_id: str, chat_id: str, user_msg: str, assistant_msg: str):
    chat = get_chat(session_id, chat_id)
    chat["history"].append({"role": "user", "content": user_msg})
    chat["history"].append({"role": "assistant", "content": assistant_msg})

    if len(chat["history"]) > MAX_HISTORY_MESSAGES:
        mid = len(chat["history"]) // 2
        older = chat["history"][:mid]
        recent = chat["history"][mid:]
        new_summary = summarize_history(older)
        if chat["summary"]:
            new_summary = f"{chat['summary']}\n{new_summary}"
        chat["summary"] = new_summary
        chat["history"] = recent


@router.post("/ask", response_model=QuestionResponse)
def ask_groq(request: QuestionRequest, user: dict = Depends(get_current_user)):
    # ensure session exists in DB
    ensure_session(user["user_id"], request.session_id)

    # ensure chat exists in DB (Streamlit may send a UUID not yet persisted)
    chat_id = create_chat(user["user_id"], request.session_id, "general", chat_id=request.chat_id)

    # set chat title from first question
    update_chat_title(chat_id, request.question)

    # save user message to DB
    save_message(chat_id, "user", request.question)

    messages = get_messages(request.session_id, chat_id)
    messages.append({"role": "user", "content": request.question})

    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        temperature=0.3,
        max_tokens=1024,
        messages=messages,
        response_format={"type": "json_object"},
    )

    raw  = completion.choices[0].message.content or ""
    cleaned = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    try:
        data = json.loads(cleaned)
    except Exception:
        data = {"answer": cleaned, "confidence": "medium", "clarifying_questions": None, "follow_up_questions": None, "uncertainty_note": None}

    # save assistant message to DB
    save_message(chat_id, "assistant", data["answer"], {
        "confidence":  data.get("confidence"),
        "follow_ups":  data.get("follow_up_questions"),
        "clarifying":  data.get("clarifying_questions"),
        "uncertainty": data.get("uncertainty_note"),
    })

    # update in-memory history
    update_history(request.session_id, chat_id, request.question, data["answer"])

    # persist summary if it was updated
    chat_data = get_chat(request.session_id, chat_id)
    if chat_data.get("summary"):
        update_chat_summary(chat_id, chat_data["summary"])

    return QuestionResponse(
        session_id=request.session_id,
        chat_id=chat_id,
        **data
    )


# debug endpoint — check what's stored
@router.get("/history/{session_id}/{chat_id}")
def get_history(session_id: str, chat_id: str):
    chat = sessions.get(session_id, {}).get(chat_id, {})
    return chat