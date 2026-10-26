"""Synthetic fixtures: no official source redistribution or live credentials."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ko_booking.archive import Archive
from ko_booking.common import CollectionError, HttpClient, iso_date, public_url
from ko_booking.law import LawCollector, parse_detail, parse_list
from ko_booking.rates import HEADERS, collect_rates, discover_notice, parse_rows, parse_workbook


NOTICE = '''<h3>2025년 귀속 기준경비율·단순경비율</h3>
<li><strong>작성일자</strong>2026.04.29.</li>
<a href="/comm/nttFileDownload.do?fileKey=synthetic">2025년 경비율.xlsx</a>'''.encode()
URL = "https://www.nts.go.kr/nts/na/ntt/selectNttInfo.do?nttSn=synthetic"
ROW = ["2025", "011001", "업태", "중분류", "세분류", "세세분류", "합성 예시", 93.5, 0, None]
LIST = '''<LawSearch><totalCnt>1</totalCnt><page>1</page><law>
<법령일련번호>123</법령일련번호><법령ID>000001</법령ID><법령명한글>시험법</법령명한글>
<시행일자>20260101</시행일자><공포일자>20251201</공포일자><현행연혁코드>현행</현행연혁코드>
</law></LawSearch>'''.encode()
DETAIL = '''<법령 법령키="00000120251201"><기본정보><법령ID>000001</법령ID>
<법령명_한글>시험법</법령명_한글><시행일자>20260101</시행일자><공포일자>20251201</공포일자>
</기본정보><조문><조문단위><조문번호>1</조문번호><항><항내용>첫째</항내용></항>
<항><항내용>둘째</항내용></항></조문단위></조문><부칙><부칙단위><부칙내용>적용례</부칙내용>
</부칙단위></부칙><별표><별표단위><별표제목>서식</별표제목></별표단위></별표></법령>'''.encode()


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return next(self.responses), {"source_url": public_url(url), "fetched_at": "synthetic"}


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.archive = Archive(Path(self.tmp.name) / "catalog.sqlite3")

    def test_archive_changes_reversion_and_export(self):
        meta = {"extension": ".xml"}
        observations = [self.archive.save("source", raw, data, meta) for raw, data in
                        [(b"one", {"v": 1}), (b"one", {"v": 1}),
                         (b"format", {"v": 1}), (b"two", {"v": 2}), (b"one", {"v": 1})]]
        self.assertEqual([o["state"] for o in observations],
                         ["new", "unchanged", "raw_changed", "changed", "changed"])
        self.assertEqual(observations[0]["version_id"], observations[-1]["version_id"])
        out = Path(self.tmp.name) / "export"
        self.archive.export(observations[0]["version_id"], out)
        self.assertEqual((out / "original.xml").read_bytes(), b"one")
        self.assertEqual(json.loads((out / "normalized.json").read_text()), {"v": 1})
        self.assertEqual(len(self.archive.list_observations()), 5)

    def test_notice_year_and_attachment(self):
        notice = discover_notice(NOTICE, URL, 2025)
        self.assertEqual(notice["published_at"], "2026-04-29")
        self.assertTrue(notice["attachment_url"].startswith("https://www.nts.go.kr/comm/"))
        with self.assertRaises(CollectionError):
            discover_notice(NOTICE, URL, 2026)
        with self.assertRaises(CollectionError):
            discover_notice(NOTICE + NOTICE, URL, 2025)

    def test_rate_identifier_zero_and_missing_are_distinct(self):
        result = parse_rows([HEADERS, ROW], 2025, "synthetic")[0]
        self.assertEqual(result["industry_code"], "011001")
        self.assertEqual(result["simple_general_pct"], "93.5")
        self.assertEqual(result["simple_excess_pct"], "0")
        self.assertIsNone(result["standard_general_pct"])
        self.assertEqual(result["source_row"], 2)

    def test_rate_rejects_wrong_year_duplicate_schema_and_invalid_rate(self):
        for rows, year in [([HEADERS, ROW], 2026), ([HEADERS, ROW, ROW], 2025),
                           ([HEADERS[:-1], ROW], 2025), ([HEADERS, ROW[:7] + [101, 0, 0]], 2025),
                           ([HEADERS, ROW[:7] + ["NaN", 0, 0]], 2025),
                           ([HEADERS, ROW[:1] + [11001] + ROW[2:]], 2025),
                           ([HEADERS, ROW[:7] + ["=1+1", 0, 0]], 2025)]:
            with self.subTest(rows=rows, year=year), self.assertRaises(CollectionError):
                parse_rows(rows, year, "synthetic")

    def test_html_download_does_not_become_a_workbook(self):
        with self.assertRaises(CollectionError):
            parse_workbook(b"<html>login required</html>", 2025)

    def test_failed_attachment_saves_no_success(self):
        with self.assertRaises(CollectionError):
            collect_rates(FakeClient([NOTICE, b"invalid"]), self.archive, 2025, URL)
        self.assertEqual(self.archive.list_observations(), [])

    def test_law_identity_and_full_tree(self):
        self.assertEqual(parse_list(LIST)[2][0]["effective_at"], "2026-01-01")
        document = parse_detail(DETAIL, "123", "20260101", "000001")
        text = json.dumps(document, ensure_ascii=False)
        for expected in ["첫째", "둘째", "적용례", "서식"]:
            self.assertIn(expected, text)
        for mst, date, law_id in [("123", "20270101", "000001"), ("123", "20260101", "2")]:
            with self.assertRaises(CollectionError):
                parse_detail(DETAIL, mst, date, law_id)

    def test_api_errors_and_empty_partial_pages(self):
        for body in [b"<html>error</html>", b"invalid", b'<!DOCTYPE x><LawSearch/>',
                     b"<LawSearch><totalCnt>1</totalCnt><page>1</page></LawSearch>"]:
            with self.assertRaises(CollectionError):
                parse_list(body)

    def test_pagination_failure_does_not_publish_partial_index(self):
        client = FakeClient([LIST.replace(b"<totalCnt>1", b"<totalCnt>2"), LIST])
        with self.assertRaises(CollectionError):
            LawCollector(client, self.archive, "synthetic").search("시험법")
        self.assertEqual(self.archive.list_observations(), [])

    def test_no_credential_in_request_metadata(self):
        client = FakeClient([LIST])
        result = LawCollector(client, self.archive, "secret-synthetic").search("시험법")
        out = Path(self.tmp.name) / "export"
        self.archive.export(result["version_id"], out)
        self.assertNotIn("secret-synthetic", (out / "metadata.json").read_text())

    def test_missing_credential_and_unsafe_host(self):
        with patch.dict("os.environ", {}, clear=True), self.assertRaises(CollectionError):
            LawCollector(FakeClient([]), self.archive)
        for url in ["http://www.law.go.kr/", "https://example.com/", "https://www.nts.go.kr:9999/"]:
            with self.assertRaises(CollectionError):
                HttpClient.validate(url)

    def test_curl_does_not_follow_unapproved_redirect(self):
        from types import SimpleNamespace

        def redirect(args, **kwargs):
            path = args[args.index("--dump-header") + 1]
            Path(path).write_text("HTTP/2 302\nLocation: https://example.com/private\n\n")
            return SimpleNamespace(returncode=0, stdout="302")

        with patch("ko_booking.common.subprocess.run", side_effect=redirect), self.assertRaises(CollectionError):
            HttpClient(interval=0, transport="curl").get("https://www.law.go.kr/DRF/lawSearch.do")

    def test_full_pagination_succeeds_and_empty_index_is_valid(self):
        from zipfile import ZipFile

        first = LIST.replace(b"<totalCnt>1", b"<totalCnt>2")
        second = first.replace(b"<page>1", b"<page>2").replace(b">123<", b">456<")
        result = LawCollector(FakeClient([first, second]), self.archive, "synthetic").search("시험법")
        self.assertEqual(len(result["records"]), 2)
        out = Path(self.tmp.name) / "pages"
        self.archive.export(result["version_id"], out)
        with ZipFile(out / "original.pages.zip") as bundle:
            self.assertEqual(bundle.read("page-0001.xml"), first)
            self.assertEqual(bundle.read("page-0002.xml"), second)
        empty = b"<LawSearch><totalCnt>0</totalCnt><page>1</page></LawSearch>"
        self.assertEqual(parse_list(empty), (0, 1, []))

    def test_invalid_source_dates(self):
        for value in ["2026111", "20260230", "not a date"]:
            with self.assertRaises(CollectionError):
                iso_date(value)

    def test_api_failure_code_is_not_an_empty_success(self):
        with self.assertRaises(CollectionError):
            parse_list(b"<LawSearch><resultCode>99</resultCode><totalCnt>0</totalCnt><page>1</page></LawSearch>")


if __name__ == "__main__":
    unittest.main()
