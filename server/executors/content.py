from server.executors import register_executor


@register_executor("summarize_transcript", read_only=True, concurrent_safe=True, emits_prompt=True)
def exec_summarize(tool_input, transcript, current_attachments):
    style = tool_input.get("style", "brief")
    if not transcript.strip():
        return "No transcript available to summarize."
    instructions = {
        "action_items": "Extract all action items, tasks, and commitments. List each with who is responsible if mentioned.",
        "meeting_notes": "Format as professional meeting notes: Attendees (from speaker labels), Topics Discussed, Decisions Made, Action Items.",
        "key_points": "Extract the key points and main takeaways as a bulleted list.",
        "brief": "Provide a 2-3 sentence summary.",
    }
    return f"STYLE: {instructions.get(style, instructions['brief'])}\n\nTRANSCRIPT:\n{transcript}"


@register_executor("analyze_document", read_only=True, concurrent_safe=True, emits_prompt=True)
def exec_analyze_document(tool_input, transcript, current_attachments):
    filename = tool_input["filename"]
    question = tool_input["question"]
    section = tool_input.get("section")
    store = current_attachments
    if not store:
        return "No documents attached."
    # Mirror the per-file cap used when injecting attachments so a huge
    # document can't blow the context window even when re-fetched whole.
    CAP = 150_000
    for att in store.values():
        if att.get("filename") == filename:
            if att["kind"] == "document":
                text = att["text"]
                if section is not None and str(section).strip():
                    from server.extract.outline import get_section

                    sec = get_section(text, att.get("sections") or [], section)
                    if sec is not None:
                        return (
                            f"DOCUMENT [{filename}] section {section}:\n{sec}\n\n"
                            f"ANALYSIS QUESTION: {question}"
                        )
                    return (
                        f"Section '{section}' not found in '{filename}'. "
                        f"Outline:\n{att.get('outline') or '(no headings)'}"
                    )
                if len(text) > CAP:
                    text = (
                        text[:CAP] + f"\n\n[Truncated at {CAP:,} chars. "
                        f"Pass section=<number from the outline> for more.]"
                    )
                return f"DOCUMENT [{filename}]:\n{text}\n\nANALYSIS QUESTION: {question}"
            if att["kind"] == "image":
                return f"'{filename}' is an image already visible in the conversation. Analyze it directly from the image content above."
    available = [a["filename"] for a in store.values()]
    return f"Document '{filename}' not found. Available files: {', '.join(available) if available else 'none'}"
