"""The model architectures the installed engines can load, read from the engines.

Discover offers a model only when the engine that would serve it knows its
architecture, so the list must come from the installed engine, not from a
hand-kept copy that falls behind every llama.cpp or mlx-lm update.

* llama.cpp: libllama exports ``llm_arch_name(llm_arch)``. The arch enum is
  dense and ends with LLM_ARCH_UNKNOWN, named "(unknown)", so naming 0, 1, 2,
  ... until that name lists every architecture the build loads. A child
  process makes the ctypes call, so the server never maps the library.
* mlx-lm: it imports ``mlx_lm.models.<model_type>`` after applying its
  ``MODEL_REMAPPING``, so the model modules plus the names remapped onto them
  are the types it serves. Both are read from the installed package files,
  without importing mlx.

Each list is read once per installed build (keyed by the file's mtime).
"""

from __future__ import annotations

import ast
import functools
import importlib.util
import logging
import os
import subprocess
import sys

log = logging.getLogger("whisper-studio")

# The C++ symbol of ``const char * llm_arch_name(llm_arch)``.
_ARCH_NAME_SYMBOL = "_Z13llm_arch_name8llm_arch"

_PROBE = f"""
import ctypes, sys
name_of = getattr(ctypes.CDLL(sys.argv[1]), "{_ARCH_NAME_SYMBOL}")
name_of.restype = ctypes.c_char_p
name_of.argtypes = [ctypes.c_int]
for i in range(4096):
    name = name_of(i).decode()
    if name == "(unknown)":
        break
    print(name)
"""


def libllama_path(binary: str) -> str | None:
    """The libllama that ``binary`` loads: both the app bundle and setup.sh's
    vendor/llama keep the dylibs next to llama-server (@rpath)."""
    candidate = os.path.join(os.path.dirname(os.path.realpath(binary)), "libllama.dylib")
    return os.path.realpath(candidate) if os.path.isfile(candidate) else None


@functools.lru_cache(maxsize=4)
def _llama_archs_of(lib: str, mtime_ns: int) -> frozenset[str]:
    out = subprocess.run(
        [sys.executable, "-I", "-c", _PROBE, lib],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return frozenset(line.strip() for line in out.stdout.splitlines() if line.strip())


def llama_archs() -> frozenset[str]:
    """Every architecture the installed llama-server loads; empty when there is
    no llama-server or its library cannot be read (then no GGUF is offered)."""
    from server.local import llama_server

    exe = llama_server.binary_path()
    if not exe:
        return frozenset()
    lib = libllama_path(exe)
    if not lib:
        log.warning("No libllama.dylib next to %s; Discover offers no GGUF models.", exe)
        return frozenset()
    try:
        return _llama_archs_of(lib, os.stat(lib).st_mtime_ns)
    except Exception as e:
        log.warning("Could not read the architectures of %s: %s", lib, e)
        return frozenset()


def _remapping(utils_py: str) -> dict[str, str]:
    """``MODEL_REMAPPING`` from mlx_lm/utils.py, read as a literal."""
    with open(utils_py, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "MODEL_REMAPPING" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            return value if isinstance(value, dict) else {}
    return {}


@functools.lru_cache(maxsize=4)
def _mlx_archs_of(package_dir: str, mtime_ns: int) -> frozenset[str]:
    models_dir = os.path.join(package_dir, "models")
    modules = {
        name[:-3]
        for name in os.listdir(models_dir)
        if name.endswith(".py") and not name.startswith("_")
    }
    remapped = {
        k for k, v in _remapping(os.path.join(package_dir, "utils.py")).items() if v in modules
    }
    return frozenset(modules | remapped)


def mlx_archs() -> frozenset[str]:
    """Every config ``model_type`` the installed mlx-lm serves; empty when
    mlx-lm is not installed (then no MLX model is offered)."""
    spec = importlib.util.find_spec("mlx_lm")
    if spec is None or not spec.submodule_search_locations:
        return frozenset()
    package_dir = list(spec.submodule_search_locations)[0]
    try:
        return _mlx_archs_of(package_dir, os.stat(os.path.join(package_dir, "models")).st_mtime_ns)
    except Exception as e:
        log.warning("Could not read the mlx-lm model types in %s: %s", package_dir, e)
        return frozenset()
