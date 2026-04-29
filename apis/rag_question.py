from fastapi import APIRouter, FastAPI, Depends, UploadFile, File, HTTPException
from pydantic import BaseModel
from groq import Groq
from typing import Optional
import pdfplumber
import uuid
import json
import io
import re
from app.auth import get_current_user
from app.chat_store import create_chat, save_message, save_document, update_chat_title, update_chat_summary, ensure_session

from dotenv import load_dotenv
import os
load_dotenv()

router = APIRouter(tags=["document"])
client = Groq(api_key=os.getenv("GROQ_API_KEY"))

MAX_HISTORY_MESSAGES = 10
sessions: dict = {}
documents: dict = {}


# ════════════════════════════════════════════════════════════════════
# LAYOUT DETECTION
# Before parsing, detect if a page is single or two-column.
# Two-column pages (common in resumes and research papers) must be
# cropped into left/right halves before text extraction — otherwise
# pdfplumber reads across columns, mixing unrelated sections.
# ════════════════════════════════════════════════════════════════════

def is_two_column(page) -> bool:
    """
    Check x-positions of all words on the page.
    If significant content exists on both left and right halves,
    treat as two-column.
    """
    words = page.extract_words()
    if not words:
        return False
    mid = page.width / 2
    left_words  = [w for w in words if float(w["x0"]) < mid - 20]
    right_words = [w for w in words if float(w["x0"]) > mid + 20]
    return len(left_words) > 10 and len(right_words) > 10


def extract_page_text(page) -> str:
    """
    Extract clean text from a page.
    Auto-detects two-column layout and handles it by cropping
    left and right halves separately, then joining top-to-bottom.
    Cleans font-encoding artifacts (control characters).
    """
    if is_two_column(page):
        mid    = page.width / 2
        height = page.height
        left   = page.crop((0,   0, mid,         height)).extract_text() or ""
        right  = page.crop((mid, 0, page.width,  height)).extract_text() or ""
        text   = left.strip() + "\n\n" + right.strip()
    else:
        text = page.extract_text() or ""

    # remove control characters from broken font encodings
    text = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]", " ", text)
    # collapse multiple spaces but preserve newlines
    text = re.sub(r"[ \t]+", " ", text)
    # collapse 3+ newlines into 2
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ════════════════════════════════════════════════════════════════════
# DOCUMENT TYPE DETECTION
# Classify the document so we can apply the right parsing and
# chunking strategy automatically.
#
# Resume:  Has EXPERIENCE, EDUCATION, SKILLS headers near the top
# Research: Has Abstract, Introduction, References/Bibliography
# Report:  Everything else — treated as general flowing text
# ════════════════════════════════════════════════════════════════════

RESUME_SIGNALS    = ["experience", "education", "skills", "summary", "objective", "certifications"]
RESEARCH_SIGNALS  = ["abstract", "introduction", "methodology", "conclusion", "references", "bibliography", "arxiv", "doi"]

def detect_doc_type(pages: list[dict]) -> str:
    # sample first 2 pages for signals
    sample = " ".join(p["text"][:1000].lower() for p in pages[:2])
    resume_hits   = sum(1 for s in RESUME_SIGNALS   if s in sample)
    research_hits = sum(1 for s in RESEARCH_SIGNALS if s in sample)

    if resume_hits >= 3:
        return "resume"
    if research_hits >= 3:
        return "research"
    return "report"


# ════════════════════════════════════════════════════════════════════
# PARSING — extract raw pages from PDF
# ════════════════════════════════════════════════════════════════════

def parse_pdf(file_bytes: bytes) -> list[dict]:
    """
    Extract text from all pages.
    Returns list of { page, text } dicts.
    Raises if no text found (scanned PDF).
    """
    pages = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for i, page in enumerate(pdf.pages):
            text = extract_page_text(page)
            if text:
                pages.append({"page": i + 1, "text": text})

    if not pages:
        raise HTTPException(
            status_code=422,
            detail="No text could be extracted. This may be a scanned PDF — only text-based PDFs are supported."
        )
    return pages


