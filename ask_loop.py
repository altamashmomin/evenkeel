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
import os

from agent_read_tools import anthropic_tools, call_read_tool

DEFAULT_MODEL = "claude-haiku-4-5"


class NotConfigured(RuntimeError):
    """No ANTHROPIC_API_KEY is set — the Ask endpoint should 503."""


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


# ── endpoint plumbing (POST /api/ask) ───────────────────────────────────────

def system_prompt(period):
    """The assistant's standing instructions. Static except for the month, so
    it (and the tool block) prompt-cache across a conversation. Carries the
    vocabulary the read tier depends on — the model reads this, not the code."""
    return (
        "You are the money assistant inside Ledger, a simple finance app that a "
        "two-person household shares. Answer their questions about their money "
        "warmly and in plain language — short, friendly, and concrete. Today's "
        f"month is {period}.\n\n"
        "Rules you must follow:\n"
        "- ALWAYS get numbers from the tools; never add, subtract, or estimate "
        "figures yourself. Every total already exists in a tool, computed the "
        "same way the app's screens compute it. Start with "
        "ledger_household_snapshot for open-ended questions.\n"
        "- Money comes back as {cents, display}; quote the `display` string "
        '(e.g. "$1,850.00") verbatim and never convert units.\n'
        "- 'income' means true_income (paychecks only). gross_inflows also "
        "counts refunds and transfers and is NOT income — never call it income. "
        "If there are unclassified inflows, say the income numbers are "
        "provisional and suggest tagging them in the app.\n"
        "- You can look but not change anything. To tag a deposit, record a "
        "payment between them, edit a transaction, or make a rule, tell them to "
        "do it in the app — it's quick.\n"
        "- If a tool errors or you're unsure, say so plainly rather than guessing."
    )


def make_app_getter(app, user_id):
    """An in-process getter: runs the app's own read endpoints under `user_id`'s
    session via a test client — no HTTP, no bearer token, same identity the
    browser has. Each read is its own sub-request with its own db connection;
    reads only, so nothing contends with the outer /api/ask request."""
    sub = app.test_client()
    with sub.session_transaction() as s:
        s["user_id"] = user_id

    def getter(path, query):
        q = {k: v for k, v in (query or {}).items() if v is not None}
        resp = sub.get(path, query_string=q)
        if resp.status_code >= 400:
            raise RuntimeError(f"read {path} failed ({resp.status_code})")
        return resp.get_json()

    return getter


def _make_client(api_key):
    import anthropic  # lazy — the app starts fine without the SDK installed
    return anthropic.Anthropic(api_key=api_key)


def answer(app, user_id, message, *, period, history=None, client=None):
    """Answer one question as `user_id`. In production the Anthropic client and
    model come from the environment; tests pass a mock `client` to bypass the
    key and the SDK entirely. Raises NotConfigured when no key is set."""
    if client is None:
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise NotConfigured()
        client = _make_client(key)
    return run_ask(
        client, make_app_getter(app, user_id), message,
        model=os.environ.get("ASK_MODEL", DEFAULT_MODEL),
        system=system_prompt(period), history=history)
