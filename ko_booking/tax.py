"""Reproducible 2025 formula components; never a complete filing calculation."""

import json
import re
from contextlib import closing
from dataclasses import dataclass
from decimal import Context, Decimal, localcontext
from functools import wraps
from importlib.resources import files

from .archive import digest
from .common import canonical


class CalculationError(ValueError):
    """Missing applicability or unsupported facts must not silently become zero."""


def exact_decimal(function):
    @wraps(function)
    def calculate(*args, **kwargs):
        # Application-wide Decimal settings must not change a reviewed calculation.
        with localcontext(Context(prec=50)):
            return function(*args, **kwargs)
    return calculate


def rulebook(year):
    if type(year) is not int or year != 2025:
        raise CalculationError("Only reviewed tax year 2025 is supported; no prior-year fallback")
    return json.loads(files("ko_booking").joinpath("rules/2025.json").read_text(encoding="utf-8"))


def won(value):
    # Reject binary floats, booleans, commas, exponents, fractions and negative amounts.
    if type(value) not in (str, int) or not re.fullmatch(r"\d{1,18}", str(value)):
        raise CalculationError("Amounts must be nonnegative integer KRW (at most 18 digits)")
    return Decimal(str(value))


def number(value):
    return format(value.normalize(), "f") if value else "0"


def result_header(rules, kind, inputs, source_ids):
    return {"calculation_kind": kind, "scope": "component_only", "tax_year": rules["tax_year"],
            "rule_version": rules["rule_version"], "rule_sha256": digest(canonical(rules).encode()),
            "inputs": inputs, "sources": {key: rules["sources"][key] for key in source_ids},
            "rounding": "exact_decimal_no_filing_rounding", "final_tax_payable_krw": None,
            "unimplemented": rules["unimplemented"]}


@exact_decimal
def income_tax_base(year, taxable_income):
    """Apply the basic schedule to a supplied, already determined tax base."""
    rules = rulebook(year)
    base = won(taxable_income)
    bracket = next(b for b in rules["brackets"] if b["upper_krw"] is None or base <= b["upper_krw"])
    rate, deduction = Decimal(bracket["rate_pct"]), Decimal(bracket["deduction_krw"])
    exact = base * rate / 100 - deduction
    return {**result_header(rules, "basic_income_tax", {"taxable_income_krw": number(base)},
                           ["tax_brackets"]),
            "steps": [{"id": "basic_income_tax", "formula": "taxable_income * rate_pct / 100 - deduction",
                       "rate_pct": number(rate), "progressive_deduction_krw": number(deduction),
                       "result_krw_exact": number(exact)}],
            "calculated_tax_krw_exact": number(exact)}


@dataclass(frozen=True)
class RateEvidence:
    """Internal formula input; the public CLI obtains it through reviewed_rates()."""

    tax_year: int
    industry_code: str
    general: Decimal
    excess: Decimal | None
    standard: Decimal | None
    provenance: dict


def reviewed_rates(archive, version_id, year, industry_code):
    rules = rulebook(year)
    expected = rules["reviewed_rate_snapshot"]
    # Read a fixed version, never whichever dataset happens to be newest today.
    with closing(archive.connect()) as db:
        row = db.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
    if row is None:
        raise CalculationError("Unknown expense-rate snapshot version")
    if (row["source_key"] != expected["source_key"]
            or digest(row["raw"]) != expected["raw_sha256"]
            or row["raw_sha256"] != expected["raw_sha256"]
            or digest(row["normalized"].encode()) != expected["normalized_sha256"]
            or row["normalized_sha256"] != expected["normalized_sha256"]):
        raise CalculationError("Expense-rate snapshot differs from the reviewed version; review is required")
    data = json.loads(row["normalized"])
    if data["tax_year"] != year or data["rate_unit"] != "percent_points":
        raise CalculationError("Expense-rate year or unit mismatch")
    matches = [r for r in data["records"] if r["industry_code"] == industry_code]
    if len(matches) != 1:
        raise CalculationError("Expected exactly one matching industry code")
    record = matches[0]
    values = [None if record[field] is None else Decimal(record[field])
              for field in ("simple_general_pct", "simple_excess_pct", "standard_general_pct")]
    meta = json.loads(row["metadata"])
    provenance = {"archive_version_id": version_id, **expected,
                  "source_url": meta["notice_url"], "source_sheet": record["source_sheet"],
                  "source_row": record["source_row"]}
    return RateEvidence(year, industry_code, *values, provenance)


