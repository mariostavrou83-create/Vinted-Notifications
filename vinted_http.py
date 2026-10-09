"""Verified browser HTTP transport with the Requests session/response interface."""

import copy
import io
import time
from datetime import timedelta
from http.client import HTTPMessage
from types import SimpleNamespace

import requests
from curl_cffi import requests as curl_requests
from curl_cffi.requests import exceptions as curl_errors
from requests.cookies import RequestsCookieJar, extract_cookies_to_jar
from requests.hooks import dispatch_hook
from requests.structures import CaseInsensitiveDict
from requests.utils import get_encoding_from_headers, resolve_proxies, select_proxy

# Native chrome146 is documented by curl_cffi as macOS. The TLS/HTTP2 profile
# stays pinned to that browser version; HTTP identity is explicitly Windows
# Chrome 146 for the matching account connection and challenge-service UA.
BROWSER_IMPERSONATE = "chrome146"
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
BROWSER_CLIENT_HINTS = {
    "Sec-CH-UA": '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}
NAVIGATION_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
    "Priority": "u=0, i",
}
API_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Dest": "empty",
    "Priority": "u=1, i",
}


def _request_error(error):
    """Keep exception categories without SDK URLs, headers or credential values."""
    for sdk_type, request_type in (
        (curl_errors.SSLError, requests.exceptions.SSLError),
        (curl_errors.ProxyError, requests.exceptions.ProxyError),
        (curl_errors.ConnectTimeout, requests.exceptions.ConnectTimeout),
        (curl_errors.ReadTimeout, requests.exceptions.ReadTimeout),
        (curl_errors.Timeout, requests.exceptions.Timeout),
        (curl_errors.ConnectionError, requests.exceptions.ConnectionError),
        (curl_errors.ContentDecodingError, requests.exceptions.ContentDecodingError),
        (curl_errors.ChunkedEncodingError, requests.exceptions.ChunkedEncodingError),
    ):
        if isinstance(error, sdk_type):
            return request_type("Vinted HTTP transport failed.")
    return requests.exceptions.RequestException("Vinted HTTP transport failed.")


class _ResponseBody:
    """Expose bounded chunks and urllib3-compatible cookie extraction metadata."""

    def __init__(self, response, message, streaming):
        self._response = response
        self._original_response = SimpleNamespace(msg=message)
        self._streaming = streaming
        self._iterator = None
        self._buffer = b""
        self._closed = False
        self._exhausted = False
        self._body = None if streaming else io.BytesIO(response.content)

    def _chunks(self):
        if self._iterator is None:
            self._iterator = iter(self._response.iter_content())
        return self._iterator

    def stream(self, amount, decode_content=True):
        if amount is not None and amount <= 0:
            raise ValueError("Chunk size must be positive.")
        failed = False
        try:
            if self._closed:
                return
            if not self._streaming:
                while chunk := self._body.read(amount or -1):
                    yield chunk
                return
            while not self._closed:
                if self._buffer:
                    chunk, self._buffer = self._buffer, b""
                else:
                    if self._exhausted:
                        break
                    try:
                        chunk = next(self._chunks())
                    except StopIteration:
                        self._exhausted = True
                        break
                if not chunk:
                    continue
                if amount is None:
                    yield chunk
                else:
                    for offset in range(0, len(chunk), amount):
                        if self._closed:
                            return
                        yield chunk[offset : offset + amount]
        except curl_errors.RequestException as error:
            failed = True
            raise _request_error(error) from None
        finally:
            try:
                self.close()
            except requests.exceptions.RequestException:
                if not failed:
                    raise

    def read(self, amount=None, decode_content=True):
        if self._closed:
            return b""
        if not self._streaming:
            return self._body.read(-1 if amount is None else amount)
        if amount == 0:
            return b""
        if amount is None or amount < 0:
            value, self._buffer = self._buffer, b""
            return value + b"".join(self.stream(None))
        try:
            while len(self._buffer) < amount and not self._exhausted:
                self._buffer += next(self._chunks())
        except StopIteration:
            self._exhausted = True
        except curl_errors.RequestException as error:
            try:
                self.close()
            except requests.exceptions.RequestException:
                pass
            raise _request_error(error) from None
        value, self._buffer = self._buffer[:amount], self._buffer[amount:]
        if self._exhausted and not self._buffer:
            self.close()
        return value

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self._response.close()
        except curl_errors.RequestException as error:
            raise _request_error(error) from None

    release_conn = close


