import streamlit as st
import requests
import uuid

import os
BASE_URL = os.getenv("API_URL", "http://127.0.0.1:8000")

st.set_page_config(page_title="BoT-GPT", layout="wide")

# ── Session state init ────────────────────────────────────────────────────────
if "user"             not in st.session_state: st.session_state.user             = None
if "session_id"       not in st.session_state: st.session_state.session_id       = None
if "chats"            not in st.session_state: st.session_state.chats            = {}
if "active_chat"      not in st.session_state: st.session_state.active_chat      = None
if "pending_question" not in st.session_state: st.session_state.pending_question = None


def auth_headers():
    return {"X-Session-ID": st.session_state.session_id}


def load_chats_from_server():
    """Fetch all chats + history from server and restore into session state."""
    try:
        res = requests.get(f"{BASE_URL}/user/chats", headers=auth_headers())
        if res.status_code != 200:
            st.sidebar.warning(f"Could not load chats: {res.text}")
            return
        chats = res.json().get("chats", [])
        st.session_state.chats = {}
        for chat in chats:
            st.session_state.chats[chat["id"]] = {
                "messages":           chat["messages"],
                "mode":               chat["mode"],
                "doc_id":             chat.get("doc_id"),
                "filename":           chat.get("filename"),
                "doc_type":           chat.get("doc_type"),
                "total_pages":        chat.get("total_pages"),
                "title":              chat.get("title"),
                "summary_structured": chat.get("summary_structured"),
            }
        if chats:
            st.session_state.active_chat = chats[0]["id"]
    except Exception as e:
        st.sidebar.warning(f"Could not load chats: {e}")


# ════════════════════════════════════════════════════════════════════
# AUTH SCREEN
# ════════════════════════════════════════════════════════════════════

if not st.session_state.user:
    st.title("🤖 Bot-GPT Assistant")
    tab_login, tab_register = st.tabs(["Login", "Register"])

    with tab_login:
        st.subheader("Login")
        with st.form("login_form"):
            email    = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Login", use_container_width=True)

        if submitted:
            if not email or not password:
                st.error("Please fill in all fields.")
            else:
                with st.spinner("Logging in..."):
                    try:
                        res = requests.post(f"{BASE_URL}/auth/login", json={"email": email, "password": password})
                        if res.status_code == 200:
                            data = res.json()
                            st.session_state.user        = {"name": data["name"], "email": data["email"], "user_id": data["user_id"]}
                            st.session_state.session_id  = data["session_id"]
                            st.session_state.active_chat = None
                            load_chats_from_server()
                            st.rerun()
                        else:
                            st.error(res.json().get("detail", "Login failed."))
                    except Exception as e:
                        st.error(f"Could not connect to server: {e}")

    with tab_register:
        st.subheader("Create account")
        with st.form("register_form"):
            name       = st.text_input("Name")
            email_r    = st.text_input("Email")
            password_r = st.text_input("Password", type="password")
            submitted_r = st.form_submit_button("Register", use_container_width=True)

        if submitted_r:
            if not name or not email_r or not password_r:
                st.error("Please fill in all fields.")
            else:
                with st.spinner("Creating account..."):
                    try:
                        res = requests.post(f"{BASE_URL}/auth/register", json={"email": email_r, "password": password_r, "name": name})
                        if res.status_code == 200:
                            st.success("Account created! Please log in.")
                        else:
                            st.error(res.json().get("detail", "Registration failed."))
                    except Exception as e:
                        st.error(f"Could not connect to server: {e}")

    st.stop()


# ════════════════════════════════════════════════════════════════════
# MAIN APP (logged in)
# ════════════════════════════════════════════════════════════════════

st.title("🤖 Bot-GPT Assistant")


def call_api(chat_id: str, question: str) -> dict:
    chat = st.session_state.chats[chat_id]
    if chat["mode"] == "rag":
        res = requests.post(f"{BASE_URL}/document/ask",
            headers=auth_headers(),
            json={
                "session_id": st.session_state.session_id,
                "chat_id":    chat_id,
                "doc_id":     chat["doc_id"],
                "question":   question
            })
    else:
        res = requests.post(f"{BASE_URL}/general/ask",
            headers=auth_headers(),
            json={
                "session_id": st.session_state.session_id,
                "chat_id":    chat_id,
                "question":   question
            })
    return res.json()


# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown(f"👤 **{st.session_state.user['name']}**")
    st.caption(st.session_state.user['email'])
    if st.button("Logout", use_container_width=True):
        for key in ["user", "session_id", "chats", "active_chat", "pending_question"]:
            st.session_state[key] = None if key != "chats" else {}
        st.rerun()

    st.divider()

    if st.button("+ New General Chat", use_container_width=True):
        cid = str(uuid.uuid4())
        st.session_state.chats[cid] = {
            "messages": [], "mode": "general",
            "doc_id": None, "filename": None
        }
        st.session_state.active_chat = cid
        st.rerun()

    st.markdown("**Upload PDF for RAG Chat**")
    uploaded = st.file_uploader("Choose a PDF", type="pdf", label_visibility="collapsed")
    if uploaded and st.button("Upload & Start RAG Chat", use_container_width=True):
        with st.spinner("Processing PDF..."):
            res = requests.post(
                f"{BASE_URL}/document/upload",
                headers=auth_headers(),
                files={"file": (uploaded.name, uploaded.getvalue(), "application/pdf")}
            )
            if res.status_code == 200:
                d   = res.json()
                cid = str(uuid.uuid4())
                st.session_state.chats[cid] = {
                    "messages":    [],
                    "mode":        "rag",
                    "doc_id":      d["doc_id"],
                    "filename":    d["filename"],
                    "total_pages": d["total_pages"],
                    "doc_type":    d["doc_type"],
                }
                st.session_state.active_chat = cid
                st.success(f"✓ {d['filename']} · {d['total_pages']} pages · type: `{d['doc_type']}`")
                st.rerun()
            else:
                st.error(res.json().get("detail", "Upload failed."))

    st.divider()
    st.markdown("**Chats**")
    for cid, chat in list(st.session_state.chats.items()):
        icon  = "📄" if chat["mode"] == "rag" else "💬"
        label = chat.get("title") or chat.get("filename") or "General Chat"
        col1, col2 = st.columns([5, 1])
        with col1:
            if st.button(f"{icon} {label[:28]}", key=f"nav-{cid}", use_container_width=True):
                st.session_state.active_chat = cid
                st.rerun()
        with col2:
            if st.button("🗑", key=f"del-{cid}", help="Delete chat"):
                res = requests.delete(
                    f"{BASE_URL}/conversations/{cid}",
                    headers=auth_headers()
                )
                if res.status_code == 200:
                    del st.session_state.chats[cid]
                    if st.session_state.active_chat == cid:
                        st.session_state.active_chat = next(iter(st.session_state.chats), None)
                    st.rerun()
                else:
                    st.error("Could not delete chat.")


# ── Main chat area ────────────────────────────────────────────────────────────

if not st.session_state.active_chat:
    st.info("Start a **General Chat** or upload a **PDF** from the sidebar.")
    st.stop()

chat_id = st.session_state.active_chat
chat    = st.session_state.chats[chat_id]

if chat["mode"] == "rag":
    st.caption(f"📄 **{chat['filename']}** — {chat['total_pages']} pages · type: `{chat.get('doc_type', 'unknown')}`")
else:
    st.caption("💬 General Chat")
st.divider()

# render existing messages
for idx, msg in enumerate(chat["messages"]):
    with st.chat_message(msg["role"]):
        if msg["role"] == "user":
            st.markdown(msg["content"])
        else:
            if msg.get("not_found"):
                st.warning("⚠️ This information was not found in the uploaded document.")
            else:
                st.markdown(msg["content"])
            if msg.get("source_ref"):  st.caption(f"📍 {msg['source_ref']}")
            if msg.get("uncertainty"): st.warning(f"⚠️ {msg['uncertainty']}")
            if msg.get("confidence"):  st.caption(f"Confidence: **{msg['confidence']}**")
            if msg.get("clarifying"):
                st.markdown("**Please clarify:**")
                for q in msg["clarifying"]: st.markdown(f"- {q}")
            if msg.get("follow_ups"):
                st.markdown("**💡 Follow-up questions:**")
                for i, q in enumerate(msg["follow_ups"]):
                    if st.button(q, key=f"fq-{idx}-{i}", use_container_width=True):
                        st.session_state.pending_question = q
                        st.rerun()

# pick question — follow-up button or typed
question = None
if st.session_state.pending_question:
    question = st.session_state.pending_question
    st.session_state.pending_question = None

typed = st.chat_input("Ask about the document..." if chat["mode"] == "rag" else "Ask anything...")
if typed:
    question = typed

# call API and render response
if question:
    chat["messages"].append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            try:
                data        = call_api(chat_id, question)
                answer      = data.get("answer", "")
                confidence  = data.get("confidence", "")
                not_found   = data.get("not_found", False)
                source_ref  = data.get("source_reference")
                clarifying  = data.get("clarifying_questions")
                follow_ups  = data.get("follow_up_questions") or []
                uncertainty = data.get("uncertainty_note")

                if not_found:
                    st.warning("⚠️ This information was not found in the uploaded document.")
                else:
                    st.markdown(answer)

                if source_ref:  st.caption(f"📍 {source_ref}")
                if uncertainty: st.warning(f"⚠️ {uncertainty}")
                st.caption(f"Confidence: **{confidence}**")

                if clarifying:
                    st.markdown("**Please clarify:**")
                    for q in clarifying: st.markdown(f"- {q}")

                if follow_ups:
                    st.markdown("**💡 Follow-up questions:**")
                    for i, q in enumerate(follow_ups):
                        if st.button(q, key=f"fq-new-{i}", use_container_width=True):
                            st.session_state.pending_question = q
                            st.rerun()

                chat["messages"].append({
                    "role":        "assistant",
                    "content":     answer,
                    "confidence":  confidence,
                    "not_found":   not_found,
                    "source_ref":  source_ref,
                    "clarifying":  clarifying,
                    "follow_ups":  follow_ups,
                    "uncertainty": uncertainty,
                })

            except Exception as e:
                st.error(f"Error: {e}")