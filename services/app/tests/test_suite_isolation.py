"""The suite must never touch a live install, whatever the shell exported.

Were conftest to use ``os.environ.setdefault`` for the DB / data / backup
paths, running pytest in a shell that had /etc/wsi/wsi.env loaded would aim
the suite's restores, resets and deletes at the production database.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

_APP_DIR = Path(__file__).resolve().parents[1]

_PROBE = """
import json, os
import tests.conftest as c
from wsi_app.config import settings as s
root = str(c._TMP_ROOT)
print(json.dumps({
    "root": root,
    "paths": [s.database_url, s.data_dir, s.backup_dir, s.restore_request_path,
              s.restore_result_path, s.restart_trigger_path],
    "cmd": s.restore_restart_cmd,
}))
c.pytest_sessionfinish(None, 0)
print(json.dumps(os.path.exists(root)))
"""


def test_live_paths_in_the_environment_are_overridden():
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite:////var/lib/wsi/wsi.db",
        "DATA_DIR": "/var/lib/wsi",
        "BACKUP_DIR": "/var/lib/wsi/backups",
        "RESTORE_REQUEST_PATH": "/run/wsi/restore.request",
        "RESTORE_RESULT_PATH": "/run/wsi/restore.result",
        "RESTART_TRIGGER_PATH": "/run/wsi/restart.trigger",
        "RESTORE_RESTART_CMD": "systemctl restart wsi-app",
    }
    out = subprocess.run(
        [sys.executable, "-c", _PROBE], env=env, cwd=_APP_DIR,
        capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    probe = json.loads(out[-2])
    root = probe["root"]
    for path in probe["paths"]:
        assert root in path, path
        assert "/var/lib/wsi" not in path and "/run/wsi" not in path
    assert probe["cmd"] == ""
    # ...and the session's temporary root is removed when the session ends.
    assert json.loads(out[-1]) is False
