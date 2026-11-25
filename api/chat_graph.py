"""LangGraph orchestration of one text-chat turn.

The graph owns the *decisions* of a turn; the existing, tested helpers do the work. Nothing about the
answer logic changed when this was introduced: retrieval, the query rewrite, grounding, the consent
gate and lead persistence are the same functions the route always used (api/chat.py, api/knowledge.py,
api/leads.py). What the graph adds is explicit, testable routing and a new branch (human handoff).

                   +---------------+
    start --------> detect_intent   |  rule-based, no LLM call (keeps latency flat)
                   +-------+-------+
                 asks a human |        | anything else
                              v        v
                        handoff     retrieve
                            |          |  nothing found, no infra error, follow-up?
              (failed: fall |          +---------> rewrite --+
               back to AI)  |          v                     |
                            |       build_prompt <-----------+
                            |          v
                            |       generate --LLM error--> fail
                            |          | tool call(s)
                            |          v
                            |     consent_gate  (pure: arguments valid + clear yes after read-back)
                            |          | at least one accepted
                            |          v
                            |     lead_capture  (database write through the same gate)
                            |          v
                            |     tool_followup --LLM error--> fail
                            v          v
                            +------> finalize --> end

Voice is deliberately NOT routed through this graph: it keeps its own Pipecat pipeline, so its latency
is unaffected. Text chat only.
"""

import json
import logging
import operator
import re
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph

from api.knowledge import (
    QUERY_REWRITE_PROMPT,
    SearchResult,
    build_grounded_system_prompt,
    build_rewrite_input,
    clean_rewritten_query,
)
from api.leads import LEAD_CAPTURE_RULES, SAVE_LEAD_TOOL, check_lead_proposal, save_lead_from_tool
from api.llm import LLMError, LLMReply

logger = logging.getLogger(__name__)

Intent = Literal["general", "human_handoff"]

# A visitor asking for a person. Deliberately narrow: "speak to / talk to / connect me with / get me" a
# human, person, rep, agent, someone or the sales team. Product questions ("does it have a sales
# pipeline?") must not trigger it.
_PERSON = r"(?:a |an |the |your |one of your )?(?:real |live |actual )?(?:human|person|people|someone|somebody|rep|representative|salesperson|sales ?person|sales team|sales rep|agent|team member|manager)"
HUMAN_REQUEST = re.compile(
    rf"\b(?:(?:talk|speak|chat|connect|transfer|put me through|get me|hand me)\b[^.?!]{{0,25}}\b(?:to|with|over to)\b[^.?!]{{0,12}}{_PERSON}"
    rf"|(?:i (?:want|need|would like|'d like|d like)|can i|could i|may i|let me)\b[^.?!]{{0,20}}\b(?:human|real person|live person|live agent|sales rep|representative|salesperson)"
    rf"|(?:human|real person|live agent|live chat)\s*(?:please|now)?\s*[.!?]?$)",
    re.IGNORECASE,
)

HANDOFF_AVAILABLE_REPLY = (
    "I've asked a member of our sales team to join this chat. Please keep this page open."
)
HANDOFF_QUEUED_REPLY = (
    "Nobody on our sales team is online right now, but I've recorded your request. "
    "If you'd like them to contact you, tell me your name and email and I can pass your details on."
)


def detect_intent_of(message: str) -> Intent:
    return "human_handoff" if HUMAN_REQUEST.search(message.strip()) else "general"


class ChatState(TypedDict, total=False):
    # inputs
    user_message: str
    history: list[dict[str, Any]]
    agent_prompt: str
    organization_id: uuid.UUID
    conversation_id: uuid.UUID
    # working
    intent: Intent
    search: SearchResult
    rewritten_query: str | None
    system_prompt: str
    sources: list[str]
    result: LLMReply
    llm_ms: int
    lead_decisions: list[dict[str, Any]]
    lead_outcomes: dict[str, dict[str, str]]
    lead_status: str | None
    handoff: dict[str, Any] | None
    handoff_failed: bool
    # outputs
    reply: str
    error: str | None
    path: Annotated[list[str], operator.add]


@dataclass
class ChatDeps:
    """Per-request collaborators, passed through the graph config (never stored in the state)."""

    session: Any
    llm: Any
    knowledge_search: Any
    settings: Any
    org: Any
    notifier: Any
    safe_search: Any
    request_handoff: Any = None


@dataclass
class TurnOutcome:
    text: str
    llm_ms: int
    rag_ms: int | None
    sources: list[str] = field(default_factory=list)
    rewritten_query: str | None = None
    lead_status: str | None = None
    rag_error: bool = False
    handoff_status: str | None = None
    path: list[str] = field(default_factory=list)


def _deps(config) -> ChatDeps:
    return config["configurable"]["deps"]


