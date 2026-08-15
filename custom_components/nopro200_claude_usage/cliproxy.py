"""Transport for reaching the Anthropic usage API through a CLIProxyAPI instance.

An entry configured with source "cliproxy" holds no Claude credentials of its
own. It asks a CLIProxyAPI instance to perform the request with a credential
that instance already stores and keeps refreshed, using two routes of its
management API:

    GET  /v0/management/auth-files  - list the stored credentials
    POST /v0/management/api-call    - run one outbound request as a credential

api-call answers 200 with an envelope of the form
``{"status_code": int, "header": {...}, "body": "<json string>"}``. The upstream
status and payload live inside it and the payload is carried as a string, so a
response has to be unwrapped twice.

One sharp edge is worth knowing about: the proxy substitutes the "$TOKEN$"
placeholder only when it recognises the requested auth_index. For an unknown
index it forwards the placeholder verbatim and Anthropic answers 401, so a 401
here means either a stale credential or a stale auth_index.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import aiohttp

from .const import (
    API_BETA_HEADER,
    CLIPROXY_API_CALL_PATH,
    CLIPROXY_AUTH_FILES_PATH,
    CLIPROXY_TIMEOUT,
    CLIPROXY_TOKEN_PLACEHOLDER,
)

_LOGGER = logging.getLogger(__name__)

# A 400 carrying one of these errors means the credential behind the requested
# auth_index is gone or unusable, which re-resolving the index may fix.
_CREDENTIAL_ERRORS = ("auth token not found", "auth token refresh failed")


class CliProxyError(Exception):
    """The CLIProxyAPI instance could not serve the request."""


class CliProxyAuthError(CliProxyError):
    """The instance rejected the management key."""


class CliProxyCredentialError(CliProxyError):
    """The instance has no usable credential for the requested auth_index."""


def normalize_base_url(base_url: str) -> str:
    """Trim whitespace and trailing slashes off a proxy base URL."""
    return base_url.strip().rstrip("/")


def is_valid_base_url(base_url: str) -> bool:
    """Return True if the base URL is an absolute http(s) URL."""
    parsed = urlparse(base_url)
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def auth_index_for_email(auths: list[dict[str, Any]], email: str) -> str | None:
    """Return the auth_index of the credential belonging to an account email."""
    for auth in auths:
        if auth.get("email") == email:
            return auth.get("auth_index")
    return None


def _headers(management_key: str) -> dict[str, str]:
    """Build the management API auth header."""
    return {"Authorization": f"Bearer {management_key}"}


async def async_list_claude_auths(
    session: aiohttp.ClientSession, base_url: str, management_key: str
) -> list[dict[str, Any]]:
    """Return the Claude credentials stored in the proxy.

    Raises CliProxyAuthError for a rejected management key and CliProxyError for
    anything else that stops the listing from being read.
    """
    try:
        resp = await session.get(
            f"{base_url}{CLIPROXY_AUTH_FILES_PATH}",
            headers=_headers(management_key),
            timeout=aiohttp.ClientTimeout(total=CLIPROXY_TIMEOUT),
        )
        if resp.status in (401, 403):
            raise CliProxyAuthError("CLI Proxy API rejected the management key")
        if not resp.ok:
            raise CliProxyError(f"CLI Proxy API returned {resp.status} for auth-files")
        payload = await resp.json()
    except aiohttp.ClientError as err:
        raise CliProxyError(f"Cannot reach CLI Proxy API at {base_url}: {err}") from err
    except ValueError as err:
        raise CliProxyError("CLI Proxy API returned a malformed auth-files response") from err

    files = payload.get("files") if isinstance(payload, dict) else None
    return [
        entry
        for entry in files or []
        if isinstance(entry, dict) and str(entry.get("provider", "")).lower() == "claude"
    ]


async def async_api_call(
    session: aiohttp.ClientSession,
    base_url: str,
    management_key: str,
    auth_index: str,
    url: str,
) -> tuple[int, Any]:
    """GET `url` through the proxy, signed with the credential `auth_index`.

    Returns the upstream status code and its decoded JSON body. The body is None
    when it was empty or not JSON, so callers that expect a payload must check
    the status code and the type of what they get back.
    """
    payload = {
        "auth_index": auth_index,
        "method": "GET",
        "url": url,
        "header": {
            "Authorization": f"Bearer {CLIPROXY_TOKEN_PLACEHOLDER}",
            "anthropic-beta": API_BETA_HEADER,
        },
    }

    try:
        resp = await session.post(
            f"{base_url}{CLIPROXY_API_CALL_PATH}",
            headers=_headers(management_key),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=CLIPROXY_TIMEOUT),
        )
        if resp.status in (401, 403):
            raise CliProxyAuthError("CLI Proxy API rejected the management key")
        if resp.status == 400:
            raise _bad_request_error(await resp.text())
        if not resp.ok:
            raise CliProxyError(f"CLI Proxy API returned {resp.status} for api-call")
        envelope = await resp.json()
    except aiohttp.ClientError as err:
        raise CliProxyError(f"Cannot reach CLI Proxy API at {base_url}: {err}") from err
    except ValueError as err:
        raise CliProxyError("CLI Proxy API returned a malformed api-call response") from err

    status_code = envelope.get("status_code") if isinstance(envelope, dict) else None
    if not isinstance(status_code, int):
        raise CliProxyError("CLI Proxy API returned an api-call envelope without a status code")

    body = envelope.get("body")
    if not body:
        return status_code, None
    try:
        return status_code, json.loads(body)
    except (TypeError, ValueError):
        _LOGGER.debug("Upstream body relayed by CLI Proxy API was not JSON")
        return status_code, None


def _bad_request_error(text: str) -> CliProxyError:
    """Classify a 400 from the management API.

    The proxy answers 400 both for a credential it cannot use and for a
    malformed request; only the former is worth re-resolving the index for.
    """
    lowered = text.lower()
    if any(marker in lowered for marker in _CREDENTIAL_ERRORS):
        return CliProxyCredentialError("CLI Proxy API has no usable token for this account")
    return CliProxyError("CLI Proxy API rejected the api-call request")