# ════════════════════════════════════════════════════════════════════
# CHUNKING STRATEGIES
#
# Resume — chunk_by_section:
#   Splits on all-caps section headers (EXPERIENCE, SKILLS etc).
#   Each chunk = one complete section.
#   Why: resume questions always target a specific section.
#   Mixing sections causes hallucination.
#
# Research — chunk_by_section with academic headers:
#   Same idea but with academic section names.
#   References section is kept as one chunk — never split mid-reference.
#
# Report — chunk_by_paragraph:
#   Split on blank lines (paragraph boundaries).
#   Why: reports have no fixed section structure, but paragraph
#   breaks are the natural semantic boundary.
#   Fallback to sentence grouping if paragraphs are very long.
# ════════════════════════════════════════════════════════════════════

RESUME_HEADERS = re.compile(
    r"^(SUMMARY|OBJECTIVE|EXPERIENCE|WORK EXPERIENCE|EDUCATION|SKILLS|"
    r"TECHNICAL SKILLS|PROJECTS|CERTIFICATIONS|ACHIEVEMENTS|KEY ACHIEVEMENTS|"
    r"AWARDS|PUBLICATIONS|VOLUNTEER|LANGUAGES|INTERESTS|CONTACT|PROFILE|"
    r"[A-Z][A-Z\s&]{3,})$",
    re.MULTILINE
)

RESEARCH_HEADERS = re.compile(
    r"^(ABSTRACT|INTRODUCTION|BACKGROUND|RELATED WORK|METHODOLOGY|METHODS|"
    r"EXPERIMENTS|RESULTS|DISCUSSION|CONCLUSION|CONCLUSIONS|FUTURE WORK|"
    r"ACKNOWLEDGEMENTS?|REFERENCES|BIBLIOGRAPHY|\d+\.?\s+[A-Z][A-Z\s]{2,})$",
    re.MULTILINE
)


def chunk_by_section(pages: list[dict], header_pattern: re.Pattern) -> list[dict]:
    """
    Split text at section header boundaries.
    Each chunk carries the section name as metadata.
    If no headers detected, falls back to paragraph chunking.
    """
    full_text = "\n".join(p["text"] for p in pages)
    lines = full_text.split("\n")

    chunks = []
    current_section = "HEADER"
    current_lines = []

    for line in lines:
        stripped = line.strip()
        if header_pattern.match(stripped) and len(stripped) > 2:
            # flush previous section
            body = "\n".join(current_lines).strip()
            if len(body) > 40:
                chunks.append({
                    "section": current_section,
                    "text": f"[{current_section}]\n{body}"
                })
            current_section = stripped
            current_lines = []
        else:
            current_lines.append(line)

    # flush last section
    body = "\n".join(current_lines).strip()
    if len(body) > 40:
        chunks.append({
            "section": current_section,
            "text": f"[{current_section}]\n{body}"
        })

    if len(chunks) < 2:
        # no headers found — fallback to paragraph
        return chunk_by_paragraph(pages)

    return chunks


def chunk_by_paragraph(pages: list[dict]) -> list[dict]:
    """
    Split on blank lines (paragraph boundaries).
    Merges very short paragraphs (<100 chars) with the next one
    to avoid tiny meaningless chunks.
    """
    chunks = []
    for page in pages:
        paragraphs = [p.strip() for p in re.split(r"\n{2,}", page["text"]) if p.strip()]
        buffer = ""
        for para in paragraphs:
            buffer = (buffer + "\n\n" + para).strip() if buffer else para
            if len(buffer) >= 150:
                chunks.append({"section": "paragraph", "text": buffer})
                buffer = ""
        if buffer:
            chunks.append({"section": "paragraph", "text": buffer})
    return chunks


def apply_chunking(pages: list[dict], doc_type: str) -> list[dict]:
    if doc_type == "resume":
        return chunk_by_section(pages, RESUME_HEADERS)
    if doc_type == "research":
        return chunk_by_section(pages, RESEARCH_HEADERS)
    return chunk_by_paragraph(pages)  # report


