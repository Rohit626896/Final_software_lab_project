"""Bounded HTTP requests to public Internet addresses, with pinned DNS per hop.

No environment proxies, credentials, private addresses, or nonstandard ports.
Each redirect is resolved and validated again before connecting.
"""

import http.client
import ipaddress
import socket
import ssl
import time
from urllib.parse import quote, urljoin, urlsplit, urlunsplit


class UnsafeURL(ValueError):
    pass


def validate_url(url):
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 32 for c in url):
        raise UnsafeURL("Invalid or overly long URL.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise UnsafeURL("Invalid URL.") from exc
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise UnsafeURL("Only public HTTP and HTTPS URLs can be checked.")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("URLs containing credentials are not allowed.")
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        raise UnsafeURL("Only standard web ports are allowed.")
    host = parts.hostname.encode("idna").decode("ascii")
    try:
        answers = socket.getaddrinfo(
            host, port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        raise OSError("DNS lookup failed.") from exc
    addresses = list(dict.fromkeys(answer[4][0] for answer in answers))
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise UnsafeURL("Private, local, and reserved destinations are blocked.")
    path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%/:?@!$'()*+,;-._~")
    return host, addresses[0], urlunsplit(("", "", path, query, ""))


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


def fetch(url, method="GET", max_bytes=20 * 1024 * 1024, timeout=12, headers=None):
    deadline = time.monotonic() + timeout
    for _ in range(6):
        host, address, path = validate_url(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("Request exceeded its time budget.")
        if urlsplit(url).scheme == "https":
            conn = PinnedHTTPS(host, address, remaining)
        else:
            conn = http.client.HTTPConnection(address, 80, timeout=remaining)
        try:
            request_headers = {
                "Host": ("[" + host + "]") if ":" in host else host,
                "User-Agent": "PaperLens/1.0",
                "Accept-Encoding": "identity",
            }
            request_headers.update(headers or {})
            conn.request(method, path, headers=request_headers)
            response = conn.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise OSError("Redirect response has no destination.")
                url = urljoin(url, location)
                continue
            data = bytearray()
            if method != "HEAD":
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError("Response exceeded its time budget.")
                    if conn.sock:
                        conn.sock.settimeout(remaining)
                    chunk = response.read(min(65536, max_bytes + 1 - len(data)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > max_bytes:
                        raise ValueError("Response exceeds the download limit.")
            return {
                "status": response.status,
                "url": url,
                "body": bytes(data),
                "content_type": response.getheader("Content-Type", ""),
            }
        finally:
            conn.close()
    raise OSError("Too many redirects.")


def download_pdf(url):
    response = fetch(url, max_bytes=20 * 1024 * 1024, timeout=35)
    if not 200 <= response["status"] < 300:
        raise ValueError("PDF server returned HTTP " + str(response["status"]) + ".")
    if b"%PDF-" not in response["body"][:1024]:
        raise ValueError("The download is not a PDF; the source may require login.")
    return response["body"]
