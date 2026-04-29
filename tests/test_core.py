import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from app.auth import hash_password, verify_password
from app.utils import safe_parse_json
from app.chunker import chunk_by_section, chunk_by_paragraph, RESUME_HEADERS


# ── Auth tests ────────────────────────────────────────────────────────────────

def test_hash_and_verify_password():
    hashed = hash_password("mysecretpassword")
    assert verify_password("mysecretpassword", hashed) is True

def test_wrong_password_fails():
    hashed = hash_password("correctpassword")
    assert verify_password("wrongpassword", hashed) is False


# ── JSON parsing tests ────────────────────────────────────────────────────────

def test_safe_parse_json_valid():
    from fastapi import HTTPException
    raw = '{"answer": "hello", "confidence": "high"}'
    result = safe_parse_json(raw, "/test")
    assert result["answer"] == "hello"
    assert result["confidence"] == "high"

def test_safe_parse_json_with_fences():
    from fastapi import HTTPException
    raw = '```json\n{"answer": "hello"}\n```'
    result = safe_parse_json(raw, "/test")
    assert result["answer"] == "hello"

def test_safe_parse_json_plain_text_fallback():
    raw = "This is a plain text response"
    result = safe_parse_json(raw, "/test")
    assert result["answer"] == "This is a plain text response"
    assert result["confidence"] == "medium"

def test_safe_parse_json_empty_raises():
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        safe_parse_json("", "/test")
    assert exc.value.status_code == 502


# ── Chunking tests ────────────────────────────────────────────────────────────

def test_chunk_by_section_resume():
    pages = [{"page": 1, "text": "John Doe\nSoftware Engineer\n\nSKILLS\nPython, FastAPI, Docker\n\nEXPERIENCE\nSoftware Engineer at Acme Corp\n2022 - Present\n\nEDUCATION\nB.Tech Computer Science 2022"}]
    chunks = chunk_by_section(pages, RESUME_HEADERS)
    sections = [c["section"] for c in chunks]
    assert "SKILLS" in sections
    assert "EXPERIENCE" in sections
    assert "EDUCATION" in sections

def test_chunk_by_section_no_headers_falls_back():
    pages = [{"page": 1, "text": "This is a paragraph.\n\nThis is another paragraph.\n\nAnd a third one here."}]
    chunks = chunk_by_section(pages, RESUME_HEADERS)
    # Should fall back to paragraph chunking
    assert len(chunks) >= 1

def test_chunk_by_paragraph():
    pages = [{"page": 1, "text": "First paragraph with enough content here.\n\nSecond paragraph with enough content here.\n\nThird paragraph with enough content."}]
    chunks = chunk_by_paragraph(pages)
    assert len(chunks) >= 2

def test_retrieval_scoring():
    from app.retriever import find_relevant_chunks
    chunks = [
        {"section": "SKILLS", "text": "[SKILLS]\nPython, FastAPI, Docker, AWS"},
        {"section": "EXPERIENCE", "text": "[EXPERIENCE]\nSoftware Engineer at Acme Corp"},
        {"section": "EDUCATION", "text": "[EDUCATION]\nB.Tech Computer Science"},
    ]
    results = find_relevant_chunks("what are her skills", chunks, top_k=1)
    assert results[0]["section"] == "SKILLS"