import os
import sys
import tempfile

# Make the add-on modules (in the repo root) importable from tests/.
sys.path.insert(0, os.path.dirname(__file__))

# Point persistent storage at a writable temp dir for tests/CI, where the
# add-on's default /data mount does not exist. Importing web_ui creates
# DATA_DIR-backed stores at import time, so this must run before collection.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="ha-em-test-"))
