"""A speaker encoder that cannot load fails with a visible error, is not
retried on every sentence (each ReDimNet2 attempt is a network call), and is
tried again after an install from Settings."""

import pytest

from server.diarization import embedder


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(embedder, "_encoder", None)
    monkeypatch.setattr(embedder, "_encoder_name", None)
    monkeypatch.setattr(embedder, "_load_error", None)
    monkeypatch.setattr(embedder, "configured_name", lambda: embedder.NAME_REDIMNET)


def test_a_failed_load_raises_once_and_is_not_retried(fresh, monkeypatch):
    calls = []

    def offline():
        calls.append(1)
        raise OSError("hub unreachable")

    monkeypatch.setattr(embedder, "_load_redimnet", offline)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="hub unreachable"):
            embedder._get_encoder()
    assert len(calls) == 1


def test_an_install_from_settings_allows_a_new_load(fresh, monkeypatch):
    state = {"online": False}

    def load():
        if not state["online"]:
            raise OSError("hub unreachable")
        return object()

    monkeypatch.setattr(embedder, "_load_redimnet", load)
    with pytest.raises(RuntimeError):
        embedder._get_encoder()
    state["online"] = True
    embedder._ensure_redimnet_files()
    assert embedder._get_encoder() is not None
    assert embedder.active_name() == embedder.NAME_REDIMNET


def test_a_changed_encoder_choice_is_loaded_fresh(fresh, monkeypatch):
    monkeypatch.setattr(embedder, "_load_redimnet", lambda: (_ for _ in ()).throw(OSError("x")))
    with pytest.raises(RuntimeError):
        embedder._get_encoder()
    monkeypatch.setattr(embedder, "configured_name", lambda: embedder.NAME_ECAPA)
    monkeypatch.setattr(embedder, "_load_ecapa", lambda: object())
    assert embedder._get_encoder() is not None
    assert embedder.active_name() == embedder.NAME_ECAPA


def test_a_new_recording_tries_a_failed_load_again(fresh, monkeypatch):
    from server.diarization.live import LiveLabeller

    state = {"online": False}

    def load():
        if not state["online"]:
            raise OSError("hub unreachable")
        return object()

    monkeypatch.setattr(embedder, "_load_redimnet", load)
    with pytest.raises(RuntimeError):
        embedder._get_encoder()
    state["online"] = True

    async def send(_frame):
        return None

    LiveLabeller(object(), split_turns=False, send=send, new_chunk_id=lambda: 0)
    assert embedder._get_encoder() is not None
