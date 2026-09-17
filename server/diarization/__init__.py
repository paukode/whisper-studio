"""Speaker identification, decoupled from any ASR backend.

Four pieces, each usable on its own:

    embedder     which speaker encoder is loaded (ReDimNet2 or ECAPA)
    turns        cut one VAD utterance at its speaker handovers
    speakers     session-scoped clustering: who is speaker N, right now
    voiceprints  named identities that survive across sessions
"""

from server.diarization import embedder, turns, voiceprints  # noqa: F401
from server.diarization.speakers import (  # noqa: F401
    MIN_EMBED_SAMPLES,
    SpeakerSession,
    drop_session,
    embed,
    executor,
    get_session,
    is_filler,
    preload,
)