# ════════════════════════════════════════════════════════════════════
# RETRIEVAL
# Two-pass scoring:
#
# Pass 1 — Section match (score +10 per hit):
#   Map query intent words to known section names.
#   "skills" → SKILLS section, "experience" → EXPERIENCE section.
#   This ensures the right section is always prioritised.
#
# Pass 2 — Keyword match (score +1 per word):
#   Count how many query words appear in the chunk text.
#   Used as tiebreaker and for queries without section intent.
#
# Fallback:
#   If nothing scores > 0, return all chunks so the model
#   has the full document to work with.
# ════════════════════════════════════════════════════════════════════

SECTION_KEYWORD_MAP = {
    # resume
    "skill": "SKILLS", "skills": "SKILLS", "technical": "SKILLS",
    "technology": "SKILLS", "tools": "SKILLS", "stack": "SKILLS",
    "experience": "EXPERIENCE", "work": "EXPERIENCE", "job": "EXPERIENCE",
    "company": "EXPERIENCE", "companies": "EXPERIENCE", "role": "EXPERIENCE", "worked": "EXPERIENCE",
    "education": "EDUCATION", "degree": "EDUCATION", "college": "EDUCATION",
    "university": "EDUCATION", "study": "EDUCATION", "studied": "EDUCATION",
    "project": "PROJECTS", "projects": "PROJECTS", "built": "PROJECTS",
    "certification": "CERTIFICATIONS", "certified": "CERTIFICATIONS",
    "achievement": "KEY ACHIEVEMENTS", "award": "KEY ACHIEVEMENTS",
    "summary": "SUMMARY", "about": "SUMMARY", "who": "SUMMARY", "profile": "SUMMARY",
    "contact": "HEADER", "email": "HEADER", "phone": "HEADER", "linkedin": "HEADER",
    # research
    "abstract": "ABSTRACT", "overview": "ABSTRACT",
    "method": "METHODOLOGY", "methodology": "METHODOLOGY", "approach": "METHODOLOGY",
    "result": "RESULTS", "results": "RESULTS", "finding": "RESULTS",
    "conclusion": "CONCLUSION", "conclusions": "CONCLUSION",
    "reference": "REFERENCES", "references": "REFERENCES", "citation": "REFERENCES",
}


def find_relevant_chunks(query: str, chunks: list[dict], top_k: int = 5) -> list[dict]:
    query_words = [w.strip("?.,!").lower() for w in query.split() if w.strip()]
    target_sections = {SECTION_KEYWORD_MAP[w] for w in query_words if w in SECTION_KEYWORD_MAP}

    scored = []
    for chunk in chunks:
        text_lower  = chunk["text"].lower()
        section     = chunk.get("section", "").upper()

        section_score = 10 if section in target_sections else 0
        keyword_score = sum(1 for w in query_words if w in text_lower)
        scored.append((section_score + keyword_score, chunk))

    scored.sort(key=lambda x: x[0], reverse=True)

    if all(s == 0 for s, _ in scored):
        return chunks   # nothing matched — return full document

    return [chunk for _, chunk in scored[:top_k]]


def build_context(chunks: list[dict]) -> str:
    if not chunks:
        return "No relevant content found."
    return "\n\n".join(
        f"[{c.get('section', 'Section')}]\n{c['text']}" for c in chunks
    )


# ════════════════════════════════════════════════════════════════════
# HISTORY
# ════════════════════════════════════════════════════════════════════

def get_chat(session_id: str, chat_id: str) -> dict:
    sessions.setdefault(session_id, {})
    sessions[session_id].setdefault(chat_id, {"history": [], "summary": None})
    return sessions[session_id][chat_id]


def summarize_history(history: list) -> str:
    text = "\n".join(f"{m['role'].upper()}: {m['content']}" for m in history)
    res = client.chat.completions.create(
        model="llama-3.3-70b-versatile", temperature=0.3, max_tokens=512,
        messages=[{"role": "user", "content": f"Summarize this conversation in 3-5 sentences:\n\n{text}"}]
    )
    return res.choices[0].message.content


