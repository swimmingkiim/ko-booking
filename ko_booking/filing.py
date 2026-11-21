"""Read-only filing preparation diagnostics; never a Hometax submission file."""

import hashlib
import sqlite3
from pathlib import Path

from .common import canonical, now
from .imports import LedgerError
from .ledger import Ledger


SPEC = {
    "tax_year": 2025,
    "title": "2025년 귀속 종합소득세 전자신고 변환파일 설명서_5차",
    "revision": "v26.05",
    "post_number": 1246,
    "post_modified_on": "2026-09-14",
    "source_url": "https://hometax.go.kr/websquare/websquare.html?w2xPath=/ui/pp/index_pp.xml&tmIdx=16&tm2lIdx=1602000000&tm3lIdx=",
    "sha256": None,
    "content_verified": False,
}


class _ReadOnlyLedger(Ledger):
    def __init__(self, path):
        self.path = path

    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=ro", uri=True, timeout=20)
        db.row_factory = sqlite3.Row
        return db


def prepare_filing(ledger_path, year):
    """Capture one consistent DB view without changing year, review or source rows."""
    if type(year) is not int or not 1900 <= year <= 2100:
        raise LedgerError("귀속연도를 확인해 주세요.")
    path = Path(ledger_path)
    if not path.is_file():
        raise LedgerError("기존 장부 DB가 필요합니다. 웹앱에서 자료를 먼저 정리해 주세요.")
    # Avoid Ledger.__init__: diagnostics must not create/migrate a database.
    try:
        state = _ReadOnlyLedger(path).state()
    except sqlite3.Error as error:
        raise LedgerError("장부 DB 형식·스키마를 확인해 주세요. 웹앱에서 만든 DB가 필요합니다.") from error
    if state["settings"]["year"] != year:
        raise LedgerError("웹앱에서 선택한 장부 연도와 요청한 귀속연도가 다릅니다.")
    return prepare_state(state)


def prepare_state(state):
    """Prepare internal category totals, preserving unknowns and provenance."""
    year = state["settings"]["year"]
    summary = state["summary"]
    transactions = sorted((t for t in state["transactions"] if int(t["date"][:4]) == year),
                          key=lambda t: t["id"])
    issues = []

    def issue(code, path, message):
        issues.append({"code": code, "path": path, "message": message})

    if year != SPEC["tax_year"]:
        issue("UNSUPPORTED_YEAR", "tax_year", "제출 준비 조사 대상은 2025년 귀속만 지원합니다.")
    if not transactions:
        issue("EMPTY_LEDGER", "transactions", "선택 연도의 거래가 없습니다.")
    if state["settings"]["vat_mode"] == "unconfirmed":
        issue("VAT_MODE_UNCONFIRMED", "vat_mode", "선택 연도의 과세유형을 확인해 주세요.")
    if summary["pending_ids"]:
        issue("TRANSACTIONS_PENDING", "pending_ids", "미분류·환불 연결을 먼저 확인해 주세요.")
    if summary["vat_pending_ids"]:
        issue("VAT_PENDING", "vat_pending_ids", "필요한 증빙의 부가세 금액을 확인해 주세요.")
    if not summary["reviewed"]:
        issue("LEDGER_NOT_REVIEWED", "ledger_fingerprint", "현재 장부의 자료 확인을 완료해 주세요.")
    # Deliberately unconditional until these inputs and physical specification exist.
    for code, field, message in (
        ("BUSINESS_IDENTITY_MISSING", "business", "사업장·업종·납세자 식별 입력을 아직 지원하지 않습니다."),
        ("INVENTORY_UNCONFIRMED", "inventory", "재고·제조비용과 매출원가 적용 여부를 검토해야 합니다."),
        ("ADJUSTMENTS_UNCONFIRMED", "adjustments", "감가상각·수입 및 필요경비 조정 입력을 아직 지원하지 않습니다."),
        ("FULL_RETURN_UNSUPPORTED", "return", "다른 소득·공제·기납부세액 등 전체 신고서를 아직 지원하지 않습니다."),
        ("SPEC_UNVERIFIED", "spec", "공식 변환명세 본문을 확보·검증하지 못했습니다."),
    ):
        issue(code, field, message)
    groups = {}
    for row in summary["rows"]:
        key = (row["kind"], row["category"])
        group = groups.setdefault(key, {"kind": key[0], "category": key[1], "book_amount_krw": "0", "rows": []})
        amount = row["book_amount_krw"]
        if amount is None or group["book_amount_krw"] is None:
            group["book_amount_krw"] = None
        else:
            group["book_amount_krw"] = str(int(group["book_amount_krw"]) + int(amount))
        group["rows"].append(dict(row))
    content = {
        "schema_version": 1, "tax_year": year, "purpose": "filing_preparation_diagnostic",
        "status": "draft", "submission_ready": False,
        "business_name": state["settings"]["name"], "vat_mode": state["settings"]["vat_mode"],
        "ledger_fingerprint": summary["fingerprint"], "ledger_reviewed": summary["reviewed"],
        "spec": dict(SPEC) if year == SPEC["tax_year"] else None,
        "calculation_rule_version": None,
        "transactions": [{key: t[key] for key in (
            "id", "revision", "date", "direction", "amount_krw", "allocations", "link_kind", "link_target"
        )} for t in transactions],
        "category_totals": [groups[key] for key in sorted(groups)],
        "pending_ids": summary["pending_ids"], "vat_pending_ids": summary["vat_pending_ids"],
        "excluded_ids": summary["excluded_ids"], "blocking_issues": issues,
    }
    # Identity covers all values and provenance, not just the ledger review hash.
    snapshot = hashlib.sha256(canonical(content).encode("utf-8")).hexdigest()
    return {**content, "snapshot_id": snapshot, "created_at": now()}
