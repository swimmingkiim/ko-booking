import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from ko_booking.filing import prepare_filing, prepare_state
from ko_booking.imports import LedgerError, inspect_csv
from ko_booking.ledger import Ledger


class FilingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.settings("합성 사업장", 2025, "general")
        raw = ("거래일,거래처,입출금,금액,부가세,거래번호\n"
               "2025-01-01,가상 고객,입금,3300000,300000,A1\n"
               "2025-01-02,가상 상점,출금,110000,10000,B1\n").encode()
        preview = self.ledger.preview("synthetic.csv", raw, inspect_csv(raw)["mapping"], "합성자료")
        self.ledger.commit(preview["token"], {})
        for tx in self.ledger.state()["transactions"]:
            parts = copy.deepcopy(tx["allocations"])
            parts[0]["category"] = "매출" if tx["direction"] == "income" else "소모품비"
            self.ledger.edit(tx["id"], {"revision": tx["revision"], "allocations": parts,
                                       "link_kind": "", "link_target": None})

    def codes(self, snapshot):
        return {i["code"] for i in snapshot["blocking_issues"]}

    def test_totals_use_book_amount_and_preserve_provenance(self):
        snapshot = prepare_filing(self.path, 2025)
        totals = {g["category"]: g for g in snapshot["category_totals"]}
        self.assertEqual(totals["매출"]["book_amount_krw"], "3000000")
        self.assertEqual(totals["소모품비"]["book_amount_krw"], "100000")
        row = totals["매출"]["rows"][0]
        self.assertEqual(row["gross_krw"], "3300000")
        self.assertEqual(row["revision"], 2)
        self.assertIn(row["transaction_id"], {t["id"] for t in snapshot["transactions"]})
        self.assertEqual(snapshot["status"], "draft")
        self.assertFalse(snapshot["submission_ready"])

    def test_review_does_not_remove_unimplemented_filing_blockers(self):
        before = prepare_filing(self.path, 2025)
        self.assertIn("LEDGER_NOT_REVIEWED", self.codes(before))
        self.ledger.review(2025)
        after = prepare_filing(self.path, 2025)
        self.assertNotIn("LEDGER_NOT_REVIEWED", self.codes(after))
        self.assertTrue({"SPEC_IMPLEMENTATION_INCOMPLETE", "FULL_RETURN_UNSUPPORTED", "ADJUSTMENTS_UNCONFIRMED",
                         "INVENTORY_UNCONFIRMED", "BUSINESS_IDENTITY_MISSING"} <= self.codes(after))
        self.assertEqual(after["spec"]["sha256"], "4edf9c51f6f3f23520abfd9d3f085144fb85312275aa0ab8e3aacea0edb11cf3")
        self.assertTrue(after["spec"]["content_verified"])
        self.assertFalse(after["spec"]["submission_format_validated"])
        self.assertIn("C115400", after["spec"]["reviewed_scope"])
        self.assertFalse(after["submission_ready"])

    def test_identity_stable_but_changes_with_name_and_transaction_revision(self):
        before = prepare_filing(self.path, 2025)
        self.assertEqual(before["snapshot_id"], prepare_filing(self.path, 2025)["snapshot_id"])
        self.ledger.settings("다른 표시 이름", 2025)
        renamed = prepare_filing(self.path, 2025)
        self.assertEqual(before["ledger_fingerprint"], renamed["ledger_fingerprint"])
        self.assertNotEqual(before["snapshot_id"], renamed["snapshot_id"])
        tx = self.ledger.state()["transactions"][0]
        parts = copy.deepcopy(tx["allocations"])
        parts[0]["note"] = "증빙 재확인"
        self.ledger.edit(tx["id"], {"revision": tx["revision"], "allocations": parts,
                                   "link_kind": "", "link_target": None})
        self.assertNotEqual(renamed["snapshot_id"], prepare_filing(self.path, 2025)["snapshot_id"])

    def test_unknown_amount_is_not_converted_to_zero(self):
        state = self.ledger.state()
        state["summary"]["rows"][0]["book_amount_krw"] = None
        state["summary"]["vat_pending_ids"] = [state["summary"]["rows"][0]["transaction_id"]]
        snapshot = prepare_state(state)
        category = state["summary"]["rows"][0]["category"]
        self.assertIsNone(next(g for g in snapshot["category_totals"] if g["category"] == category)["book_amount_krw"])
        self.assertIn("VAT_PENDING", self.codes(snapshot))

    def test_missing_invalid_and_wrong_year_leave_database_untouched(self):
        missing = self.path.parent / "missing.sqlite3"
        with self.assertRaises(LedgerError):
            prepare_filing(missing, 2025)
        self.assertFalse(missing.exists())
        before = hashlib.sha256(self.path.read_bytes()).hexdigest()
        with self.assertRaises(LedgerError):
            prepare_filing(self.path, 2026)
        prepare_filing(self.path, 2025)
        self.assertEqual(before, hashlib.sha256(self.path.read_bytes()).hexdigest())
        bad = self.path.parent / "bad.sqlite3"
        bad.write_bytes(b"not a database")
        with self.assertRaises(LedgerError):
            prepare_filing(bad, 2025)

    def test_other_year_has_no_2025_spec_and_no_cross_year_rows(self):
        self.ledger.settings("합성 사업장", 2026)
        snapshot = prepare_filing(self.path, 2026)
        self.assertIsNone(snapshot["spec"])
        self.assertEqual(snapshot["transactions"], [])
        self.assertEqual(snapshot["category_totals"], [])
        self.assertTrue({"EMPTY_LEDGER", "UNSUPPORTED_YEAR", "VAT_MODE_UNCONFIRMED"} <= self.codes(snapshot))

    def test_cli_emits_diagnostics_and_does_not_create_source_catalog(self):
        catalog = self.path.parent / "catalog.sqlite3"
        result = subprocess.run([sys.executable, "-m", "ko_booking", "--db", str(catalog),
                                 "filing-prepare", "--year", "2025", "--ledger-db", str(self.path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["purpose"], "filing_preparation_diagnostic")
        self.assertFalse(catalog.exists())


if __name__ == "__main__":
    unittest.main()
