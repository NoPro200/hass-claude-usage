# AGENTS.md - Design Decisions & Implementation Notes

This document captures key architectural decisions, API discoveries, and design rationale for the Claude Usage Home Assistant integration.

## Project Preferences

- **Versioning:** Major version numbers only (1, 2, 3...). No semver.
- **Git commits:** Atomic commits. Each commit should be one logical change.
- **Code style:** Keep it short, simple, easy to read. Prefer DRY but don't over-abstract. Three similar lines is better than a premature abstraction.
- **No manufacturer attribution:** The device info should not claim Anthropic wrote this integration. They provide the API; the maintainer maintains the code.

## Project Origin

Refactored from a standalone daemon concept originally explored in the `cc-playground/cc-usage-graphs` project (private).

## Architecture Decisions

### OAuth Over API Keys

**Decision:** Use Anthropic's OAuth 2.0 flow with PKCE instead of API keys.

**Rationale:**
- The usage API endpoint (`/api/oauth/usage`) is only available via OAuth bearer tokens, not API keys
- API keys don't provide access to consumer subscription usage data (Pro/Max plans)
- OAuth tokens can be refreshed automatically without user intervention
- Matches the authentication pattern used by Claude Code CLI

### CLIProxyAPI as an Alternative Transport

**Decision:** Let an entry read usage through a [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)
instance instead of holding its own OAuth tokens, selected by a `source` key
(`"official"` | `"cliproxy"`) in the entry data.

**Rationale:**
- Users who already run the proxy have the accounts and refreshed tokens there; a second OAuth
  copy/paste and a second token store in HA is duplicated state that can drift
- The proxy relays the response unchanged, so `_parse_usage` / `_parse_limits` and the whole sensor
  platform are untouched — only the transport differs
- The proxy already lists its accounts, so multi-account setup becomes a dropdown

**Why not a plain custom base URL:** CLIProxyAPI serves only inference endpoints (`/v1/messages` …)
plus its management API. It has no route for `/api/oauth/usage` — a base-URL swap 404s. The
transport therefore goes through the management API (see API Discoveries below).

**No migration:** entries predating this read `data.get(CONF_SOURCE, SOURCE_OFFICIAL)`, so
`VERSION` stays at 2.

**Trade-off accepted:** the management key is far more privileged than an OAuth token — it can
rewrite the proxy config, read its logs, and issue arbitrary requests with *any* credential it
holds. This is stated in the config flow and the README; it makes the feature trusted-network only.

### DataUpdateCoordinator Pattern

**Decision:** Use Home Assistant's `DataUpdateCoordinator` for data management.

**Rationale:**
- Built-in automatic refresh scheduling
- Centralized error handling
- Efficient updates across multiple sensor entities
- Standard HA pattern for polling integrations
- Handles debouncing and prevents redundant API calls

### Single Device, Multiple Sensors

**Decision:** Create one logical "Claude Usage" device with 10 sensor entities.

**Rationale:**
- Clean UI organization in HA dashboard
- All metrics come from the same API call
- Easier device management and removal
- Follows HA best practices for service-based integrations

### Deferred Loading

**Decision:** Sensors show as "unavailable" when their data key is missing from API response.

**Rationale:**
- Free tier users don't get usage data
- Extra usage is optional
- Cleaner than showing "0" or "unknown" for non-existent metrics
- HA standard pattern for optional attributes

## API Discoveries

### Usage Endpoint

**URL:** `https://api.anthropic.com/api/oauth/usage`

**Discovery:** Found in Claude Code CLI source at `~/.nvm/versions/node/v22.18.0/lib/node_modules/@anthropic-ai/claude-code/cli.js:4032`

**Method:** GET with Bearer token authentication

**Required Headers:**
```python
{
    "Authorization": f"Bearer {access_token}",
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",  # API version header
}
```

### Response Structure

