"""The ontology manifest: the whole system described as one artifact
(ONTOLOGY-MANIFEST-DESIGN). Computed from source on every call — the
"nothing derived is stored" law applied to metadata. Introspection only:
the manifest DESCRIBES objects, actions, functions, and vocabularies; it
never mediates a write, a read, or a permission (engine features
CORE-DESIGN refuses).

Every fact is derived, never hand-declared:
- objects        <- actions.GOVERNED_TABLES; creation provenance by scanning
                    migrations/ DDL (001 carries the full v1.0 baseline, so
                    every governed table maps to a real migration file)
- actions        <- public verbs in actions.py (def name(db, actor, ...)),
                    their writes by scanning each verb's source with WRITE_RE,
                    expanded through the module call graph so a verb that
                    delegates (delete_transaction -> delete_transaction_graph,
                    every verb -> _write_audit) is charged with its helpers'
                    writes too; params from PARAM_SPECS where declared
- two_phase      <- the verbs confirm_action actually dispatches to, found by
                    scanning its source — not a hand-kept mapping
- registered     <- CORE-DESIGN's action-registry table (same parse the
                    architecture test uses); the manifest reports it, the
                    test enforces it
- functions      <- public db-taking callables in derivations.py (the
                    tripwire's own discovery rule); reads_inflows from the
                    tripwire's EXEMPT set, parsed textually so production
                    code never imports test modules
- vocabularies   <- the ordered *_ORDER tuples in actions.py, referenced

WRITE_RE lives here (not in the architecture test) so the invariant-1 test
and the manifest share one definition of "a raw SQL write"."""
import ast
import inspect
import re
import textwrap
from pathlib import Path

import actions
import derivations
from schema_runtime import REQUIRED_SCHEMA_VERSION

REPO = Path(__file__).resolve().parent

# One definition of "a raw SQL write" for the whole repo: the manifest's
# writes-scan and tests/test_architecture.py's invariant-1 scan import this.
# Widened (CODE-REVIEW-2026-08-07 #11) to also catch the SQLite upsert forms
# (INSERT OR REPLACE/IGNORE INTO, REPLACE INTO) and schema DDL (DROP/ALTER TABLE
# — invariant 7's guard, previously uncovered). \s+ spans newlines, so the
# full-text scan in test_architecture catches a table name wrapped onto the next
# line. An optional leading "/'/[ lets a quoted identifier still be caught.
WRITE_RE = re.compile(
    r"\b(?:INSERT(?:\s+OR\s+\w+)?\s+INTO|REPLACE\s+INTO|UPDATE|DELETE\s+FROM"
    r"|DROP\s+TABLE|ALTER\s+TABLE)\s+[\"'\[]?([a-z_]+)", re.IGNORECASE)

_CREATE_RE_TMPL = r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?{table}\b"


def _module_functions(module):
    """Name -> function for everything DEFINED in the module (imports like
    actions' compute_balance are excluded by the __module__ check)."""
    return {name: fn for name, fn in inspect.getmembers(module, inspect.isfunction)
            if fn.__module__ == module.__name__}


def _verbs():
    """The action verbs: public functions in actions.py whose first two
    parameters are exactly (db, actor) — the registry contract."""
    out = {}
    for name, fn in _module_functions(actions).items():
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if params[:2] == ["db", "actor"]:
            out[name] = fn
    return out


# FROM/JOIN targets — the read-side counterpart of WRITE_RE, for charging
# derivations with the tables they SELECT. Same quoted-identifier tolerance.
READ_RE = re.compile(r"\b(?:FROM|JOIN)\s+[\"'\[]?([a-z_]+)", re.IGNORECASE)


def _code_only(fn):
    """A function's source with its docstring removed — so prose that mentions
    a verb ("…via propose_action/confirm_action (the MCP write tier)") or
    SQL-looking text can never register as a call or a table reference. This
    fixed a real bug: the old regex call-scan credited create_income_rule with
    confirm_action's writes off exactly such a docstring mention."""
    src = textwrap.dedent(inspect.getsource(fn))
    doc = ast.get_docstring(ast.parse(src).body[0], clean=False)
    return src.replace(doc, "", 1) if doc else src


