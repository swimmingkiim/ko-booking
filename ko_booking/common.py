"""Bounded HTTPS requests and lossless metadata helpers."""

import json
import re
import subprocess
import tempfile
import time
from email.parser import Parser
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class CollectionError(ValueError):
    """An incomplete or untrusted response must not become a valid dataset."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def now():
    return datetime.now(timezone.utc).isoformat()


def public_url(url):
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != "oc"]
    return urlunsplit(parts._replace(query=urlencode(params)))


def iso_date(value):
    value = re.sub(r"[.\-\s]", "", str(value or ""))
    if not value:
        return None
    if not re.fullmatch(r"\d{8}", value):
        raise CollectionError("Source date must contain exactly eight digits")
    try:
        return datetime.strptime(value, "%Y%m%d").date().isoformat()
    except ValueError:
        raise CollectionError("Invalid source date") from None


class HttpClient:
    """Only requests explicit official hosts; retries transient failures twice."""

    def __init__(self, interval=0.5, transport="urllib"):
        self.interval = interval
        self.last_request = 0.0
        self.transport = transport

    @staticmethod
    def validate(url):
        p = urlsplit(url)
        if p.scheme != "https" or p.hostname not in {
            "www.nts.go.kr", "nts.go.kr", "www.law.go.kr", "law.go.kr"
        } or p.username or p.password or p.port not in (None, 443):
            raise CollectionError("Only approved official HTTPS hosts are supported")

    def get(self, url):
        self.validate(url)
        client = self

        class Redirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                client.validate(newurl)
                return super().redirect_request(req, fp, code, msg, headers, newurl)

        opener = build_opener(Redirect())
        for attempt in range(3):
            time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                if self.transport == "curl":
                    return self._curl(url)
                request = Request(url, headers={"User-Agent": "ko-booking/0.1 official-source-research"})
                with opener.open(request, timeout=30) as response:
                    body = response.read(20 * 1024 * 1024 + 1)
                    if len(body) > 20 * 1024 * 1024:
                        raise CollectionError("Response exceeds 20 MiB limit")
                    return body, {
                        "source_url": public_url(url),
                        "resolved_url": public_url(response.url),
                        "fetched_at": now(),
                        "content_type": response.headers.get("Content-Type"),
                        "etag": response.headers.get("ETag"),
                        "last_modified": response.headers.get("Last-Modified"),
                    }
            except HTTPError as error:
                if error.code not in (429, 500, 502, 503, 504) or attempt == 2:
                    raise CollectionError(f"Official source returned HTTP {error.code}") from None
            except (URLError, TimeoutError, OSError, subprocess.TimeoutExpired):
                if attempt == 2:
                    # Exception text can contain the credential-bearing request URL.
                    raise CollectionError(
                        "Official source network/TLS request failed; check network and trusted CA configuration"
                    ) from None
            time.sleep(2 ** attempt)

    def _curl(self, url):
        """Optional OS TLS transport; validates every redirect, keeps OC off argv."""
        current = url
        for _ in range(6):
            self.validate(current)
            with tempfile.TemporaryDirectory(prefix="ko-booking-http-") as directory:
                body_path, header_path = Path(directory) / "body", Path(directory) / "headers"
                process = subprocess.run([
                    "curl", "--silent", "--show-error", "--proto", "=https", "--max-time", "30",
                    "--max-filesize", str(20 * 1024 * 1024), "--output", str(body_path),
                    "--dump-header", str(header_path), "--write-out", "%{http_code}",
                    "--user-agent", "ko-booking/0.1 official-source-research", "--config", "-",
                ], input="url = " + json.dumps(current) + "\n", text=True,
                    capture_output=True, timeout=35)
                if process.returncode:
                    raise URLError("curl transport failed")
                status = int(process.stdout)
                blocks = re.split(r"\r?\n\r?\n", header_path.read_text(encoding="iso-8859-1").strip())
                headers = Parser().parsestr(blocks[-1].split("\n", 1)[1])
                if status in (301, 302, 303, 307, 308):
                    location = headers.get("Location")
                    if not location:
                        raise CollectionError("Redirect is missing a destination")
                    current = urljoin(current, location)
                    continue
                if status >= 400:
                    raise HTTPError(current, status, "HTTP failure", headers, None)
                if status != 200:
                    raise CollectionError("Unexpected HTTP response status")
                body = body_path.read_bytes()
                if len(body) > 20 * 1024 * 1024:
                    raise CollectionError("Response exceeds 20 MiB limit")
                return body, {"source_url": public_url(url), "resolved_url": public_url(current),
                    "fetched_at": now(), "content_type": headers.get("Content-Type"),
                    "etag": headers.get("ETag"), "last_modified": headers.get("Last-Modified")}
        raise CollectionError("Too many official-source redirects")
