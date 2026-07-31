"""ask_loop — the bounded Anthropic tool-use loop behind Charlee's Ask tab.

Read-only: it wires the shared read tools (agent_read_tools) to the Anthropic
Messages API and runs the propose→tool→answer loop until the model has an
answer or the round cap is hit. The Anthropic client and the read `getter` are
both injected, so this runs headless in tests against a mocked client with no
API key and no live calls.

Everything money-related comes back through the read tools (which bottom out
in the app's own derivations); the loop itself does no math and never writes.
"""
import json

from agent_read_tools import anthropic_tools, call_read_tool


def _text(content):
    """Join the text blocks of an assistant message into a plain string."""
    return "".join(getattr(b, "text", "") for b in content
                   if getattr(b, "type", None) == "text").strip()


def run_ask(client, getter, user_message, *, model, system,
            history=None, max_rounds=6, max_tokens=1024):
    """Run one Ask turn.

    client   — an Anthropic client (or a mock) exposing messages.create(...).
    getter   — getter(path, query) -> parsed JSON of a Flask read endpoint,
               already carrying the caller's identity/auth.
    user_message — the person's question (a string).
    history  — prior [{role, content}] messages for multi-turn (client-held);
               not mutated.
    max_rounds — hard cap on model↔tool round-trips, bounding cost/latency.

    Returns {answer, tools_used, rounds, stopped} where `stopped` is 'end'
    (the model finished) or 'max_rounds' (hit the cap)."""
    tools = anthropic_tools()
    messages = list(history or []) + [{"role": "user", "content": user_message}]
    used = []

    for rnd in range(1, max_rounds + 1):
        resp = client.messages.create(
            model=model, system=system, tools=tools,
            messages=messages, max_tokens=max_tokens)
        messages.append({"role": "assistant", "content": resp.content})

        if getattr(resp, "stop_reason", None) != "tool_use":
            return {"answer": _text(resp.content), "tools_used": used,
                    "rounds": rnd, "stopped": "end"}

        # Execute every tool_use block the model emitted this round.
        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            used.append(block.name)
            try:
                data = call_read_tool(getter, block.name, block.input)
                content, is_error = json.dumps(data), False
            except Exception as e:  # unknown tool, or a read that errored
                content, is_error = f"tool error: {e}", True
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": content, "is_error": is_error})
        messages.append({"role": "user", "content": results})

    # Cap hit — return whatever text the model last produced, honestly labeled.
    return {"answer": _text(messages[-2]["content"]) or
            "I wasn't able to finish answering that — try asking more simply.",
            "tools_used": used, "rounds": max_rounds, "stopped": "max_rounds"}