def _called_names(src):
    """Names invoked as calls in `src`, via the AST — Call nodes only, so
    docstrings and comments can't contribute (the regex approach could not
    make that guarantee)."""
    out = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _closure_tables(module, regex, stop_at=frozenset()):
    """Per-function governed-table references matched by `regex` (over
    docstring-stripped source), expanded through the module's internal call
    graph (AST-detected calls) — so a function that delegates to a helper is
    charged with the helper's tables too. Cycle-safe.

    `stop_at` names callees NOT followed: passing the verb set makes a verb's
    result its DIRECT footprint (its own body + private helpers), leaving what
    it reaches by calling another verb to the cascade edges — the distinction
    the Trace Web draws. An empty stop_at gives the full transitive closure
    (what `written_by` reports)."""
    fns = _module_functions(module)
    sources = {name: _code_only(fn) for name, fn in fns.items()}
    direct, calls = {}, {}
    for name, src in sources.items():
        direct[name] = {t.lower() for t in regex.findall(src)
                        if t.lower() in actions.GOVERNED_TABLES}
        calls[name] = {c for c in _called_names(src)
                       if c in fns and c != name and c not in stop_at}

    memo = {}

    def resolve(name, seen):
        if name in memo:
            return memo[name]
        if name in seen:            # cycle guard: contribute only direct tables
            return direct[name]
        seen = seen | {name}
        tables = set(direct[name])
        for callee in calls[name]:
            tables |= resolve(callee, seen)
        memo[name] = tables
        return tables

    return {name: resolve(name, frozenset()) for name in fns}


def _with_reset_tables(writes):
    """reset_money DELETEs its tables via an f-string loop over RESET_TABLES —
    the one dynamic write WRITE_RE cannot see in the source text. The constant
    itself IS importable source, so charging the verb with it here stays
    derived-not-hand-kept (a table added to RESET_TABLES appears automatically)."""
    writes["reset_money"] = writes["reset_money"] | set(actions.RESET_TABLES)
    return writes


def _transitive_writes():
    """Governed tables each actions.py function writes — full closure (a verb
    that dispatches to another verb, like confirm_action, is charged with the
    target's writes too). Feeds objects[].written_by."""
    return _with_reset_tables(_closure_tables(actions, WRITE_RE))


def _direct_writes():
    """Governed tables each verb writes ITSELF (own body + private helpers,
    stopping at other public verbs). This is the write EDGE a diagram should
    draw for the verb — what it reaches by calling another verb is a cascade
    edge, not a write edge. Feeds actions[].writes_direct."""
    return _with_reset_tables(
        _closure_tables(actions, WRITE_RE, stop_at=frozenset(_verbs())))


def _derivation_reads():
    """Governed tables each public derivation reads (FROM/JOIN in its own body
    + everything it calls inside derivations.py — a derivation that reads via a
    helper or another derivation is charged transitively). Feeds
    functions[].reads."""
    return _closure_tables(derivations, READ_RE)


def _cascades():
    """verb -> verbs it calls (the internal dispatch edges), AST-detected so
    docstring prose can't register. Today confirm_action is the only cascader;
    a new one is picked up automatically."""
    verbs = _verbs()
    out = {}
    for name, fn in verbs.items():
        callees = {c for c in _called_names(_code_only(fn))
                   if c in verbs and c != name}
        if callees:
            out[name] = sorted(callees)
    return out


# ── caller derivation: which entry surface reaches which verb ────────────────

_ROUTE_SPLIT_RE = re.compile(r"@app\.(get|post|put|delete)\(\"([^\"]+)\"\)")
_PLACEHOLDER_RE = re.compile(r"<[^>]+>|\{[^}]+\}")


def _route_verbs():
    """(METHOD, normalized-path) -> verbs the app.py route handler references.
    Path params (<int:id> / f-string {…}) normalize to {} so the two spellings
    compare equal. References, not just calls: `actions.contribute_to_goal if
    cents > 0 else actions.withdraw_from_goal` counts (the paren-less form a
    call-only scan missed)."""
    text = (REPO / "app.py").read_text()
    verbs = set(_verbs())
    hits = list(_ROUTE_SPLIT_RE.finditer(text))
    out = {}
    for i, m in enumerate(hits):
        body = text[m.end(): hits[i + 1].start() if i + 1 < len(hits) else len(text)]
        found = {v for v in re.findall(r"actions\.([a-z_]+)\b", body)
                 if v in verbs}
        key = (m.group(1).upper(), _PLACEHOLDER_RE.sub("{}", m.group(2)))
        out[key] = out.get(key, set()) | found
    return out


_HTTP_CALL_RE = re.compile(
    r"(?:api_write|caller)\(\s*\n?\s*\"(GET|POST|PUT|DELETE)\",\s*f?\"([^\"]+)\"")


def _door_verbs(source_file):
    """Verbs an agent door reaches, derived by walking its HTTP calls through
    the app.py route table: scan the door's source for (method, path) pairs,
    normalize path params, and collect the matched routes' verbs. This is the
    honest mapping — the doors talk to ROUTES, and the routes name their verbs."""
    routes = _route_verbs()
    text = (REPO / source_file).read_text()
    out = set()
    for method, path in _HTTP_CALL_RE.findall(text):
        out |= routes.get((method, _PLACEHOLDER_RE.sub("{}", path)), set())
    return out


