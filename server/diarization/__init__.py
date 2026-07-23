"""Speaker identification, decoupled from any ASR backend."""

from server.diarization.speakers import (  # noqa: F401
    MIN_EMBED_SAMPLES,
    SpeakerSession,
    drop_session,
    embed,
    executor,
    get_session,
    preload,
)
