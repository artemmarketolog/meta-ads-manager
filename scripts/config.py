"""Settings of meta-ads-manager: one private env file outside the repository.

Order: real environment variables win, then ~/.config/meta-ads-manager/meta.env
(or $META_ADS_CONFIG_DIR/meta.env). Nothing here prints or sends the token.
"""

import os
from pathlib import Path

CONFIG_DIR = Path(os.environ.get("META_ADS_CONFIG_DIR")
                  or Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "meta-ads-manager").expanduser()
ENV_FILE = CONFIG_DIR / "meta.env"
CLIENTS_FILE = Path(os.environ.get("META_ADS_CLIENTS_FILE") or CONFIG_DIR / "clients.json").expanduser()
DATA_DIR = Path(os.environ.get("META_ADS_DATA_DIR")
                or Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "meta-ads-manager").expanduser()
LOGS_DIR = DATA_DIR / "logs"
STATE_DIR = Path(os.environ.get("META_ADS_STATE_DIR") or DATA_DIR / "state").expanduser()


def _load_env(path=ENV_FILE):
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env()

META_TOKEN = os.environ.get("META_TOKEN", "")
GRAPH_VERSION = os.environ.get("GRAPH_VERSION", "v26.0")
# utm_medium в метках сайта: название вашего агентства или канала (латиница, без пробелов).
UTM_MEDIUM = os.environ.get("META_UTM_MEDIUM", "meta_ads")
# Площадки, если в спеке их нет: instagram,threads (метод по умолчанию) или instagram / instagram,facebook.
DEFAULT_PLATFORMS = [p.strip() for p in os.environ.get("META_DEFAULT_PLATFORMS", "instagram,threads").split(",") if p.strip()]
