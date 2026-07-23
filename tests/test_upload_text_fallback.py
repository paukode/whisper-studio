"""`/api/upload` must accept arbitrary UTF-8 text files (so the file-tree
"Add file to chat" works for .log/.sql/.sh/.tsx/... that aren't in the
markitdown/code extension lists), while still rejecting genuinely binary
content.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.attachments as A

app = FastAPI()
app.include_router(A.router)
client = TestClient(app)


def test_unknown_text_extension_uploads_as_document():
    r = client.post(
        "/api/upload",
        files={"files": ("server (10).log", b"INFO line one\nERROR line two\n", "text/plain")},
    )
    assert r.status_code == 200, r.text
    att = r.json()["attachments"][0]
    assert att["filename"] == "server (10).log"
    stored = A.attachments[att["id"]]
    assert stored["kind"] == "document"
    assert "ERROR line two" in stored["text"]


def test_extensionless_text_uploads_as_document():
    r = client.post(
        "/api/upload",
        files={"files": ("Makefile", b"all:\n\techo hi\n", "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    att = r.json()["attachments"][0]
    assert A.attachments[att["id"]]["kind"] == "document"
    # No extension → type falls back to "text".
    assert att["type"] == "text"


def test_binary_content_still_rejected():
    # Invalid UTF-8 (lone 0xff start byte) → genuinely binary → 400.
    r = client.post(
        "/api/upload",
        files={"files": ("blob.bin", b"\xff\xfe\x00\x01rubbish", "application/octet-stream")},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["error"]
