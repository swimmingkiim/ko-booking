"""Effective-date law editions; retains articles, addenda and annex metadata."""

import os
import re
import xml.etree.ElementTree as ET
from io import BytesIO
from urllib.parse import urlencode
from zipfile import ZipFile, ZipInfo

from .common import CollectionError, iso_date, public_url

BASE = "https://www.law.go.kr/DRF/"


def xml_root(body, expected):
    if b"<!DOCTYPE" in body.upper() or b"<!ENTITY" in body.upper():
        raise CollectionError("Unexpected XML document declaration")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise CollectionError("Law API did not return valid XML") from None
    if root.tag != expected:
        raise CollectionError("Unexpected law response; check authentication and API availability")
    return root


def required(node, field):
    value = node.findtext(field)
    if not value or not value.strip():
        raise CollectionError(f"Missing required law field: {field}")
    return value.strip()


def parse_list(body):
    root = xml_root(body, "LawSearch")
    if root.findtext("resultCode") not in (None, "00"):
        raise CollectionError("Law API returned an unsuccessful result code")
    try:
        total, page = int(required(root, "totalCnt")), int(required(root, "page"))
    except ValueError:
        raise CollectionError("Invalid law result count or page") from None
    records = []
    for law in root.findall("law"):
        records.append({"mst": required(law, "법령일련번호"),
                        "law_id": required(law, "법령ID"),
                        "name": required(law, "법령명한글"),
                        "effective_at": iso_date(required(law, "시행일자")),
                        "promulgated_at": iso_date(required(law, "공포일자")),
                        "status": required(law, "현행연혁코드")})
    if total < 0 or page < 1 or (total > 0 and not records):
        raise CollectionError("Incomplete law search response")
    return total, page, records


def tree_value(node):
    """Keep repeated children and attributes instead of dropping legal text."""
    return {"tag": node.tag, "attributes": node.attrib,
            "text": public_url(node.text.strip()) if node.text and node.text.strip().startswith("https://")
            else (node.text or "").strip(),
            "tail": (node.tail or "").strip(),
            "children": [tree_value(child) for child in node]}


def parse_detail(body, mst, effective, expected_law_id=None):
    root = xml_root(body, "법령")
    info = root.find("기본정보")
    if info is None:
        raise CollectionError("Law basic information is missing")
    law_id = required(info, "법령ID")
    actual_effective = iso_date(required(info, "시행일자"))
    if actual_effective != iso_date(effective) or (expected_law_id and law_id != expected_law_id):
        raise CollectionError("Returned law edition does not match the requested identity")
    # 법령키 begins with 법령ID, not the requested MST.
    key = root.attrib.get("법령키", "")
    if key and not key.startswith(law_id):
        raise CollectionError("Returned law key does not match its law ID")
    if not root.findall("./조문/조문단위"):
        raise CollectionError("Law response contains no articles")
    return {"schema_version": 1, "mst": mst, "law_id": law_id,
            "name": required(info, "법령명_한글"), "effective_at": actual_effective,
            "promulgated_at": iso_date(required(info, "공포일자")),
            "document": tree_value(root)}


class LawCollector:
    def __init__(self, client, archive, oc=None):
        self.client, self.archive = client, archive
        self.oc = oc or os.environ.get("LAW_OC")
        if not self.oc:
            raise CollectionError("Set LAW_OC to your registered law API authentication value")

    def request(self, endpoint, params):
        return self.client.get(BASE + endpoint + "?" + urlencode(
            {"OC": self.oc, "target": "eflaw", "type": "XML", **params}))

    def search(self, query, status="3", max_pages=100):
        records, originals, request_meta = [], [], []
        total_expected = None
        for page in range(1, max_pages + 1):
            raw, meta = self.request("lawSearch.do", {"query": query, "search": 1,
                "nw": status, "display": 100, "page": page, "sort": "efdes"})
            total, actual_page, found = parse_list(raw)
            if actual_page != page or (total_expected is not None and total != total_expected):
                raise CollectionError("Law pagination changed during collection; retry the search")
            total_expected = total
            records.extend(found)
            originals.append(raw)
            request_meta.append(meta)
            if len(records) >= total:
                break
        else:
            raise CollectionError("Law search reached page limit; no partial index was saved")
        identities = {(r["mst"], r["effective_at"]) for r in records}
        if len(records) != total_expected or len(identities) != len(records):
            raise CollectionError("Law search count mismatch or duplicate editions")
        # Deterministic ZIP preserves the exact original bytes, including encoding/BOM.
        buffer = BytesIO()
        with ZipFile(buffer, "w") as bundle:
            for number, body in enumerate(originals, 1):
                bundle.writestr(ZipInfo(f"page-{number:04d}.xml"), body)
        original = buffer.getvalue()
        dataset = {"schema_version": 1, "query": query, "status_filter": status,
                   "total_count": len(records), "records": records}
        result = self.archive.save(f"law:eflaw:search:{query}:{status}", original, dataset,
            {"publisher": "법제처", "requests": request_meta, "extension": ".pages.zip",
             "tax_year": None, "effective_at": None, "terms_url": "https://open.law.go.kr/LSO/usrJoin.do",
             "review_status": "source_parsed_not_applicability_reviewed"})
        return {**result, "records": records}

    def detail(self, mst, effective, expected_law_id=None):
        if not re.fullmatch(r"\d+", mst):
            raise CollectionError("MST must contain only digits")
        date = iso_date(effective)
        if date is None:
            raise CollectionError("An effective date is required")
        raw, meta = self.request("lawService.do", {"MST": mst, "efYd": date.replace("-", "")})
        document = parse_detail(raw, mst, date, expected_law_id)
        metadata = {**meta, "publisher": "법제처", "extension": ".xml", "tax_year": None,
                    "effective_at": date, "promulgated_at": document["promulgated_at"],
                    "terms_url": "https://open.law.go.kr/LSO/usrJoin.do",
                    "review_status": "source_parsed_not_applicability_reviewed"}
        return self.archive.save(f"law:eflaw:{mst}:{date}", raw, document, metadata)
