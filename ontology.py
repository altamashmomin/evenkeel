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
import inspect
import re
from pathlib import Path

import actions
import derivations
from schema_runtime import REQUIRED_SCHEMA_VERSION

REPO = Path(__file__).resolve().parent

# One definition of "a raw SQL write" for the whole repo: the manifest's
# writes-scan and tests/test_architecture.py's invariant-1 scan import this.
WRITE_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+([a-z_]+)", re.IGNORECASE)

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


def _transitive_writes():
    """Governed tables each actions.py function writes, expanded through the
    module's internal call graph — so a verb that delegates its SQL to a
    helper is charged with the helper's writes. Cycle-safe."""
    fns = _module_functions(actions)
    sources = {name: inspect.getsource(fn) for name, fn in fns.items()}
    call_re = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in fns) + r")\s*\(")
    direct, calls = {}, {}
    for name, src in sources.items():
        direct[name] = {t.lower() for t in WRITE_RE.findall(src)
                        if t.lower() in actions.GOVERNED_TABLES}
        calls[name] = {c for c in call_re.findall(src) if c != name}

    memo = {}

    def resolve(name, seen):
        if name in memo:
            return memo[name]
        if name in seen:            # cycle guard: contribute only direct writes
            return direct[name]
        seen = seen | {name}
        tables = set(direct[name])
        for callee in calls[name]:
            tables |= resolve(callee, seen)
        memo[name] = tables
        return tables

    return {name: resolve(name, frozenset()) for name in fns}


def _two_phase_targets():
    """The verbs confirm_action dispatches to — derived from its source, so
    a new proposable action type is picked up when its dispatch lands."""
    verbs = _verbs()
    src = inspect.getsource(actions.confirm_action)
    call_re = re.compile(
        r"\b(" + "|".join(re.escape(n) for n in verbs) + r")\s*\(")
    return {c for c in call_re.findall(src) if c != "confirm_action"}


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
            "params": actions.param_schema(name)
                      if name in actions.PARAM_SPECS else None,
            "two_phase": name in two_phase,
            "registered": name in registry,
        })

    functions = [{"name": n, "reads_inflows": n in exempt}
                 for n in _functions()]

    return {
        "schema_version": REQUIRED_SCHEMA_VERSION,
        "objects": objects,
        "actions": acts,
        "functions": functions,
        "vocabularies": {
            "income_types": list(actions.REAL_INCOME_TYPE_ORDER),
            "item_kinds": list(actions.ITEM_KIND_ORDER),
            "item_statuses": list(actions.ITEM_STATUS_ORDER),
        },
        "doors": ["api", "mcp", "ask"],
    }
