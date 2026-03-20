from __future__ import annotations

import ipaddress
import json
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from services.errors import ProviderSearchError

REDIRECT_CODES = {301, 302, 303, 307, 308}
RETRYABLE_HTTP_STATUS_CODES = {429}


@dataclass(frozen=True, slots=True)
class HttpClientConfig:
    user_agent: str
    accept_json: str = "application/json,text/plain,*/*"
    accept_language: str = "en-US,en;q=0.9"
    accept_images: str = "image/*,*/*;q=0.8"


class SafeHttpClient:
    def __init__(self, config: HttpClientConfig):
        self._config = config
        self._default_opener = urllib.request.build_opener()
        self._no_redirect_opener = urllib.request.build_opener(NoRedirectHandler())
        self._lock = threading.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return bool(self._closed)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._default_opener.handlers[:] = []
            self._no_redirect_opener.handlers[:] = []

    def get_json(
        self,
        url: str,
        *,
        provider_id: str,
        params: dict[str, object] | None = None,
        headers: dict[str, str] | None = None,
        timeout_seconds: float,
    ) -> dict[str, object]:
        request = urllib.request.Request(
            self._final_url(url, params=params),
            headers=self._request_headers(
                accept=self._config.accept_json,
                extra_headers=headers,
            ),
            method="GET",
        )

        try:
            with self._open(self._default_opener, request, timeout_seconds) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise self._provider_error_from_http_error(
                exc,
                provider_id=provider_id,
                url=request.full_url,
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise ProviderSearchError(
                code="provider_network_error",
                message=(
                    f"{provider_id} request failed for '{request.full_url}': {exc}"
                ),
                provider_id=provider_id,
                retryable=True,
                details={"url": request.full_url},
            ) from exc

        try:
            data = json.loads(raw.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as exc:
            raise ProviderSearchError(
                code="provider_malformed_payload",
                message=f"{provider_id} returned malformed JSON payload.",
                provider_id=provider_id,
                details={"url": request.full_url},
            ) from exc

        if not isinstance(data, dict):
            raise ProviderSearchError(
                code="provider_malformed_payload",
                message=f"{provider_id} returned a non-object JSON payload.",
                provider_id=provider_id,
                details={"url": request.full_url},
            )
        return data

    def open_with_safe_redirects(
        self,
        raw_url: str,
        *,
        timeout_seconds: float,
        max_redirects: int,
        accept_header: str,
    ) -> tuple[Any, str]:
        current = validate_public_url(raw_url)
        headers = self._request_headers(accept=accept_header)

        for _ in range(max_redirects + 1):
            request = urllib.request.Request(current, headers=headers, method="GET")
            try:
                response = self._open(
                    self._no_redirect_opener,
                    request,
                    timeout_seconds,
                )
            except urllib.error.HTTPError as exc:
                if exc.code in REDIRECT_CODES:
                    location = exc.headers.get("Location")
                    exc.close()
                    if not location:
                        raise ValueError(
                            "Redirect response has no Location header"
                        ) from exc
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

    def _final_url(
        self,
        url: str,
        *,
        params: dict[str, object] | None = None,
    ) -> str:
        if not params:
            return url
        query = urllib.parse.urlencode(params, doseq=True)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}{query}"

    def _request_headers(
        self,
        *,
        accept: str,
        extra_headers: dict[str, str] | None = None,
    ) -> dict[str, str]:
        headers = {
            "User-Agent": self._config.user_agent,
            "Accept": accept,
            "Accept-Language": self._config.accept_language,
        }
        if extra_headers:
            headers.update(extra_headers)
        return headers

    def _open(
        self,
        opener: urllib.request.OpenerDirector,
        request: urllib.request.Request,
        timeout_seconds: float,
    ) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("SafeHttpClient is closed")
        return opener.open(request, timeout=timeout_seconds)

    def _provider_error_from_http_error(
        self,
        exc: urllib.error.HTTPError,
        *,
        provider_id: str,
        url: str,
    ) -> ProviderSearchError:
        status_code = int(getattr(exc, "code", 0) or 0)
        retryable = status_code in RETRYABLE_HTTP_STATUS_CODES or 500 <= status_code < 600
        return ProviderSearchError(
            code=f"provider_http_{status_code or 'unknown'}",
            message=f"{provider_id} request failed with HTTP {status_code}.",
            provider_id=provider_id,
            retryable=retryable,
            details={"http_status": status_code, "url": url},
        )


class ManagedResponse:
    def __init__(self, response: Any, release: Callable[[], None]):
        self._response = response
        self._release = release
        self._closed = False

    @property
    def headers(self) -> Any:
        return self._response.headers

    def read(self, size: int = -1) -> bytes:
        return self._response.read(size)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        finally:
            self._release()

    def __enter__(self) -> "ManagedResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        del exc_type, exc, tb
        self.close()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


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
    provider_id: str,
    http_client: SafeHttpClient | None = None,
) -> dict[str, object]:
    client = http_client or SafeHttpClient(HttpClientConfig(user_agent=user_agent))
    try:
        return client.get_json(
            url,
            provider_id=provider_id,
            params=params,
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
    finally:
        if http_client is None:
            client.close()


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
    http_client: SafeHttpClient | None = None,
) -> tuple[Any, str]:
    client = http_client or SafeHttpClient(HttpClientConfig(user_agent=user_agent))
    try:
        response, final_url = client.open_with_safe_redirects(
            raw_url,
            timeout_seconds=timeout_seconds,
            max_redirects=max_redirects,
            accept_header=accept_header,
        )
        if http_client is None:
            return ManagedResponse(response, client.close), final_url
        return response, final_url
    except Exception:
        if http_client is None:
            client.close()
        raise


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
    iterator = None
    if hasattr(response, "iter_bytes"):
        iterator = response.iter_bytes(chunk_size=chunk_size)
    elif hasattr(response, "stream"):
        iterator = response.stream(chunk_size)

    if iterator is not None:
        for chunk in iterator:
            if not chunk:
                continue
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(
                    f"{payload_name} exceeds max size of {max_bytes} bytes"
                )
    else:
        while True:
            chunk = response.read(chunk_size)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > max_bytes:
                raise ValueError(
                    f"{payload_name} exceeds max size of {max_bytes} bytes"
                )

    if not data:
        raise ValueError("Empty response body")
    return bytes(data)


__all__ = [
    "HttpClientConfig",
    "NoRedirectHandler",
    "SafeHttpClient",
    "http_get_json",
    "is_public_host",
    "open_with_safe_redirects",
    "read_limited",
    "validate_public_url",
]
