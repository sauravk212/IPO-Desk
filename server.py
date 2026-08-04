"""Web server for the IPO agent.

    pip install fastapi uvicorn
    uvicorn server:app --reload --port 8000

Two endpoints:
    GET  /            -> the UI
    GET  /api/board   -> open + upcoming IPOs for the left rail
    POST /api/chat    -> Server-Sent Events stream of the agent's turn

SSE is the right fit here: one-way server push, plain HTTP, no websocket
handshake or reconnect logic to write. Each line is `data: {json}\\n\\n`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse, StreamingResponse
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.memory import InMemorySaver
from langchain.agents import create_agent
from pydantic import BaseModel

from registary_tools import IPO_TOOLS, _all_ipos, ist_today

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("ipo-desk")

HERE = Path(__file__).parent

try:
    from constants import SYSTEM_PROMPT
except ImportError:
    SYSTEM_PROMPT = "You are an IPO tracking assistant for the Indian stock market."


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------
# The date is appended at request time, not at import time. A server that
# stays up for three days would otherwise keep insisting it's Tuesday, and
# every deadline the model computes would drift with it.

def dated_prompt() -> str:
    return (
        f"{SYSTEM_PROMPT}\n\n"
        f"Today's date is {ist_today():%A, %d %B %Y} (IST). "
        f"Use this for every deadline calculation. Do not rely on your training data for dates."
    )


AGENT = create_agent(
    model=ChatOpenAI(model="gpt-4o-mini", temperature=0, streaming=True),
    tools=IPO_TOOLS,
    system_prompt=dated_prompt(),
    checkpointer=InMemorySaver(),
)

app = FastAPI(title="IPO Desk")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/")
async def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/board")
async def board(type: str = "all"):
    """Rail data. Calls the plain function, not the @tool wrapper -- no LLM
    involved, so there's no reason to pay the tool-invocation overhead."""
    try:
        rows = await asyncio.to_thread(_all_ipos, type)
    except Exception as e:
        log.warning("board fetch failed: %s", e)
        return {"error": "Couldn't reach the IPO source. Retry in a moment.", "open": [], "upcoming": []}

    open_rows = sorted(
        (r for r in rows if r["status"] == "open"),
        key=lambda r: r["days_until_close"],
    )
    upcoming = sorted(
        (r for r in rows if r["status"] == "upcoming"),
        key=lambda r: r["days_until_open"],
    )[:6]

    return {
        "today": date.isoformat(ist_today()),
        "type": type,
        "open": open_rows,
        "upcoming": upcoming,
    }


class ChatRequest(BaseModel):
    message: str
    thread_id: str = "web"
    ipo_type: str = "all"


SCOPE_NOTE = {
    "mainboard": "The user has the view filtered to Mainboard IPOs. Pass "
                 "ipo_type=\"mainboard\" to every tool call and mention only Mainboard issues.",
    "sme": "The user has the view filtered to SME IPOs. Pass ipo_type=\"sme\" to "
           "every tool call and mention only SME issues.",
}


def sse(event: str, **data) -> str:
    return f"data: {json.dumps({'event': event, **data})}\n\n"


# Streamed chunks are AIMessageChunk, whose .type is "AIMessageChunk" -- NOT
# "ai". Checking for "ai" alone silently drops every token while tool events
# still work, because those arrive via "updates" instead.
AI_TYPES = {"ai", "AIMessageChunk"}


def text_of(msg) -> str:
    """Content can be a plain string or a list of content blocks depending on
    model and version. Flatten both to text."""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return ""


async def run_turn(message: str, thread_id: str, ipo_type: str = "all"):
    """Stream one agent turn as SSE.

    Two stream modes at once:
      "updates"  -> node-level output, which is where tool calls and results live
      "messages" -> token-by-token text for the typing effect
    Text comes only from "messages" and tool events only from "updates", so
    nothing gets emitted twice.
    """
    config = {"configurable": {"thread_id": thread_id}}

    # The UI filter is stated per turn rather than baked into the system prompt,
    # because the person can change it mid-conversation and the checkpointer
    # would otherwise keep replaying the old scope.
    note = SCOPE_NOTE.get(ipo_type)
    content = f"[{note}]\n\n{message}" if note else message
    payload = {"messages": [HumanMessage(content=content)]}
    seen_calls: set[str] = set()
    streamed_any = False
    last_answer = ""  # fallback, harvested from "updates"

    yield sse("start")

    try:
        async for mode, chunk in AGENT.astream(
            payload, config=config, stream_mode=["updates", "messages"]
        ):
            if mode == "messages":
                msg, _meta = chunk
                if getattr(msg, "type", "") in AI_TYPES:
                    text = text_of(msg)
                    if text:
                        streamed_any = True
                        yield sse("token", text=text)

            elif mode == "updates":
                for node, update in (chunk or {}).items():
                    for msg in (update or {}).get("messages", []) or []:
                        for call in getattr(msg, "tool_calls", None) or []:
                            if call["id"] in seen_calls:
                                continue
                            seen_calls.add(call["id"])
                            yield sse(
                                "tool_call",
                                name=call["name"],
                                args=call.get("args") or {},
                            )

                        if getattr(msg, "type", "") == "tool":
                            raw = str(msg.content)
                            # Tools that return a list arrive as JSON, so a row
                            # count is far more useful in the UI than a byte count.
                            count = None
                            try:
                                parsed = json.loads(raw)
                                if isinstance(parsed, list):
                                    count = len(parsed)
                            except (ValueError, TypeError):
                                pass
                            yield sse(
                                "tool_result",
                                name=getattr(msg, "name", node),
                                preview=raw[:400],
                                length=len(raw),
                                count=count,
                            )

                        # A complete AI message with no tool calls is the answer.
                        # Keep it in reserve in case token streaming yielded nothing.
                        elif getattr(msg, "type", "") in AI_TYPES and not getattr(msg, "tool_calls", None):
                            text = text_of(msg)
                            if text:
                                last_answer = text

        # Belt and braces: if streaming produced no text but the graph did
        # produce an answer, send it in one go rather than showing an empty turn.
        if not streamed_any and last_answer:
            log.warning("token streaming produced nothing; falling back to final message")
            yield sse("token", text=last_answer)

    except Exception as e:
        log.exception("turn failed")
        yield sse("error", message=f"{type(e).__name__}: {e}")

    yield sse("done")


@app.post("/api/chat")
async def chat(req: ChatRequest):
    return StreamingResponse(
        run_turn(req.message, req.thread_id, req.ipo_type),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # stops nginx buffering the stream
        },
    )