import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

from ko_booking.archive import Archive
from ko_booking.common import canonical
from ko_booking.tax import CalculationError, RateEvidence, business_income, income_tax_base, reviewed_rates, rulebook


CASES = json.loads((Path(__file__).parent / "fixtures/tax_cases_2025.json").read_text())


def request(method="simple", code="940908", revenue="45000000"):
    result = {"tax_year": 2025, "method": method, "industry_code": code, "revenue_krw": revenue,
              "conditions": {"single_business": True, "single_industry": True, "joint_business": False,
                             "eligibility_confirmed": True, "general_rate_applicable": True,
                             "special_adjustments": [], "job_stabilization_funds_krw": "0"}}
    if method == "standard":
        result.update(bookkeeping="simple", major_expenses_confirmed=True,
                      major_expenses={"purchases_krw": "20000000", "rent_krw": "0", "payroll_krw": "0"})
    return result


def evidence(code="940908", general="75", excess="65", standard="19.9"):
    return RateEvidence(2025, code, Decimal(general), None if excess is None else Decimal(excess),
                        None if standard is None else Decimal(standard), {"fixture": "unit_test_only"})


class TaxTests(unittest.TestCase):
    def test_official_examples_and_separate_synthetic_cases(self):
        for case in CASES:
            with self.subTest(case=case["id"]):
                if case["calculation"] == "basic_income_tax":
                    result = income_tax_base(case["tax_year"], case["taxable_income_krw"])
                    amount = result["calculated_tax_krw_exact"]
                else:
                    method = "standard" if case["calculation"] == "standard_business_income" else "simple"
                    data = request(method, case["industry_code"], case["revenue_krw"])
                    rates = evidence(case["industry_code"], case["general_rate_pct"],
                                     case.get("excess_rate_pct"), case.get("standard_rate_pct"))
                    if method == "standard":
                        data["bookkeeping"] = case["bookkeeping"]
                        data["major_expenses"]["purchases_krw"] = case["purchases_krw"]
                    result = business_income(data, rates)
                    amount = result["business_income_krw_exact"]
                    if method == "standard":
                        steps = {s["id"]: s for s in result["steps"]}
                        self.assertEqual(steps["standard_income"]["result_krw_exact"], case["expected_direct_krw_exact"])
                        self.assertEqual(steps["comparison_income"]["result_krw_exact"], case["expected_cap_krw_exact"])
                self.assertEqual(amount, case["expected_krw_exact"])
                self.assertEqual(result["scope"], "component_only")
                self.assertIsNone(result["final_tax_payable_krw"])
                self.assertTrue(result["rule_sha256"])
                self.assertTrue(result["sources"])

    def test_all_tax_boundaries_against_independent_marginal_sum(self):
        # Independent marginal-band addition, not the implementation's quick deductions.
        bands = [(14000000, 6), (50000000, 15), (88000000, 24), (150000000, 35),
                 (300000000, 38), (500000000, 40), (1000000000, 42), (None, 45)]
        inputs = [0]
        for bound, _ in bands[:-1]:
            inputs.extend([bound - 1, bound, bound + 1])
        generator = random.Random(2025)
        inputs.extend(generator.randrange(0, 2000000000) for _ in range(100))
        for base in inputs:
            remaining, lower, expected = base, 0, Decimal(0)
            for upper, pct in bands:
                width = remaining if upper is None else min(remaining, upper - lower)
                expected += Decimal(width) * pct / 100
                remaining -= width
                if remaining == 0:
                    break
                lower = upper
            with self.subTest(base=base):
                actual = income_tax_base(2025, base)
                self.assertEqual(Decimal(actual["calculated_tax_krw_exact"]), expected)

    def test_fraction_is_not_silently_rounded_to_filing_amount(self):
        result = income_tax_base(2025, "14000001")
        self.assertEqual(result["calculated_tax_krw_exact"], "840000.15")
        self.assertEqual(result["rounding"], "exact_decimal_no_filing_rounding")

    def test_application_decimal_context_cannot_change_results(self):
        with localcontext() as context:
            context.prec = 4
            self.assertEqual(income_tax_base(2025, "14000001")["calculated_tax_krw_exact"], "840000.15")
            self.assertEqual(business_income(request(revenue="40000001"), evidence())[
                "business_income_krw_exact"], "10000000.35")

    def test_personal_service_split_boundary(self):
        for revenue, expected in [(0, "0"), (39999999, "9999999.75"),
                                  (40000000, "10000000"), (40000001, "10000000.35")]:
            with self.subTest(revenue=revenue):
                result = business_income(request(revenue=str(revenue)), evidence())
                self.assertEqual(result["business_income_krw_exact"], expected)

    def test_other_industry_does_not_use_personal_service_tier(self):
        result = business_income(request(code="525104"), evidence("525104", "86", "0", "10.4"))
        self.assertEqual(result["business_income_krw_exact"], "6300000")
        self.assertEqual(len(result["steps"]), 1)

    def test_expenses_are_summed_and_direct_result_can_win(self):
        data = request("standard", "525104", "70000000")
        data["major_expenses"] = {"purchases_krw": "40000000", "rent_krw": "10000000", "payroll_krw": "5000000"}
        result = business_income(data, evidence("525104", "86", "0", "10.4"))
        self.assertEqual(result["business_income_krw_exact"], "7720000")

    def test_year_is_explicit_and_evidence_cannot_cross_years(self):
        for year in [2023, 2024, 2026, "2025", True, None]:
            with self.assertRaises(CalculationError):
                income_tax_base(year, 100)
        with self.assertRaises(CalculationError):
            business_income(request(), replace(evidence(), tax_year=2024))

    def test_bad_money_never_becomes_zero(self):
        for value in [None, True, False, -1, 1.0, "1.5", "1,000", "1e3", "NaN", "Infinity", "", "9" * 19]:
            with self.subTest(value=value), self.assertRaises(CalculationError):
                income_tax_base(2025, value)

    def test_unconfirmed_or_unsupported_conditions_fail(self):
        mutations = [("single_business", False), ("single_industry", False), ("joint_business", True),
                     ("eligibility_confirmed", False), ("eligibility_confirmed", "true"),
                     ("general_rate_applicable", False), ("special_adjustments", ["disability"]),
                     ("special_adjustments", ["owner_occupied"]), ("job_stabilization_funds_krw", "1")]
        for key, value in mutations:
            data = request()
            data["conditions"][key] = value
            with self.subTest(condition=key, value=value), self.assertRaises(CalculationError):
                business_income(data, evidence())

    def test_missing_or_unknown_fields_fail(self):
        variants = [request(), request(), request(), request()]
        del variants[0]["conditions"]["eligibility_confirmed"]
        variants[1]["deductions_krw"] = "1500000"
        variants[2]["method"] = "unknown"
        variants[3]["industry_code"] = 940908
        for data in variants:
            with self.assertRaises(CalculationError):
                business_income(data, evidence())

    def test_missing_and_zero_rates_are_not_guessed(self):
        for rates in [evidence(general="0"), evidence(excess=None), evidence(excess="0"),
                      evidence(general="NaN"), evidence(general="101")]:
            with self.assertRaises(CalculationError):
                business_income(request(), rates)

    def test_standard_rejects_unconfirmed_expenses_missing_category_or_losses(self):
        variants = [request("standard") for _ in range(4)]
        variants[0]["major_expenses_confirmed"] = False
        variants[1]["bookkeeping"] = "unknown"
        del variants[2]["major_expenses"]["rent_krw"]
        variants[3]["major_expenses"]["purchases_krw"] = "100000000"
        for data in variants:
            with self.assertRaises(CalculationError):
                business_income(data, evidence())

    def test_cli_tax_component(self):
        completed = subprocess.run([sys.executable, "-m", "ko_booking", "tax-base", "--year", "2025",
                                    "--taxable-income", "30000000"], text=True, capture_output=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["calculated_tax_krw_exact"], "3240000")


class RateSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archive = Archive(Path(self.temp.name) / "rates.sqlite3")
        data = {"tax_year": 2025, "rate_unit": "percent_points", "records": [
            {"industry_code": "940908", "simple_general_pct": "75", "simple_excess_pct": "65",
             "standard_general_pct": "19.9", "source_sheet": "synthetic", "source_row": 2}]}
        saved = self.archive.save("nts:expense-rates:2025", b"synthetic original, not official XLSX", data,
                                  {"notice_url": "synthetic fixture"})
        self.version = saved["version_id"]
        self.rules = copy.deepcopy(rulebook(2025))
        # Explicit test-only review fingerprint; production trusts the checked-in official hashes.
        self.rules["reviewed_rate_snapshot"] = {key: saved[key] for key in
                                               ("source_key", "raw_sha256", "normalized_sha256")}

    def test_fixed_snapshot_provenance(self):
        with patch("ko_booking.tax.rulebook", return_value=self.rules):
            selected = reviewed_rates(self.archive, self.version, 2025, "940908")
        self.assertEqual(selected.provenance["archive_version_id"], self.version)
        self.assertEqual(selected.provenance["source_row"], 2)
        self.assertEqual(selected.general, Decimal("75"))

    def test_unreviewed_snapshot_is_not_automatically_used(self):
        with self.assertRaises(CalculationError):
            reviewed_rates(self.archive, self.version, 2025, "940908")

    def test_tampered_normalized_data_and_raw_bytes_fail(self):
        for column, value in [("normalized", canonical({"tax_year": 2024})), ("raw", b"tampered")]:
            with self.subTest(column=column):
                with self.archive.connect() as db:
                    original = db.execute(f"SELECT {column} FROM versions WHERE id=?", (self.version,)).fetchone()[0]
                    db.execute(f"UPDATE versions SET {column}=? WHERE id=?", (value, self.version))
                with patch("ko_booking.tax.rulebook", return_value=self.rules), self.assertRaises(CalculationError):
                    reviewed_rates(self.archive, self.version, 2025, "940908")
                with self.archive.connect() as db:
                    db.execute(f"UPDATE versions SET {column}=? WHERE id=?", (original, self.version))

    def test_unknown_version_or_code_fail(self):
        with patch("ko_booking.tax.rulebook", return_value=self.rules):
            for version, code in [(999, "940908"), (self.version, "000000")]:
                with self.assertRaises(CalculationError):
                    reviewed_rates(self.archive, version, 2025, code)


if __name__ == "__main__":
    unittest.main()