def update_history(session_id: str, chat_id: str, user_msg: str, assistant_msg: str):
    chat = get_chat(session_id, chat_id)
    chat["history"].append({"role": "user",      "content": user_msg})
    chat["history"].append({"role": "assistant",  "content": assistant_msg})
    if len(chat["history"]) > MAX_HISTORY_MESSAGES:
        mid = len(chat["history"]) // 2
        summary = summarize_history(chat["history"][:mid])
        chat["summary"] = f"{chat['summary']}\n{summary}" if chat["summary"] else summary
        chat["history"] = chat["history"][mid:]


# ════════════════════════════════════════════════════════════════════
# JSON PARSING
# ════════════════════════════════════════════════════════════════════

def safe_parse_json(raw: str, route: str) -> dict:
    if not raw or not raw.strip():
        raise HTTPException(status_code=502, detail=f"[{route}] Groq returned empty response.")
    cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        print(f"[{route}] Non-JSON response, wrapping plain text. Raw: {raw[:300]}")
        return {
            "answer": cleaned, "confidence": "medium",
            "clarifying_questions": None, "follow_up_questions": None,
            "uncertainty_note": None, "source_reference": None, "not_found": False,
        }


# ════════════════════════════════════════════════════════════════════
# PROMPTS
# ════════════════════════════════════════════════════════════════════

RAG_SYSTEM_PROMPT = """You are a strict document Q&A assistant.

RULES — FOLLOW EXACTLY:
1. Answer ONLY from the DOCUMENT CONTEXT provided below. No outside knowledge whatsoever.
2. If the answer is not in the context, set not_found to true and say: "I could not find this in the document."
3. NEVER invent names, phone numbers, emails, companies, dates, or any facts not in the context.
4. Reference the section name where you found the answer.
5. After answering, suggest 2-3 follow-up questions based only on the document content.

Respond ONLY in this exact JSON — no extra text:
{
  "answer": "answer here",
  "confidence": "high | medium | low",
  "source_reference": "section name where found",
  "clarifying_questions": ["q1"] or null,
  "follow_up_questions": ["q1", "q2"] or null,
  "not_found": false
}
"""

GENERAL_SYSTEM_PROMPT = """You are a precise, honest Q&A assistant.

RULES:
1. NEVER fabricate facts, statistics, dates, names, or sources.
2. If unsure, clearly say so and suggest where to verify.
3. If the question is vague, ask 1-2 clarifying questions.
4. Keep answers concise and structured.
5. Suggest 2-3 follow-up questions after answering.

Respond ONLY in this exact JSON:
{
  "answer": "answer here",
  "confidence": "high | medium | low",
  "clarifying_questions": ["q1"] or null,
  "follow_up_questions": ["q1", "q2"] or null,
  "uncertainty_note": "note if unsure" or null
}
"""


# ════════════════════════════════════════════════════════════════════
# MODELS
# ════════════════════════════════════════════════════════════════════

class GeneralAskRequest(BaseModel):
    session_id: str
    chat_id: str
    question: str

class GeneralAskResponse(BaseModel):
    session_id: str
    chat_id: str
    answer: str
    confidence: str
    clarifying_questions: Optional[list[str]] = None
    follow_up_questions:   Optional[list[str]] = None
    uncertainty_note:      Optional[str]       = None

class RagAskRequest(BaseModel):
    session_id: str
    chat_id: str
    doc_id: str
    question: str

class RagAskResponse(BaseModel):
    session_id: str
    chat_id: str
    doc_id: str
    answer: str
    confidence: str
    source_reference:     Optional[str]       = None
    clarifying_questions: Optional[list[str]] = None
    follow_up_questions:  Optional[list[str]] = None
    not_found: bool = False


# ════════════════════════════════════════════════════════════════════
# ROUTES
# ════════════════════════════════════════════════════════════════════