# ---------------------------------------------------------------- nodes


async def detect_intent(state: ChatState, config) -> dict:
    return {"intent": detect_intent_of(state["user_message"]), "path": ["detect_intent"]}


async def handoff(state: ChatState, config) -> dict:
    d = _deps(config)
    try:
        outcome = await d.request_handoff(
            d.session, d.org, state["conversation_id"], "Visitor asked for a person", d.notifier
        )
    except Exception:
        # The AI must still answer if the handoff machinery is down.
        logger.exception("Handoff request failed for conversation %s", state["conversation_id"])
        await d.session.rollback()
        return {"handoff_failed": True, "path": ["handoff"]}
    reply = HANDOFF_AVAILABLE_REPLY if outcome["available_now"] else HANDOFF_QUEUED_REPLY
    return {"handoff": {"status": outcome["handoff"].status, "available_now": outcome["available_now"],
                        "created": outcome["created"]},
            "reply": reply, "llm_ms": 0, "path": ["handoff"]}


async def retrieve(state: ChatState, config) -> dict:
    d = _deps(config)
    search = await d.safe_search(d.knowledge_search, d.session, state["organization_id"],
                                 state["user_message"], d.settings)
    return {"search": search, "rewritten_query": None, "path": ["retrieve"]}


async def rewrite(state: ChatState, config) -> dict:
    d = _deps(config)
    history = state["history"]
    await d.session.commit()  # no transaction held open across an LLM call
    try:
        rewritten = await d.llm.generate(QUERY_REWRITE_PROMPT, build_rewrite_input(history))
        candidate = clean_rewritten_query(rewritten.text)
    except LLMError:
        logger.warning("Query rewrite failed for conversation %s", state["conversation_id"])
        candidate = ""
    update: dict = {"path": ["rewrite"]}
    if candidate and candidate.lower() != state["user_message"].lower():
        retry = await d.safe_search(d.knowledge_search, d.session, state["organization_id"], candidate, d.settings)
        if retry.chunks:
            update.update({"search": retry, "rewritten_query": candidate})
    return update


async def build_prompt(state: ChatState, config) -> dict:
    d = _deps(config)
    search = state["search"]
    prompt = (f"{build_grounded_system_prompt(state['agent_prompt'], search.chunks, error=search.error)}"
              f"\n\n{LEAD_CAPTURE_RULES}")
    await d.session.commit()  # the LLM call may take a while: hold no transaction
    return {"system_prompt": prompt, "sources": [c.heading for c in search.chunks], "path": ["build_prompt"]}


async def generate(state: ChatState, config) -> dict:
    d = _deps(config)
    try:
        result = await d.llm.generate(state["system_prompt"], state["history"], tools=[SAVE_LEAD_TOOL])
    except LLMError:
        logger.exception("LLM call failed for conversation %s", state["conversation_id"])
        return {"error": "llm", "path": ["generate"]}
    return {"result": result, "llm_ms": result.latency_ms, "reply": result.text, "path": ["generate"]}


async def consent_gate(state: ChatState, config) -> dict:
    """Pure decision, no database: is each proposed save_lead call valid and backed by clear consent?"""
    decisions = []
    for call in state["result"].tool_calls:
        if call.name != "save_lead":
            decisions.append({"id": call.id, "name": call.name, "accepted": False, "reason": "Unknown tool."})
            continue
        lead, problem = check_lead_proposal(state["history"], call.arguments)
        decisions.append({"id": call.id, "name": call.name, "accepted": lead is not None, "reason": problem})
    return {"lead_decisions": decisions, "path": ["consent_gate"]}


async def lead_capture(state: ChatState, config) -> dict:
    """Persists the accepted calls. save_lead_from_tool re-applies the same gate before it writes."""
    d = _deps(config)
    outcomes: dict[str, dict[str, str]] = {}
    status = None
    accepted_ids = {x["id"] for x in state["lead_decisions"] if x["accepted"]}
    for call in state["result"].tool_calls:
        if call.id in accepted_ids:
            outcome = await _save_lead(d, state, call)
            status = outcome["status"]
            outcomes[call.id] = outcome
    await d.session.commit()
    return {"lead_outcomes": outcomes, "lead_status": status, "path": ["lead_capture"]}


async def _save_lead(d: ChatDeps, state: ChatState, call) -> dict:
    return await save_lead_from_tool(d.session, state["organization_id"], state["conversation_id"],
                                     state["history"], call.arguments)