class BrowserSession(requests.Session):
    """Use one verified Chrome-profile connection while preserving Requests APIs.

    Redirects and retries are always disabled here. Callers classify redirects
    and explicitly request any authorised next operation themselves.
    """

    def __init__(self):
        super().__init__()
        self.headers.update(BROWSER_CLIENT_HINTS)
        self.headers["User-Agent"] = BROWSER_USER_AGENT
        self._browser = curl_requests.Session(
            impersonate=BROWSER_IMPERSONATE,
            default_headers=False,
            verify=True,
            trust_env=False,
            retry=0,
        )
        # Requests resolves custom CA settings before send(); avoid an SDK
        # environment default overriding trust_env=False on individual calls.
        self._browser.verify = True

    def send(self, request, **kwargs):
        if not isinstance(request, requests.PreparedRequest):
            raise TypeError("You can only send PreparedRequests.")
        streaming = kwargs.get("stream", self.stream)
        verify = kwargs.get("verify", self.verify)
        if verify is False or not verify:
            raise requests.exceptions.SSLError("Vinted TLS verification is required.")
        proxies = kwargs.get("proxies")
        if proxies is None:
            proxies = resolve_proxies(request, self.proxies, self.trust_env)
        proxy = select_proxy(request.url, proxies)
        headers = CaseInsensitiveDict(request.headers)
        headers.pop("Cookie", None)
        if (
            request.method == "POST"
            and request.body is None
            and "Content-Type" not in headers
        ):
            # Requests removed this header for a bodyless POST. Preserve that
            # omission on the wire: curl's None sentinel suppresses libcurl's
            # automatic application/x-www-form-urlencoded default.
            headers["Content-Type"] = None
        # A caller override must not make the request differ from the identity
        # supplied to the account's challenge solver or persisted connection.
        headers.update(BROWSER_CLIENT_HINTS)
        headers["User-Agent"] = BROWSER_USER_AGENT
        jar = RequestsCookieJar()
        source = getattr(request, "_cookies", None)
        if source is None:
            source = self.cookies
        jar.set_policy(copy.copy(source.get_policy()))
        for cookie in source:
            jar.set_cookie(copy.copy(cookie))
        self._browser.cookies = jar
        start = time.monotonic()
        try:
            result = self._browser.request(
                request.method,
                request.url,
                content=request.body,
                headers=dict(headers),
                timeout=kwargs.get("timeout"),
                verify=verify,
                cert=kwargs.get("cert", self.cert),
                proxies={"all": proxy or ""},
                stream=streaming,
                allow_redirects=False,
            )
        except curl_errors.RequestException as error:
            raise _request_error(error) from None
        message = HTTPMessage()
        for name, value in result.headers.multi_items():
            if value is not None:
                message.add_header(name, value)
        response = requests.Response()
        response.status_code = result.status_code
        response.url = result.url
        response.reason = result.reason
        response.headers = CaseInsensitiveDict(result.headers.items())
        response.encoding = get_encoding_from_headers(response.headers)
        response.elapsed = timedelta(seconds=time.monotonic() - start)
        response.request = request
        response.raw = _ResponseBody(result, message, streaming)
        # Only cookies genuinely supplied by this response establish rotation.
        extract_cookies_to_jar(response.cookies, request, response.raw)
        extract_cookies_to_jar(self.cookies, request, response.raw)
        if not streaming:
            response._content = result.content
            response._content_consumed = True
        return dispatch_hook("response", request.hooks, response, **kwargs)

    def close(self):
        try:
            self._browser.close()
        except curl_errors.RequestException as error:
            raise _request_error(error) from None
        finally:
            super().close()
