"""CSV normalization with explicit mapping, errors and duplicate decisions."""

import csv
import hashlib
import io
import re
from datetime import datetime

from .common import canonical

MAX_FILE = 2 * 1024 * 1024
MAX_ROWS = 2000
FIELDS = {
    "date": ("거래일", "거래일자", "일자", "date"),
    "merchant": ("거래처", "적요", "내용", "merchant"),
    "direction": ("입출금", "구분", "방향", "direction"),
    "amount": ("금액", "거래금액", "amount"),
    "vat": ("부가세", "부가세액", "vat"),
    "source": ("자료출처", "출처", "결제수단", "source"),
    "reference": ("거래번호", "승인번호", "reference"),
}
REQUIRED = {"date", "merchant", "direction", "amount"}


class LedgerError(ValueError):
    pass


def text(value, label, limit=300, required=False):
    if not isinstance(value, str) or len(value) > limit or "\x00" in value:
        raise LedgerError(f"{label} 형식을 확인해 주세요.")
    result = value.strip()
    if required and not result:
        raise LedgerError(f"{label}이 비어 있습니다.")
    return result


def integer(value, label="금액", optional=False):
    if optional and (value is None or value == ""):
        return None
    if type(value) not in (int, str):
        raise LedgerError(f"{label}은 원 단위 정수여야 합니다.")
    original = str(value).strip()
    normalized = original.replace(",", "")
    if not re.fullmatch(r"-?(?:\d{1,15}|\d{1,3}(?:,\d{3}){1,4})", original) or len(normalized.lstrip("-")) > 15:
        raise LedgerError(f"{label}은 15자리 이내 원 단위 정수여야 합니다.")
    return int(normalized)


def check_vat(amount, vat):
    if vat is not None and (abs(vat) > abs(amount) or vat * amount < 0):
        raise LedgerError("부가세액의 부호·금액을 거래 금액과 맞춰 주세요.")


def read_csv(raw):
    if not raw or len(raw) > MAX_FILE:
        raise LedgerError("2MB 이하의 CSV 파일을 선택해 주세요.")
    for encoding in ("utf-8-sig", "cp949"):
        try:
            decoded = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise LedgerError("UTF-8 또는 CP949로 저장한 CSV가 필요합니다.")
    if "\x00" in decoded:
        raise LedgerError("텍스트 CSV 파일이 필요합니다.")
    try:
        rows = list(csv.reader(io.StringIO(decoded, newline=""), strict=True))
    except csv.Error:
        raise LedgerError("CSV 따옴표·열 구성을 확인해 주세요.") from None
    if len(rows) < 2 or len(rows) > MAX_ROWS + 1:
        raise LedgerError("CSV는 제목 행과 1~2,000개의 거래 행이 필요합니다.")
    headers = rows[0]
    if not headers or len(headers) > 50 or any(not h.strip() or len(h) > 100 for h in headers) or len(set(headers)) != len(headers):
        raise LedgerError("중복·빈 열 이름 없이 50개 이하의 열을 사용해 주세요.")
    return headers, rows[1:], encoding


def inspect_csv(raw):
    headers, rows, encoding = read_csv(raw)
    mapping = {field: next((h for h in headers if h.strip().lower() in aliases), "")
               for field, aliases in FIELDS.items()}
    return {"headers": headers, "mapping": mapping, "encoding": encoding,
            "row_count": len(rows), "sample": rows[:3]}


def parse_csv(raw, mapping, source_name):
    headers, rows, encoding = read_csv(raw)
    if not isinstance(mapping, dict) or set(mapping) != set(FIELDS):
        raise LedgerError("열 연결을 확인해 주세요.")
    if any(not isinstance(v, str) for v in mapping.values()):
        raise LedgerError("연결할 열 이름을 확인해 주세요.")
    if any(not mapping[f] for f in REQUIRED) or any(v and v not in headers for v in mapping.values()):
        raise LedgerError("날짜·거래처·입출금·금액 열을 연결해 주세요.")
    selected = [v for v in mapping.values() if v]
    if len(selected) != len(set(selected)):
        raise LedgerError("서로 다른 항목에 같은 열을 연결할 수 없습니다.")
    source_name = text(source_name, "자료 출처", required=True, limit=100)
    output = []
    for index, row in enumerate(rows, 2):
        item = {"row_number": index, "raw_row": row}
        try:
            if len(row) != len(headers):
                raise LedgerError("제목 행과 열 개수가 다릅니다.")
            values = {k: row[headers.index(v)].strip() if v else "" for k, v in mapping.items()}
            date = None
            for fmt in ("%Y-%m-%d", "%Y.%m.%d", "%Y/%m/%d", "%Y%m%d"):
                try:
                    parsed_date = datetime.strptime(values["date"], fmt).date()
                    if not 1900 <= parsed_date.year <= 2100:
                        continue
                    date = parsed_date.isoformat()
                    break
                except ValueError:
                    continue
            if not date:
                raise LedgerError("거래일을 확인해 주세요. 예: 2025-01-15")
            direction = {"입금": "income", "수입": "income", "income": "income",
                         "출금": "expense", "지출": "expense", "expense": "expense"}.get(values["direction"])
            if not direction:
                raise LedgerError("입출금은 입금·출금으로 표시해 주세요.")
            amount, vat = integer(values["amount"]), integer(values["vat"], "부가세", optional=True)
            if amount == 0:
                raise LedgerError("0원 거래는 가져올 수 없습니다.")
            check_vat(amount, vat)
            item.update(date=date, merchant=text(values["merchant"], "거래처", required=True),
                        direction=direction, amount_krw=str(amount), vat_krw=None if vat is None else str(vat),
                        source=text(values["source"] or source_name, "자료 출처", required=True, limit=100),
                        reference=text(values["reference"], "거래번호", limit=100))
            identity = {k: item[k] for k in ("date", "merchant", "direction", "amount_krw", "vat_krw", "source", "reference")}
            item["fingerprint"] = hashlib.sha256(canonical(identity).encode()).hexdigest()
        except LedgerError as error:
            item["error"] = str(error)
        output.append(item)
    return output, encoding