def positive_rate(rate):
    if rate is None or not rate.is_finite() or not 0 < rate <= 100:
        raise CalculationError("Missing or zero expense rate requires applicability review")
    return rate


def simple_parts(revenue, rates, rules):
    general = positive_rate(rates.general)
    tiers = rates.industry_code.startswith(rules["personal_service_prefix"])
    first = min(revenue, Decimal(rules["personal_service_threshold_krw"])) if tiers else revenue
    parts = [("general_income", first, general)]
    if tiers and revenue > first:
        parts.append(("excess_income", revenue - first, positive_rate(rates.excess)))
    steps, income = [], Decimal(0)
    for label, amount, rate in parts:
        part = amount * (1 - rate / 100)
        income += part
        steps.append({"id": label, "revenue_krw": number(amount), "rate_pct": number(rate),
                      "formula": "revenue * (1 - rate_pct / 100)", "result_krw_exact": number(part)})
    return income, steps


@exact_decimal
def business_income(request, rates):
    """Estimate one externally qualified business with unadjusted general rates."""
    if not isinstance(request, dict):
        raise CalculationError("Business calculation input must be a JSON object")
    method = request.get("method")
    common = {"tax_year", "method", "industry_code", "revenue_krw", "conditions"}
    fields = common | ({"bookkeeping", "major_expenses", "major_expenses_confirmed"} if method == "standard" else set())
    if method not in ("simple", "standard") or set(request) != fields:
        raise CalculationError("Missing or unexpected calculation fields")
    rules = rulebook(request["tax_year"])
    if rates.tax_year != request["tax_year"]:
        raise CalculationError("Expense-rate evidence belongs to a different tax year")
    code = request["industry_code"]
    if not isinstance(code, str) or not re.fullmatch(r"\d{6}", code) or code != rates.industry_code:
        raise CalculationError("Industry code does not match the expense-rate evidence")
    conditions = request["conditions"]
    required_conditions = {"single_business", "single_industry", "joint_business",
                           "eligibility_confirmed", "general_rate_applicable",
                           "special_adjustments", "job_stabilization_funds_krw"}
    if not isinstance(conditions, dict) or set(conditions) != required_conditions:
        raise CalculationError("All applicability conditions must be supplied explicitly")
    if any(conditions[k] is not True for k in (
            "single_business", "single_industry", "eligibility_confirmed", "general_rate_applicable"
    )) or conditions["joint_business"] is not False:
        raise CalculationError("Single-business eligibility and general-rate applicability require confirmation")
    if conditions["special_adjustments"] != [] or won(conditions["job_stabilization_funds_krw"]) != 0:
        raise CalculationError("Special rate adjustments or job stabilization funds are not implemented")
    revenue = won(request["revenue_krw"])
    simple, steps = simple_parts(revenue, rates, rules)
    if method == "simple":
        income = simple
    else:
        bookkeeping = request["bookkeeping"]
        if bookkeeping not in ("simple", "double") or request["major_expenses_confirmed"] is not True:
            raise CalculationError("Bookkeeping category and eligible major-expense confirmation are required")
        expenses = request["major_expenses"]
        if not isinstance(expenses, dict) or set(expenses) != {"purchases_krw", "rent_krw", "payroll_krw"}:
            raise CalculationError("Major expenses must specify purchases, rent and payroll")
        major = sum((won(value) for value in expenses.values()), Decimal(0))
        weight = Decimal(rules["standard_rate_weights"][bookkeeping])
        standard = positive_rate(rates.standard) * weight
        direct = revenue - major - revenue * standard / 100
        cap = simple * Decimal(rules["standard_multipliers"][bookkeeping])
        if direct < 0:
            raise CalculationError("Negative standard-rate income requires separate review")
        income = min(direct, cap)
        steps.extend([
            {"id": "standard_income", "major_expenses_krw": number(major), "rate_pct": number(standard),
             "formula": "revenue - major_expenses - revenue * adjusted_standard_rate_pct / 100",
             "result_krw_exact": number(direct)},
            {"id": "comparison_income", "multiplier": rules["standard_multipliers"][bookkeeping],
             "formula": "simple_income * multiplier", "result_krw_exact": number(cap)},
            {"id": "selected_income", "formula": "min(standard_income, comparison_income)",
             "result_krw_exact": number(income)},
        ])
    return {**result_header(rules, "estimated_business_income", request,
                           ["expense_formulas", "personal_service_tiers"]),
            "rate_evidence": rates.provenance, "steps": steps,
            "business_income_krw_exact": number(income),
            "applicability": "caller_confirmed_not_automatically_determined"}
