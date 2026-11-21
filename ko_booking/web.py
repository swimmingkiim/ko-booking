"""Loopback-only development app. No cloud hosting or financial-provider access."""

import base64
import binascii
import json
import sqlite3
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .archive import Archive
from .imports import LedgerError, inspect_csv
from .ledger import Ledger
from .tax import CalculationError, business_income, income_tax_base, reviewed_rates, rulebook


def rate_status(path):
    if not Path(path).is_file():
        return {"available": False}
    expected = rulebook(2025)["reviewed_rate_snapshot"]
    with closing(sqlite3.connect(f"file:{Path(path).resolve()}?mode=ro", uri=True)) as db:
        row = db.execute("SELECT id FROM versions WHERE source_key=? AND raw_sha256=? AND normalized_sha256=?",
                         (expected["source_key"], expected["raw_sha256"], expected["normalized_sha256"])).fetchone()
    return {"available": bool(row), "version_id": row[0] if row else None, "tax_year": 2025}


def make_server(ledger, source_db, port=8765):
    class Handler(BaseHTTPRequestHandler):
        server_version = "KoBooking/0.1"

        def log_message(self, format, *args):
            # Do not write imported financial data or arbitrary request paths to terminal logs.
            pass

        def send(self, status, body, content_type="application/json; charset=utf-8", attachment=None):
            if isinstance(body, (dict, list)):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            if attachment:
                self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
            self.end_headers()
            self.wfile.write(body)

        def allowed(self, mutation=False):
            hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
            host = self.headers.get("Host")
            if host not in hosts:
                self.send(403, {"error": "로컬 앱 주소로 접속해 주세요."})
                return False
            if mutation and (self.headers.get("Origin") != "http://" + host
                             or self.headers.get("X-Ko-Booking") != "local-app"):
                self.send(403, {"error": "앱 화면에서 요청해 주세요."})
                return False
            return True

        def do_GET(self):
            if not self.allowed():
                return
            url = urlsplit(self.path)
            try:
                if url.path == "/api/state":
                    state = ledger.state()
                    try:
                        state["rates"] = rate_status(source_db)
                    except sqlite3.Error:
                        state["rates"] = {"available": False}
                    self.send(200, state)
                elif url.path.startswith("/api/history/"):
                    self.send(200, ledger.history(url.path.rsplit("/", 1)[1]))
                elif url.path == "/api/export.csv":
                    params = parse_qs(url.query)
                    year = int(params.get("year", [ledger.state()["settings"]["year"]])[0])
                    kind = params.get("kind", ["ledger"])[0]
                    body = ledger.export(year, kind)
                    self.send(200, body, "text/csv; charset=utf-8", f"ko-booking-{kind}-{year}.csv")
                elif url.path in ("/", "/app.js", "/styles.css", "/demo.csv"):
                    name = {"/": "index.html"}.get(url.path, url.path[1:])
                    content_type = {"index.html": "text/html; charset=utf-8", "app.js": "text/javascript; charset=utf-8",
                                    "styles.css": "text/css; charset=utf-8", "demo.csv": "text/csv; charset=utf-8"}[name]
                    self.send(200, files("ko_booking").joinpath("static", name).read_bytes(), content_type,
                              "ko-booking-demo.csv" if name == "demo.csv" else None)
                else:
                    self.send(404, {"error": "페이지를 찾을 수 없습니다."})
            except (ValueError, OSError) as error:
                self.send(400, {"error": str(error)})
            except sqlite3.Error:
                self.send(500, {"error": "로컬 자료를 읽지 못했습니다. 앱을 다시 실행해 주세요."})

        def do_POST(self):
            if not self.allowed(mutation=True):
                return
            try:
                if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                    raise LedgerError("JSON 요청이 필요합니다.")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 3 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise LedgerError("요청 크기를 확인해 주세요.")
                data = json.loads(self.rfile.read(size))
                if not isinstance(data, dict):
                    raise LedgerError("요청 항목을 확인해 주세요.")
                path = urlsplit(self.path).path
                if path in ("/api/import/inspect", "/api/import/preview"):
                    try:
                        raw = base64.b64decode(data["content"], validate=True)
                    except (binascii.Error, TypeError):
                        raise LedgerError("파일을 다시 선택해 주세요.") from None
                    result = inspect_csv(raw) if path.endswith("inspect") else ledger.preview(
                        data["filename"], raw, data["mapping"], data["source_name"])
                elif path == "/api/import/commit":
                    result = ledger.commit(data["token"], data["decisions"])
                elif path.startswith("/api/transaction/"):
                    result = ledger.edit(path.rsplit("/", 1)[1], data)
                elif path == "/api/settings":
                    ledger.settings(data["name"], data["year"], data.get("vat_mode"))
                    result = {"saved": True}
                elif path == "/api/review":
                    if data.get("confirmed") is not True or type(data.get("year")) is not int:
                        raise LedgerError("선택 연도 자료를 확인해 주세요.")
                    ledger.review(data["year"])
                    result = {"reviewed": True}
                elif path == "/api/tax/base":
                    if data.get("confirmed") is not True:
                        raise LedgerError("공제 등을 반영한 과세표준인지 먼저 확인해 주세요.")
                    result = income_tax_base(data["year"], data["taxable_income_krw"])
                elif path == "/api/tax/business":
                    if not Path(source_db).is_file():
                        raise LedgerError("검토한 경비율 자료가 아직 준비되지 않았습니다.")
                    calculation = data["calculation"]
                    evidence = reviewed_rates(Archive(source_db), data["version_id"],
                                              calculation["tax_year"], calculation["industry_code"])
                    result = business_income(calculation, evidence)
                else:
                    self.send(404, {"error": "요청 경로를 찾을 수 없습니다."})
                    return
                self.send(200, result)
            except (ValueError, KeyError, TypeError, OSError) as error:
                message = str(error) if isinstance(error, ValueError) else "입력 항목을 확인해 주세요."
                if isinstance(error, CalculationError):
                    message = ("경비율 자료가 검토한 판본과 다릅니다. 자료를 재검토해 주세요."
                               if "snapshot" in str(error) else "계산 입력과 적용 조건을 확인해 주세요. 현재 검토한 2025년 귀속의 지원 범위 안에서 계산할 수 있습니다.")
                self.send(400, {"error": message})
            except sqlite3.Error:
                self.send(500, {"error": "로컬 저장에 실패했습니다. 변경은 적용되지 않았습니다."})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    return server


def serve(ledger_path, source_db, port):
    with make_server(Ledger(ledger_path), source_db, port) as server:
        print(f"ko-booking 로컬 앱: http://127.0.0.1:{server.server_port}", flush=True)
        print("종료: Ctrl+C", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
