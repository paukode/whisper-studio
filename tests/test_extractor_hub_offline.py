"""GLiNER's load is held offline on its own thread only.

The load forces Hugging Face Hub offline mode so transformers makes no
cache-freshness call for the backbone tokenizer and config. That flag is a
process-wide library global: flipping it outright failed any other thread's
Hub download that overlapped the load (a catalog model, a first-use
language-ID fetch). These tests pin the relation: offline on the loading
thread, untouched on every other one, restored afterwards.
"""

import sys
import threading
import types

import httpx
import huggingface_hub
import huggingface_hub.constants as hfc
import pytest
from huggingface_hub.errors import OfflineModeIsEnabled
from huggingface_hub.utils._http import hf_request_event_hook

from server.index import extractor


def _hub_request_allowed() -> bool:
    try:
        hf_request_event_hook(httpx.Request("GET", "https://huggingface.co/api/models/x"))
    except OfflineModeIsEnabled:
        return False
    return True


@pytest.fixture
def fake_gliner(monkeypatch):
    seen: dict[str, bool] = {}

    class _Model:
        def eval(self) -> None:
            pass

    class _GLiNER:
        @staticmethod
        def from_pretrained(path):
            seen["loader_offline"] = bool(huggingface_hub.is_offline_mode())
            seen["loader_request"] = _hub_request_allowed()
            other: dict[str, bool] = {}

            def elsewhere() -> None:
                other["offline"] = bool(huggingface_hub.is_offline_mode())
                other["request"] = _hub_request_allowed()

            t = threading.Thread(target=elsewhere)
            t.start()
            t.join()
            seen["other_offline"] = other["offline"]
            seen["other_request"] = other["request"]
            return _Model()

    monkeypatch.setitem(sys.modules, "gliner", types.SimpleNamespace(GLiNER=_GLiNER))
    monkeypatch.setattr(extractor, "ensure_gliner_model", lambda: extractor.GLINER_MODEL_DIR)
    monkeypatch.setattr(extractor, "_quiet_spurious_tokenizer_warning", lambda: None)
    monkeypatch.setattr(extractor, "_model", None)
    return seen


def test_gliner_load_is_offline_on_its_thread_only(fake_gliner, monkeypatch):
    monkeypatch.setattr(hfc, "HF_HUB_OFFLINE", False)
    extractor._load()
    assert fake_gliner["loader_offline"] is True
    assert fake_gliner["loader_request"] is False
    assert fake_gliner["other_offline"] is False
    assert fake_gliner["other_request"] is True
    # Restored exactly: the library global is the plain value again.
    assert hfc.HF_HUB_OFFLINE is False


def test_a_user_offline_setting_still_holds_everywhere(fake_gliner, monkeypatch):
    """HF_HUB_OFFLINE=1 from the environment keeps every thread offline."""
    monkeypatch.setattr(hfc, "HF_HUB_OFFLINE", True)
    extractor._load()
    assert fake_gliner["loader_offline"] is True
    assert fake_gliner["other_offline"] is True
    assert hfc.HF_HUB_OFFLINE is True