def _callers():
    """Entry surface -> verbs it can invoke, each derived from that surface's
    own source:
    - ui:   actions.<verb> references in app.py's route handlers
    - sync: the same scan of simplefin_sync.py
    - mcp / ask: the door's HTTP calls walked through the route table
      (ledger_mcp.py / agent_write_tools.py)
    - cli:  verbs reachable from NO surface — the maintenance escape hatch
      (today: reset_money, which deliberately has no route)."""
    verbs = set(_verbs())
    ui = set().union(*(_route_verbs().values() or [set()]))
    sync_text = (REPO / "simplefin_sync.py").read_text()
    sync = {v for v in re.findall(r"actions\.([a-z_]+)\b", sync_text)
            if v in verbs}
    mcp = _door_verbs("ledger_mcp.py")
    ask = _door_verbs("agent_write_tools.py")
    cli = verbs - (ui | sync | mcp | ask)
    return {
        "ui": sorted(ui),
        "sync": sorted(sync),
        "mcp": sorted(mcp),
        "ask": sorted(ask),
        "cli": sorted(cli),
    }


def _two_phase_targets():
    """The verbs confirm_action dispatches to — AST-derived from its source
    (docstring-proof), so a new proposable action type is picked up when its
    dispatch lands."""
    verbs = _verbs()
    return {c for c in _called_names(_code_only(actions.confirm_action))
            if c in verbs and c != "confirm_action"}


def _registry_verbs():
    """Backticked identifiers on CORE-DESIGN's markdown table rows — the
    same parse tests/test_architecture.py uses to enforce registration."""
    doc = (REPO / "docs" / "CORE-DESIGN.md").read_text()
    registry = set()
    for line in doc.splitlines():
        if line.lstrip().startswith("|"):
            registry |= set(re.findall(r"`([a-z][a-z_]*)`", line))
    return registry


def _creating_migration(table):
    """The earliest migration whose DDL creates the table. \\b after the
    name keeps 'transactions' from matching 'transactions_new' (002's
    rebuild scratch table). Returns None if no migration creates it —
    the coherence test requires that never happens."""
    pat = re.compile(_CREATE_RE_TMPL.format(table=re.escape(table)),
                     re.IGNORECASE)
    for path in sorted((REPO / "migrations").iterdir()):
        if path.suffix not in {".sql", ".py"}:
            continue
        if pat.search(path.read_text()):
            return path.name
    return None


def _exempt_derivations():
    """Names in the derivation tripwire's EXEMPT set — the derivations that
    read inflows ON PURPOSE. Parsed textually from the test file: the fact
    belongs to the test (its allowlist), and production code must not
    import test modules to learn it."""
    src = (REPO / "tests" / "test_derivation_tripwire.py").read_text()
    block = re.search(r"^EXEMPT = \{(.*?)^\}", src, re.S | re.M)
    return set(re.findall(r'"([a-z_]+)":', block.group(1))) if block else set()


def _functions():
    """Public db-taking derivations — exactly the tripwire's discovery rule,
    so a new aggregate appears in the manifest automatically."""
    out = []
    for name, fn in _module_functions(derivations).items():
        if name.startswith("_"):
            continue
        params = list(inspect.signature(fn).parameters)
        if params and params[0] == "db":
            out.append(name)
    return sorted(out)


def manifest():
    """The whole ontology as one JSON-serializable dict. No db argument on
    purpose: the ontology is a property of the CODE, not of any database's
    contents."""
    writes = _transitive_writes()
    direct = _direct_writes()
    reads = _derivation_reads()
    cascades = _cascades()
    callers = _callers()
    verbs = _verbs()
    registry = _registry_verbs()
    two_phase = _two_phase_targets()
    exempt = _exempt_derivations()

    objects = []
    for table in sorted(actions.GOVERNED_TABLES):
        objects.append({
            "name": table,
            "created_by_migration": _creating_migration(table),
            "written_by": sorted(v for v in verbs if table in writes[v]),
            "governed": True,
        })

    acts = []
    for name in sorted(verbs):
        acts.append({
            "name": name,
            "writes": sorted(writes[name]),
            # the verb's OWN write edges (body + private helpers, stopping at
            # other verbs) — what a diagram draws; `writes` stays the full
            # closure through cascades. `cascades` are the verb->verb edges.
            "writes_direct": sorted(direct[name]),
            "cascades": cascades.get(name, []),
            "params": actions.param_schema(name)
                      if name in actions.PARAM_SPECS else None,
            "two_phase": name in two_phase,
            "registered": name in registry,
        })

    functions = [{"name": n, "reads": sorted(reads[n]),
                  "reads_inflows": n in exempt}
                 for n in _functions()]

    return {
        "schema_version": REQUIRED_SCHEMA_VERSION,
        "objects": objects,
        "actions": acts,
        "functions": functions,
        # entry surface -> verbs it can invoke, each derived from that
        # surface's own source ('cli' = reachable from no surface).
        "callers": callers,
        "vocabularies": {
            "income_types": list(actions.REAL_INCOME_TYPE_ORDER),
            "item_kinds": list(actions.ITEM_KIND_ORDER),
            "item_statuses": list(actions.ITEM_STATUS_ORDER),
        },
        "doors": ["api", "mcp", "ask"],
    }
