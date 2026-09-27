"""Both installs ship what the code tools run (setup.sh and macapp/build_app.sh
parity is two-sided).

- ruff and python-lsp-server are runtime requirements, so the bundled Python
  and the venv both carry them (build_app.sh installs requirements.txt whole,
  setup.sh too); they must not sit under the dev-only section.
- node: build_app.sh bundles bin/node, setup.sh installs node into the venv.
- ruff on the shell PATH: the venv's bin/ruff in a source install, bin/ruff in
  the bundle.
- No code tool reaches for npx, which can download packages from the internet.
"""

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_ruff_and_pylsp_are_runtime_requirements():
    runtime = _read("requirements.txt").split("# Dev / test", 1)[0]
    assert re.search(r"^ruff==", runtime, re.M)
    assert re.search(r"^python-lsp-server==", runtime, re.M)


def test_both_installs_provide_node_and_ruff_on_path():
    build = _read("macapp/build_app.sh")
    setup = _read("setup.sh")
    assert '"$NODE_DIR/node" "$RES_DIR/bin/"' in build
    assert 'ln -s ../python/bin/ruff "$RES_DIR/bin/ruff"' in build
    assert "nodeenv --python-virtualenv" in setup


def test_no_code_tool_uses_npx():
    sources = [os.path.join("server", "lsp.py"), os.path.join("server", "lsp_proxy.py")]
    pkg = os.path.join(ROOT, "server", "code_tools")
    sources += [
        os.path.join("server", "code_tools", n) for n in os.listdir(pkg) if n.endswith(".py")
    ]
    for rel in sources:
        code = _read(rel)
        assert '"npx"' not in code and "'npx'" not in code, rel
