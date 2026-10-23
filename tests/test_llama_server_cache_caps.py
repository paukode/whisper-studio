"""llama-server's own caches are capped to the memory-fit budget
(server/local/llama_server.py::cache_caps).

The prompt cache (--cache-ram) and the context checkpoints (--ctx-checkpoints)
grow in host memory during a session, beyond the weights, KV cache and
compute buffers the fit grade counts. These tests pin how the caps relate to
the model and the budget, never a byte count. Model files are header-only
GGUFs with a sparse tail, so they report a weights size without using it.
"""

from __future__ import annotations

import pytest

from server.local import llama_server as LS
from server.model_browser import gguf_kv, service
from tests.gguf_headers import header, write_gguf

GIB = 1 << 30
MIB = 1 << 20
BUDGET = 12 * GIB
LLAMA_DEFAULT_CHECKPOINTS = 32


def cache_mib(args: list[str]) -> int:
    return int(args[args.index("--cache-ram") + 1])


def checkpoints(args: list[str]) -> int:
    return int(args[args.index("--ctx-checkpoints") + 1])


@pytest.fixture()
def budget(monkeypatch):
    monkeypatch.setattr(service, "memory_budget_bytes", lambda: BUDGET)


# ── cache_caps: the arithmetic ───────────────────────────────────────────────
def test_a_model_that_needs_more_leaves_a_smaller_prompt_cache():
    _, small = LS.cache_caps(6 * GIB, BUDGET, 0)
    _, large = LS.cache_caps(9 * GIB, BUDGET, 0)
    assert small > large > 0


def test_larger_checkpoints_leave_a_smaller_prompt_cache():
    _, hybrid = LS.cache_caps(6 * GIB, BUDGET, 50 * MIB)
    _, windowed = LS.cache_caps(6 * GIB, BUDGET, 320 * MIB)
    assert hybrid > windowed


@pytest.mark.parametrize("needed_gib", [2, 6, 10, 11.5])
@pytest.mark.parametrize("checkpoint", [0, 50 * MIB, 320 * MIB])
def test_the_caps_keep_a_loaded_model_inside_the_budget(needed_gib, checkpoint):
    needed = int(needed_gib * GIB)
    n, cache = LS.cache_caps(needed, BUDGET, checkpoint)
    if cache:
        assert needed + n * checkpoint + cache * MIB <= BUDGET


def test_a_large_machine_never_gets_more_than_the_server_default():
    """The caps only ever lower llama-server's memory use: with room to spare,
    the prompt cache stays at the server's own default."""
    _, big = LS.cache_caps(6 * GIB, 256 * GIB, 320 * MIB)
    _, bigger = LS.cache_caps(6 * GIB, 512 * GIB, 320 * MIB)
    assert big == bigger == LS._CACHE_RAM_DEFAULT_MIB


def test_a_model_that_does_not_fit_gets_no_prompt_cache():
    assert LS.cache_caps(BUDGET + GIB, BUDGET, 0)[1] == 0


@pytest.mark.parametrize("needed,budget_bytes", [(None, BUDGET), (6 * GIB, None)])
def test_unknown_sizes_give_no_prompt_cache_and_few_checkpoints(needed, budget_bytes):
    n, cache = LS.cache_caps(needed, budget_bytes, 320 * MIB)
    assert cache == 0
    assert 0 < n < LLAMA_DEFAULT_CHECKPOINTS


# ── _cache_cap_args: from the model file on disk ─────────────────────────────
def test_more_attention_layers_leave_a_smaller_prompt_cache(tmp_path, budget):
    shallow = write_gguf(tmp_path / "shallow.gguf", header(layers=8), size=4 * GIB)
    deep = write_gguf(tmp_path / "deep.gguf", header(layers=40), size=4 * GIB)
    assert cache_mib(LS._cache_cap_args(shallow, 32768)) > cache_mib(
        LS._cache_cap_args(deep, 32768)
    )


def test_a_larger_context_leaves_a_smaller_prompt_cache(tmp_path, budget):
    model = write_gguf(tmp_path / "m.gguf", header(layers=24), size=4 * GIB)
    assert cache_mib(LS._cache_cap_args(model, 16384)) > cache_mib(LS._cache_cap_args(model, 32768))


