from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd
import plotly.express as px
import requests
import streamlit as st


st.set_page_config(
    page_title="US Census AI Agent",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

DEFAULT_API_BASE = "http://127.0.0.1:8000"
REQUEST_TIMEOUT = 180


def inject_css() -> None:
    st.markdown(
        """
        <style>
            .stApp {
                background: #f6f7fb;
            }

            .block-container {
                max-width: 1200px;
                padding-top: 1.6rem;
                padding-bottom: 1rem;
            }

            .hero-title {
                font-size: 3rem;
                font-weight: 800;
                color: #1f2937;
                margin-bottom: 0.25rem;
            }

            .hero-subtitle {
                font-size: 1.05rem;
                color: #6b7280;
                margin-bottom: 1rem;
            }

            .status-pill {
                display: inline-block;
                background: #eef2ff;
                color: #374151;
                border: 1px solid #dbeafe;
                border-radius: 999px;
                padding: 0.3rem 0.75rem;
                font-size: 0.88rem;
                font-weight: 600;
                margin-bottom: 1rem;
            }

            .soft-box {
                background: white;
                border: 1px solid #e5e7eb;
                border-radius: 18px;
                padding: 1rem 1rem;
                box-shadow: 0 2px 10px rgba(0,0,0,0.04);
            }

            section[data-testid="stSidebar"] {
                background: #f0f2f6;
            }

            .stButton > button {
                border-radius: 12px !important;
            }

            .stTextInput > div > div > input {
                border-radius: 12px !important;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


inject_css()


def init_state() -> None:
    defaults = {
        "api_base": DEFAULT_API_BASE,
        "active_session_id": None,
        "chat_messages": [],
        "debug_mode": False,
        "pending_prompt": None,
        "sessions_cache": [],
    }

    for key, value in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = value


init_state()


def api_url(path: str) -> str:
    return f"{st.session_state.api_base.rstrip('/')}{path}"


def safe_request(method: str, path: str, json_body: Optional[Dict[str, Any]] = None):
    try:
        response = requests.request(
            method=method,
            url=api_url(path),
            json=json_body,
            timeout=REQUEST_TIMEOUT,
        )

        try:
            payload = response.json()
        except Exception:
            payload = {"detail": response.text}

        return response.ok, payload
    except requests.RequestException as e:
        return False, {"detail": str(e)}


def get_health():
    return safe_request("GET", "/health")


def create_session(title: Optional[str] = None):
    return safe_request("POST", "/sessions", {"title": title})


def get_sessions():
    return safe_request("GET", "/sessions")


def get_session_detail(session_id: str):
    return safe_request("GET", f"/sessions/{session_id}")


def clear_session_api(session_id: str):
    return safe_request("POST", f"/sessions/{session_id}/clear", {})


def delete_session_api(session_id: str):
    return safe_request("DELETE", f"/sessions/{session_id}")


def rename_session_api(session_id: str, title: str):
    return safe_request("PATCH", f"/sessions/{session_id}", {"title": title})


def send_chat(message: str, session_id: Optional[str]):
    return safe_request("POST", "/chat", {"message": message, "session_id": session_id})


def refresh_sessions_cache() -> None:
    ok, payload = get_sessions()
    if ok and isinstance(payload, list):
        st.session_state.sessions_cache = payload


def format_timestamp(ts: Optional[str]) -> str:
    if not ts:
        return ""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %I:%M %p")
    except Exception:
        return ts


def get_active_session_summary() -> Optional[Dict[str, Any]]:
    sid = st.session_state.active_session_id
    if not sid:
        return None
    for session in st.session_state.sessions_cache:
        if session["session_id"] == sid:
            return session
    return None


def start_new_session() -> None:
    ok, payload = create_session()
    if not ok:
        st.error(f"Could not create session: {payload}")
        return

    st.session_state.active_session_id = payload["session_id"]
    st.session_state.chat_messages = []
    refresh_sessions_cache()
    st.rerun()


def load_session(session_id: str) -> None:
    ok, payload = get_session_detail(session_id)
    if not ok or not isinstance(payload, dict):
        st.error(f"Could not load session: {payload}")
        return

    st.session_state.active_session_id = payload["session_id"]
    st.session_state.chat_messages = payload.get("messages", [])


def clear_current_session() -> None:
    session_id = st.session_state.active_session_id
    if not session_id:
        return

    ok, payload = clear_session_api(session_id)
    if not ok:
        st.error(f"Could not clear session: {payload}")
        return

    st.session_state.chat_messages = []
    refresh_sessions_cache()
    st.rerun()


def delete_current_session() -> None:
    session_id = st.session_state.active_session_id
    if not session_id:
        return

    ok, payload = delete_session_api(session_id)
    if not ok:
        st.error(f"Could not delete session: {payload}")
        return

    st.session_state.active_session_id = None
    st.session_state.chat_messages = []
    refresh_sessions_cache()
    st.rerun()


def rename_current_session(new_title: str) -> None:
    session_id = st.session_state.active_session_id
    title = new_title.strip()

    if not session_id or not title:
        return

    ok, payload = rename_session_api(session_id, title)
    if not ok:
        st.error(f"Could not rename session: {payload}")
        return

    refresh_sessions_cache()
    st.rerun()


def result_to_df(result: Any) -> pd.DataFrame:
    if isinstance(result, list) and result:
        try:
            return pd.DataFrame(result)
        except Exception:
            return pd.DataFrame()
    return pd.DataFrame()


def numeric_columns(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]


def categorical_columns(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if not pd.api.types.is_numeric_dtype(df[c])]


def render_chart_area(result_preview: Any, key_prefix: str) -> None:
    df = result_to_df(result_preview)
    if df.empty:
        return

    if len(df) == 1:
        st.dataframe(df, use_container_width=True)
        return

    nums = numeric_columns(df)
    cats = categorical_columns(df)

    if len(cats) >= 1 and len(nums) >= 1:
        x_col = cats[0]
        y_col = nums[0]

        tabs = st.tabs(["Bar", "Line", "Table"])

        with tabs[0]:
            fig = px.bar(df, x=x_col, y=y_col, title=f"{y_col} by {x_col}")
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_bar")

        with tabs[1]:
            fig = px.line(df, x=x_col, y=y_col, markers=True, title=f"{y_col} by {x_col}")
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_line")

        with tabs[2]:
            st.dataframe(df, use_container_width=True)

        return

    if len(nums) >= 2:
        tabs = st.tabs(["Scatter", "Table"])
        with tabs[0]:
            fig = px.scatter(df, x=nums[0], y=nums[1], title=f"{nums[1]} vs {nums[0]}")
            st.plotly_chart(fig, use_container_width=True, key=f"{key_prefix}_scatter")
        with tabs[1]:
            st.dataframe(df, use_container_width=True)
        return

    st.dataframe(df, use_container_width=True)


def process_message(message: str) -> None:
    if not message.strip():
        return

    if not st.session_state.active_session_id:
        ok, payload = create_session()
        if not ok:
            st.error(f"Could not create session: {payload}")
            return
        st.session_state.active_session_id = payload["session_id"]

    with st.chat_message("user"):
        st.markdown(message)

    with st.chat_message("assistant"):
        with st.spinner("Thinking..."):
            ok, payload = send_chat(
                message=message,
                session_id=st.session_state.active_session_id,
            )

    if not ok:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        st.error(f"Request failed: {detail}")
        return

    st.session_state.active_session_id = payload["session_id"]
    load_session(payload["session_id"])
    refresh_sessions_cache()
    st.rerun()


with st.sidebar:
    st.markdown("## ⚙️ Controls")

    st.session_state.debug_mode = st.toggle(
        "Show Debug Info",
        value=st.session_state.debug_mode,
    )

    st.markdown("### Backend")
    st.session_state.api_base = st.text_input(
        "Backend URL",
        value=st.session_state.api_base,
        label_visibility="collapsed",
    )

    health_ok, _ = get_health()
    if health_ok:
        st.success("Connected")
    else:
        st.error("Disconnected")

    st.write("")
    if st.button("🧹 Start New Chat", use_container_width=True):
        start_new_session()

    active_summary = get_active_session_summary()
    if active_summary:
        st.write("")
        st.markdown("### Current Session")
        with st.form("rename_session_form", clear_on_submit=False):
            title_input = st.text_input(
                "Session title",
                value=active_summary.get("title", "New Chat"),
            )
            col_a, col_b = st.columns(2)
            with col_a:
                rename_clicked = st.form_submit_button("Rename", use_container_width=True)
            with col_b:
                delete_clicked = st.form_submit_button("Delete", use_container_width=True)

            if rename_clicked:
                rename_current_session(title_input)
            if delete_clicked:
                delete_current_session()

        if st.button("Clear Current Chat", use_container_width=True):
            clear_current_session()

    st.write("")
    st.markdown("### 💡 Example Queries")

    example_queries = [
        "What is the total population?",
        "What is the female population?",
        "Top 10 areas by population",
        "Compare population between 2019 and 2020",
        "What is the median household income?",
        "Show me rent-related statistics",
    ]

    for idx, query in enumerate(example_queries):
        if st.button(query, key=f"example_{idx}", use_container_width=True):
            st.session_state.pending_prompt = query

    st.write("")
    with st.expander("Recent Sessions", expanded=False):
        if not st.session_state.sessions_cache:
            refresh_sessions_cache()

        if not st.session_state.sessions_cache:
            st.caption("No sessions yet.")
        else:
            for session in st.session_state.sessions_cache[:20]:
                sid = session["session_id"]
                title = session.get("title", "New Chat")
                updated = format_timestamp(session.get("updated_at"))
                label = f"{title}\n{updated}"
                if st.button(label, key=f"session_{sid}", use_container_width=True):
                    load_session(sid)
                    st.rerun()


st.markdown('<div class="hero-title">📊 US Census AI Agent</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="hero-subtitle">Ask questions about US population and census data.</div>',
    unsafe_allow_html=True,
)

if st.session_state.active_session_id:
    short_id = st.session_state.active_session_id[:8]
    st.markdown(
        f'<div class="status-pill">Active Session: {short_id}</div>',
        unsafe_allow_html=True,
    )

if not st.session_state.chat_messages:
    st.markdown(
        """
        <div class="soft-box">
            Start with a question like <b>“What is the total population?”</b> or click an example query on the left.
        </div>
        """,
        unsafe_allow_html=True,
    )
else:
    for idx, msg in enumerate(st.session_state.chat_messages):
        role = msg.get("role", "assistant")
        content = msg.get("content", "")
        metadata = msg.get("metadata", {}) or {}

        with st.chat_message("user" if role == "user" else "assistant"):
            st.markdown(content)

            if role == "assistant":
                result_preview = metadata.get("result_preview", [])
                sql = metadata.get("sql")
                error = metadata.get("error")

                if result_preview:
                    with st.expander("View result", expanded=False):
                        render_chart_area(result_preview=result_preview, key_prefix=f"msg_{idx}")

                if st.session_state.debug_mode:
                    with st.expander("Debug Info", expanded=False):
                        st.json(metadata)
                        if sql:
                            st.code(sql, language="sql")
                        if error:
                            st.error(error)

if st.session_state.pending_prompt:
    pending = st.session_state.pending_prompt
    st.session_state.pending_prompt = None
    process_message(pending)

user_input = st.chat_input("Ask your question...")

if user_input:
    process_message(user_input)