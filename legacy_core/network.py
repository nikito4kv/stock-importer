from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any
import urllib.error
import urllib.parse
import urllib.request


REDIRECT_CODES = {301, 302, 303, 307, 308}


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        del req, fp, code, msg, headers, newurl
        return None


def http_get_json(
    url: str,
    *,
    params: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
    user_agent: str,
) -> dict[str, object]:
    if params:
        query = urllib.parse.urlencode(params, doseq=True)
        sep = "&" if "?" in url else "?"
        final_url = f"{url}{sep}{query}"
    else:
        final_url = url

    req_headers = {
        "User-Agent": user_agent,
        "Accept": "application/json,text/plain,*/*",
    }
    if headers:
        req_headers.update(headers)

    req = urllib.request.Request(final_url, headers=req_headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        raw = resp.read()
    data = json.loads(raw.decode("utf-8", errors="replace"))
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def is_public_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower().strip(".")
    if not host or host == "localhost" or host.endswith(".local"):
        return False

    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False

    ips: set[str] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_raw = str(sockaddr[0]).split("%", 1)[0]
        ips.add(ip_raw)

    if not ips:
        return False

    for ip_raw in ips:
        try:
            ip = ipaddress.ip_address(ip_raw)
        except ValueError:
            return False
        if not ip.is_global:
            return False

    return True


def validate_public_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("Empty URL")

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Unsupported URL scheme")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("URL has no host")
    if not is_public_host(parsed.hostname):
        raise ValueError("Host is not public")
    return value


def open_with_safe_redirects(
    raw_url: str,
    *,
    timeout_seconds: float,
    max_redirects: int,
    accept_header: str,
    user_agent: str,
) -> tuple[Any, str]:
    opener = urllib.request.build_opener(NoRedirectHandler())
    current = validate_public_url(raw_url)

    headers = {
        "User-Agent": user_agent,
        "Accept": accept_header,
        "Accept-Language": "en-US,en;q=0.9",
    }

    for _ in range(max_redirects + 1):
        request = urllib.request.Request(current, headers=headers, method="GET")
        try:
            response = opener.open(request, timeout=timeout_seconds)
        except urllib.error.HTTPError as exc:
            if exc.code in REDIRECT_CODES:
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise ValueError("Redirect response has no Location header")
                next_url = urllib.parse.urljoin(current, location)
                current = validate_public_url(next_url)
                continue
            raise

        status_code = getattr(response, "status", response.getcode())
        if status_code in REDIRECT_CODES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Redirect response has no Location header")
            next_url = urllib.parse.urljoin(current, location)
            current = validate_public_url(next_url)
            continue

        final_url = validate_public_url(response.geturl())
        return response, final_url

    raise ValueError(f"Too many redirects while fetching URL: {raw_url}")


def read_limited(
    response: Any,
    *,
    max_bytes: int,
    payload_name: str,
    chunk_size: int,
) -> bytes:
    content_length = response.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                raise ValueError(
                    f"{payload_name} too large by Content-Length: {content_length} bytes"
                )
        except ValueError as exc:
            if f"{payload_name} too large" in str(exc):
                raise

    data = bytearray()
    while True:
        chunk = response.read(chunk_size)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > max_bytes:
            raise ValueError(f"{payload_name} exceeds max size of {max_bytes} bytes")

    if not data:
        raise ValueError("Empty response body")
    return bytes(data)