@router.post("/document/upload")
async def upload_document(file: UploadFile = File(...), user: dict = Depends(get_current_user)):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    file_bytes = await file.read()
    pages      = parse_pdf(file_bytes)
    doc_type   = detect_doc_type(pages)
    chunks     = apply_chunking(pages, doc_type)
    doc_id     = str(uuid.uuid4())

    # persist to DB
    save_document(user["user_id"], doc_id, file.filename, doc_type, len(pages), len(chunks), chunks)

    # also keep in memory for this session
    documents[doc_id] = {
        "chunks":       chunks,
        "filename":     file.filename,
        "total_pages":  len(pages),
        "total_chunks": len(chunks),
        "doc_type":     doc_type,
    }

    return {
        "doc_id":       doc_id,
        "filename":     file.filename,
        "total_pages":  len(pages),
        "total_chunks": len(chunks),
        "doc_type":     doc_type,
        "sections":     list({c.get("section","") for c in chunks}),
        "message":      "PDF processed. Use doc_id in /document/ask."
    }


@router.post("/document/ask", response_model=RagAskResponse)
def document_ask(request: RagAskRequest, user: dict = Depends(get_current_user)):
    # load chunks from memory or DB
    if request.doc_id not in documents:
        from app.chat_store import load_document_chunks
        chunks = load_document_chunks(request.doc_id)
        if not chunks:
            raise HTTPException(status_code=404, detail="Document not found. Upload via /document/upload first.")
        documents[request.doc_id] = {"chunks": chunks}

    # ensure session exists in DB
    ensure_session(user["user_id"], request.session_id)

    # ensure chat exists in DB (Streamlit may send a UUID not yet persisted)
    chat_id = create_chat(user["user_id"], request.session_id, "rag", request.doc_id, chat_id=request.chat_id)

    update_chat_title(chat_id, request.question)
    save_message(chat_id, "user", request.question)

    doc             = documents[request.doc_id]
    relevant_chunks = find_relevant_chunks(request.question, doc["chunks"])
    context         = build_context(relevant_chunks)

    print(f"\n[/document/ask] Query: {request.question}")
    print(f"[/document/ask] Matched sections: {[c.get('section') for c in relevant_chunks]}")

    chat = get_chat(request.session_id, chat_id)
    system = RAG_SYSTEM_PROMPT + f"\n\nDOCUMENT CONTEXT:\n{context}"
    messages = [{"role": "system", "content": system}]
    if chat["summary"]:
        messages.append({"role": "system", "content": f"Earlier conversation summary:\n{chat['summary']}"})
    messages.extend(chat["history"])
    messages.append({"role": "user", "content": request.question})

    completion = client.chat.completions.create(
        model="llama-3.3-70b-versatile", temperature=0.1, max_tokens=2048,
        messages=messages, response_format={"type": "json_object"},
    )
    raw  = completion.choices[0].message.content or ""
    data = safe_parse_json(raw, "/document/ask")
    update_history(request.session_id, chat_id, request.question, data["answer"])

    save_message(chat_id, "assistant", data["answer"], {
        "confidence":  data.get("confidence"),
        "source_ref":  data.get("source_reference"),
        "follow_ups":  data.get("follow_up_questions"),
        "clarifying":  data.get("clarifying_questions"),
        "not_found":   data.get("not_found", False),
    })

    chat_data = get_chat(request.session_id, chat_id)
    if chat_data.get("summary"):
        update_chat_summary(chat_id, chat_data["summary"])

    return RagAskResponse(
        session_id=request.session_id, chat_id=chat_id, doc_id=request.doc_id, **data
    )


@router.get("/document/list")
def list_documents():
    return {
        doc_id: {
            "filename":     v["filename"],
            "total_pages":  v["total_pages"],
            "total_chunks": v["total_chunks"],
            "doc_type":     v["doc_type"],
        }
        for doc_id, v in documents.items()
    }


@router.get("/document/debug/{doc_id}")
def debug_document(doc_id: str):
    if doc_id not in documents:
        raise HTTPException(status_code=404, detail="Document not found.")
    doc = documents[doc_id]
    return {
        "filename":     doc["filename"],
        "doc_type":     doc["doc_type"],
        "total_chunks": doc["total_chunks"],
        "chunks": [{"section": c.get("section"), "text_preview": c["text"][:300]} for c in doc["chunks"]]
    }