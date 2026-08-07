"""Catalog of Ledger's autonomous layer — the agents, assistants, and
scheduled jobs acting on the app. Powers the Agents tab (shared with the
household — both members see it).

v1 is a CURATED catalog: honest static facts (role, powers, cadence, model),
NOT live health — wiring real green/amber/red from the Pi guardian, service
state, and key expiry is a deliberate follow-up increment. The on-demand
subagents have no live per-agent status the app could observe anyway; only
the always-on/scheduled pieces do.

The 'subagent' entries are coherence-tested against .claude/agents/*.md
(tests/test_agents.py), so adding or removing an agent file without updating
this catalog fails the suite — the drift-tested single-source discipline
applied to the agent roster. Each subagent's `model` is read from its file
(so it can't drift either); the presentation (icon, group, one-liner) is
curated here."""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parent
AGENTS_DIR = REPO / ".claude" / "agents"

# Group order + labels for the tiled view.
GROUPS = [
    ("analysts", "Analysts & advisors"),
    ("build", "Maintainers & release"),
    ("ops", "Live operations"),
    ("redvault", "Security squad · REDVAULT"),
    ("assistants", "The assistants"),
]

# Presentation for the 14 role subagents in .claude/agents/. Keyed by the
# file stem (which is the agent's `name`). `model` is read from the file.
# Display names carry a tactical codename (the standing team + the REDVAULT
# security squad both have them) followed by the plain role.
SUBAGENTS = {
    "ledger-analyst": {
        "name": "ORACLE · Analyst", "group": "analysts", "icon": "📊",
        "access": "read-only", "surface": "Claude Code",
        "tagline": "Answers money questions from live data."},
    "ledger-security": {
        "name": "WARDEN · Security", "group": "analysts", "icon": "🛡️",
        "access": "advisory", "surface": "Claude Code",
        "tagline": "Defensive review — auth, injection, secrets, deps."},
    "ledger-chief-of-staff": {
        "name": "QUILL · Chief of Staff", "group": "analysts", "icon": "🗂️",
        "access": "read-only", "surface": "cloud · Fri",
        "tagline": "Reconciles every report into one weekly briefing."},
    "ledger-maintenance": {
        "name": "KEEPER · Maintenance", "group": "build", "icon": "🔧",
        "access": "edits · stops before commit", "surface": "Claude Code",
        "tagline": "Dependency freshness & backend health."},
    "ledger-release": {
        "name": "GATEKEEPER · Release", "group": "build", "icon": "🚀",
        "access": "recommend-only", "surface": "Claude Code",
        "tagline": "Go / no-go for a gated deploy; never pulls the trigger."},
    "ledger-health-sweep": {
        "name": "PULSE · Health Sweep", "group": "build", "icon": "🩺",
        "access": "read-only", "surface": "cloud · Mon",
        "tagline": "Weekly red / amber / green sweep of the whole app."},
    "ledger-ops": {
        "name": "BEACON · Ops", "group": "ops", "icon": "🛰️",
        "access": "recommend-only", "surface": "tailnet",
        "tagline": "Diagnoses the live Pi; recommends, never mutates."},

    # REDVAULT — the authorized security squad over the household's own stack.
    # Offense runs on dev copies; only Alta deploys. See docs/OPERATING-CHARTER.md.
    "ledger-scout": {
        "name": "SCOUT · Recon", "group": "redvault", "icon": "🧭",
        "access": "read-only", "surface": "Claude Code",
        "tagline": "Maps the attack surface; sets the test boundary."},
    "ledger-picklock": {
        "name": "PICKLOCK · Auth", "group": "redvault", "icon": "🔓",
        "access": "advisory", "surface": "dev copy",
        "tagline": "Tests auth, sessions & token scope on a copy."},
    "ledger-mirage": {
        "name": "MIRAGE · Injection", "group": "redvault", "icon": "💉",
        "access": "advisory", "surface": "dev copy",
        "tagline": "Probes injection & prompt-injection of the agent tools."},
    "ledger-keyring": {
        "name": "KEYRING · Access", "group": "redvault", "icon": "🔑",
        "access": "advisory", "surface": "dev copy",
        "tagline": "Checks member isolation & the two-phase write flow."},
    "ledger-blackout": {
        "name": "BLACKOUT · Infra", "group": "redvault", "icon": "🔒",
        "access": "recommend-only", "surface": "tailnet",
        "tagline": "Checks the Pi's secrets & tailnet exposure."},
    "ledger-patchwright": {
        "name": "PATCHWRIGHT · Fix", "group": "redvault", "icon": "🩹",
        "access": "edits · stops before commit", "surface": "Claude Code",
        "tagline": "Writes the fix + a proven regression test; stops before commit."},
    "ledger-tribunal": {
        "name": "TRIBUNAL · Purple lead", "group": "redvault", "icon": "⚖️",
        "access": "advisory", "surface": "Claude Code",
        "tagline": "Verifies findings, runs the fix loop, hands you go / no-go."},
}

