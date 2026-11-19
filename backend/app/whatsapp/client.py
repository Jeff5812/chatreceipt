"""The WhatsApp Cloud API: sending messages and downloading media.

Failures say whether they are worth retrying. The access token is only ever sent to the Graph API
and to Meta's media hosts, and media downloads are capped by size while they stream.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.whatsapp.events import IMAGE_TYPES

_MEDIA_HOST_SUFFIXES = (".fbsbx.com", ".facebook.com", ".fbcdn.net", ".whatsapp.net")
_MAX_REDIRECTS = 3
# Graph API error codes that clear up by themselves: overload, throttling, temporary outages, and
# credentials someone can renew while the message waits.
_RETRYABLE_CODES = frozenset({0, 1, 2, 4, 10, 190, 80007, 130429, 131000, 131005, 131016, 131056, 133004})
CUSTOMER_WINDOW_CLOSED = 131047


class WhatsAppAPIError(Exception):
    def __init__(self, detail: str, *, retryable: bool, status: int | None = None, code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status = status
        self.code = code


class MediaError(Exception):
    """The image couldn't be fetched. ``retryable`` is false when asking again won't help."""

    def __init__(self, code: str, *, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


def signature_is_valid(body: bytes, header: str | None, app_secret: str) -> bool:
    """Check X-Hub-Signature-256: an HMAC-SHA256 of the raw request body, keyed by the app secret."""
    if not header or not header.startswith("sha256=") or not app_secret:
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.removeprefix("sha256=").strip().lower())


def _api_error(response: httpx.Response) -> WhatsAppAPIError:
    try:
        error = response.json().get("error") or {}
    except (ValueError, AttributeError):
        error = {}
    code = error.get("code") if isinstance(error, dict) else None
    code = code if isinstance(code, int) else None
    status = response.status_code
    retryable = status >= 500 or status == 429 or status in (401, 403) or code in _RETRYABLE_CODES
    # Only the status and code: Meta's error text can repeat the recipient's details.
    return WhatsAppAPIError(f"graph api returned {status} (code {code})", retryable=retryable, status=status, code=code)


class WhatsAppClient:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._graph_host = urlsplit(settings.whatsapp_graph_base_url).hostname or ""

    @property
    def _token(self) -> str:
        token = self._settings.whatsapp_access_token
        return token.get_secret_value() if token else ""

    @property
    def _base(self) -> str:
        return f"{self._settings.whatsapp_graph_base_url.rstrip('/')}/{self._settings.whatsapp_graph_api_version}"

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=httpx.Timeout(self._settings.whatsapp_timeout_seconds, connect=8.0),
                transport=self._transport,
                follow_redirects=False,
            )
        return self._http

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}

    async def _post_message(self, body: dict[str, Any]) -> httpx.Response:
        url = f"{self._base}/{self._settings.whatsapp_phone_number_id}/messages"
        try:
            return await self._client().post(url, json=body, headers=self._auth())
        except httpx.HTTPError as exc:
            raise WhatsAppAPIError(type(exc).__name__, retryable=True) from exc

    async def send(self, payload: dict[str, Any], *, to_phone: str | None, to_user_id: str | None) -> str:
        """Send one message and return its WhatsApp message ID."""
        address: dict[str, str]
        if to_phone:
            address = {"to": to_phone}
        elif to_user_id:
            address = {"recipient": to_user_id}
        else:
            raise WhatsAppAPIError("no recipient", retryable=False)
        response = await self._post_message({"messaging_product": "whatsapp", "recipient_type": "individual", **address, **payload})
        if response.status_code != 200:
            raise _api_error(response)
        try:
            return str(response.json()["messages"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            return ""  # accepted, but without a readable ID: never resend it

    async def upload_media(self, data: bytes, *, mime_type: str, filename: str) -> str:
        """Upload a file to WhatsApp and return the media ID a message can send it with."""
        url = f"{self._base}/{self._settings.whatsapp_phone_number_id}/media"
        try:
            response = await self._client().post(
                url,
                headers=self._auth(),
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, data, mime_type)},
            )
        except httpx.HTTPError as exc:
            raise WhatsAppAPIError(type(exc).__name__, retryable=True) from exc
        if response.status_code != 200:
            raise _api_error(response)
        try:
            return str(response.json()["id"])
        except (ValueError, KeyError, TypeError) as exc:
            raise WhatsAppAPIError("upload returned no media id", retryable=True) from exc

    async def show_typing(self, message_id: str) -> None:
        """Mark a message as read and show the typing indicator. Purely cosmetic, so failures are ignored."""
        body = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
            "typing_indicator": {"type": "text"},
        }
        try:
            await self._post_message(body)
        except WhatsAppAPIError:
            return

    def _media_host_allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if host == self._graph_host:
            return True
        return parts.scheme == "https" and any(host.endswith(suffix) for suffix in _MEDIA_HOST_SUFFIXES)

    async def download_media(self, media_id: str, *, max_bytes: int) -> bytes:
        """Fetch an image by its media ID. The download URL lasts minutes, so it's requested each time."""
        try:
            meta = await self._client().get(f"{self._base}/{media_id}", headers=self._auth())
        except httpx.HTTPError as exc:
            raise MediaError("media_lookup_failed", retryable=True) from exc
        if meta.status_code != 200:
            error = _api_error(meta)
            raise MediaError("media_not_found" if meta.status_code in (400, 404) else "media_lookup_failed", retryable=error.retryable)
        try:
            info = meta.json()
            url, mime_type = str(info["url"]), str(info.get("mime_type") or "").split(";")[0].strip().lower()
            file_size = int(info.get("file_size") or 0)
        except (ValueError, KeyError, TypeError) as exc:
            raise MediaError("media_lookup_failed", retryable=True) from exc
        if mime_type and mime_type not in IMAGE_TYPES:
            raise MediaError("media_type_rejected", retryable=False)
        if file_size > max_bytes:
            raise MediaError("media_too_large", retryable=False)
        return await self._fetch(url, max_bytes)

    async def _fetch(self, url: str, max_bytes: int) -> bytes:
        for _ in range(_MAX_REDIRECTS + 1):
            if not self._media_host_allowed(url):
                raise MediaError("media_host_rejected", retryable=False)
            try:
                async with self._client().stream("GET", url, headers=self._auth()) as response:
                    if response.is_redirect and response.headers.get("location"):
                        url = str(response.url.join(response.headers["location"]))
                        continue
                    if response.status_code != 200:
                        raise MediaError("media_download_failed", retryable=response.status_code >= 500 or response.status_code in (404, 429))
                    data = bytearray()
                    async for chunk in response.aiter_bytes():
                        data.extend(chunk)
                        if len(data) > max_bytes:
                            raise MediaError("media_too_large", retryable=False)
                    return bytes(data)
            except httpx.HTTPError as exc:
                raise MediaError("media_download_failed", retryable=True) from exc
        raise MediaError("media_download_failed", retryable=True)

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
