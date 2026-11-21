"""Local bookkeeping with immutable imports and revisioned allocations."""

import csv
import hashlib
import io
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path

from .common import canonical, now
from .imports import LedgerError, check_vat, integer, parse_csv, text

CATEGORIES = {
    "income": ["매출", "기타수입", "자산매각", "개인거래", "자금이동"],
    "expense": ["상품매입", "임차료", "급여", "지급수수료", "소모품비", "여비교통비", "통신비", "기타경비", "자산취득", "개인거래", "자금이동"],
}
EXCLUDED = {"개인거래", "자금이동"}


def uid():
    return uuid.uuid4().hex


class Ledger:
    def __init__(self, path):
        self.path = Path(path)
        created = not self.path.exists()
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1), name TEXT, year INTEGER);
                INSERT OR IGNORE INTO settings VALUES (1,'나의 사업장',2025);
                CREATE TABLE IF NOT EXISTS previews (
                    token TEXT PRIMARY KEY, filename TEXT, raw BLOB, mapping TEXT, source_name TEXT, created_at TEXT
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id TEXT PRIMARY KEY, import_key TEXT UNIQUE, filename TEXT, raw BLOB,
                    raw_sha256 TEXT, mapping TEXT, source_name TEXT, created_at TEXT, decisions TEXT
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id TEXT PRIMARY KEY, batch_id TEXT REFERENCES batches(id), row_number INTEGER,
                    original TEXT NOT NULL, fingerprint TEXT, source TEXT, reference TEXT,
                    date TEXT, direction TEXT, amount INTEGER, allocations TEXT,
                    link_kind TEXT NOT NULL DEFAULT '', link_target TEXT REFERENCES transactions(id), revision INTEGER DEFAULT 1
                );
                CREATE UNIQUE INDEX IF NOT EXISTS unique_reference ON transactions(source,reference) WHERE reference<>'';
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, transaction_id TEXT REFERENCES transactions(id),
                    at TEXT, actor TEXT, before_state TEXT, after_state TEXT
                );
                CREATE TABLE IF NOT EXISTS reviews (year INTEGER PRIMARY KEY, fingerprint TEXT, reviewed_at TEXT);
                CREATE TABLE IF NOT EXISTS profiles (year INTEGER PRIMARY KEY, vat_mode TEXT NOT NULL);
            """)
        if created:
            self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @staticmethod
    def _transaction(row):
        return {**json.loads(row["original"]), "id": row["id"], "batch_id": row["batch_id"],
                "allocations": json.loads(row["allocations"]), "link_kind": row["link_kind"],
                "link_target": row["link_target"], "revision": row["revision"]}

    @staticmethod
    def _rows(db):
        return [Ledger._transaction(r) for r in db.execute("SELECT * FROM transactions ORDER BY date DESC,rowid DESC")]

    @staticmethod
    def _preview(db, raw, mapping, source_name):
        rows, encoding = parse_csv(raw, mapping, source_name)
        import_key = hashlib.sha256(raw + canonical(mapping).encode() + source_name.encode()).hexdigest()
        already = db.execute("SELECT id FROM batches WHERE import_key=?", (import_key,)).fetchone()
        refs, fingerprints = {}, set()
        for r in db.execute("SELECT source,reference,fingerprint FROM transactions"):
            if r["reference"]:
                refs[(r["source"], r["reference"])] = r["fingerprint"]
            fingerprints.add(r["fingerprint"])
        for row in rows:
            if "error" in row:
                row["status"] = "error"
                continue
            key = (row["source"], row["reference"])
            if already:
                row["status"] = "already_imported"
            elif row["reference"] and key in refs:
                if refs[key] == row["fingerprint"]:
                    row["status"] = "already_imported"
                else:
                    row.update(status="error", error="같은 출처·거래번호의 금액이나 내용이 다릅니다. 원본을 확인해 주세요.")
            else:
                row["status"] = "possible_duplicate" if row["fingerprint"] in fingerprints else "new"
            if row["reference"]:
                refs[key] = row["fingerprint"]
            fingerprints.add(row["fingerprint"])
        return {"rows": rows, "encoding": encoding, "import_key": import_key,
                "already_imported": bool(already), "row_count": len(rows),
                "error_count": sum(r["status"] == "error" for r in rows),
                "duplicate_count": sum(r["status"] in ("already_imported", "possible_duplicate") for r in rows)}

    def preview(self, filename, raw, mapping, source_name):
        filename = text(filename, "파일 이름", limit=200, required=True)
        source_name = text(source_name, "자료 출처", limit=100, required=True)
        with closing(self.connect()) as db, db:
            result = self._preview(db, raw, mapping, source_name)
            token = uid()
            db.execute("INSERT INTO previews VALUES (?,?,?,?,?,?)",
                       (token, filename, raw, canonical(mapping), source_name, now()))
        return {**result, "token": token, "filename": filename}

    def commit(self, token, decisions):
        if not isinstance(token, str) or not isinstance(decisions, dict):
            raise LedgerError("중복 후보 처리 방법을 확인해 주세요.")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            preview = db.execute("SELECT * FROM previews WHERE token=?", (token,)).fetchone()
            if preview is None:
                raise LedgerError("미리보기를 다시 실행해 주세요.")
            mapping = json.loads(preview["mapping"])
            result = self._preview(db, preview["raw"], mapping, preview["source_name"])
            if result["already_imported"]:
                return {"imported": 0, "skipped": result["row_count"], "already_imported": True}
            if result["error_count"]:
                raise LedgerError("오류 행을 수정한 CSV로 미리보기를 다시 실행해 주세요. 일부 행만 저장하지 않습니다.")
            candidates = {str(r["row_number"]) for r in result["rows"] if r["status"] == "possible_duplicate"}
            if set(decisions) != candidates or any(v not in ("keep", "skip") for v in decisions.values()):
                raise LedgerError("모든 중복 후보를 확인한 뒤 별도 거래·제외 중 하나를 선택해 주세요.")
            batch = uid()
            db.execute("INSERT INTO batches VALUES (?,?,?,?,?,?,?,?,?)",
                       (batch, result["import_key"], preview["filename"], preview["raw"],
                        hashlib.sha256(preview["raw"]).hexdigest(), preview["mapping"], preview["source_name"], now(), canonical(decisions)))
            imported = 0
            for row in result["rows"]:
                if row["status"] == "already_imported" or decisions.get(str(row["row_number"])) == "skip":
                    continue
                tx_id = uid()
                allocations = [{"category": None, "amount_krw": row["amount_krw"], "vat_krw": row["vat_krw"], "note": ""}]
                db.execute("""INSERT INTO transactions
                    (id,batch_id,row_number,original,fingerprint,source,reference,date,direction,amount,allocations)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (tx_id, batch, row["row_number"], canonical(row), row["fingerprint"], row["source"], row["reference"],
                     row["date"], row["direction"], int(row["amount_krw"]), canonical(allocations)))
                tx = self._transaction(db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone())
                db.execute("INSERT INTO audit (transaction_id,at,actor,before_state,after_state) VALUES (?,?,?,?,?)",
                           (tx_id, now(), "local_user", "null", canonical(tx)))
                imported += 1
            return {"imported": imported, "skipped": result["row_count"] - imported, "batch_id": batch}

    def edit(self, tx_id, payload):
        if not isinstance(payload, dict) or set(payload) != {"revision", "allocations", "link_kind", "link_target"}:
            raise LedgerError("거래 수정 형식을 확인해 주세요.")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
            if row is None:
                raise LedgerError("거래를 찾을 수 없습니다.")
            before = self._transaction(row)
            if type(payload["revision"]) is not int or payload["revision"] != before["revision"]:
                raise LedgerError("다른 화면에서 거래가 변경되었습니다. 새로고침 후 다시 확인해 주세요.")
            parts = payload["allocations"]
            if not isinstance(parts, list) or not 1 <= len(parts) <= 20:
                raise LedgerError("분할 거래는 1~20개로 입력해 주세요.")
            normalized = []
            for part in parts:
                if not isinstance(part, dict) or set(part) != {"category", "amount_krw", "vat_krw", "note"}:
                    raise LedgerError("분할 항목을 확인해 주세요.")
                category = part["category"]
                if category is not None and category not in CATEGORIES[before["direction"]]:
                    raise LedgerError("입출금에 맞는 거래 분류를 선택해 주세요.")
                amount = integer(part["amount_krw"])
                vat = integer(part["vat_krw"], "부가세", optional=True)
                if amount == 0 or amount * row["amount"] < 0:
                    raise LedgerError("분할 금액의 부호는 원래 거래와 같아야 합니다.")
                check_vat(amount, vat)
                normalized.append({"category": category, "amount_krw": str(amount),
                                   "vat_krw": None if vat is None else str(vat), "note": text(part["note"], "메모", 500)})
            if sum(int(p["amount_krw"]) for p in normalized) != row["amount"]:
                raise LedgerError("분할 금액의 합계가 원래 거래 금액과 같아야 합니다.")
            original_vat = before["vat_krw"]
            if original_vat is not None and (any(p["vat_krw"] is None for p in normalized)
                    or sum(int(p["vat_krw"]) for p in normalized) != int(original_vat)):
                raise LedgerError("원본에 있는 부가세액을 분할 항목의 합계와 맞춰 주세요.")
            kind, target_id = payload["link_kind"], payload["link_target"]
            if kind not in ("", "duplicate", "refund") or (not kind and target_id is not None):
                raise LedgerError("거래 연결 방법을 확인해 주세요.")
            if kind:
                if not isinstance(target_id, str):
                    raise LedgerError("연결할 원거래를 선택해 주세요.")
                target = db.execute("SELECT * FROM transactions WHERE id=?", (target_id,)).fetchone()
                if target is None or target_id == tx_id or target["amount"] <= 0 or target["link_kind"]:
                    raise LedgerError("연결되지 않은 원래 입금·출금 거래를 선택해 주세요.")
                if target["direction"] != row["direction"] or target["date"][:4] != row["date"][:4]:
                    raise LedgerError("같은 연도·입출금 유형의 원거래만 연결할 수 있습니다.")
                if db.execute("SELECT 1 FROM transactions WHERE link_target=?", (tx_id,)).fetchone():
                    raise LedgerError("다른 거래가 연결되어 있습니다. 연결된 거래를 먼저 확인해 주세요.")
                if kind == "duplicate" and row["amount"] != target["amount"]:
                    raise LedgerError("중복 연결은 금액이 같은 거래만 가능합니다.")
                if kind == "refund":
                    other = sum(r[0] for r in db.execute(
                        "SELECT amount FROM transactions WHERE link_kind='refund' AND link_target=? AND id<>?", (target_id, tx_id)))
                    if row["amount"] >= 0 or abs(other + row["amount"]) > target["amount"]:
                        raise LedgerError("취소·환불은 음수 금액이며 원거래 금액을 초과할 수 없습니다.")
                    target_parts = json.loads(target["allocations"])
                    refund_parts = normalized + [p for r in db.execute(
                        "SELECT allocations FROM transactions WHERE link_kind='refund' AND link_target=? AND id<>?",
                        (target_id, tx_id)) for p in json.loads(r[0])]
                    if all(p["vat_krw"] is not None for p in target_parts + refund_parts):
                        if abs(sum(int(p["vat_krw"]) for p in refund_parts)) > sum(int(p["vat_krw"]) for p in target_parts):
                            raise LedgerError("취소·환불 부가세액의 합계가 원거래를 초과합니다.")
            after = {**before, "allocations": normalized, "link_kind": kind, "link_target": target_id,
                     "revision": before["revision"] + 1}
            db.execute("UPDATE transactions SET allocations=?,link_kind=?,link_target=?,revision=? WHERE id=?",
                       (canonical(normalized), kind, target_id, after["revision"], tx_id))
            db.execute("INSERT INTO audit (transaction_id,at,actor,before_state,after_state) VALUES (?,?,?,?,?)",
                       (tx_id, now(), "local_user", canonical(before), canonical(after)))
            return after

    def history(self, tx_id):
        with closing(self.connect()) as db:
            return [{"at": r["at"], "actor": r["actor"], "before": json.loads(r["before_state"]),
                     "after": json.loads(r["after_state"])} for r in db.execute(
                         "SELECT * FROM audit WHERE transaction_id=? ORDER BY id DESC", (tx_id,))]

    @staticmethod
    def summarize(rows, year, vat_mode="unconfirmed"):
        selected = [r for r in rows if int(r["date"][:4]) == year]
        index = {r["id"]: r for r in rows}
        totals = {"income": 0, "expense": 0, "asset": 0}
        monthly = [{"month": m, "income": 0, "expense": 0} for m in range(1, 13)]
        pending, vat_pending, excluded, ledger_rows = set(), set(), set(), []
        for tx in selected:
            if tx["link_kind"] == "duplicate":
                excluded.add(tx["id"])
                continue
            if int(tx["amount_krw"]) < 0:
                target = index.get(tx["link_target"])
                categories = {p["category"] for p in target["allocations"]} if target else set()
                if tx["link_kind"] != "refund" or any(p["category"] is None or p["category"] not in categories for p in tx["allocations"]):
                    pending.add(tx["id"])
                    continue
                refund_parts = [p for r in rows if r["link_kind"] == "refund" and r["link_target"] == target["id"] for p in r["allocations"]]
                if all(p["vat_krw"] is not None for p in target["allocations"] + refund_parts):
                    if abs(sum(int(p["vat_krw"]) for p in refund_parts)) > sum(int(p["vat_krw"]) for p in target["allocations"]):
                        pending.add(tx["id"])
                        continue
            for part in tx["allocations"]:
                category = part["category"]
                if category is None:
                    pending.add(tx["id"])
                    continue
                if category in EXCLUDED:
                    excluded.add(tx["id"])
                    continue
                amount = int(part["amount_krw"])
                vat = None if part["vat_krw"] is None else int(part["vat_krw"])
                separate_vat = tx["direction"] == "expense" or vat_mode not in ("simplified", "exempt")
                if vat is None and separate_vat:
                    vat_pending.add(tx["id"])
                kind = "asset" if category in ("자산취득", "자산매각") else tx["direction"]
                sign = -1 if category == "자산매각" else 1
                totals[kind] += amount * sign
                if kind != "asset":
                    monthly[int(tx["date"][5:7]) - 1][kind] += amount
                ledger_rows.append({"date": tx["date"], "merchant": tx["merchant"], "category": category,
                    "kind": kind, "gross_krw": str(amount * sign),
                    "supply_krw": None if vat is None else str((amount - vat) * sign),
                    "vat_krw": None if vat is None else str(vat * sign), "note": part["note"],
                    "book_amount_krw": (None if vat is None else str((amount - vat) * sign)) if separate_vat else str(amount * sign),
                    "book_vat_krw": None if vat is None or not separate_vat else str(vat * sign),
                    "vat_separated": separate_vat, "revision": tx["revision"],
                    "transaction_id": tx["id"], "source": tx["source"], "reference": tx["reference"],
                    "row_number": tx["row_number"], "batch_id": tx["batch_id"]})
        fingerprint = hashlib.sha256(canonical({"vat_mode": vat_mode,
            "transactions": [(t["id"], t["revision"]) for t in selected]}).encode()).hexdigest()
        return {"year": year, "count": len(selected), "totals": {k: str(v) for k, v in totals.items()},
                "difference_krw": str(totals["income"] - totals["expense"]),
                "monthly": [{k: str(v) if k != "month" else v for k, v in m.items()} for m in monthly],
                "pending_ids": sorted(pending), "vat_pending_ids": sorted(vat_pending),
                "excluded_ids": sorted(excluded), "fingerprint": fingerprint,
                "rows": sorted(ledger_rows, key=lambda r: (r["date"], r["transaction_id"]))}

    def state(self):
        with closing(self.connect()) as db:
            db.execute("BEGIN")
            settings = dict(db.execute("SELECT name,year FROM settings WHERE id=1").fetchone())
            profile = db.execute("SELECT vat_mode FROM profiles WHERE year=?", (settings["year"],)).fetchone()
            settings["vat_mode"] = profile[0] if profile else "unconfirmed"
            rows = self._rows(db)
            summary = self.summarize(rows, settings["year"], settings["vat_mode"])
            review = db.execute("SELECT * FROM reviews WHERE year=?", (settings["year"],)).fetchone()
            summary["reviewed"] = bool(review and review["fingerprint"] == summary["fingerprint"])
            summary["reviewed_at"] = review["reviewed_at"] if summary["reviewed"] else None
            return {"settings": settings, "transactions": rows, "summary": summary,
                    "categories": CATEGORIES, "years": sorted({2025, 2026, settings["year"], *(int(r["date"][:4]) for r in rows)})}

    def settings(self, name, year, vat_mode=None):
        name = text(name, "사업장 이름", 80, required=True)
        if type(year) is not int or not 1900 <= year <= 2100:
            raise LedgerError("장부 연도를 확인해 주세요.")
        if vat_mode is not None and vat_mode not in ("unconfirmed", "general", "simplified", "exempt"):
            raise LedgerError("확인한 과세유형을 선택해 주세요.")
        with closing(self.connect()) as db, db:
            db.execute("UPDATE settings SET name=?,year=? WHERE id=1", (name, year))
            if vat_mode is not None:
                db.execute("INSERT OR REPLACE INTO profiles VALUES (?,?)", (year, vat_mode))

    def review(self, year):
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            profile = db.execute("SELECT vat_mode FROM profiles WHERE year=?", (year,)).fetchone()
            vat_mode = profile[0] if profile else "unconfirmed"
            if vat_mode == "unconfirmed":
                raise LedgerError("선택 연도의 과세유형을 사업장 설정에서 먼저 확인해 주세요.")
            summary = self.summarize(self._rows(db), year, vat_mode)
            if not summary["count"] or summary["pending_ids"] or summary["vat_pending_ids"]:
                raise LedgerError("거래 분류·환불 연결·부가세액을 먼저 확인해 주세요.")
            db.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?)", (year, summary["fingerprint"], now()))

    def export(self, year, kind="ledger"):
        state = self.state()
        summary = self.summarize(state["transactions"], year, state["settings"]["vat_mode"])
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        safe = lambda v: "'" + v if v.lstrip("\ufeff \t\r\n").startswith(("=", "+", "-", "@")) else v
        if kind == "transactions":
            writer.writerow(["일자", "거래처", "입출금", "원래금액", "분류", "분할금액", "부가세액", "메모",
                             "연결종류", "연결거래ID", "출처", "거래번호", "원본행", "거래ID", "수정버전"])
            for tx in state["transactions"]:
                if int(tx["date"][:4]) != year:
                    continue
                for part in tx["allocations"]:
                    writer.writerow([tx["date"], safe(tx["merchant"]), tx["direction"], tx["amount_krw"],
                        part["category"] or "미분류", part["amount_krw"], part["vat_krw"] or "",
                        safe(part["note"]), tx["link_kind"], tx["link_target"] or "", safe(tx["source"]),
                        safe(tx["reference"]), tx["row_number"], tx["id"], tx["revision"]])
            return ("\ufeff" + output.getvalue()).encode("utf-8")
        if kind != "ledger" or year != state["settings"]["year"] or not state["summary"]["reviewed"]:
            raise LedgerError("선택 연도의 거래와 부가세액을 확인하고 자료 확인을 완료해 주세요. 검토 중에는 거래내역을 내려받을 수 있습니다.")
        writer.writerow(["일자", "거래처", "계정과목", "수입금액", "수입부가세", "비용금액", "비용부가세",
                         "자산증감금액", "자산부가세", "거래총액", "확인상태", "메모", "자료출처", "거래번호", "원본행", "거래ID", "수정버전", "과세유형"])
        for r in summary["rows"]:
            amounts = []
            for kind in ("income", "expense", "asset"):
                amounts.extend([r["book_amount_krw"] or "", r["book_vat_krw"] or ""] if r["kind"] == kind else ["", ""])
            # Only text cells are escaped; signed integer money remains numeric.
            writer.writerow([r["date"], safe(r["merchant"]), r["category"], *amounts, r["gross_krw"],
                             "분류·자료 확인",
                             safe(r["note"]), safe(r["source"]), safe(r["reference"]), r["row_number"], r["transaction_id"],
                             r["revision"], {"general": "일반과세자", "simplified": "간이과세자", "exempt": "면세사업자"}[state["settings"]["vat_mode"]]])
        return ("\ufeff" + output.getvalue()).encode("utf-8")
