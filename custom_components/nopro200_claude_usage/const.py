"""Constants for Claude Usage integration."""

DOMAIN = "nopro200_claude_usage"

# OAuth
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
OAUTH_TOKEN_URL = "https://console.anthropic.com/v1/oauth/token"
OAUTH_REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
OAUTH_SCOPES = "org:create_api_key user:profile user:inference"

# API
USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
PROFILE_API_URL = "https://api.anthropic.com/api/oauth/profile"
API_BETA_HEADER = "oauth-2025-04-20"

# CLIProxyAPI management API. Used when an entry reaches the endpoints above
# through a proxy instance instead of holding its own OAuth tokens.
CLIPROXY_AUTH_FILES_PATH = "/v0/management/auth-files"
CLIPROXY_API_CALL_PATH = "/v0/management/api-call"
CLIPROXY_TOKEN_PLACEHOLDER = "$TOKEN$"  # proxy swaps this for the credential's token
CLIPROXY_TIMEOUT = 20  # seconds; the proxy itself allows the upstream call up to 60

# Where an entry gets its data from
SOURCE_OFFICIAL = "official"
SOURCE_CLIPROXY = "cliproxy"

# Defaults
DEFAULT_UPDATE_INTERVAL = 300  # seconds

# Config keys
CONF_SOURCE = "source"
CONF_BASE_URL = "base_url"
CONF_MANAGEMENT_KEY = "management_key"
CONF_AUTH_INDEX = "auth_index"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_EXPIRES_AT = "expires_at"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_ACCOUNT_ID = "account_id"
CONF_ACCOUNT_NAME = "account_name"
CONF_ACCOUNT_EMAIL = "account_email"
CONF_SUBSCRIPTION_LEVEL = "subscription_level"

# Sensor definitions: (key, name, unit, icon, device_class)
# key corresponds to a path in the parsed usage data dict
SENSOR_DEFINITIONS = [
    ("session_usage_percent", "Session Usage", "%", "mdi:timer-sand", None),
    (
        "session_reset_time",
        "Session Reset Time",
        None,
        "mdi:timer-refresh",
        "timestamp",
    ),
    ("week_usage_percent", "Week Usage", "%", "mdi:calendar-week", None),
    ("week_usage_pace", "Week Usage Pace", "%", "mdi:speedometer", None),
    ("week_reset_time", "Weekly Reset Time", None, "mdi:calendar-clock", "timestamp"),
    (
        "week_sonnet_usage_percent",
        "Weekly Sonnet Usage",
        "%",
        "mdi:calendar-week",
        None,
    ),
    (
        "week_sonnet_reset_time",
        "Weekly Sonnet Reset Time",
        None,
        "mdi:calendar-clock",
        "timestamp",
    ),
    ("extra_usage_enabled", "Extra Usage Enabled", None, "mdi:toggle-switch", None),
    ("extra_usage_percent", "Extra Usage", "%", "mdi:credit-card", None),
    (
        "extra_usage_credits",
        "Extra Usage Credits",
        "credits",
        "mdi:credit-card-outline",
        None,
    ),
    (
        "extra_usage_limit",
        "Extra Usage Limit",
        "credits",
        "mdi:credit-card-settings",
        None,
    ),
    ("api_error", "API Error", "errors", "mdi:alert-circle", None),
]
