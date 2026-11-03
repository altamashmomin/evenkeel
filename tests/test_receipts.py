"""Receipt scan (INVENTORY-DESIGN, receipt-scan amendment): the record_receipt
verb, the read-time suggestion/charge derivations, the photo reader (with a
fake model client — the suite never calls the API), and the two routes.

The load-bearing claim: saving a receipt touches the pantry only. No
transaction, split, bill payment or balance changes, ever."""
import importlib.util
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import derivations
import receipt_reader

ACTOR = "ui:avery"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

# Tables a receipt must never write. Everything money-shaped in the schema.
PANTRY_ONLY = {"items", "receipts", "receipt_lines", "audit_log", "schema_version",
               "sqlite_sequence"}


def snapshot_money(db):
    """Every row of every table outside the pantry, as plain data."""
    out = {}
    for (name,) in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"):
        if name in PANTRY_ONLY:
            continue
        out[name] = [tuple(r) for r in db.execute(f'SELECT * FROM "{name}" ORDER BY rowid')]
    return out


class ReceiptBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-receipts-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=51, months=1)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def item(self, name, store=None, **kw):
        row = actions.add_item(self.db, ACTOR, {"name": name, **kw})
        if store is not None:
            row = actions.set_item_store(self.db, ACTOR, row["id"], store)
        return row

    def txn(self, date, cents, desc, direction="out", source="simplefin",
            is_transfer=0):
        cur = self.db.execute(
            "INSERT INTO transactions (txn_date, amount_cents, description, "
            "category, paid_by, is_shared, source, created_at, direction, "
            "is_transfer) VALUES (?, ?, ?, 'Groceries', 1, 1, ?, "
            "'2026-10-01T00:00:00', ?, ?)",
            (date, cents, desc, source, direction, is_transfer))
        self.db.commit()
        return cur.lastrowid

    def save(self, lines, **kw):
        data = {"store": "ShopRite", "purchased_on": "2026-10-10",
                "total_cents": None, "lines": lines, **kw}
        return actions.record_receipt(self.db, ACTOR, data)

    def count(self, table):
        return self.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# --------------------------------------------------------------- the verb
