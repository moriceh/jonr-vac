"""Constants for the JONR Vacuum integration."""
from __future__ import annotations

DOMAIN = "jonr_vac"

CONF_HOST = "host"
CONF_TOKEN = "token"
CONF_MODEL = "model"
CONF_MAC = "mac"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_SERVER = "server"
CONF_USER_ID = "user_id"
CONF_DEVICE_ID = "device_id"
CONF_WIFI_SN = "wifi_sn"
CONF_SSECURITY = "ssecurity"
CONF_SERVICE_TOKEN = "service_token"
CONF_PASS_TOKEN = "pass_token"
CONF_OAUTH_ACCESS_TOKEN = "oauth_access_token"
CONF_OAUTH_REFRESH_TOKEN = "oauth_refresh_token"
CONF_OAUTH_EXPIRES_TS = "oauth_expires_ts"
CONF_OAUTH_REGION = "oauth_region"
CONF_OAUTH_DEVICE_ID = "oauth_device_id"
CONF_OAUTH_REDIRECT_URI = "oauth_redirect_uri"

DEFAULT_SCAN_INTERVAL = 10  # seconds, local MIoT polling
MAP_SCAN_INTERVAL = 30      # seconds, cloud map refresh while cleaning
MAP_IDLE_INTERVAL = 300     # seconds, cloud map refresh while docked/idle

# Error events on the HA bus (always fired, dreame-vacuum style): payload
# {entity_id, error|warning, code} — see error_events.py.
EVENT_ERROR = f"{DOMAIN}_error"
EVENT_STATION_ERROR = f"{DOMAIN}_station_error"

# Config-entry option (entry.options): persistent_notification bubbles for
# robot/station errors. Events fire regardless; the toggle only gates the
# notification bubbles (dreame-vacuum's "notifications" option).
OPT_ERROR_NOTIFICATIONS = "error_notifications"

# Public static path serving www/ (registered in __init__.py, cache=False)
# and the fault-picture folder under it. Error bubbles reference vendor
# images at /jonr-vac-card/images/faults/<file>.png — same-origin, no auth,
# no base64 in the message (docs/NOTIFICATIONS.md).
CARD_STATIC_BASE = "/jonr-vac-card"
FAULT_IMAGE_URL = f"{CARD_STATIC_BASE}/images/faults"

# Servers accepted by the Xiaomi cloud.
SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]