async def tool_followup(state: ChatState, config) -> dict:
    d = _deps(config)
    result = state["result"]
    messages = []
    status = state.get("lead_status")
    outcomes = state.get("lead_outcomes", {})
    for decision in state["lead_decisions"]:
        if decision["accepted"]:
            outcome = outcomes[decision["id"]]
        else:
            outcome = {"status": "rejected", "reason": decision["reason"]}
            if decision["name"] == "save_lead":
                status = outcome["status"]
        messages.append({"role": "tool", "tool_call_id": decision["id"], "content": json.dumps(outcome)})
    follow_up = [
        *state["history"],
        {"role": "assistant", "content": result.text or None,
         "tool_calls": [c.to_message_part() for c in result.tool_calls]},
        *messages,
    ]
    try:
        final = await d.llm.generate(state["system_prompt"], follow_up, tools=[SAVE_LEAD_TOOL], tool_choice="none")
    except LLMError:
        logger.exception("LLM follow-up failed for conversation %s", state["conversation_id"])
        return {"error": "llm", "path": ["tool_followup"]}
    return {"reply": final.text, "llm_ms": state["llm_ms"] + final.latency_ms, "lead_status": status,
            "path": ["tool_followup"]}


async def finalize(state: ChatState, config) -> dict:
    return {"path": ["finalize"]}


async def fail(state: ChatState, config) -> dict:
    return {"path": ["fail"]}


# ---------------------------------------------------------------- routing (pure functions, unit-tested)


def route_after_intent(state: ChatState) -> str:
    return "handoff" if state.get("intent") == "human_handoff" else "retrieve"


def route_after_handoff(state: ChatState) -> str:
    return "retrieve" if state.get("handoff_failed") else "finalize"


def route_after_retrieve(state: ChatState) -> str:
    search = state["search"]
    # Rewriting cannot fix an infrastructure failure, and a first message has nothing to refer back to.
    if not search.chunks and not search.error and len(state["history"]) > 1:
        return "rewrite"
    return "build_prompt"


def route_after_generate(state: ChatState) -> str:
    if state.get("error"):
        return "fail"
    return "consent_gate" if state["result"].tool_calls else "finalize"


def route_after_gate(state: ChatState) -> str:
    return "lead_capture" if any(x["accepted"] for x in state["lead_decisions"]) else "tool_followup"


def route_after_followup(state: ChatState) -> str:
    return "fail" if state.get("error") else "finalize"


def build_graph():
    g = StateGraph(ChatState)
    for name, fn in [("detect_intent", detect_intent), ("handoff", handoff), ("retrieve", retrieve),
                     ("rewrite", rewrite), ("build_prompt", build_prompt), ("generate", generate),
                     ("consent_gate", consent_gate), ("lead_capture", lead_capture),
                     ("tool_followup", tool_followup), ("finalize", finalize), ("fail", fail)]:
        g.add_node(name, fn)
    g.add_edge(START, "detect_intent")
    g.add_conditional_edges("detect_intent", route_after_intent, ["handoff", "retrieve"])
    g.add_conditional_edges("handoff", route_after_handoff, ["retrieve", "finalize"])
    g.add_conditional_edges("retrieve", route_after_retrieve, ["rewrite", "build_prompt"])
    g.add_edge("rewrite", "build_prompt")
    g.add_edge("build_prompt", "generate")
    g.add_conditional_edges("generate", route_after_generate, ["consent_gate", "finalize", "fail"])
    g.add_conditional_edges("consent_gate", route_after_gate, ["lead_capture", "tool_followup"])
    g.add_edge("lead_capture", "tool_followup")
    g.add_conditional_edges("tool_followup", route_after_followup, ["finalize", "fail"])
    g.add_edge("finalize", END)
    g.add_edge("fail", END)
    return g.compile()


GRAPH = build_graph()


class ChatTurnFailed(Exception):
    """The LLM could not produce a reply (the route turns this into HTTP 503)."""


async def run_chat_turn(deps: ChatDeps, *, user_message: str, history: list[dict[str, Any]], agent_prompt: str,
                        organization_id: uuid.UUID, conversation_id: uuid.UUID) -> TurnOutcome:
    final = await GRAPH.ainvoke(
        {"user_message": user_message, "history": history, "agent_prompt": agent_prompt,
         "organization_id": organization_id, "conversation_id": conversation_id, "path": []},
        config={"configurable": {"deps": deps}},
    )
    if final.get("error"):
        raise ChatTurnFailed(final["error"])
    search = final.get("search")
    handled = final.get("handoff")
    return TurnOutcome(
        text=final["reply"],
        llm_ms=final.get("llm_ms", 0),
        rag_ms=None if handled else (search.elapsed_ms if search else None),
        sources=final.get("sources", []),
        rewritten_query=final.get("rewritten_query"),
        lead_status=final.get("lead_status"),
        rag_error=bool(search and search.error),
        handoff_status=handled["status"] if handled else None,
        path=final.get("path", []),
    )