class RecordReceiptTests(ReceiptBase):
    def test_restock_new_and_skip_in_one_save(self):
        milk = self.item("Milk", status="low")
        before = snapshot_money(self.db)
        result = self.save([
            {"raw_text": "GV 2% MLK GAL", "quantity": 1, "price_cents": 399,
             "action": "item", "item_id": milk["id"]},
            {"raw_text": "BANANAS", "quantity": 2, "price_cents": 129,
             "action": "new", "name": "Bananas"},
            {"raw_text": "BAG FEE", "price_cents": 10, "action": "skip"},
        ], total_cents=538)
        self.assertEqual([milk["id"]], result["restocked"])
        self.assertEqual(1, len(result["added"]))
        self.assertEqual(1, result["skipped"])

        milk_now = self.db.execute("SELECT * FROM items WHERE id = ?", (milk["id"],)).fetchone()
        self.assertEqual("stocked", milk_now["status"])
        self.assertEqual("ShopRite", milk_now["store"])          # filled (was empty)
        bananas = self.db.execute("SELECT * FROM items WHERE id = ?",
                                  (result["added"][0],)).fetchone()
        self.assertEqual(("Bananas", "staple", "stocked", "ShopRite", 1),
                         (bananas["name"], bananas["kind"], bananas["status"],
                          bananas["store"], bananas["active"]))

        lines = self.db.execute(
            "SELECT raw_text, quantity, price_cents, item_id FROM receipt_lines "
            "WHERE receipt_id = ? ORDER BY position", (result["receipt"]["id"],)).fetchall()
        self.assertEqual([("GV 2% MLK GAL", 1, 399, milk["id"]),
                          ("BANANAS", 2, 129, bananas["id"]),
                          ("BAG FEE", None, 10, None)], [tuple(r) for r in lines])
        self.assertEqual(538, result["receipt"]["total_cents"])
        self.assertEqual(ACTOR, result["receipt"]["created_by"])

        # Audited: the receipt, the restock, the new item, the store fill.
        verbs = self.db.execute(
            "SELECT target FROM audit_log WHERE action = 'record_receipt' "
            "ORDER BY id").fetchall()
        targets = [r["target"] for r in verbs]
        self.assertIn(f"receipt:{result['receipt']['id']}", targets)
        self.assertGreaterEqual(targets.count(f"item:{milk['id']}"), 2)  # status + store
        self.assertIn(f"item:{bananas['id']}", targets)

        self.assertEqual(before, snapshot_money(self.db))       # no money moved

    def test_bad_line_saves_nothing(self):
        milk = self.item("Milk", status="low")
        items_before = self.count("items")
        good = {"raw_text": "MILK", "action": "item", "item_id": milk["id"]}
        for bad, exc in [
            ({"raw_text": "X", "action": "item", "item_id": 99999}, actions.NotFound),
            ({"raw_text": "X", "action": "eat"}, ValueError),
            ({"raw_text": "", "action": "skip"}, ValueError),
            ({"raw_text": "X", "action": "new", "name": " "}, ValueError),
            ({"raw_text": "X", "action": "skip", "quantity": 0}, ValueError),
            ({"raw_text": "X", "action": "skip", "price_cents": 1.5}, ValueError),
            ({"raw_text": "X", "action": "skip", "price_cents": True}, ValueError),
        ]:
            with self.subTest(bad=bad), self.assertRaises(exc):
                self.save([{"raw_text": "NEW THING", "action": "new", "name": "Thing"},
                           good, bad])
        self.assertEqual(0, self.count("receipts"))
        self.assertEqual(0, self.count("receipt_lines"))
        self.assertEqual(items_before, self.count("items"))
        self.assertEqual("low", self.db.execute(
            "SELECT status FROM items WHERE id = ?", (milk["id"],)).fetchone()[0])

    def test_top_level_validation(self):
        line = [{"raw_text": "X", "action": "skip"}]
        for data in [
            {"store": "", "lines": line},
            {"store": "ShopRite", "lines": []},
            {"store": "ShopRite", "lines": "nope"},
            {"store": "ShopRite", "lines": line, "purchased_on": "10/10/2026"},
            {"store": "ShopRite", "lines": line, "total_cents": -1},
            {"store": "ShopRite", "lines": [{"raw_text": "X", "action": "skip"}] * 151},
        ]:
            with self.subTest(data=str(data)[:60]), self.assertRaises(ValueError):
                actions.record_receipt(self.db, ACTOR, data)
        with self.assertRaises(ValueError):
            actions.record_receipt(self.db, ACTOR, ["not", "a", "dict"])

    def test_double_save_is_refused(self):
        lines = [{"raw_text": "MILK", "action": "skip"},
                 {"raw_text": "EGGS", "action": "skip"}]
        self.save(lines, total_cents=700)
        with self.assertRaises(ValueError) as cm:
            self.save(lines, total_cents=700, store="shoprite")
        self.assertIn("already saved", str(cm.exception))
        self.save(lines, total_cents=701)                 # a different receipt is fine
        self.assertEqual(2, self.count("receipts"))

    def test_existing_store_is_left_alone(self):
        coffee = self.item("Coffee", store="Costco", status="out")
        result = self.save([{"raw_text": "COFFEE", "action": "item",
                             "item_id": coffee["id"]}])
        self.assertEqual([], result["stores_set"])
        self.assertEqual("Costco", self.db.execute(
            "SELECT store FROM items WHERE id = ?", (coffee["id"],)).fetchone()[0])

    def test_new_names_never_make_twins(self):
        eggs = self.item("Eggs", status="out")
        result = self.save([
            {"raw_text": "BREAD WHT", "action": "new", "name": "Bread"},
            {"raw_text": "BREAD WHT", "action": "new", "name": "bread"},
            {"raw_text": "LG EGGS", "action": "new", "name": "eggs"},
        ])
        self.assertEqual(1, len(result["added"]))
        self.assertEqual([eggs["id"]], result["restocked"])   # existing item restocked
        self.assertEqual(1, self.db.execute(
            "SELECT COUNT(*) FROM items WHERE lower(name) = 'bread'").fetchone()[0])
        self.assertEqual("stocked", self.db.execute(
            "SELECT status FROM items WHERE id = ?", (eggs["id"],)).fetchone()[0])
        ids = [r[0] for r in self.db.execute(
            "SELECT item_id FROM receipt_lines ORDER BY position")]
        self.assertEqual([result["added"][0], result["added"][0], eggs["id"]], ids)

    def test_oneoff_on_a_receipt_is_bought(self):
        cake = self.item("Birthday cake", kind="oneoff")
        self.save([{"raw_text": "CAKE", "action": "item", "item_id": cake["id"]}])
        self.assertEqual(0, self.db.execute(
            "SELECT active FROM items WHERE id = ?", (cake["id"],)).fetchone()[0])

    def test_charge_link_rules(self):
        ok = self.txn("2026-10-11", 538, "SHOPRITE 312")
        inflow = self.txn("2026-10-10", 538, "Refund", direction="in")
        transfer = self.txn("2026-10-10", 538, "Move", is_transfer=1)
        bill = self.txn("2026-10-10", 538, "Rent", source="bill")
        far = self.txn("2026-10-20", 538, "SHOPRITE 312")
        line = [{"raw_text": "MILK", "action": "skip"}]
        for txn_id, exc in [(inflow, ValueError), (transfer, ValueError),
                            (bill, ValueError), (far, ValueError),
                            (99999, actions.NotFound)]:
            with self.subTest(txn_id=txn_id), self.assertRaises(exc):
                self.save(line, txn_id=txn_id)
        before = snapshot_money(self.db)
        saved = self.save(line, txn_id=ok, total_cents=538)
        self.assertEqual(ok, saved["receipt"]["txn_id"])
        self.assertEqual(before, snapshot_money(self.db))   # linking moves nothing
        with self.assertRaises(ValueError) as cm:           # one receipt per charge
            self.save([{"raw_text": "EGGS", "action": "skip"}], txn_id=ok)
        self.assertIn("another receipt", str(cm.exception))

    def test_balance_is_unchanged(self):
        balance_before = derivations.compute_balance(self.db)
        milk = self.item("Milk", status="low")
        after_item = derivations.compute_balance(self.db)
        self.assertEqual(balance_before, after_item)
        self.save([{"raw_text": "MILK", "action": "item", "item_id": milk["id"],
                    "price_cents": 399}], total_cents=399)
        self.assertEqual(balance_before, derivations.compute_balance(self.db))


