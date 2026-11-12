"""Gemini tool-calling agents with persisted chat threads and collected artifacts."""

import json
import os
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Dict, List, Optional

import ai_image
from ai_image import AIImageError

THREADS_DIR = Path(__file__).resolve().parent.parent / "data" / "agents"
HISTORY_TURNS = 30
MAX_TOOL_CALLS = 12

_thread_lock = Lock()
_artifacts: ContextVar[Optional[List[dict]]] = ContextVar("agent_artifacts", default=None)


def add_artifact(kind: str, url: str, label: str = "", **extra: Any) -> None:
    """Called by tools: image / video / file / link shown under the agent's reply."""
    bucket = _artifacts.get()
    if bucket is None:
        return
    item = {"type": kind, "url": url, "label": label, **extra}
    if item not in bucket:
        bucket.append(item)


PREFERRED_MODEL_TTL_S = 15 * 60
_preferred: Dict[str, Any] = {"model": "", "until": 0.0}


def agent_models() -> List[str]:
    """Model chain for agents; the model that last succeeded goes first for a while."""
    raw = os.environ.get("AGENT_TEXT_MODEL", "").strip()
    chain = [m.strip() for m in raw.split(",") if m.strip()] if raw else ai_image.text_models()
    pref = _preferred["model"]
    if pref in chain and time.time() < _preferred["until"]:
        chain = [pref] + [m for m in chain if m != pref]
    return chain


def _remember_model(model: str) -> None:
    _preferred["model"] = model
    _preferred["until"] = time.time() + PREFERRED_MODEL_TTL_S


@dataclass
class Agent:
    id: str
    name: str
    role: str
    instructions: str
    tools: List[Callable] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    status_check: Optional[Callable[[], Dict[str, Any]]] = None

    def status(self) -> Dict[str, Any]:
        if not ai_image.ai_enabled():
            return {"ok": False, "detail": "Chưa có GEMINI_API_KEY"}
        if self.status_check:
            return self.status_check()
        return {"ok": True, "detail": ""}

    def chat(self, message: str, origin: str = "user") -> dict:
        """Run one turn; appends both messages to this agent's thread and returns the reply entry."""
        message = (message or "").strip()
        if not message:
            raise ValueError("Tin nhắn trống.")
        thread = load_thread(self.id)
        history = thread[-HISTORY_TURNS:]
        user_entry = _entry("user", message, origin=origin)
        append_thread(self.id, user_entry)

        token = _artifacts.set([]) if _artifacts.get() is None else None
        bucket = _artifacts.get()
        start = len(bucket)
        try:
            text, tool_log, model = self._generate(history, message)
            reply = _entry(
                "model",
                text,
                tools=tool_log,
                artifacts=list(bucket[start:]),
                model=model,
            )
        except Exception as err:  # noqa: BLE001
            reply = _entry("model", f"Lỗi khi xử lý: {err}", error=True, artifacts=list(bucket[start:]))
        finally:
            if token is not None:
                _artifacts.reset(token)
        append_thread(self.id, reply)
        return reply

    def _generate(self, history: List[dict], message: str):
        from google.genai import types

        contents = []
        for e in history:
            if e.get("error") or not e.get("text"):
                continue
            role = "model" if e["role"] == "model" else "user"
            text = e["text"]
            if e.get("origin") and e["origin"] != "user" and role == "user":
                text = f"[Yêu cầu từ {e['origin']}] {text}"
            contents.append(types.Content(role=role, parts=[types.Part(text=text)]))
        contents.append(types.Content(role="user", parts=[types.Part(text=message)]))

        config = types.GenerateContentConfig(
            system_instruction=self.instructions,
            tools=list(self.tools) or None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        tool_map = {fn.__name__: fn for fn in self.tools}
        tool_log: List[dict] = []
        models = agent_models()
        response = None

        # Manual tool loop: the model fallback applies per round, so a failing model never
        # causes tools (publish, render, image generation) to run a second time.
        for _ in range(MAX_TOOL_CALLS + 1):
            try:
                response = ai_image.generate_with_fallback(models, contents=contents, config=config)
            except Exception as err:  # noqa: BLE001
                raise AIImageError(_friendly_error(err)) from err
            used = getattr(response, "model_version", "") or ""
            if used in models:
                models = [used] + [m for m in models if m != used]
                _remember_model(used)

            calls = response.function_calls or []
            if not calls or len(tool_log) >= MAX_TOOL_CALLS:
                break
            contents.append(response.candidates[0].content)
            parts = []
            for fc in calls:
                result = _run_tool(tool_map, fc.name, dict(fc.args or {}))
                tool_log.append({"name": fc.name, "args": _short(dict(fc.args or {}), 300), "result": _short(result)})
                parts.append(types.Part.from_function_response(name=fc.name, response={"result": result}))
            contents.append(types.Content(role="user", parts=parts))

        text = _response_text(response)
        if not text:
            text = "Đã xử lý xong." if tool_log else "(Agent không trả lời — thử diễn đạt lại.)"
        return text, tool_log, getattr(response, "model_version", "") or ""


def _run_tool(tool_map: Dict[str, Callable], name: str, args: Dict[str, Any]) -> Any:
    fn = tool_map.get(name)
    if fn is None:
        return {"error": f"Không có tool {name}."}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": f"Sai tham số cho {name}: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{name} lỗi: {e}"}


def _response_text(response) -> str:
    if response is None or not response.candidates:
        return ""
    content = response.candidates[0].content
    texts = [p.text for p in (getattr(content, "parts", None) or []) if getattr(p, "text", None) and not getattr(p, "thought", False)]
    return "".join(texts).strip()


def _friendly_error(err: Exception) -> str:
    low = str(err).lower()
    if "429" in low or "resource_exhausted" in low:
        return "Gemini báo hết quota (gói Free). Thử lại sau hoặc bật billing."
    if "503" in low or "unavailable" in low or "high demand" in low:
        return "Các model Gemini đang quá tải. Thử lại sau ít phút."
    if "504" in low or "deadline" in low or "timeout" in low or "timed out" in low:
        return "Gemini phản hồi quá lâu (timeout). Thử lại sau ít phút."
    return str(err)[:400]


def _short(value: Any, limit: int = 600) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return value
    return text[:limit] + "…"


def _entry(role: str, text: str, **extra: Any) -> dict:
    return {
        "id": uuid.uuid4().hex[:12],
        "role": role,
        "text": text,
        "ts": datetime.now(timezone.utc).isoformat(),
        **extra,
    }


def _thread_path(agent_id: str) -> Path:
    return THREADS_DIR / f"{agent_id}.json"


def load_thread(agent_id: str) -> List[dict]:
    path = _thread_path(agent_id)
    with _thread_lock:
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []


def append_thread(agent_id: str, entry: dict) -> None:
    path = _thread_path(agent_id)
    with _thread_lock:
        THREADS_DIR.mkdir(parents=True, exist_ok=True)
        data: List[dict] = []
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = []
        data.append(entry)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def clear_thread(agent_id: str) -> None:
    with _thread_lock:
        _thread_path(agent_id).unlink(missing_ok=True)
