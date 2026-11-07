"""server.main imports huggingface_hub's API modules at boot. Its lazy
submodule import is not thread-safe: Discover's search threads making the first
import at once broke the package for the life of the process, so every search
came back empty."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_concurrent_hub_use_after_server_import_never_fails(tmp_path):
    """In a subprocess, so the package is not already imported by this one."""
    env = dict(os.environ)
    env["WHISPER_USER_DIR"] = str(tmp_path / "user")
    env["WHISPER_HOME"] = str(tmp_path / "home")
    env["WHISPER_DATA_DIR"] = str(tmp_path / "data")
    code = (
        "import threading\n"
        "import server.main\n"
        "import huggingface_hub as hub\n"
        "names = ['HfApi', 'HfFileSystem', 'hf_hub_download', 'snapshot_download', 'list_models']\n"
        "errors = []\n"
        "start = threading.Barrier(25)\n"
        "def use(i):\n"
        "    start.wait()\n"
        "    try:\n"
        "        getattr(hub, names[i % len(names)])\n"
        "    except Exception as e:\n"
        "        errors.append(repr(e))\n"
        "threads = [threading.Thread(target=use, args=(i,)) for i in range(25)]\n"
        "[t.start() for t in threads]\n"
        "[t.join() for t in threads]\n"
        "assert not errors, errors[:3]\n"
        "print('HUB_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=env, capture_output=True, text=True, timeout=240
    )
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert "HUB_OK" in proc.stdout