# --------------------------------------------------------- the derivations
class ReceiptDerivationTests(ReceiptBase):
    def suggest(self, lines, store="ShopRite"):
        return derivations.receipt_line_suggestions(self.db, store, lines)

    def test_memory_beats_everything_and_prefers_this_store(self):
        milk = self.item("Milk")
        oat = self.item("Oat milk")
        self.save([{"raw_text": "GV MLK GAL", "action": "item", "item_id": milk["id"]}],
                  store="Costco")
        line = {"raw_text": "gv mlk, gal", "name": "Oat milk", "kind": "product",
                "item_hint": oat["id"]}
        self.assertEqual(("item", milk["id"], "remembered"),
                         tuple(self.suggest([line])[0][k] for k in ("action", "item_id", "source")))
        self.save([{"raw_text": "GV MLK GAL", "action": "item", "item_id": oat["id"]}],
                  purchased_on="2026-10-11")
        self.assertEqual(oat["id"], self.suggest([line])[0]["item_id"])           # this store
        self.assertEqual(milk["id"], self.suggest([line], store="COSTCO")[0]["item_id"])

    def test_remembered_skip_and_gone_items(self):
        self.save([{"raw_text": "CRV DEPOSIT", "action": "skip"}])
        pick = self.suggest([{"raw_text": "CRV DEPOSIT", "name": "Deposit",
                              "kind": "product"}])[0]
        self.assertEqual(("skip", "remembered"), (pick["action"], pick["source"]))
        cake = self.item("Cake", kind="oneoff")
        self.save([{"raw_text": "CAKE", "action": "item", "item_id": cake["id"]}],
                  purchased_on="2026-10-12")                     # archives the one-off
        pick = self.suggest([{"raw_text": "CAKE", "name": "Cake", "kind": "product"}])[0]
        self.assertEqual(("new", "Cake"), (pick["action"], pick["name"]))

    def test_hint_then_name_then_new_or_skip(self):
        milk = self.item("Milk")
        oat = self.item("Oat milk")
        picks = self.suggest([
            {"raw_text": "A", "name": "whatever", "kind": "product", "item_hint": milk["id"]},
            {"raw_text": "B", "name": "oat milk", "kind": "product", "item_hint": 99999},
            {"raw_text": "SILK OAT MILK 64OZ", "name": "Oat milk, 64 oz", "kind": "product"},
            {"raw_text": "C", "name": "Apples", "kind": "product"},
            {"raw_text": "BAG FEE", "name": "Bag fee", "kind": "fee"},
            {"raw_text": "COUPON", "name": "", "kind": "product"},
        ])
        self.assertEqual([("item", milk["id"], "guess"), ("item", oat["id"], "name"),
                          ("item", oat["id"], "name"), ("new", None, "new"),
                          ("skip", None, "not an item"), ("skip", None, "not an item")],
                         [(p["action"], p["item_id"], p["source"]) for p in picks])
        self.assertEqual([], derivations.receipt_line_suggestions(self.db))

    def test_store_names_and_canonical(self):
        self.item("Milk", store="ShopRite")
        self.item("Eggs", store="ShopRite")
        self.item("Rice", store="SHOPRITE")
        self.item("Wine", store="Trader Joe's")
        self.assertEqual(["ShopRite", "Trader Joe's"],
                         derivations.receipt_store_names(self.db))
        canon = derivations.receipt_canonical_store
        self.assertEqual("ShopRite", canon(self.db, "SHOPRITE OF PISCATAWAY"))
        self.assertEqual("Trader Joe's", canon(self.db, "TRADER JOES #551"))
        self.assertEqual("Wegmans", canon(self.db, "Wegmans"))
        self.assertIsNone(canon(self.db))

    def test_charge_candidates_rank_and_exclude(self):
        both = self.txn("2026-10-11", 538, "SHOPRITE 312 PISCATAWAY")
        exact = self.txn("2026-10-09", 538, "POS PURCHASE 4471")
        named = self.txn("2026-10-10", 1999, "ShopRite #312")
        self.txn("2026-10-10", 538, "ShopRite refund", direction="in")
        self.txn("2026-10-10", 538, "ShopRite xfer", is_transfer=1)
        self.txn("2026-10-10", 538, "ShopRite settle", source="settlement")
        self.txn("2026-10-10", 777, "Gas station")
        self.txn("2026-10-20", 538, "ShopRite later")
        got = derivations.receipt_charge_candidates(self.db, "ShopRite", "2026-10-10", 538)
        self.assertEqual([both, exact, named], [c["txn_id"] for c in got])
        self.assertEqual((True, True, 1), (got[0]["exact_total"], got[0]["store_match"],
                                           got[0]["days_from_receipt"]))
        self.save([{"raw_text": "X", "action": "skip"}], txn_id=both)
        got = derivations.receipt_charge_candidates(self.db, "ShopRite", "2026-10-10", 538)
        self.assertNotIn(both, [c["txn_id"] for c in got])
        self.assertEqual([], derivations.receipt_charge_candidates(self.db))
        self.assertEqual([], derivations.receipt_charge_candidates(self.db, "X", "bad"))

    def test_totals_check(self):
        check = derivations.receipt_totals_check
        lines = [{"price_cents": 399}, {"price_cents": 258}, {"price_cents": -100}]
        self.assertEqual({"lines_total_cents": 557, "missing_prices": 0,
                          "expected_cents": 600, "difference_cents": 0, "adds_up": True},
                         check(lines, 43, 600))
        self.assertFalse(check(lines, 43, 650)["adds_up"])
        self.assertEqual(50, check(lines, 43, 650)["difference_cents"])
        self.assertIsNone(check(lines + [{"price_cents": None}], 43, 600)["adds_up"])
        self.assertIsNone(check(lines, None, None)["adds_up"])

    def test_text_keys(self):
        self.assertEqual("gv 2% mlk gal", derivations.receipt_text_key("GV 2% MLK  GAL."))
        self.assertEqual("traderjoes", derivations.receipt_store_key("Trader Joe's"))