```json
{
  "five_hour": {
    "utilization": 42.5,           // float, percentage (0-100)
    "resets_at": "2026-01-28T..."  // ISO 8601 timestamp
  },
  "seven_day": {
    "utilization": 68.2,
    "resets_at": "2026-02-01T..."
  },
  "seven_day_sonnet": {
    "utilization": 55.0,
    "resets_at": "2026-02-01T..."
  },
  "extra_usage": {
    "is_enabled": true,
    "utilization": 23.4,
    "used_credits": 12345,         // cents (divide by 100 for dollars)
    "monthly_limit": 50000         // cents (divide by 100 for dollars)
  }
}
```

**Notes:**
- All fields are optional depending on subscription tier
- Free tier returns empty object `{}`
- Extra usage only present if enabled by user
- Credits are in cents (U.S. currency)

### CLIProxyAPI Management API

Two routes are used, both requiring `Authorization: Bearer <MANAGEMENT_KEY>`:

**`GET /v0/management/auth-files`** — lists stored credentials. Entries carry `auth_index`,
`provider`, `email`, `status` and `last_refresh`. Filter on `provider == "claude"`.

**`POST /v0/management/api-call`** — performs one outbound request with a stored credential:

```json
{
  "auth_index": "<from auth-files>",
  "method": "GET",
  "url": "https://api.anthropic.com/api/oauth/usage",
  "header": {"Authorization": "Bearer $TOKEN$", "anthropic-beta": "oauth-2025-04-20"}
}
```

The proxy replaces `$TOKEN$` with the credential's `metadata.access_token`. The reply is **always**
HTTP 200 with an envelope — the upstream status and payload live inside it, and the payload is a
JSON **string**, so it must be decoded twice:

```json
{"status_code": 200, "header": {...}, "body": "{\"five_hour\": ...}"}
```

**Footgun — an unknown `auth_index` fails silently.** In `internal/api/handlers/management/api_tools.go`
the handler resolves the index to `nil`, skips its own error branch because `auth != nil` is false,
and then `continue`s past the substitution — leaving the header as the literal string
`Bearer $TOKEN$`. Anthropic answers 401. The proxy reports no error of its own, so a 401 here is
ambiguous: either the credential went stale, or the stored `auth_index` moved. The coordinator
therefore re-resolves the index by account email once before treating a 401 as an auth failure.

**Deployment note:** the instance needs `remote-management.allow-remote: true` when HA is not on
the same host.

### OAuth Configuration

**Client ID:** `9d1c250a-e61b-44d9-88ed-5944d1962f5e` (same as Claude Code CLI)

**Endpoints:**
- Authorize: `https://console.anthropic.com/oauth/authorize`
- Token: `https://console.anthropic.com/v1/oauth/token`
- Redirect URI: `https://console.anthropic.com/oauth/code/callback`

**Scopes:** `org:create_api_key user:profile user:inference user:sessions:claude_code`

**Flow:** OAuth 2.0 Authorization Code with PKCE (SHA-256)

**PKCE Implementation:**
```python
# Code verifier: 32 random URL-safe bytes
verifier = secrets.token_urlsafe(32)

# Code challenge: base64url(sha256(verifier))
digest = hashlib.sha256(verifier.encode("ascii")).digest()
challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
```

### Token Refresh

**Endpoint:** `https://console.anthropic.com/v1/oauth/token`

**Payload:**
```json
{
  "grant_type": "refresh_token",
  "refresh_token": "...",
  "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
}
```

**Response:**
```json
{
  "access_token": "...",
  "refresh_token": "...",  // may be the same or a new token
  "expires_in": 3600,      // seconds
  "token_type": "Bearer"
}
```

**Implementation:** Refresh 60 seconds before expiry to avoid race conditions.

## Design Decisions

### Config Flow UX

**Decision:** Two-step flow - auth code entry, then options.

**Trade-off Considered:** Embedded browser OAuth flow vs. manual code copy/paste.

**Chosen:** Manual code entry.

**Rationale:**
- HA runs headless on many installations
- Browser-based flow requires complex callback server setup
- Manual flow works in all environments (Docker, supervised, core)
- User only does this once during setup
- Matches pattern of other HA OAuth integrations

### Polling Interval

**Default:** 300 seconds (5 minutes)

**Range:** 60-3600 seconds

**Rationale:**
- Usage data updates every 5 hours (session) and 7 days (weekly)
- No need for real-time updates
- Avoid hitting API rate limits
- Balance freshness with API courtesy
- User can adjust via options flow

