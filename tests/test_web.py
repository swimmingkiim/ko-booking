import base64
import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path

from ko_booking.ledger import Ledger
from ko_booking.web import make_server


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / "ledger.sqlite3")
        self.ledger.settings("나의 사업장", 2025, "general")
        self.server = make_server(self.ledger, Path(self.temp.name) / "absent.sqlite3", 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, method, path, data=None, extra_headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        headers = {"Content-Type": "application/json", "X-Ko-Booking": "local-app",
                   "Origin": f"http://127.0.0.1:{self.server.server_port}"}
        headers.update(extra_headers or {})
        body = json.dumps(data).encode() if data is not None else None
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        status, response_headers, result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return status, response_headers, result

    def test_static_pages_and_missing_assets(self):
        for path, mime in [("/", "text/html"), ("/app.js", "text/javascript"), ("/styles.css", "text/css")]:
            status, headers, body = self.request("GET", path)
            self.assertEqual(status, 200)
            self.assertIn(mime, headers["Content-Type"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertTrue(body)
        self.assertEqual(self.request("GET", "/../../pyproject.toml")[0], 404)

    def test_mutations_require_local_origin_and_custom_header(self):
        for extra in [{"Origin": "https://example.com"}, {"Origin": ""}, {"X-Ko-Booking": ""}]:
            self.assertEqual(self.request("POST", "/api/settings", {"name": "변경", "year": 2025}, extra)[0], 403)
        self.assertEqual(self.ledger.state()["settings"]["name"], "나의 사업장")
        self.assertEqual(self.request("GET", "/api/state", extra_headers={"Host": "evil.example"})[0], 403)

    def test_complete_http_import_preview_commit_edit(self):
        raw = "거래일,거래처,입출금,금액,부가세\n2025-01-01,합성판매처,입금,11000,1000\n".encode()
        content = base64.b64encode(raw).decode()
        status, _, body = self.request("POST", "/api/import/inspect", {"content": content})
        self.assertEqual(status, 200)
        mapping = json.loads(body)["mapping"]
        status, _, body = self.request("POST", "/api/import/preview", {
            "content": content, "filename": "synthetic.csv", "source_name": "합성", "mapping": mapping})
        self.assertEqual(status, 200)
        self.assertEqual(self.ledger.state()["transactions"], [])
        token = json.loads(body)["token"]
        self.assertEqual(self.request("POST", "/api/import/commit", {"token": token, "decisions": {}})[0], 200)
        tx = self.ledger.state()["transactions"][0]
        parts = tx["allocations"]
        parts[0]["category"] = "매출"
        status, _, _ = self.request("POST", "/api/transaction/" + tx["id"], {
            "revision": 1, "allocations": parts, "link_kind": "", "link_target": None})
        self.assertEqual(status, 200)
        self.assertEqual(self.request("GET", "/api/export.csv?year=2025")[0], 400)
        self.assertEqual(self.request("POST", "/api/review", {"year": 2025, "confirmed": True})[0], 200)
        status, headers, body = self.request("GET", "/api/export.csv?year=2025")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertIn("10000", body.decode("utf-8-sig"))

    def test_tax_confirmation_and_reviewed_year_are_required(self):
        self.assertEqual(self.request("POST", "/api/tax/base", {"year": 2025, "taxable_income_krw": "30000000"})[0], 400)
        status, _, body = self.request("POST", "/api/tax/base", {
            "year": 2025, "taxable_income_krw": "30000000", "confirmed": True})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["calculated_tax_krw_exact"], "3240000")
        self.assertIsNone(json.loads(body)["final_tax_payable_krw"])
        self.assertEqual(self.request("POST", "/api/tax/base", {
            "year": 2026, "taxable_income_krw": "30000000", "confirmed": True})[0], 400)

    def test_invalid_content_and_missing_rates(self):
        self.assertEqual(self.request("POST", "/api/import/inspect", {"content": "not-base64"})[0], 400)
        self.assertEqual(self.request("POST", "/api/settings", {"name": "a", "year": 2025},
                                      {"Content-Type": "text/plain"})[0], 400)
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)["rates"]["available"])
        self.assertEqual(self.request("POST", "/api/tax/business", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
