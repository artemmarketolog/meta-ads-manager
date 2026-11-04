"""Изолировать тесты от настоящего конфига: временные каталоги, пустой токен, без сети."""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNTIME = Path(os.environ.get("META_ADS_TEST_RUNTIME") or tempfile.mkdtemp(prefix="meta-ads-test-"))
os.environ["META_ADS_TEST_RUNTIME"] = str(RUNTIME)
os.environ["META_ADS_CONFIG_DIR"] = str(RUNTIME / "config")
os.environ["META_ADS_DATA_DIR"] = str(RUNTIME / "data")
os.environ["META_ADS_STATE_DIR"] = str(RUNTIME / "state")
os.environ["META_TOKEN"] = ""
for key in ("META_UTM_MEDIUM", "META_DEFAULT_PLATFORMS", "GRAPH_VERSION"):
    os.environ.pop(key, None)
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