### Timestamp Handling

**Decision:** Store ISO timestamps as-is, let HA handle conversion.

**Rationale:**
- HA automatically converts ISO timestamps to local time
- Device class "timestamp" provides proper formatting
- Sensors show human-readable "resets in X hours" in UI
- No timezone math needed in integration code

### Error Handling

**Token Refresh Failures:**
- Log detailed error with HTTP status and body
- Raise `UpdateFailed` to mark sensors unavailable
- User must reconfigure integration (no auto-retry)

**API Fetch Failures:**
- Temporary network errors: raise `UpdateFailed`, retry on next interval
- 401 Unauthorized: likely invalid token, trigger refresh
- Other errors: log and mark unavailable

**Rationale:** Distinguish between transient failures (retry) and auth failures (user action required).

## Known Limitations

1. **Manual OAuth Flow:** Users must copy/paste authorization code (see Config Flow UX above)

2. **No Token Keep-Alive:** Anthropic tokens expire after prolonged inactivity regardless of refresh attempts. Users may need to re-authenticate after weeks of inactivity.

3. **Rate Limiting:** The `/api/oauth/usage` endpoint rate limits are undocumented. Conservative 300s default prevents issues.

4. **Free Tier Support:** Free tier accounts get empty response `{}`. All sensors show unavailable, which is correct but might confuse users.

5. **ToS Compliance:** Using Claude Code's OAuth client ID in a third-party integration may violate Anthropic's Terms of Service. Consider this a prototype/personal-use integration.

## Future Enhancements

### Potential Improvements

1. **Binary Sensors:** Convert `extra_usage_enabled` to a binary sensor instead of regular sensor

2. **Attributes:** Add rich attributes to sensors:
   - Session sensor: add `resets_at` as attribute
   - Extra usage sensor: add `used_credits` and `monthly_limit` as attributes
   - Reduces sensor count from 10 to ~5

3. **Diagnostics:** Add diagnostic data for debugging:
   - Last successful API call timestamp
   - Last token refresh timestamp
   - API response cache

4. **Notifications:** HA automation triggers when usage exceeds thresholds:
   - Session usage >80%
   - Week usage >90%
   - Extra usage approaching limit

5. **OAuth Browser Flow:** Implement proper OAuth callback server for smoother UX (complex, see Design Decisions above)

6. **Usage History:** Store historical data in HA recorder for graphing trends over time

### Non-Goals

- **Real-time Updates:** Usage data is inherently delayed (5-hour buckets), no value in frequent polling
- **Multiple Accounts Per Entry:** One entry tracks one Claude account; add another entry for another account
- **API Key Auth:** Not supported by the usage endpoint, OAuth-only

## Testing Recommendations

### Manual Testing Checklist

1. **Config Flow:**
   - [ ] OAuth URL opens correctly in browser
   - [ ] Authorization code exchange succeeds
   - [ ] Invalid code shows error message
   - [ ] Can't configure twice (unique_id check)

2. **Sensors:**
   - [ ] All 10 sensors created under "Claude Usage" device
   - [ ] Session sensors show correct % and timestamp
   - [ ] Weekly sensors show correct % and timestamp
   - [ ] Extra usage sensors reflect actual state
   - [ ] Sensors show "unavailable" when data missing (e.g., free tier)

3. **Token Refresh:**
   - [ ] Access token refreshes before expiry
   - [ ] Config entry data updated with new tokens
   - [ ] No service interruption during refresh

4. **Options Flow:**
   - [ ] Can change update interval
   - [ ] Coordinator respects new interval
   - [ ] Validation enforces 60-3600 range

5. **Error Handling:**
   - [ ] Network timeout shows error in HA logs
   - [ ] Invalid token triggers refresh
   - [ ] Persistent auth failures mark sensors unavailable

### Integration Quality Checks

Run HA's integration validation:
```bash
# Install HA dev environment
python3 -m venv venv
source venv/bin/activate
pip install homeassistant

# Validate manifest
python3 -m homeassistant.scripts.check_config \
  --script check_config \
  --files custom_components/nopro200_claude_usage/manifest.json

# Type checking (if adding type hints)
pip install mypy
mypy custom_components/nopro200_claude_usage/
```

