import copy
import csv
import io
import json
import tempfile
import unittest
from pathlib import Path

from ko_booking.imports import LedgerError, inspect_csv, parse_csv
from ko_booking.ledger import Ledger


def sample(rows):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(["거래일", "거래처", "입출금", "금액", "부가세", "자료출처", "거래번호"])
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def record(name="문구점", amount="110000", vat="10000", ref="A1", direction="출금", date="2025-01-10"):
    return [date, name, direction, amount, vat, "합성자료", ref]


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "ledger.sqlite3"
        self.ledger = Ledger(self.path)
        self.ledger.settings("나의 사업장", 2025, "general")

    def preview(self, raw):
        return self.ledger.preview("synthetic.csv", raw, inspect_csv(raw)["mapping"], "합성자료")

    def import_rows(self, rows, decisions=None):
        preview = self.preview(sample(rows))
        self.ledger.commit(preview["token"], decisions or {})
        return self.ledger.state()["transactions"]

    def classify(self, tx, category="소모품비", **changes):
        parts = copy.deepcopy(tx["allocations"])
        for part in parts:
            part["category"] = category
        payload = {"revision": tx["revision"], "allocations": parts,
                   "link_kind": tx["link_kind"], "link_target": tx["link_target"]}
        payload.update(changes)
        return self.ledger.edit(tx["id"], payload)

    def test_original_bytes_and_row_are_preserved_and_same_file_is_idempotent(self):
        raw = sample([record(ref="00001")])
        p = self.preview(raw)
        self.assertEqual(self.ledger.commit(p["token"], {})["imported"], 1)
        self.assertEqual(self.ledger.commit(p["token"], {})["imported"], 0)
        p2 = self.preview(raw)
        self.assertEqual(self.ledger.commit(p2["token"], {})["imported"], 0)
        tx = self.ledger.state()["transactions"][0]
        self.assertEqual(tx["reference"], "00001")
        self.assertEqual(tx["raw_row"], record(ref="00001"))
        with self.ledger.connect() as db:
            self.assertEqual(db.execute("SELECT raw FROM batches").fetchone()[0], raw)

    def test_bad_rows_prevent_partial_commit(self):
        p = self.preview(sample([record(), record(date="2025-02-30", ref="B1")]))
        self.assertEqual(p["error_count"], 1)
        with self.assertRaises(LedgerError):
            self.ledger.commit(p["token"], {})
        self.assertEqual(self.ledger.state()["transactions"], [])

    def test_malformed_thousands_separator_is_not_silently_corrected(self):
        self.assertEqual(self.preview(sample([record(amount="11,00")]))["error_count"], 1)
        self.assertEqual(self.preview(sample([record(amount="110,000")]))["error_count"], 0)

    def test_reference_conflict_is_not_silently_skipped(self):
        self.import_rows([record()])
        p = self.preview(sample([record(amount="220000")]))
        self.assertEqual(p["error_count"], 1)
        with self.assertRaises(LedgerError):
            self.ledger.commit(p["token"], {})

    def test_reference_free_repeated_charge_requires_explicit_decision(self):
        p = self.preview(sample([record(ref=""), record(ref="")]))
        self.assertEqual(p["rows"][1]["status"], "possible_duplicate")
        with self.assertRaises(LedgerError):
            self.ledger.commit(p["token"], {})
        self.assertEqual(self.ledger.commit(p["token"], {"3": "keep"})["imported"], 2)

    def test_commit_rechecks_duplicates_against_concurrent_import(self):
        first = self.preview(sample([record(ref="")]))
        second = self.preview(sample([record(ref="", date="2025/01/10")]))
        self.ledger.commit(first["token"], {})
        with self.assertRaises(LedgerError):
            self.ledger.commit(second["token"], {})
        self.assertEqual(self.ledger.commit(second["token"], {"2": "skip"})["imported"], 0)

    def test_cp949_mapping_and_unknown_vat(self):
        raw = sample([record(vat="")]).decode().encode("cp949")
        inspection = inspect_csv(raw)
        self.assertEqual(inspection["encoding"], "cp949")
        rows, _ = parse_csv(raw, inspection["mapping"], "기본 출처")
        self.assertIsNone(rows[0]["vat_krw"])
        with self.assertRaises(LedgerError):
            parse_csv(raw, {**inspection["mapping"], "amount": "거래처"}, "출처")

    def test_split_invariants_audit_and_stale_revision(self):
        tx = self.import_rows([record(amount="330000", vat="30000")])[0]
        parts = [{"category": "소모품비", "amount_krw": "220000", "vat_krw": "20000", "note": "합성"},
                 {"category": "통신비", "amount_krw": "110000", "vat_krw": "10000", "note": "합성"}]
        updated = self.classify(tx, allocations=parts)
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(len(self.ledger.history(tx["id"])), 2)
        with self.assertRaises(LedgerError):
            self.classify(tx)
        broken = copy.deepcopy(parts)
        broken[0]["amount_krw"] = "220001"
        with self.assertRaises(LedgerError):
            self.classify(updated, allocations=broken)
        self.assertEqual(self.ledger.state()["summary"]["totals"]["expense"], "330000")
        self.assertEqual(len(self.ledger.history(tx["id"])), 2)

    def test_refund_and_duplicate_link_prevent_double_count(self):
        txs = self.import_rows([record(), record(name="부분취소", amount="-22000", vat="-2000", ref="REF"),
                                record(name="카드대금", ref="PAY")])
        lookup = {t["reference"]: t for t in txs}
        self.classify(lookup["A1"])
        refund = self.classify(lookup["REF"])
        self.assertIn(refund["id"], self.ledger.state()["summary"]["pending_ids"])
        self.classify(refund, link_kind="refund", link_target=lookup["A1"]["id"])
        self.classify(lookup["PAY"], link_kind="duplicate", link_target=lookup["A1"]["id"])
        summary = self.ledger.state()["summary"]
        self.assertEqual(summary["totals"]["expense"], "88000")
        self.assertEqual(summary["pending_ids"], [])
        self.assertEqual(len(summary["excluded_ids"]), 1)

    def test_refunds_cannot_exceed_original_gross_or_vat(self):
        txs = self.import_rows([record(), record(amount="-120000", vat="-10000", ref="REF1"),
                               record(amount="-22000", vat="-20000", ref="REF2")])
        lookup = {t["reference"]: t for t in txs}
        for name in ("REF1", "REF2"):
            with self.assertRaises(LedgerError):
                self.classify(lookup[name], link_kind="refund", link_target=lookup["A1"]["id"])

    def test_refund_classification_changes_require_review_again(self):
        lookup = {t["reference"]: t for t in self.import_rows([
            record(), record(amount="-22000", vat="-2000", ref="REF")])}
        parent = self.classify(lookup["A1"])
        self.classify(lookup["REF"], link_kind="refund", link_target=parent["id"])
        self.ledger.review(2025)
        self.classify(parent, "개인거래")
        state = self.ledger.state()
        self.assertFalse(state["summary"]["reviewed"])
        self.assertIn(lookup["REF"]["id"], state["summary"]["pending_ids"])

    def test_private_and_asset_do_not_inflate_expenses(self):
        lookup = {t["reference"]: t for t in self.import_rows([
            record(name="개인", ref="PRIVATE", vat=""), record(name="노트북", ref="ASSET"),
            record(name="매출", ref="SALE", direction="입금", amount="330000", vat="30000")])}
        self.classify(lookup["PRIVATE"], "개인거래")
        self.classify(lookup["ASSET"], "자산취득")
        self.classify(lookup["SALE"], "매출")
        summary = self.ledger.state()["summary"]
        self.assertEqual(summary["totals"], {"income": "330000", "expense": "0", "asset": "110000"})
        self.assertEqual(summary["vat_pending_ids"], [])

    def test_review_and_export_require_completeness_and_invalidate_on_change(self):
        tx = self.import_rows([record(vat="")])[0]
        with self.assertRaises(LedgerError):
            self.ledger.review(2025)
        tx = self.classify(tx)
        with self.assertRaises(LedgerError):
            self.ledger.export(2025)
        tx = self.classify(tx, allocations=[{"category": "소모품비", "amount_krw": "110000", "vat_krw": "0", "note": "확인"}])
        self.ledger.review(2025)
        exported = self.ledger.export(2025).decode("utf-8-sig")
        self.assertIn("110000", exported)
        self.assertTrue(Ledger(self.path).state()["summary"]["reviewed"])
        self.classify(tx, "기타경비")
        self.assertFalse(self.ledger.state()["summary"]["reviewed"])

    def test_transaction_export_includes_pending_and_escapes_formula_text(self):
        self.import_rows([record(name="=1+1", vat="")])
        rows = list(csv.reader(io.StringIO(self.ledger.export(2025, "transactions").decode("utf-8-sig"))))
        self.assertEqual(rows[1][1], "'=1+1")
        self.assertIn("미분류", rows[1])
        self.assertEqual(rows[1][6], "")

    def test_year_filter_does_not_merge_periods(self):
        rows = self.import_rows([record(), record(ref="NEXT", date="2026-01-10")])
        for tx in rows:
            self.classify(tx)
        self.assertEqual(self.ledger.state()["summary"]["totals"]["expense"], "110000")
        self.ledger.settings("합성사업장", 2026)
        self.assertEqual(self.ledger.state()["summary"]["count"], 1)

    def test_tax_type_is_explicit_and_year_specific(self):
        tx = self.import_rows([record(direction="입금", vat="")])[0]
        self.classify(tx, "매출")
        self.ledger.settings("나의 사업장", 2025, "unconfirmed")
        with self.assertRaises(LedgerError):
            self.ledger.review(2025)
        self.ledger.settings("나의 사업장", 2025, "exempt")
        self.ledger.review(2025)
        self.assertTrue(self.ledger.state()["summary"]["reviewed"])
        self.ledger.settings("나의 사업장", 2026)
        self.assertEqual(self.ledger.state()["settings"]["vat_mode"], "unconfirmed")
        self.ledger.settings("나의 사업장", 2025, "general")
        self.assertFalse(self.ledger.state()["summary"]["reviewed"])

    def test_simplified_and_exempt_income_uses_gross_expenses_follow_evidence(self):
        lookup = {t["reference"]: t for t in self.import_rows([
            record(direction="입금", ref="SALE"), record(ref="BUY")])}
        self.classify(lookup["SALE"], "매출")
        self.classify(lookup["BUY"])
        for mode in ("simplified", "exempt"):
            self.ledger.settings("사업장", 2025, mode)
            self.ledger.review(2025)
            rows = list(csv.DictReader(io.StringIO(self.ledger.export(2025).decode("utf-8-sig"))))
            income = next(r for r in rows if r["계정과목"] == "매출")
            expense = next(r for r in rows if r["계정과목"] == "소모품비")
            self.assertEqual((income["수입금액"], income["수입부가세"]), ("110000", ""))
            self.assertEqual((expense["비용금액"], expense["비용부가세"]), ("100000", "10000"))


if __name__ == "__main__":
    unittest.main()