def test_window_checkpoints_leave_a_smaller_prompt_cache(tmp_path, budget):
    """Same weights and the same full-attention layer: the model with window
    layers loses more prompt cache than its extra window KV cache alone,
    because its checkpoints (copies of the window state) are set aside too."""
    pattern = [True] * 5 + [False]
    plain_meta = header(arch="gemma4", layers=1)
    windowed_meta = header(
        arch="gemma4", layers=6, sliding_window=1024, sliding_window_pattern=pattern
    )
    plain = write_gguf(tmp_path / "plain.gguf", plain_meta, size=4 * GIB)
    windowed = write_gguf(tmp_path / "windowed.gguf", windowed_meta, size=4 * GIB)
    args_plain, args_windowed = (
        LS._cache_cap_args(plain, 32768),
        LS._cache_cap_args(windowed, 32768),
    )

    def kv(meta):
        return gguf_kv.kv_cache_bytes(gguf_kv.kv_shape(meta), 32768)

    assert checkpoints(args_plain) == checkpoints(args_windowed)
    lost = (cache_mib(args_plain) - cache_mib(args_windowed)) * MIB
    assert lost > kv(windowed_meta) - kv(plain_meta)


def test_every_shard_counts_toward_the_weights(tmp_path, budget):
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    single = write_gguf(tmp_path / "one" / "M-Q4_K_M.gguf", header(), size=4 * GIB)
    first = write_gguf(tmp_path / "two" / "M-Q4_K_M-00001-of-00002.gguf", header(), size=2 * GIB)
    write_gguf(tmp_path / "two" / "M-Q4_K_M-00002-of-00002.gguf", {}, size=2 * GIB)
    assert cache_mib(LS._cache_cap_args(first, 32768)) == cache_mib(
        LS._cache_cap_args(single, 32768)
    )


@pytest.mark.parametrize("content", [None, b"<html>not a model</html>"])
def test_an_unreadable_model_gets_no_prompt_cache(tmp_path, budget, content):
    path = tmp_path / "m.gguf"
    if content is not None:
        path.write_bytes(content)
    args = LS._cache_cap_args(str(path), 32768)
    assert cache_mib(args) == 0
    assert 0 < checkpoints(args) < LLAMA_DEFAULT_CHECKPOINTS


# ── ensure_serving: the command line ─────────────────────────────────────────
def test_ensure_serving_passes_the_caps_and_lets_the_environment_override(
    tmp_path, budget, monkeypatch
):
    import server.local.runtime as L
    from server.local import registry as R

    model = write_gguf(tmp_path / "m.gguf", header(layers=8), size=2 * GIB)
    monkeypatch.setattr(
        R,
        "_config_local_models",
        lambda: {
            "local_m": {
                "id": "local:m",
                "label": "M",
                "repo_id": "a/M-GGUF",
                "filename": "m.gguf",
                "dir": "m",
                "ctx": 32768,
            }
        },
    )
    monkeypatch.setattr(LS, "ensure_available", lambda: "/usr/bin/llama-server")
    monkeypatch.setattr(L, "ensure_downloaded", lambda key: model)
    monkeypatch.setattr(LS, "stop", lambda: None)
    monkeypatch.setattr(LS, "_wait_healthy", lambda port, proc, deadline: True)
    monkeypatch.setenv("WHISPER_LLAMA_SERVER_ARGS", "--cache-ram 512")
    seen = {}

    class FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(cmd, **kw):
        seen["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(LS.subprocess, "Popen", fake_popen)
    try:
        LS.ensure_serving("local_m", n_ctx=32768)
    finally:
        LS._state.update(key=None, port=None, n_ctx=None)
        LS._proc = None
    cmd = seen["cmd"]
    computed = cmd.index("--cache-ram")
    assert int(cmd[computed + 1]) > 0  # a small model leaves room on a 12 GiB budget
    assert 0 < int(cmd[cmd.index("--ctx-checkpoints") + 1]) < LLAMA_DEFAULT_CHECKPOINTS
    # The environment's flag comes last, so it wins.
    last = len(cmd) - 1 - cmd[::-1].index("--cache-ram")
    assert last > computed and cmd[last + 1] == "512"
