"""find_doubles.py: the read-only report of rent and utility payments Ledger
may be counting twice. Pins that it never writes (the file is byte-identical
afterwards), that it finds the three kinds of double (Mark paid + bank charge,
a Venmo for rent already counted, the same amount twice), that a bill paid by
LINKING the bank charge is not a double, and that each "what the fix does"
line comes from running the real verbs on a copy."""
import contextlib
import hashlib
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import _seedbase

import actions
import find_doubles


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class FindDoublesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="ledger-doubles-test-")
        self.db_path = Path(self.tmp.name) / "test.db"
        _seedbase.seed_into(self.db_path, seed=23, months=2)
        self.db = sqlite3.connect(self.db_path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.rent = actions.create_bill(self.db, "ui:test", {
            "name": "Apartment rent", "amount": "2655.00", "due_day": 1,
            "category": "Rent"})
        self.power = actions.create_bill(self.db, "ui:test", {
            "name": "Electric & gas (Public Grid)", "amount": "60.00",
            "due_day": 15, "category": "Utilities"})
        self.n = 0

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def charge(self, txn_date, amount, description, category="Bills",
               source="simplefin", direction="out"):
        self.n += 1
        return actions.record_transaction(self.db, "sync", {
            "date": txn_date, "amount": amount, "description": description,
            "category": category, "paid_by": 1, "is_shared": True,
            "payer_share_pct": 50,
        }, source=source, external_id=f"dbl-{self.n}" if source == "simplefin" else None,
            direction=direction)

    def run_cli(self, start="2030-01-25", end="2030-02-28"):
        self.db.commit()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = find_doubles.main(["--from", start, "--to", end,
                                      "--db", str(self.db_path)])
        return code, out.getvalue(), err.getvalue()

    def test_clean_window_reports_nothing(self):
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Nothing counted twice found", out)

    def test_mark_paid_plus_bank_charge_is_found_and_nothing_written(self):
        bank = self.charge("2030-02-01", "2655.00", "AVALON COMMUNITIES RENT", "Rent")
        actions.mark_bill_paid(self.db, "ui:test", self.rent["id"],
                               {"period": "2030-02"}, 1)
        before = sha(self.db_path)
        code, out, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertEqual(before, sha(self.db_path), "the report wrote to the db")
        self.assertIn("1. Bills marked paid that the bank also imported: 1", out)
        self.assertIn(f"link #{bank['id']}", out)
        # The fix (Undo + link) removes the logged copy from that month's spend.
        self.assertRegex(out, r"What the fix does: \d{4}-\d{2} spending: .* -> ")
        # The pair is explained by check 1, so check 3 doesn't repeat it.
        self.assertIn("utility charge: 0", out)

    def test_linked_bank_charge_is_not_a_double(self):
        bank = self.charge("2030-02-01", "2655.00", "AVALON COMMUNITIES RENT", "Rent")
        actions.link_bill_payment(self.db, "ui:test", self.rent["id"],
                                  {"period": "2030-02", "txn_id": bank["id"]})
        code, out, _ = self.run_cli()
        self.assertIn("1. Bills marked paid that the bank also imported: 0", out)
        self.assertIn(f"linked bank charge #{bank['id']}", out)

    def test_venmo_for_rent_already_counted(self):
        bank = self.charge("2030-02-01", "2655.00", "AVALON COMMUNITIES RENT", "Rent")
        actions.link_bill_payment(self.db, "ui:test", self.rent["id"],
                                  {"period": "2030-02", "txn_id": bank["id"]})
        venmo = self.charge("2030-01-30", "1327.50", "VENMO PAYMENT Portion of rent")
        self.charge("2030-02-03", "6.50", "VENMO coffee")
        code, out, _ = self.run_cli()
        self.assertIn("2. Person-to-person payments that still count as money: 2", out)
        self.assertIn(f"#{venmo['id']}", out)
        self.assertIn("LIKELY RENT (says rent; amount close to rent or half of it)", out)
        self.assertIn("probably not rent", out)
        self.assertIn("Mark as transfer", out)
        self.assertNotIn("Also possible", out)
        self.assertIn("What Mark as transfer does: 2030-01 spending:", out)
        # The real row is still not a transfer.
        row = self.db.execute("SELECT is_transfer FROM transactions WHERE id = ?",
                              (venmo["id"],)).fetchone()
        self.assertEqual(row["is_transfer"], 0)

    def test_venmo_is_never_offered_as_the_bills_charge(self):
        self.charge("2030-01-30", "1327.50", "VENMO PAYMENT Portion of rent")
        actions.mark_bill_paid(self.db, "ui:test", self.rent["id"],
                               {"period": "2030-02"}, 1)
        code, out, _ = self.run_cli()
        self.assertIn("1. Bills marked paid that the bank also imported: 0", out)
        self.assertIn("logged by Mark paid", out)

    def test_venmo_with_no_rent_in_ledger_says_leave_it(self):
        self.charge("2030-01-30", "1327.50", "VENMO PAYMENT Portion of rent")
        code, out, _ = self.run_cli()
        self.assertIn("none (no rent bill is marked paid", out)
        self.assertIn("may be the only record of it", out)

    def test_venmo_already_a_transfer_is_skipped(self):
        venmo = self.charge("2030-01-30", "1327.50", "VENMO PAYMENT Portion of rent")
        actions.set_transfer(self.db, "ui:test", venmo["id"], True)
        code, out, _ = self.run_cli()
        self.assertIn("still count as money: 0", out)

    def test_same_amount_twice_on_a_utility(self):
        self.charge("2030-02-14", "61.20", "PUBLIC GRID ENERGY", "Utilities")
        self.charge("2030-02-15", "61.20", "Electric bill", "Utilities", source="manual")
        self.charge("2030-02-15", "12.00", "LUNCH", "Food")
        self.charge("2030-02-16", "12.00", "LUNCH", "Food")
        code, out, _ = self.run_cli()
        self.assertIn("utility charge: 1", out)
        self.assertIn("if one is a copy, delete that one", out)

    def test_venmo_linked_as_the_rent_payment_is_not_a_double(self):
        venmo = self.charge("2030-02-01", "2655.00", "VENMO PAYMENT landlord rent")
        actions.link_bill_payment(self.db, "ui:test", self.rent["id"],
                                  {"period": "2030-02", "txn_id": venmo["id"]})
        code, out, _ = self.run_cli()
        self.assertIn("still count as money: 0", out)
        self.assertIn(f"linked bank charge #{venmo['id']}", out)

    def test_one_charge_is_offered_to_one_bill_only(self):
        internet = actions.create_bill(self.db, "ui:test", {
            "name": "Internet", "amount": "60.00", "due_day": 14,
            "category": "Utilities"})
        bank = self.charge("2030-02-15", "60.00", "PUBLIC GRID ENERGY", "Utilities")
        actions.mark_bill_paid(self.db, "ui:test", self.power["id"],
                               {"period": "2030-02"}, 1)
        actions.mark_bill_paid(self.db, "ui:test", internet["id"],
                               {"period": "2030-02"}, 1)
        code, out, _ = self.run_cli()
        self.assertIn("imported: 1", out)
        # The name match wins the charge over the amount-only bill.
        self.assertIn("Electric & gas (Public Grid), 2030-02", out)
        self.assertEqual(out.count(f"link #{bank['id']}"), 1)

    def test_known_merchant_beats_an_unrelated_exact_amount(self):
        jan = self.charge("2030-01-15", "131.00", "COTTAGE ENERGY AIRWALLEX", "Utilities")
        actions.link_bill_payment(self.db, "ui:test", self.power["id"],
                                  {"period": "2030-01", "txn_id": jan["id"]})
        actions.update_bill(self.db, "ui:test", self.power["id"], {"amount": "140.00"})
        self.assertEqual(self.db.execute("SELECT amount_cents FROM bills WHERE id = ?",
                                         (self.power["id"],)).fetchone()[0], 14000)
        energy = self.charge("2030-02-15", "88.40", "COTTAGE ENERGY AIRWALLEX", "Utilities")
        self.charge("2030-02-14", "140.00", "GYM MEMBERSHIP", "Health")
        actions.mark_bill_paid(self.db, "ui:test", self.power["id"],
                               {"period": "2030-02"}, 1)
        code, out, _ = self.run_cli()
        self.assertIn(f"Bank charge (same merchant as last time): #{energy['id']}", out)

    def test_bad_window_is_refused(self):
        code, _, err = self.run_cli(start="2030-03-01", end="2030-02-01")
        self.assertEqual(code, 2)
        self.assertIn("--from is after --to", err)

    def test_missing_db_is_refused(self):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = find_doubles.main(["--db", str(Path(self.tmp.name) / "nope.db")])
        self.assertEqual(code, 2)
        self.assertFalse((Path(self.tmp.name) / "nope.db").exists())


if __name__ == "__main__":
    unittest.main()
