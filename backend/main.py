from __future__ import annotations

import os
import re
import time
import threading
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.memory_service import (
    append_message,
    clear_session,
    create_session,
    delete_session,
    get_session,
    get_session_messages,
    list_sessions,
    update_session_title,
)
from backend.schema_service import warm_schema_cache
from backend.sql_agent import ask_question

load_dotenv()

APP_TITLE = "Snowflake Census Agent API"
APP_VERSION = "7.0.0"
MAX_RESULT_ROWS_IN_RESPONSE = 100
MAX_RESULT_ROWS_IN_MESSAGE_METADATA = 25


def _background_warm_schema() -> None:
    try:
        warm_schema_cache()
        print("[startup] schema cache warmed in background")
    except Exception as e:
        print(f"[startup] schema warmup failed: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # IMPORTANT:
    # Do NOT block startup waiting for schema warmup.
    # Start server first, warm cache in background.
    
    yield


app = FastAPI(title=APP_TITLE, version=APP_VERSION, lifespan=lifespan)

raw_origins = os.getenv("FRONTEND_ORIGIN", "*")
allow_origins = ["*"] if raw_origins.strip() == "*" else [x.strip() for x in raw_origins.split(",") if x.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allow_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class RootResponse(BaseModel):
    service: str
    version: str
    docs_url: str


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class CreateSessionRequest(BaseModel):
    title: Optional[str] = None


class RenameSessionRequest(BaseModel):
    title: str = Field(..., min_length=1)


class SessionSummary(BaseModel):
    session_id: str
    title: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    message_count: int


class MessageItem(BaseModel):
    message_id: str
    role: str
    content: str
    timestamp: str
    metadata: Dict[str, Any] = Field(default_factory=dict)


class SessionDetail(BaseModel):
    session_id: str
    title: str
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    messages: List[MessageItem]


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class ChatResponse(BaseModel):
    status: str
    session_id: str
    question: str
    answer: str
    sql: Optional[str] = None
    result: List[Dict[str, Any]] = Field(default_factory=list)
    row_count: int = 0
    error: Optional[str] = None
    selected_tables: List[str] = Field(default_factory=list)
    chart_hint: Optional[str] = None
    total_ms: int = 0


class ClearSessionResponse(BaseModel):
    success: bool
    session_id: str


class DeleteSessionResponse(BaseModel):
    success: bool
    session_id: str


class RenameSessionResponse(BaseModel):
    success: bool
    session_id: str
    title: str


def _truncate_rows(result: Any, max_rows: int = MAX_RESULT_ROWS_IN_RESPONSE) -> List[Dict[str, Any]]:
    if not isinstance(result, list):
        return []
    return result[:max_rows]


def _truncate_rows_for_metadata(result: Any, max_rows: int = MAX_RESULT_ROWS_IN_MESSAGE_METADATA) -> List[Dict[str, Any]]:
    if not isinstance(result, list):
        return []
    return result[:max_rows]


def _recent_user_messages(session_id: str, limit: int = 3) -> List[str]:
    messages = get_session_messages(session_id)
    user_msgs = [m.get("content", "") for m in messages if m.get("role") == "user"]
    return user_msgs[-limit:]


import re

def _is_follow_up(current_message: str) -> bool:
    lower = current_message.lower().strip()

    triggers = [
        "what about",
        "how about",
        "same for",
        "same in",
        "same with",
        "same question",
        "and for",
        "and what about",
        "compare that",
        "compare this",
        "that one",
        "this one",
        "those",
        "them",
        "it",
        "instead",
    ]

    return any(trigger in lower for trigger in triggers)


def _resolve_anchor_question(session_id: str) -> str:
    recent = _recent_user_messages(session_id=session_id, limit=12)
    if not recent:
        return ""

    # Walk backwards to find the most recent NON-follow-up user query.
    for msg in reversed(recent):
        if not _is_follow_up(msg):
            return msg.strip()

    # If everything is follow-up, fall back to oldest available.
    return recent[0].strip()


def _build_effective_question(session_id: str, current_message: str) -> str:
    current = current_message.strip()

    if not _is_follow_up(current):
        return current

    recent = _recent_user_messages(session_id=session_id, limit=6)
    previous_user_question = recent[-1].strip() if recent else ""
    anchor_question = _resolve_anchor_question(session_id=session_id)

    return (
        f"Anchor question: {anchor_question}\n"
        f"Previous user question: {previous_user_question}\n"
        f"Follow-up user question: {current}\n"
        f"Instruction: Preserve the original metric/topic from the anchor question unless the follow-up clearly changes the metric. "
        f"Apply modifications from the latest follow-up, such as geography, ranking, year, or comparison. "
        f"If the follow-up says things like 'same for Texas', 'what about counties', or 'top 5 states instead', keep the same metric and only change the requested filter/grouping."
    )




def _build_assistant_metadata(
    *,
    status: str,
    sql: Optional[str],
    row_count: int,
    error: Optional[str],
    selected_tables: List[str],
    chart_hint: Optional[str],
    total_ms: int,
    result: Any,
) -> Dict[str, Any]:
    return {
        "status": status,
        "sql": sql,
        "row_count": row_count,
        "error": error,
        "selected_tables": selected_tables,
        "chart_hint": chart_hint,
        "total_ms": total_ms,
        "result_preview": _truncate_rows_for_metadata(result),
    }


@app.get("/", response_model=RootResponse)
def root():
    return RootResponse(service=APP_TITLE, version=APP_VERSION, docs_url="/docs")


@app.get("/health", response_model=HealthResponse)
def health():
    return HealthResponse(status="ok", service=APP_TITLE, version=APP_VERSION)


@app.post("/sessions", response_model=SessionSummary)
def create_chat_session(payload: CreateSessionRequest):
    session_id = create_session(title=payload.title)
    session = get_session(session_id)

    return SessionSummary(
        session_id=session["session_id"],
        title=session.get("title", "New Chat"),
        created_at=session.get("created_at"),
        updated_at=session.get("updated_at"),
        message_count=len(session.get("messages", [])),
    )


@app.get("/sessions", response_model=List[SessionSummary])
def get_all_sessions():
    sessions = list_sessions()
    return [SessionSummary(**session) for session in sessions]


@app.get("/sessions/{session_id}", response_model=SessionDetail)
def get_one_session(session_id: str):
    session = get_session(session_id)
    if not session:
        return SessionDetail(
            session_id=session_id,
            title="Missing Session",
            created_at=None,
            updated_at=None,
            messages=[],
        )

    messages = [MessageItem(**msg) for msg in get_session_messages(session_id)]
    return SessionDetail(
        session_id=session["session_id"],
        title=session.get("title", "New Chat"),
        created_at=session.get("created_at"),
        updated_at=session.get("updated_at"),
        messages=messages,
    )


@app.patch("/sessions/{session_id}", response_model=RenameSessionResponse)
def rename_one_session(session_id: str, payload: RenameSessionRequest):
    success = update_session_title(session_id, payload.title)
    return RenameSessionResponse(
        success=success,
        session_id=session_id,
        title=payload.title.strip(),
    )


@app.post("/sessions/{session_id}/clear", response_model=ClearSessionResponse)
def clear_one_session(session_id: str):
    return ClearSessionResponse(success=clear_session(session_id), session_id=session_id)


@app.delete("/sessions/{session_id}", response_model=DeleteSessionResponse)
def delete_one_session(session_id: str):
    return DeleteSessionResponse(success=delete_session(session_id), session_id=session_id)


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest):
    started = time.perf_counter()

    session_id = payload.session_id or create_session()
    user_message = payload.message.strip()

    append_message(
        session_id=session_id,
        role="user",
        content=user_message,
        metadata={},
    )

    effective_question = _build_effective_question(
        session_id=session_id,
        current_message=user_message,
    )

    try:
        agent_response = ask_question(effective_question)
    except Exception as e:
        total_ms = int((time.perf_counter() - started) * 1000)
        append_message(
            session_id=session_id,
            role="assistant",
            content="The backend hit an internal error while processing your question.",
            metadata={
                "status": "error",
                "sql": None,
                "row_count": 0,
                "error": str(e),
                "selected_tables": [],
                "chart_hint": "table",
                "total_ms": total_ms,
                "result_preview": [],
            },
        )
        return ChatResponse(
            status="error",
            session_id=session_id,
            question=user_message,
            answer="The backend hit an internal error while processing your question.",
            sql=None,
            result=[],
            row_count=0,
            error=str(e),
            selected_tables=[],
            chart_hint="table",
            total_ms=total_ms,
        )

    total_ms = int((time.perf_counter() - started) * 1000)

    answer = agent_response.get("answer", "I could not generate an answer.")
    sql = agent_response.get("sql")
    result = agent_response.get("result") or []
    row_count = int(agent_response.get("row_count", 0) or 0)
    error = agent_response.get("error")
    status = agent_response.get("status", "ok")
    selected_tables = agent_response.get("selected_tables", [])
    chart_hint = agent_response.get("chart_hint")

    assistant_metadata = _build_assistant_metadata(
        status=status,
        sql=sql,
        row_count=row_count,
        error=error,
        selected_tables=selected_tables,
        chart_hint=chart_hint,
        total_ms=total_ms,
        result=result,
    )

    append_message(
        session_id=session_id,
        role="assistant",
        content=answer,
        metadata=assistant_metadata,
    )

    return ChatResponse(
        status=status,
        session_id=session_id,
        question=user_message,
        answer=answer,
        sql=sql,
        result=_truncate_rows(result),
        row_count=row_count,
        error=error,
        selected_tables=selected_tables,
        chart_hint=chart_hint,
        total_ms=total_ms,
    )