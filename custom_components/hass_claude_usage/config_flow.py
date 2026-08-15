"""Config flow for Claude Usage integration."""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import aiohttp_client
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import cliproxy
from .const import (
    API_BETA_HEADER,
    CONF_ACCESS_TOKEN,
    CONF_ACCOUNT_EMAIL,
    CONF_ACCOUNT_ID,
    CONF_ACCOUNT_NAME,
    CONF_AUTH_INDEX,
    CONF_BASE_URL,
    CONF_EXPIRES_AT,
    CONF_MANAGEMENT_KEY,
    CONF_REFRESH_TOKEN,
    CONF_SOURCE,
    CONF_SUBSCRIPTION_LEVEL,
    CONF_UPDATE_INTERVAL,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    OAUTH_AUTHORIZE_URL,
    OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
    OAUTH_SCOPES,
    OAUTH_TOKEN_URL,
    PROFILE_API_URL,
    SOURCE_CLIPROXY,
    SOURCE_OFFICIAL,
)

_LOGGER = logging.getLogger(__name__)


class ClaudeUsageConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Claude Usage."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._pkce_verifier: str | None = None
        self._pkce_challenge: str | None = None
        self._state: str | None = None
        self._base_url: str | None = None
        self._management_key: str | None = None
        self._auths: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Let the user pick where usage data should come from."""
        return self.async_show_menu(step_id="user", menu_options=["official", "cliproxy"])

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-point an existing entry, optionally at a different source.

        Entity unique ids are derived from the entry id, so switching an entry
        between sources keeps its sensors and their recorder history.
        """
        return self.async_show_menu(step_id="reconfigure", menu_options=["official", "cliproxy"])

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """Handle reauth when the entry can no longer read usage data."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-authenticate an entry using the source it was configured with."""
        if self._get_reauth_entry().data.get(CONF_SOURCE) == SOURCE_CLIPROXY:
            return await self.async_step_reauth_cliproxy(user_input)
        return await self._async_oauth_step(user_input, "reauth_confirm")

    # --- Official Anthropic API (OAuth) ---------------------------------

    async def async_step_official(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Authenticate directly with Anthropic."""
        return await self._async_oauth_step(user_input, "official")

    def _oauth_url(self) -> str:
        """Build the authorization URL, generating PKCE material on first use."""
        if self._pkce_verifier is None:
            self._pkce_verifier, self._pkce_challenge = generate_pkce()
            self._state = secrets.token_urlsafe(32)

        params = urlencode(
            {
                "code": "true",
                "client_id": OAUTH_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": OAUTH_REDIRECT_URI,
                "scope": OAUTH_SCOPES,
                "code_challenge": self._pkce_challenge,
                "code_challenge_method": "S256",
                "state": self._state,
            }
        )
        return f"{OAUTH_AUTHORIZE_URL}?{params}"

    async def _async_oauth_step(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        """Run the manual authorization-code step shared by setup and reauth."""
        errors: dict[str, str] = {}
        oauth_url = self._oauth_url()

        if user_input is not None:
            auth_code = user_input.get("auth_code", "").strip()
            if not auth_code:
                errors["auth_code"] = "missing_code"
            else:
                token_data = await self._exchange_code(auth_code)
                if token_data is None:
                    errors["auth_code"] = "exchange_failed"
                else:
                    account_id, account_name, account_email, subscription_level = (
                        await self._fetch_account_info(token_data["access_token"])
                    )
                    return await self._async_finish(
                        unique_id=account_id or account_name,
                        title=_build_title(account_name, subscription_level),
                        data={
                            CONF_SOURCE: SOURCE_OFFICIAL,
                            CONF_ACCESS_TOKEN: token_data["access_token"],
                            CONF_REFRESH_TOKEN: token_data.get("refresh_token", ""),
                            CONF_EXPIRES_AT: time.time() + token_data.get("expires_in", 3600),
                            CONF_ACCOUNT_ID: account_id,
                            CONF_ACCOUNT_NAME: account_name,
                            CONF_ACCOUNT_EMAIL: account_email,
                            CONF_SUBSCRIPTION_LEVEL: subscription_level,
                        },
                    )

        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema({vol.Required("auth_code"): str}),
            description_placeholders={"url": oauth_url},
            errors=errors,
        )

    async def _exchange_code(self, code: str) -> dict[str, Any] | None:
        """Exchange authorization code for tokens."""
        # The code from the callback URL may contain a # separator with state
        code_parts = code.split("#")
        auth_code = code_parts[0]
        state = code_parts[1] if len(code_parts) > 1 else ""

        # Validate state parameter to prevent CSRF
        if state and self._state and state != self._state:
            _LOGGER.error("OAuth state mismatch - possible CSRF attack")
            return None

        payload = {
            "grant_type": "authorization_code",
            "code": auth_code,
            "state": state,
            "client_id": OAUTH_CLIENT_ID,
            "redirect_uri": OAUTH_REDIRECT_URI,
            "code_verifier": self._pkce_verifier,
        }

        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.post(
                OAUTH_TOKEN_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if not resp.ok:
                _LOGGER.error("Token exchange failed (%s)", resp.status)
                return None
            token_data = await resp.json()
            if "access_token" not in token_data:
                _LOGGER.error("Token exchange response missing access_token")
                return None
            return token_data
        except aiohttp.ClientError:
            _LOGGER.exception("Token exchange request failed")
            return None

    async def _fetch_account_info(
        self, access_token: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Fetch account id, name, email, and subscription level from the profile API."""
        try:
            session = aiohttp_client.async_get_clientsession(self.hass)
            resp = await session.get(
                PROFILE_API_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": API_BETA_HEADER,
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
            if not resp.ok:
                _LOGGER.warning("Failed to fetch account profile (%s)", resp.status)
                return None, None, None, None
            return _parse_profile(await resp.json())
        except (aiohttp.ClientError, KeyError):
            _LOGGER.exception("Error fetching account info")
            return None, None, None, None

    # --- CLI Proxy API ---------------------------------------------------

    async def async_step_cliproxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Connect to a CLIProxyAPI instance and list its Claude accounts."""
        return await self._async_cliproxy_connect(user_input, "cliproxy")

    async def async_step_reauth_cliproxy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reconnect an entry to its CLIProxyAPI instance."""
        return await self._async_cliproxy_connect(user_input, "reauth_cliproxy")

    async def _async_cliproxy_connect(
        self, user_input: dict[str, Any] | None, step_id: str
    ) -> ConfigFlowResult:
        """Validate the proxy connection, then move on to picking an account."""
        errors: dict[str, str] = {}

        if user_input is not None:
            base_url = cliproxy.normalize_base_url(user_input[CONF_BASE_URL])
            management_key = user_input[CONF_MANAGEMENT_KEY].strip()

            if not cliproxy.is_valid_base_url(base_url):
                errors["base"] = "invalid_base_url"
            else:
                session = aiohttp_client.async_get_clientsession(self.hass)
                try:
                    auths = await cliproxy.async_list_claude_auths(
                        session, base_url, management_key
                    )
                except cliproxy.CliProxyAuthError:
                    errors["base"] = "invalid_auth"
                except cliproxy.CliProxyError as err:
                    _LOGGER.debug("CLI Proxy API listing failed: %s", err)
                    errors["base"] = "cannot_connect"
                else:
                    if not auths:
                        errors["base"] = "no_claude_accounts"
                    else:
                        self._base_url = base_url
                        self._management_key = management_key
                        self._auths = auths
                        return await self.async_step_cliproxy_account()

        return self.async_show_form(
            step_id=step_id,
            data_schema=self._cliproxy_schema(user_input),
            errors=errors,
        )

    def _cliproxy_schema(self, user_input: dict[str, Any] | None) -> vol.Schema:
        """Build the connection form, prefilled from a previous entry or attempt."""
        base_url = self._base_url or ""
        management_key = self._management_key or ""

        if not base_url:
            # Adding a second account from the same instance shouldn't mean
            # typing the connection details out again. The entry being reauthed
            # wins over other entries, which may point at a different instance.
            target = self._target_entry()
            candidates = [*([target] if target else []), *self._async_current_entries()]
            for entry in candidates:
                if entry.data.get(CONF_SOURCE) == SOURCE_CLIPROXY:
                    base_url = entry.data.get(CONF_BASE_URL, "")
                    management_key = entry.data.get(CONF_MANAGEMENT_KEY, "")
                    break

        if user_input:
            base_url = user_input.get(CONF_BASE_URL, base_url)
            management_key = user_input.get(CONF_MANAGEMENT_KEY, management_key)

        return vol.Schema(
            {
                vol.Required(CONF_BASE_URL, default=base_url): str,
                vol.Required(CONF_MANAGEMENT_KEY, default=management_key): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD)
                ),
            }
        )

    async def async_step_cliproxy_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Pick which Claude account of the instance this entry should track."""
        errors: dict[str, str] = {}

        base_url = self._base_url
        management_key = self._management_key
        if not base_url or not management_key:
            # Reached without a validated connection (e.g. a resumed flow) —
            # send the user back rather than showing an empty account list.
            return await self.async_step_cliproxy()

        if user_input is not None:
            auth_index = user_input[CONF_AUTH_INDEX]
            session = aiohttp_client.async_get_clientsession(self.hass)

            # Reading the profile through the proxy proves the whole chain works
            # and yields the same account identity the OAuth path produces, so an
            # account cannot end up configured twice via different sources.
            try:
                status, profile = await cliproxy.async_api_call(
                    session, base_url, management_key, auth_index, PROFILE_API_URL
                )
            except cliproxy.CliProxyAuthError:
                errors["base"] = "invalid_auth"
            except cliproxy.CliProxyError as err:
                _LOGGER.debug("CLI Proxy API profile call failed: %s", err)
                errors["base"] = "cannot_connect"
            else:
                if status != 200 or not isinstance(profile, dict):
                    errors["base"] = "credential_rejected"
                else:
                    account_id, account_name, account_email, subscription_level = _parse_profile(
                        profile
                    )
                    return await self._async_finish(
                        unique_id=account_id or account_name or auth_index,
                        title=_build_title(account_name, subscription_level),
                        data={
                            CONF_SOURCE: SOURCE_CLIPROXY,
                            CONF_BASE_URL: base_url,
                            CONF_MANAGEMENT_KEY: management_key,
                            CONF_AUTH_INDEX: auth_index,
                            CONF_ACCOUNT_ID: account_id,
                            CONF_ACCOUNT_NAME: account_name,
                            CONF_ACCOUNT_EMAIL: account_email,
                            CONF_SUBSCRIPTION_LEVEL: subscription_level,
                        },
                    )

        return self.async_show_form(
            step_id="cliproxy_account",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_INDEX): vol.In(self._auth_choices())}),
            errors=errors,
        )

    def _auth_choices(self) -> dict[str, str]:
        """Map auth_index to a human label for the account dropdown."""
        choices: dict[str, str] = {}
        for auth in self._auths:
            auth_index = auth.get("auth_index")
            if not auth_index:
                continue
            label = auth.get("email") or auth.get("name") or auth_index
            status = auth.get("status")
            choices[auth_index] = f"{label} ({status})" if status else label
        return choices

    # --- Shared ----------------------------------------------------------

    def _target_entry(self) -> ConfigEntry | None:
        """Return the entry this flow is reauthing or reconfiguring, if any."""
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        return None

    async def _async_finish(
        self, unique_id: str | None, title: str, data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Create the entry, or update the one being reauthed/reconfigured.

        Entry data is replaced rather than merged so that switching an entry
        between sources leaves no credentials of the previous one behind.
        """
        entry = self._target_entry()
        if entry is not None:
            # Both flows may re-point an entry at a different account, so guard
            # against two entries ending up on the same one. This also replaces
            # the entry_id placeholder left by migration when the account could
            # not be identified, since no other entry holds the real id.
            if unique_id and any(
                other.entry_id != entry.entry_id and other.unique_id == unique_id
                for other in self._async_current_entries()
            ):
                return self.async_abort(reason="already_configured")

            return self.async_update_reload_and_abort(
                entry, unique_id=unique_id or entry.unique_id, data=data
            )

        await self.async_set_unique_id(unique_id or DOMAIN)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=title,
            data=data,
            options={CONF_UPDATE_INTERVAL: DEFAULT_UPDATE_INTERVAL},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Get the options flow."""
        return ClaudeUsageOptionsFlow()


class ClaudeUsageOptionsFlow(OptionsFlow):
    """Handle options for Claude Usage."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        current_interval = self.config_entry.options.get(
            CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL
        )

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UPDATE_INTERVAL, default=current_interval): vol.All(
                        int, vol.Range(min=60, max=3600)
                    ),
                }
            ),
        )


def generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _parse_profile(
    profile: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Pull account id, name, email and subscription level out of a profile payload."""
    account = profile.get("account") or {}

    account_email = account.get("email")

    # Stable unique identifier — prefer an account uuid/id, fall back to email
    account_id = account.get("uuid") or account.get("id") or account_email

    # Get account name (prefer display name, fall back to email)
    account_name = account.get("display_name") or account.get("full_name") or account_email

    # Get subscription level
    subscription_level = None
    if account.get("has_claude_max"):
        subscription_level = "Max"
    elif account.get("has_claude_pro"):
        subscription_level = "Pro"

    return account_id, account_name, account_email, subscription_level


def _build_title(account_name: str | None, subscription_level: str | None) -> str:
    """Build the entry title from the account name and plan."""
    if not account_name:
        return "Claude Usage"
    if subscription_level:
        return f"Claude Usage ({account_name} - {subscription_level})"
    return f"Claude Usage ({account_name})"
