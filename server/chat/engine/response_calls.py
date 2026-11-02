"""Responses API function-call assembly, with explicit per-call completion."""

from server.tool_inputs import ToolInputError, parse_tool_arguments


class ResponseCalls:
    def __init__(self):
        self.calls: dict[str, dict] = {}
        self.error: str | None = None
        self.seen = False

    def refuse(self, message: str) -> None:
        self.error = self.error or message

    @property
    def complete(self) -> bool:
        """Only positively completed items can skip the provider's terminal tail."""
        return (
            bool(self.calls)
            and not self.error
            and all(
                c["done"] and c["item_done"] and c["status"] == "completed"
                for c in self.calls.values()
            )
        )

    def start(self, item):
        self.seen = True
        item_id = getattr(item, "id", None)
        call_id = getattr(item, "call_id", None)
        name = getattr(item, "name", None)
        if not all(isinstance(v, str) and v.strip() for v in (item_id, call_id, name)):
            self.refuse("Tool call has a missing id or name.")
            return None
        if item_id in self.calls or any(c["call_id"] == call_id for c in self.calls.values()):
            self.refuse("Duplicate tool call identity in the response.")
            return None
        raw = getattr(item, "arguments", "")
        if not isinstance(raw, str):
            self.refuse(f"Tool '{name}' arguments must be a JSON string.")
            return None
        status = getattr(item, "status", None)
        if status not in (None, "in_progress", "completed"):
            self.refuse(f"Tool '{name}' item is incomplete ({status}).")
        call = {
            "call_id": call_id,
            "name": name,
            "args": raw,
            "done": False,
            "item_done": False,
            "status": status,
        }
        self.calls[item_id] = call
        return call

    def _pending(self, item_id):
        self.seen = True
        call = self.calls.get(item_id) if isinstance(item_id, str) else None
        if call is None:
            self.refuse("Orphan tool arguments: no matching call was started.")
        elif call["done"] or call["item_done"]:
            self.refuse(f"Duplicate or late arguments for tool '{call['name']}'.")
            return None
        return call

    def delta(self, item_id, piece):
        call = self._pending(item_id)
        if call is not None:
            if not isinstance(piece, str):
                self.refuse(f"Tool '{call['name']}' argument delta is not text.")
                return None
            call["args"] += piece
        return call

    def finish(self, item_id, raw):
        call = self._pending(item_id)
        if call is None:
            return None
        if call["args"] and call["args"] != raw:
            self.refuse(f"Conflicting arguments for tool '{call['name']}'.")
            return None
        if isinstance(raw, str):
            call["args"] = raw
        try:
            call["parsed"] = parse_tool_arguments(raw, call["name"])
        except ToolInputError as exc:
            self.refuse(str(exc))
            return None
        call["done"] = True
        return call

    def check_item(self, item, *, terminal: bool = False) -> None:
        """Final items confirm calls, never invent or repair missing arguments.

        The terminal response repeats its output items. Only duplicate streamed
        output_item.done events are replays; that terminal snapshot is expected.
        """
        item_id = getattr(item, "id", None)
        call = self.calls.get(item_id) if isinstance(item_id, str) else None
        if getattr(item, "type", None) != "function_call":
            if call is not None:
                self.refuse("Conflicting output item type for a tool call.")
            return
        self.seen = True
        if call is None:
            self.refuse("Orphan completed tool call: no matching call was started.")
            return
        if not terminal and call["item_done"]:
            self.refuse(f"Duplicate completed item for tool '{call['name']}'.")
        if (
            getattr(item, "call_id", None) != call["call_id"]
            or getattr(item, "name", None) != call["name"]
            or getattr(item, "arguments", None) != call["args"]
        ):
            self.refuse(f"Conflicting completed data for tool '{call['name']}'.")
        status = getattr(item, "status", None)
        if status not in (None, "completed"):
            self.refuse(f"Tool '{call['name']}' item is incomplete ({status}).")
        if not call["done"]:
            self.refuse(f"Tool '{call['name']}' arguments never finished.")
        call["item_done"] = True
        call["status"] = status

    def check_snapshot(self, output) -> None:
        """Reconcile an explicit terminal snapshot with the streamed call set."""
        if not isinstance(output, list):
            self.refuse("The final response output must be a list.")
            return
        seen = set()
        for item in output:
            self.check_item(item, terminal=True)
            if getattr(item, "type", None) != "function_call":
                continue
            item_id = getattr(item, "id", None)
            if not isinstance(item_id, str) or item_id in seen:
                self.refuse("Duplicate or missing tool identity in the final response.")
                return
            seen.add(item_id)
        if seen != set(self.calls):
            self.refuse("The final response does not match the streamed tool calls.")

    def failure(self, *, response_finished: bool) -> str | None:
        if self.error:
            return self.error
        if self.seen and (
            not self.calls
            or not all(c["done"] for c in self.calls.values())
            or (not response_finished and not self.complete)
        ):
            return "Tool arguments were incomplete: a call never finished."
        return None
