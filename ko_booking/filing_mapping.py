"""Candidate fields for a single full-year, non-rental business; no serialization."""

import copy
import re

from .imports import LedgerError

COST_INPUTS = {11, 12, 13, 15, 16, 17, 19, 20}
EXPENSE_FIELDS = set(range(22, 29)) | set(range(33, 41))
ADJUSTMENTS = {9, 10, 13, 14, 17, 18}
CATEGORY_FIELDS = {"급여": 22, "임차료": 24, "지급수수료": 35,
                   "소모품비": 36, "여비교통비": 40, "통신비": 28, "기타경비": 28}


def _amounts(value, allowed, path):
    if not isinstance(value, dict) or any(k not in {str(n) for n in allowed} for k in value):
        raise LedgerError(f"{path}: 허용된 필드 순번을 사용해 주세요.")
    result = {}
    for key, entry in value.items():
        if (not isinstance(entry, dict) or set(entry) != {"value", "note"}
                or not isinstance(entry["value"], str)
                or not re.fullmatch(r"[0-9]{1,13}", entry["value"])
                or not isinstance(entry["note"], str) or not entry["note"].strip()
                or len(entry["note"]) > 1000):
            raise LedgerError(f"{path}.{key}: 13자리 이내의 음이 아닌 원 단위 문자열과 검토 사유가 필요합니다.")
        result[int(key)] = {"value": str(int(entry["value"])), "note": entry["note"].strip()}
    return result


