"""FastAPI routes for the Agent Workspace (page + chat API)."""

import hmac
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from agents.registry import AGENTS, COORDINATOR_ID, agent_list
from agents.runtime import clear_thread, load_thread


def _require_token(request: Request) -> None:
    """When AGENTS_TOKEN is set (public deploys), every workspace API call must send it."""
    expected = os.environ.get("AGENTS_TOKEN", "").strip()
    if not expected:
        return
    auth = request.headers.get("authorization", "")
    given = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    if not hmac.compare_digest(given, expected):
        raise HTTPException(status_code=401, detail="Cần AGENTS_TOKEN để dùng Agent Workspace.")


router = APIRouter(dependencies=[Depends(_require_token)])


class ChatBody(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)


def _agent(agent_id: str):
    agent = AGENTS.get(agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Không có agent này.")
    return agent


def register(app, templates, landing_ctx: dict) -> None:
    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            "agents.html", {"request": request, **landing_ctx, "default_agent": COORDINATOR_ID}
        )

    app.include_router(router)


@router.get("/api/agents")
def list_agents() -> JSONResponse:
    return JSONResponse({"agents": agent_list(), "default": COORDINATOR_ID})


@router.get("/api/agents/{agent_id}/thread")
def get_thread(agent_id: str) -> JSONResponse:
    _agent(agent_id)
    return JSONResponse({"messages": load_thread(agent_id)})


@router.delete("/api/agents/{agent_id}/thread")
def delete_thread(agent_id: str) -> JSONResponse:
    _agent(agent_id)
    clear_thread(agent_id)
    return JSONResponse({"ok": True})


@router.post("/api/agents/{agent_id}/chat")
def chat(agent_id: str, body: ChatBody) -> JSONResponse:
    agent = _agent(agent_id)
    status = agent.status()
    if not status.get("ok") and "GEMINI" in status.get("detail", ""):
        raise HTTPException(status_code=503, detail=status["detail"])
    try:
        reply = agent.chat(body.message)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return JSONResponse({"reply": reply})
