import copy
import hashlib
import json
import subprocess
import sys
import unittest

import test_filing as fixtures
from ko_booking.filing import prepare_state
from ko_booking.filing_mapping import COST_INPUTS, EXPENSE_FIELDS, ADJUSTMENTS
from ko_booking.imports import LedgerError


class MappingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FilingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.ledger.review(2025)
        self.state = self.fixture.ledger.state()
        self.input = {
            "tax_year": 2025, "ledger_fingerprint": self.state["summary"]["fingerprint"],
            "business": {"registration_number": "0000000001", "industry_code": "940908", "income_code": "40",
                         "address": "합성 주소", "industry_name": "합성 업종", "period_start": "20250101",
                         "period_end": "20251231", "note": "자동 테스트 합성 입력"},
            "expense_mapping": {"소모품비": 36},
            "costs": {str(n): {"value": "0", "note": "해당 없음 확인"} for n in COST_INPUTS},
            "additional_expenses": {str(n): {"value": "0", "note": "장부 외 금액 없음 확인"} for n in EXPENSE_FIELDS},
            "adjustments": {str(n): {"value": "0", "note": "해당 없음 확인"} for n in ADJUSTMENTS},
        }

    def output(self, request=None):
        return prepare_state(self.state, self.input if request is None else request)

    def values(self, output, code):
        return next(r["values"] for r in output["candidate_records"] if r["form_code"] == code)

    def test_book_amounts_manual_costs_adjustments_and_provenance(self):
        self.input["costs"]["11"]["value"] = "10000"
        self.input["costs"]["12"]["value"] = "50000"
        self.input["costs"]["13"]["value"] = "20000"
        self.input["adjustments"]["9"]["value"] = "1000"
        self.input["adjustments"]["10"]["value"] = "3000"
        self.input["additional_expenses"]["33"]["value"] = "9000"
        out = self.output()
        d = self.values(out, "C115400"); i = self.values(out, "C115300")
        self.assertEqual((d["8"], d["14"], d["29"], d["30"]), ("3000000", "40000", "109000", "149000"))
        self.assertEqual((i["8"], i["11"], i["12"], i["19"]), ("3000000", "3002000", "149000", "2853000"))
        rec = next(r for r in out["candidate_records"] if r["form_code"] == "C115400")
        self.assertEqual(rec["provenance"]["36"]["rows"][0]["book_amount_krw"], "100000")
        self.assertFalse(out["submission_ready"])
        self.assertIn("CROSS_RECORD_MAPPING_PROVISIONAL", {x["code"] for x in out["blocking_issues"]})
        self.assertNotIn("BUSINESS_IDENTITY_MISSING", {x["code"] for x in out["blocking_issues"]})

    def test_missing_manual_values_propagate_null_instead_of_zero(self):
        del self.input["costs"]["11"]
        del self.input["additional_expenses"]["33"]
        del self.input["adjustments"]["9"]
        out = self.output()
        d = self.values(out, "C115400"); i = self.values(out, "C115300")
        self.assertIsNone(d["14"]); self.assertIsNone(d["29"]); self.assertIsNone(i["19"])
        self.assertTrue({"COST_INPUT_MISSING", "ADJUSTMENT_INPUT_MISSING", "ADDITIONAL_EXPENSE_UNCONFIRMED"}
                        <= {x["code"] for x in out["blocking_issues"]})

    def test_stale_year_and_period_are_rejected(self):
        for path, value in (("ledger_fingerprint", "stale"), ("tax_year", 2026)):
            request = copy.deepcopy(self.input); request[path] = value
            with self.assertRaises(LedgerError): self.output(request)
        self.input["business"]["period_start"] = "20250301"
        with self.assertRaises(LedgerError): self.output()

    def test_invalid_amounts_notes_and_field_keys_are_rejected(self):
        for value in (True, 1, "1.2", "-1", "1,000", "9" * 14):
            request = copy.deepcopy(self.input); request["costs"]["11"]["value"] = value
            with self.assertRaises(LedgerError): self.output(request)
        for entry in ({"value": "0", "note": ""}, {"value": "0"}):
            request = copy.deepcopy(self.input); request["costs"]["11"] = entry
            with self.assertRaises(LedgerError): self.output(request)
        self.input["costs"]["999"] = {"value": "0", "note": "잘못된 필드"}
        with self.assertRaises(LedgerError): self.output()

    def test_input_affects_identity_and_is_not_mutated_or_shared(self):
        before = copy.deepcopy(self.input)
        one = self.output()
        self.assertEqual(self.input, before)
        self.input["adjustments"]["9"]["note"] = "근거 변경"
        two = self.output()
        self.assertNotEqual(one["snapshot_id"], two["snapshot_id"])
        two["supplement"]["business"]["note"] = "출력 변경"
        self.assertNotEqual(self.input["business"]["note"], "출력 변경")

    def test_mapping_omission_and_amount_overflow_are_diagnosed(self):
        self.input["expense_mapping"] = {}
        self.assertIn("CATEGORY_MAPPING_UNCONFIRMED", {x["code"] for x in self.output()["blocking_issues"]})
        self.assertIsNone(self.values(self.output(), "C115400")["30"])
        self.input["expense_mapping"] = {"소모품비": 36}
        self.input["additional_expenses"]["33"]["value"] = "9999999999999"
        self.assertIn("FIELD_AMOUNT_OVERFLOW", {x["code"] for x in self.output()["blocking_issues"]})

    def test_cli_input_and_read_only_database(self):
        path = self.fixture.path.parent / "supplement.json"
        path.write_text(json.dumps(self.input), encoding="utf-8")
        before = hashlib.sha256(self.fixture.path.read_bytes()).hexdigest()
        result = subprocess.run([sys.executable, "-m", "ko_booking", "filing-prepare", "--year", "2025",
                                 "--ledger-db", str(self.fixture.path), "--input", str(path)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["schema_version"], 2)
        self.assertEqual(self.values(output, "C115300")["19"], "2900000")
        self.assertEqual(before, hashlib.sha256(self.fixture.path.read_bytes()).hexdigest())

    def test_asset_and_pending_rows_remain_blocking(self):
        self.state["summary"]["rows"][0]["kind"] = "asset"
        self.state["summary"]["pending_ids"] = ["pending-row"]
        codes = {x["code"] for x in self.output()["blocking_issues"]}
        self.assertTrue({"ASSET_REVIEW_REQUIRED", "TRANSACTIONS_PENDING"} <= codes)
        self.assertFalse(self.output()["submission_ready"])

    def test_invalid_identity_mapping_and_unknown_keys_are_rejected(self):
        for key, value in (("registration_number", "123"), ("industry_code", "abcdef"),
                           ("income_code", "32"), ("note", "")):
            request = copy.deepcopy(self.input); request["business"][key] = value
            with self.assertRaises(LedgerError): self.output(request)
        request = copy.deepcopy(self.input); request["expense_mapping"] = {"소모품비": 28}
        with self.assertRaises(LedgerError): self.output(request)
        request = copy.deepcopy(self.input); request["unexpected"] = True
        with self.assertRaises(LedgerError): self.output(request)

    def test_loss_preserved_as_candidate_not_final_tax(self):
        self.input["adjustments"]["9"]["value"] = "4000000"
        self.assertEqual(self.values(self.output(), "C115300")["19"], "-1100000")


if __name__ == "__main__":
    unittest.main()
