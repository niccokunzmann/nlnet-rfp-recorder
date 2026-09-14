from pathlib import Path

import environ

env = environ.Env()

APP_NAME = "nlnet-rfp-recorder"

XDG_DATA_HOME = env.str("XDG_DATA_HOME", default=str(Path.home() / ".local" / "share"))

_db_override = env.str("RFP_DB", default="")
if _db_override:
    DATABASE_FILE = Path(_db_override).expanduser()
else:
    DATABASE_FILE = Path(XDG_DATA_HOME).expanduser() / APP_NAME / "rfp.db"

RFP_EUROS_PER_HOUR = env.float("RFP_EUROS_PER_HOUR", default=50)

# Below this, a task defaults to being excluded when `rfp report review`
# asks about it - a few euros usually isn't worth the paperwork of
# submitting it now, when it can just as well wait and be claimed by a
# later report. Also used by `rfp report print` to show a "tasks above
# X€" subtotal.
REVIEW_DEFAULT_EXCLUDE_BELOW = env.float("REVIEW_DEFAULT_EXCLUDE_BELOW", default=50)

SECRET_KEY = "django-insecure-not-used-outside-tests"
DEBUG = False
USE_TZ = False
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

INSTALLED_APPS = [
    "nlnet_rfp_recorder.timetracking",
]

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(DATABASE_FILE),
        "TEST": {"NAME": ":memory:"},
    }
}
