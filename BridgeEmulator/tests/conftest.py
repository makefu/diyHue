"""Test bootstrap for the BridgeEmulator package.

``configManager`` parses ``sys.argv`` and touches the filesystem at *import*
time (``configHandler.Config.argsDict`` is a class attribute initialised by
``parse_arguments()``), so the environment has to be primed before any
production module is imported.  Doing it here keeps every test module free of
import-order rituals.
"""

import os
import sys
import tempfile
from pathlib import Path

BRIDGE_EMULATOR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BRIDGE_EMULATOR))

# argparse would choke on pytest's own arguments.
sys.argv = [sys.argv[0]]

_CONFIG_DIR = Path(tempfile.mkdtemp(prefix="diyhue-test-config-"))
# process_arguments() shells out to genCert.sh unless a certificate is already
# present; an empty file is enough to keep the tests offline and fast.
(_CONFIG_DIR / "cert.pem").touch()

os.environ.setdefault("CONFIG_PATH", str(_CONFIG_DIR))
# Both avoid shelling out to `ip` / `cat /sys/class/net/...` during discovery of
# the host's addresses.
os.environ.setdefault("MAC", "00:11:22:33:44:55")
os.environ.setdefault("IP", "192.168.1.2")
os.environ.setdefault("DIYHUE_LOG_FILE", str(_CONFIG_DIR / "diyhue.log"))
os.environ.pop("DEBUG", None)