## References

### Research Sources

- Claude OAuth implementation (opencode-anthropic-auth) — `github.com/anomalyco/opencode-anthropic-auth/blob/master/index.mjs` (repo since removed)
- [OpenCode OAuth usage dashboard feature request](https://github.com/anomalyco/opencode/issues/8911)
- [Claude Code source (local installation)](file://~/.nvm/versions/node/v22.18.0/lib/node_modules/@anthropic-ai/claude-code/cli.js)
- [Home Assistant DataUpdateCoordinator docs](https://developers.home-assistant.io/docs/integration_fetching_data)
- [Home Assistant Config Flow docs](https://developers.home-assistant.io/docs/config_entries_config_flow_handler)

### Related Projects

- **claude_max:** Python tool for using Claude Max subscriptions programmatically
- **Claude Code Usage Monitor:** Terminal-based usage monitor with ML predictions
- **opencode-anthropic-auth:** Reusable OAuth library for Anthropic services

## Release Process

This project uses **major version numbering only** (1, 2, 3...). No semver.

### Creating a Release

1. **Update manifest.json version**:
   ```bash
   # Edit custom_components/nopro200_claude_usage/manifest.json
   # Change "version": "2" to "version": "3"
   ```

2. **Commit the version bump**:
   ```bash
   git add custom_components/nopro200_claude_usage/manifest.json
   git commit -m "Bump version to 3"
   git push
   ```

3. **Create and push the git tag** — this is the whole release:
   ```bash
   git tag v3
   git push origin v3
   ```

   `.github/workflows/release.yml` takes it from there: it verifies the tag
   matches the manifest version, packages the integration, and publishes a
   GitHub Release with generated notes and the zip attached. A tag that
   disagrees with `manifest.json` fails the workflow instead of shipping.

   Tags exist in both spellings here (`2`-`4` bare, `v5` onward prefixed).
   Either works; prefer the `v` prefix to match the recent ones.

   `.gitlab-ci.yml` mirrors this for the GitLab remote. It is kept because the
   project is also pushed there, but GitHub is what HACS reads.

### HACS Requirements

- HACS requires **GitHub Releases**, not just tags
- The `version` field in `manifest.json` must match the latest release tag
- HACS shows the 5 most recent releases to users during install/upgrade
- Version must be compatible with AwesomeVersion (simple integers work fine)

## Version History

### v11 (2026-08-16)
- Renamed the domain to `nopro200_claude_usage` so this fork installs alongside the upstream
  integration instead of colliding with it
- Display name is now "NoPro200 - Claude Usage" in `manifest.json` and `hacs.json`
- `codeowners`, `documentation` and `issue_tracker` point at this fork, not upstream
- Added `.github/workflows/release.yml`; a tag push now publishes the GitHub Release
- Added `.gitignore` for `__pycache__`

### v10 (2026-08-16)
- Usage data can be read through a CLIProxyAPI instance instead of Anthropic directly
- Config flow starts with a source menu; reconfigure can move an entry between sources
- New `cliproxy.py` holds the management-API transport
- Coordinator re-resolves a moved `auth_index` by account email before failing auth
- Unified the OAuth setup and reauth steps, which had drifted into near-duplicates

### v2 (2026-02-06)
- Renamed "Weekly Usage" to "Week Usage"
- Fixed manifest URLs to point to correct repository
- Switched to major version numbering
- Removed misleading manufacturer attribution
- Fixed OAuth security issues (separate state parameter, CSRF validation, shared aiohttp session)
- Fixed token refresh response validation
- Added error handling for malformed timestamps
- Moved `generate_pkce` to config_flow module
- Added Week Usage Pace sensor (shows if you're ahead/behind expected usage rate)

### v0.1.0 (2026-01-28)
- Initial implementation
- OAuth 2.0 with PKCE authentication
- 10 sensor entities (session, weekly, extra usage)
- Automatic token refresh
- HACS compatible
- Based on original daemon concept from cc-playground/cc-usage-graphs
