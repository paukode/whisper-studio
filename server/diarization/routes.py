"""Voiceprint gallery endpoints.

Enrollment piggybacks on the rename the user already performs: naming
"Speaker 2" as "Anna" in the transcript is the enrollment gesture, and
from then on Anna is recognised in later recordings. Nothing here is on
the transcription hot path.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from server.diarization import get_session, voiceprints

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/speakers", tags=["speakers"])


class EnrollRequest(BaseModel):
    session_id: str
    speaker: str
    name: str


@router.get("/voiceprints")
def list_voiceprints() -> dict:
    """Names in the gallery for the encoder currently loaded."""
    return {"names": voiceprints.names(), "enabled": voiceprints.enabled()}


@router.post("/enroll")
def enroll(req: EnrollRequest) -> dict:
    """Store the named speaker's voiceprint from this session's clusters.

    Returns ``stored: false`` (not an error) when the cluster is still too
    thin to make a centroid worth keeping — the rename itself has already
    taken effect in the UI either way, and re-naming later re-tries.
    """
    session = get_session(req.session_id)
    embeddings = session.embeddings_for(req.speaker)
    stored = voiceprints.enroll(req.name, embeddings)
    if stored:
        session.adopt_name(req.speaker, req.name)
    return {"stored": stored, "embeddings": len(embeddings)}


@router.delete("/voiceprints/{name}")
def forget_voiceprint(name: str) -> dict:
    """Drop one stored identity."""
    return {"removed": voiceprints.forget(name)}
