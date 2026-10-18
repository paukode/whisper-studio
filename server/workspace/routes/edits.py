"""The stored edit behind one tool row: GET /api/workspace/edits/{tool_use_id}.

The activity row of a write opens a diff dialog from this record (the file
before and after the call, recorded by server/approval/executors.py _do_write
through server/workspace/edits.py). 404 when nothing was recorded for the id:
a row from an older transcript, a write that never ran, a write whose file
could not be read before it, a save to an absolute path, or a pruned record.
A record with ``stored`` false kept its counts but not its texts (a file over
the size cap, or a dotenv or credential file).
"""

import asyncio

from fastapi import HTTPException

from .. import router
from ..edits import read_edit


@router.get("/edits/{tool_use_id}")
async def ws_edit_record(tool_use_id: str):
    record = await asyncio.to_thread(read_edit, tool_use_id)
    if record is None:
        raise HTTPException(status_code=404, detail="No edit is recorded for that tool call")
    return record