# ------------------------------------------------------------- the reader
def tool_response(payload, stop_reason="tool_use"):
    return SimpleNamespace(stop_reason=stop_reason, content=[
        SimpleNamespace(type="text", text="ok"),
        SimpleNamespace(type="tool_use", name="record_receipt", input=payload)])


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []
        self.messages = self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


GOOD = {"readable": True, "store": "ShopRite", "purchased_on": "2026-10-10",
        "subtotal": "5.38", "tax": "0.00", "total": "$5.38",
        "lines": [{"raw_text": "GV 2% MLK GAL", "name": "2% milk, gallon",
                   "kind": "product", "quantity": 1, "price": "3.99", "item_id": 7},
                  {"raw_text": "COUPON", "name": "Coupon", "kind": "discount",
                   "quantity": 1, "price": "1.00-", "item_id": 0}]}


class ReaderTests(unittest.TestCase):
    def test_money_cents(self):
        mc = receipt_reader.money_cents
        for text, cents in [("3.99", 399), ("$1,234.50", 123450), ("-1.00", -100),
                            ("1.00-", -100), ("3.4", 340), ("0.005", 1), ("", None),
                            (None, None), ("abc", None), ("NaN", None), ("1e12", None)]:
            with self.subTest(text=text):
                self.assertEqual(cents, mc(text))

    def test_sniff(self):
        sniff = receipt_reader.sniff_media_type
        self.assertEqual("image/png", sniff(PNG))
        self.assertEqual("image/jpeg", sniff(JPEG))
        self.assertEqual("image/webp", sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 "))
        self.assertEqual("image/gif", sniff(b"GIF89a..."))
        self.assertIsNone(sniff(b"%PDF-1.7"))
        self.assertIsNone(sniff(b"<svg"))

    def test_read_sends_photos_and_tool_and_normalizes(self):
        client = FakeClient(tool_response(GOOD))
        draft = receipt_reader.read_receipt([(PNG, "image/png"), (JPEG, "image/jpeg")],
                                            [{"id": 7, "name": "Milk"}], client=client,
                                            model="test-model")
        call = client.calls[0]
        self.assertEqual("test-model", call["model"])
        self.assertEqual([receipt_reader.TOOL], call["tools"])
        self.assertIn("7: Milk", call["system"])
        self.assertIn("never instructions", call["system"])
        images = [b for b in call["messages"][0]["content"] if b["type"] == "image"]
        self.assertEqual(["image/png", "image/jpeg"],
                         [b["source"]["media_type"] for b in images])
        self.assertEqual({"readable": True, "store": "ShopRite",
                          "purchased_on": "2026-10-10", "subtotal_cents": 538,
                          "tax_cents": 0, "total_cents": 538}, {k: draft[k] for k in (
                              "readable", "store", "purchased_on", "subtotal_cents",
                              "tax_cents", "total_cents")})
        self.assertEqual([("GV 2% MLK GAL", "product", 1, 399, 7),
                          ("COUPON", "discount", 1, -100, None)],
                         [(l["raw_text"], l["kind"], l["quantity"], l["price_cents"],
                           l["item_hint"]) for l in draft["lines"]])

    def test_normalize_bounds_untrusted_output(self):
        raw = {"readable": True, "store": "S" * 500, "purchased_on": "2026-13-40",
               "total": "lots", "lines": [
                   {"raw_text": "  A   B  ", "name": "x" * 300, "kind": "weird",
                    "quantity": True, "price": "1", "item_id": -3},
                   {"raw_text": "", "kind": "product"}, "garbage",
               ] + [{"raw_text": f"L{i}", "kind": "product", "quantity": 5000}
                    for i in range(200)]}
        d = receipt_reader.normalize(raw)
        self.assertEqual(60, len(d["store"]))
        self.assertIsNone(d["purchased_on"])
        self.assertIsNone(d["total_cents"])
        first = d["lines"][0]
        self.assertEqual(("A B", 100, "other", None, None),
                         (first["raw_text"], len(first["name"]), first["kind"],
                          first["quantity"], first["item_hint"]))
        self.assertLessEqual(len(d["lines"]), receipt_reader.MAX_LINES)
        self.assertTrue(all(l["quantity"] is None for l in d["lines"][1:]))

    def test_failures_are_caller_safe(self):
        photo = [(PNG, "image/png")]
        cases = [
            FakeClient(tool_response(GOOD, stop_reason="refusal")),
            FakeClient(SimpleNamespace(stop_reason="end_turn",
                                       content=[SimpleNamespace(type="text", text="hi")])),
            FakeClient(tool_response({"readable": False, "lines": []})),
            FakeClient(error=type("APITimeoutError", (Exception,), {})("secret detail")),
            FakeClient(error=type("RateLimitError", (Exception,), {})("secret detail")),
            FakeClient(error=RuntimeError("sk-ant-secret in a message")),
        ]
        for client in cases:
            with self.subTest(), self.assertRaises(receipt_reader.ReadFailed) as cm:
                receipt_reader.read_receipt(photo, [], client=client)
            self.assertNotIn("secret", str(cm.exception))

    def test_no_key_is_not_configured(self):
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            with self.assertRaises(receipt_reader.NotConfigured):
                receipt_reader.read_receipt([(PNG, "image/png")], [])


# ------------------------------------------------------------- the routes
class ReceiptRouteTests(ReceiptBase):
    def setUp(self):
        super().setUp()
        os.environ["DATABASE_PATH"] = str(self.db_path)
        os.environ.setdefault("SECRET_KEY", "receipt-route-secret")
        spec = importlib.util.spec_from_file_location("app_receipt_test", REPO / "app.py")
        self.app_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.app_module)
        self.app_module.app.config["TESTING"] = True
        self.app_module._RATE_BUCKETS.clear()

    def client(self, session=True):
        c = self.app_module.app.test_client()
        if session:
            with c.session_transaction() as s:
                s["user_id"] = 1
        return c

    def post_photos(self, c, *blobs):
        return c.post("/api/receipts/read", content_type="multipart/form-data",
                      data={"photo": [(io.BytesIO(b), f"r{i}.jpg")
                                      for i, b in enumerate(blobs)]})

    def fake_draft(self, milk_id):
        return {"readable": True, "store": "SHOPRITE #312",
                "purchased_on": "2026-10-10", "subtotal_cents": 538,
                "tax_cents": 0, "total_cents": 538,
                "lines": [{"raw_text": "GV MLK", "name": "Milk", "kind": "product",
                           "quantity": 1, "price_cents": 399, "item_hint": None},
                          {"raw_text": "BANANAS", "name": "Bananas", "kind": "product",
                           "quantity": 1, "price_cents": 139, "item_hint": None}]}

    def test_read_returns_a_prefilled_draft(self):
        milk = self.item("Milk", store="ShopRite", status="low")
        charge = self.txn("2026-10-10", 538, "SHOPRITE 312")
        with mock.patch.object(receipt_reader, "read_receipt",
                               return_value=self.fake_draft(milk["id"])) as read:
            res = self.post_photos(self.client(), JPEG, PNG)
        self.assertEqual(200, res.status_code, res.get_json())
        images, pantry = read.call_args[0]
        self.assertEqual(["image/jpeg", "image/png"], [m for _, m in images])
        self.assertIn({"id": milk["id"], "name": "Milk"}, pantry)
        body = res.get_json()
        self.assertEqual(("ShopRite", "SHOPRITE #312"), (body["store"], body["store_as_read"]))
        self.assertEqual({"cents": 538, "display": "$5.38"}, body["total"])
        self.assertEqual(["item", "new"], [l["suggestion"]["action"] for l in body["lines"]])
        self.assertTrue(body["check"]["adds_up"])
        self.assertEqual([charge], [c["txn_id"] for c in body["charges"]])
        self.assertIn("ShopRite", body["stores"])
        self.assertEqual(0, self.count("receipts"))            # reading saves nothing

    def test_read_input_checks(self):
        c = self.client()
        self.assertEqual(400, c.post("/api/receipts/read").status_code)
        self.assertEqual(400, self.post_photos(c, b"%PDF-1.7 not a photo").status_code)
        self.assertEqual(400, self.post_photos(c, JPEG, JPEG, JPEG, JPEG).status_code)
        with mock.patch.object(receipt_reader, "MAX_IMAGE_BYTES", 32):
            self.assertEqual(413, self.post_photos(c, JPEG).status_code)
        with mock.patch.object(self.app_module, "RECEIPT_UPLOAD_LIMIT", 100):
            res = self.post_photos(c, JPEG + b"\x00" * 500)
            self.assertEqual(413, res.status_code)
            self.assertIn("error", res.get_json())
        # The app-wide 64KB cap still holds on every other route.
        big = c.post("/api/inventory", data=b"x" * (70 * 1024),
                     content_type="application/json")
        self.assertEqual(413, big.status_code)

    def test_read_needs_a_session_and_maps_reader_errors(self):
        self.assertEqual(401, self.post_photos(self.client(session=False), JPEG).status_code)
        c = self.client()
        with mock.patch.object(receipt_reader, "read_receipt",
                               side_effect=receipt_reader.NotConfigured()):
            self.assertEqual(503, self.post_photos(c, JPEG).status_code)
        with mock.patch.object(receipt_reader, "read_receipt",
                               side_effect=receipt_reader.ReadFailed("try a clearer photo")):
            res = self.post_photos(c, JPEG)
        self.assertEqual((502, "try a clearer photo"), (res.status_code, res.get_json()["error"]))

    def test_read_is_rate_limited(self):
        c = self.client()
        with mock.patch.object(self.app_module, "RECEIPT_READ_MAX", 2), \
                mock.patch.object(receipt_reader, "read_receipt",
                                  return_value=self.fake_draft(0)) as read:
            codes = [self.post_photos(c, JPEG).status_code for _ in range(3)]
        self.assertEqual([200, 200, 429], codes)
        self.assertEqual(2, read.call_count)

    def test_save_route(self):
        milk = self.item("Milk", status="low")
        c = self.client()
        payload = {"store": "ShopRite", "purchased_on": "2026-10-10", "total_cents": 538,
                   "lines": [{"raw_text": "GV MLK", "action": "item", "item_id": milk["id"]},
                             {"raw_text": "BANANAS", "action": "new", "name": "Bananas"}]}
        self.assertEqual(401, self.client(session=False).post(
            "/api/receipts", json=payload).status_code)
        res = c.post("/api/receipts", json=payload)
        self.assertEqual(201, res.status_code, res.get_json())
        body = res.get_json()
        self.assertEqual([{"id": milk["id"], "name": "Milk"}], body["restocked"])
        self.assertEqual(["Bananas"], [a["name"] for a in body["added"]])
        self.assertEqual({"cents": 538, "display": "$5.38"}, body["receipt"]["total"])
        self.assertEqual(400, c.post("/api/receipts", json=payload).status_code)  # dupe
        missing = dict(payload, total_cents=1,
                       lines=[{"raw_text": "X", "action": "item", "item_id": 99999}])
        self.assertEqual(404, c.post("/api/receipts", json=missing).status_code)
        self.assertEqual(400, c.post("/api/receipts", json={"store": "x"}).status_code)


if __name__ == "__main__":
    unittest.main()