def map_candidates(state, groups, supplement, records):
    """Return normalized input, candidate records and blocking diagnostics."""
    year = state["settings"]["year"]
    if not isinstance(supplement, dict) or set(supplement) != {
        "tax_year", "ledger_fingerprint", "business", "expense_mapping", "costs", "additional_expenses", "adjustments"
    }:
        raise LedgerError("보완 입력의 필수 키와 알 수 없는 키를 확인해 주세요.")
    if type(supplement["tax_year"]) is not int or supplement["tax_year"] != year or year != 2025:
        raise LedgerError("보완 입력은 현재 장부와 같은 2025년 귀속만 지원합니다.")
    if supplement["ledger_fingerprint"] != state["summary"]["fingerprint"]:
        raise LedgerError("장부가 변경되었습니다. 최신 진단의 ledger_fingerprint로 보완 입력을 재검토해 주세요.")
    business = supplement["business"]
    if not isinstance(business, dict) or set(business) != {
        "registration_number", "industry_code", "income_code", "address", "industry_name", "period_start", "period_end", "note"
    }:
        raise LedgerError("사업장 식별 입력 항목을 확인해 주세요.")
    if (any(not isinstance(v, str) for v in business.values())
            or not re.fullmatch(r"[0-9]{10}", business["registration_number"])
            or not re.fullmatch(r"[0-9]{6}", business["industry_code"])
            or business["income_code"] != "40"
            or not business["note"].strip() or any(len(v) > 1000 for v in business.values())):
        raise LedgerError("사업장 번호 10자리·업종코드 6자리·소득구분 40 및 검토 사유를 확인해 주세요.")
    if (business["period_start"], business["period_end"]) != (f"{year}0101", f"{year}1231"):
        raise LedgerError("이번 후보 매핑은 1월 1일~12월 31일 단일 사업장만 지원합니다.")
    mapping = supplement["expense_mapping"]
    if (not isinstance(mapping, dict) or any(k not in CATEGORY_FIELDS or type(v) is not int
                                           or v != CATEGORY_FIELDS[k] for k, v in mapping.items())):
        raise LedgerError("비용 분류별 검토한 후보 필드 순번을 확인해 주세요.")
    costs = _amounts(supplement["costs"], COST_INPUTS, "costs")
    extras = _amounts(supplement["additional_expenses"], EXPENSE_FIELDS, "additional_expenses")
    adjustments = _amounts(supplement["adjustments"], ADJUSTMENTS, "adjustments")
    issues = []
    output = {}
    provenance = {}
    for record in records:
        code = record["form_code"]
        output[code] = {str(f["ordinal"]): None for f in record["fields"]}
        provenance[code] = {}

    def set_field(code, number, value, source):
        output[code][str(number)] = value
        provenance[code][str(number)] = source

    def problem(code, path, message):
        issues.append({"code": code, "path": path, "message": message})

    for code in output:
        for number, value in {1: "52", 2: code, 3: business["income_code"], 4: business["registration_number"],
                              5: business["address"], 6: business["industry_code"], 7: business["industry_name"]}.items():
            set_field(code, number, value, {"kind": "business_input", "note": business["note"]})
    detail = "C115400"
    income = "C115300"
    # This only establishes an absent *ledger* category as zero; pending rows still block readiness.
    for number, category in ((8, "매출"), (9, "기타수입")):
        matching = [g for g in groups if g["kind"] == "income" and g["category"] == category]
        value = None if any(g["book_amount_krw"] is None for g in matching) else str(sum(int(g["book_amount_krw"]) for g in matching))
        set_field(detail, number, value, {"kind": "ledger", "category": category,
                                          "rows": [r for g in matching for r in g["rows"]]})
    for number in EXPENSE_FIELDS:
        matching = [g for g in groups if g["kind"] == "expense" and mapping.get(g["category"]) == number]
        missing = number not in extras
        # An explicit zero for additional expense confirms that the ledger has all amounts for this field.
        if missing:
            problem("ADDITIONAL_EXPENSE_UNCONFIRMED", f"additional_expenses.{number}", "장부 외 비용의 유무와 금액을 확인해 주세요.")
        value = None if missing or any(g["book_amount_krw"] is None for g in matching) else str(
            sum(int(g["book_amount_krw"]) for g in matching) + int(extras[number]["value"]))
        set_field(detail, number, value, {"kind": "ledger_plus_manual", "additional": extras.get(number),
                                          "rows": [r for g in matching for r in g["rows"]]})
    for g in groups:
        if g["kind"] == "asset":
            problem("ASSET_REVIEW_REQUIRED", "category_totals", "자산 취득·매각의 감가상각·수입 반영은 별도 검토가 필요합니다.")
        elif g["kind"] == "expense" and g["category"] not in mapping and g["category"] != "상품매입":
            problem("CATEGORY_MAPPING_UNCONFIRMED", f"expense_mapping.{g['category']}", "해당 비용 분류의 후보 매핑을 확인해 주세요.")
            if g["category"] in CATEGORY_FIELDS:
                set_field(detail, CATEGORY_FIELDS[g["category"]], None, {"kind": "unconfirmed_category_mapping"})
    for number in sorted(COST_INPUTS):
        entry = costs.get(number)
        set_field(detail, number, entry["value"] if entry else None, {"kind": "manual_cost", "input": entry})
        if entry is None:
            problem("COST_INPUT_MISSING", f"costs.{number}", "재고·매입·제조비용을 확인해 주세요. 해당 없으면 사유와 0을 입력합니다.")
    for number in sorted(ADJUSTMENTS):
        entry = adjustments.get(number)
        set_field(income, number, entry["value"] if entry else None, {"kind": "manual_adjustment", "input": entry})
        if entry is None:
            problem("ADJUSTMENT_INPUT_MISSING", f"adjustments.{number}", "조정 금액과 사유를 확인해 주세요.")

    def formula(code, target, terms):
        values = [output[c][str(n)] for c, n, sign in terms]
        value = None if any(v is None for v in values) else str(sum(int(v) * term[2] for v, term in zip(values, terms)))
        set_field(code, target, value, {"kind": "formula", "terms": [{"form_code": c, "ordinal": n, "sign": sign} for c, n, sign in terms]})

    for target, terms in ((10, [(8, 1), (9, 1)]), (14, [(11, 1), (12, 1), (13, -1)]),
                          (18, [(15, 1), (16, 1), (17, -1)]), (21, [(18, 1), (19, 1), (20, 1)]),
                          (29, [(n, 1) for n in sorted(EXPENSE_FIELDS)]), (30, [(14, 1), (29, 1)])):
        formula(detail, target, [(detail, n, sign) for n, sign in terms])
    formula(income, 8, [(detail, 10, 1)])
    formula(income, 12, [(detail, 30, 1)])
    for target, terms in ((11, [(8, 1), (9, -1), (10, 1)]), (15, [(12, 1), (13, -1), (14, 1)]),
                          (16, [(11, 1), (15, -1)]), (19, [(16, 1), (17, 1), (18, -1)])):
        formula(income, target, [(income, n, sign) for n, sign in terms])
    for code, start, end, blank in ((income, 20, 21, 22), (detail, 31, 32, 41)):
        set_field(code, start, business["period_start"], {"kind": "business_input"})
        set_field(code, end, business["period_end"], {"kind": "business_input"})
        set_field(code, blank, None, {"kind": "reserved_space_not_serialized"})
    for code, fields in output.items():
        for n, value in fields.items():
            field = next(f for r in records if r["form_code"] == code for f in r["fields"] if str(f["ordinal"]) == n)
            if field["type"] == "NUMBER" and value is not None and len(value.lstrip('-')) > field["length_bytes"]:
                problem("FIELD_AMOUNT_OVERFLOW", f"{code}.{n}", "계산 결과가 명세 금액 길이를 초과합니다.")
    # Prose references are inconsistent with table numbering; keep mapping explicitly provisional.
    problem("CROSS_RECORD_MAPPING_PROVISIONAL", "candidate_records", "교차 서식 주석 번호 불일치가 있어 이름·산식 기반 후보 매핑이며 호환 검증 전입니다.")
    return copy.deepcopy(supplement), [{"form_code": code, "values": output[code], "provenance": provenance[code]} for code in output], issues