# The always-on / scheduled pieces — not .claude/agents files, so their
# facts are curated here in full (these are the ones a later increment can
# give real live status).
SERVICES = [
    {"id": "ask", "kind": "assistant", "name": "Ask assistant",
     "group": "assistants", "icon": "💬", "access": "reads + tags",
     "model": "haiku 4.5", "surface": "in-app", "cadence": "always-on",
     "tagline": "Charlee's chat — answers questions and tags deposits."},
    {"id": "mcp", "kind": "service", "name": "MCP server",
     "group": "assistants", "icon": "🔌", "access": "reads + writes",
     "model": "—", "surface": "tailnet", "cadence": "always-on",
     "tagline": "Your Claude's read + write tools over Tailscale."},
    {"id": "sync", "kind": "routine", "name": "SimpleFIN sync",
     "group": "ops", "icon": "🔄", "access": "writes",
     "model": "—", "surface": "Pi timer", "cadence": "daily · 06:30",
     "tagline": "Pulls new bank transactions into Ledger."},
    {"id": "guardian", "kind": "routine", "name": "Pi Ops guardian",
     "group": "ops", "icon": "🌡️", "access": "read-only",
     "model": "—", "surface": "Pi timer", "cadence": "daily · 07:00",
     "tagline": "Checks services, disk, backups, and keys; alerts on trouble."},
]

# Plain-language explanations for every pill, so anyone — not just Alta —
# can read a tile. Grouped by dimension for the on-page Key. A test asserts
# every access/cadence/surface/kind value used in the catalog has an entry
# here, so a new label can't ship without an explanation.
GLOSSARY = [
    ("What it can do", [
        ("read-only", "Only looks at data — never changes anything."),
        ("advisory", "Reviews and reports; changes nothing itself."),
        ("recommend-only", "Diagnoses and suggests; a person makes the change."),
        ("edits · stops before commit", "Edits code, then stops so you can review before anything's saved."),
        ("writes", "Creates or updates data on its own."),
        ("reads + writes", "Can both look at and change data."),
        ("reads + tags", "Reads your data, and can label (tag) deposits."),
    ]),
    ("When it runs", [
        ("on-demand", "Only when you ask it, inside a Claude session."),
        ("always-on", "A running service — there whenever you need it."),
        ("daily · 06:30", "Automatically every morning at 6:30."),
        ("daily · 07:00", "Automatically every morning at 7:00."),
    ]),
    ("Where it runs", [
        ("Claude Code", "In your Claude Code sessions, on your computer."),
        ("cloud · Mon", "A scheduled run in the cloud, every Monday."),
        ("cloud · Fri", "A scheduled run in the cloud, every Friday."),
        ("tailnet", "Over your private Tailscale network."),
        ("Pi timer", "A scheduled job on the Raspberry Pi."),
        ("in-app", "Built right into the Ledger app."),
        ("dev copy", "On a throwaway copy of the data (dev.db), never the live one."),
    ]),
    ("Type", [
        ("subagent", "A role-scoped Claude agent you invoke in a session."),
        ("assistant", "A conversational AI surface inside the app."),
        ("service", "An always-running background process."),
        ("routine", "A scheduled, automated job."),
    ]),
]


def glossary_terms():
    """Every term the Key explains — the coherence test checks the catalog's
    labels are a subset of this."""
    return {term for _, pairs in GLOSSARY for term, _ in pairs}


_MODEL_RE = re.compile(r"^model:\s*(\S+)", re.M)


def _subagent_model(stem):
    """The model a subagent file declares, or 'inherits' when unset (most
    inherit the session model). Read from the file so it can't drift."""
    path = AGENTS_DIR / f"{stem}.md"
    if not path.exists():
        return "inherits"
    m = _MODEL_RE.search(path.read_text())
    return m.group(1) if m else "inherits"


def agent_files():
    """The .claude/agents/*.md stems actually on disk — the coherence check
    compares this to SUBAGENTS."""
    if not AGENTS_DIR.exists():
        return set()
    return {p.stem for p in AGENTS_DIR.glob("*.md")}


def _entries():
    entries = []
    for stem, meta in SUBAGENTS.items():
        entries.append({
            "id": stem, "kind": "subagent",
            "model": _subagent_model(stem),
            "cadence": "on-demand", **meta})
    entries.extend(SERVICES)
    return entries


def catalog():
    """The autonomous layer grouped for the tiled Agents view. A pure
    function of the code + the agent files — no db, no live state (v1)."""
    entries = _entries()
    by_group = {gid: [] for gid, _ in GROUPS}
    for e in entries:
        by_group[e["group"]].append(e)
    return {
        "live_status": False,   # v1 is catalog-only; a later increment flips this
        "groups": [{"id": gid, "label": label, "agents": by_group[gid]}
                   for gid, label in GROUPS],
        "glossary": [{"label": label,
                      "terms": [{"term": t, "gloss": g} for t, g in pairs]}
                     for label, pairs in GLOSSARY],
    }
