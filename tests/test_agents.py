"""The Agents tab: the autonomous-layer catalog, its label glossary, and the
/api/agents endpoint (login-gated, shared with the household). Seeded members
are avery (id 1) and blake (id 2) — both can read it."""
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase
SEED_AS_OF = "2026-07-19"

import agent_catalog


class CatalogTests(unittest.TestCase):
    def test_subagents_match_agent_files(self):
        # Coherence: the curated subagent set == the actual .claude/agents/*.md
        # files. Add or remove an agent file without updating the catalog and
        # this fails — drift-tested single source for the agent roster.
        self.assertEqual(set(agent_catalog.SUBAGENTS), agent_catalog.agent_files())

    def test_catalog_structure_and_grouping(self):
        cat = agent_catalog.catalog()
        self.assertFalse(cat["live_status"])   # v1 is catalog-only
        self.assertEqual([g for g, _ in agent_catalog.GROUPS],
                         [grp["id"] for grp in cat["groups"]])
        fields = {"id", "kind", "name", "group", "icon", "access", "model",
                  "surface", "cadence", "tagline"}
        seen = 0
        for grp in cat["groups"]:
            for ag in grp["agents"]:
                self.assertTrue(fields <= set(ag), f"{ag.get('id')} missing fields")
                self.assertEqual(grp["id"], ag["group"])
                seen += 1
        self.assertEqual(len(agent_catalog.SUBAGENTS) + len(agent_catalog.SERVICES),
                         seen)

    def test_subagent_models_are_read_from_files(self):
        models = {ag["id"]: ag["model"] for grp in agent_catalog.catalog()["groups"]
                  for ag in grp["agents"]}
        self.assertEqual("sonnet", models["ledger-analyst"])   # only one pinned
        self.assertEqual("inherits", models["ledger-maintenance"])  # unset -> inherits

    def test_glossary_explains_every_label(self):
        # Every access/surface/cadence/kind pill shown must have a plain-language
        # entry in the Key. Add a new label without glossing it and this fails.
        cat = agent_catalog.catalog()
        used = set()
        for grp in cat["groups"]:
            for ag in grp["agents"]:
                used |= {ag["access"], ag["surface"], ag["cadence"], ag["kind"]}
        missing = used - agent_catalog.glossary_terms()
        self.assertEqual(set(), missing, f"unexplained labels: {missing}")
        # And the catalog ships the glossary the frontend renders as the Key.
        self.assertTrue(cat["glossary"] and cat["glossary"][0]["terms"])


class AgentsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-agents-")
        self.db_path = Path(self.tmp.name) / "route.db"
        _seedbase.seed_into(self.db_path, seed=61, months=1)
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "agents-route-secret")
        spec = importlib.util.spec_from_file_location("app_agents_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True

    def tearDown(self):
        self.tmp.cleanup()

    def client(self, user_id=None):
        c = self.app_module.app.test_client()
        if user_id is not None:
            with c.session_transaction() as s:
                s["user_id"] = user_id
        return c

    def test_any_member_gets_the_catalog(self):
        # Shared with the household — both members see it.
        for uid in (1, 2):
            r = self.client(uid).get("/api/agents")
            self.assertEqual(200, r.status_code)
            body = r.get_json()
            self.assertTrue(any(grp["agents"] for grp in body["groups"]))
            self.assertIn("glossary", body)

    def test_unauthenticated_is_401(self):
        r = self.client().get("/api/agents")
        self.assertEqual(401, r.status_code)


if __name__ == "__main__":
    unittest.main()
