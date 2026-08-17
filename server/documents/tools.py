"""Tool schemas for the document tools.

Pure data — no imports, no side effects (mirrors server/executors/tools.py).
Implementations live in server/documents/executors.py.

Two tiers, and the descriptions below exist mostly to route between them:

- create_docx / create_pptx / create_xlsx / create_pdf take a fixed schema
  (HTML, title+bullets, rows of values, paragraphs) and apply the house
  layout. Fast and consistent for ordinary prose, decks, and tabular data.
  Their schemas are also their ceiling: no merges, no custom borders, no
  shapes, no images, and no way to touch a file that already exists.
- office_script hands the model python-docx / python-pptx / openpyxl
  directly, so anything those libraries can express is reachable, including
  editing an existing document. inspect_document is its read-only companion:
  it reports the table/sheet/slide coordinates a script needs to address.

Each description also states that these are the supported paths, so the
model doesn't shell out to pandoc or LibreOffice via ws_run_command (neither
is installed here, so that just fails).
"""

_OFFICE_SCRIPT_LIBRARIES = (
    "Available in the script: python-docx (`from docx import Document`), "
    "python-pptx (`from pptx import Presentation`), openpyxl "
    "(`from openpyxl import load_workbook, Workbook`), plus lxml and Pillow. "
    "The standard library is available; there is no network access to rely on."
)

# Appended to every document-producing tool. Exists because a model iterating
# on one deliverable (fit the page, fix a table, apply feedback) otherwise
# invents a fresh filename per attempt and litters the user's folder with
# 'v2'/'final'/'clean' strays — writes replace an existing destination
# atomically, so reusing the path is always safe.
_SAME_PATH_RULE = (
    "ONE DELIVERABLE, ONE FILE: another attempt or revision of something you "
    "already saved this conversation is NOT a new document. Pass the SAME path "
    "(and destination_path) as before — the write replaces that file in place — "
    "so the user ends up with one file, never a trail of 'v2'/'final'/'clean' "
    "variants. Pick a new name only when the user explicitly asks for a "
    "separate copy or version."
)

OFFICE_SCRIPT_TOOL = {
    "name": "office_script",
    "description": (
        "[Workspace] Build or EDIT a Word/Excel/PowerPoint file by writing real Python "
        "against python-docx / python-pptx / openpyxl. Use this whenever the user asks "
        "for anything create_docx/create_pptx/create_xlsx cannot express — table cell "
        "shading or fills, custom borders, merged cells, per-run fonts and colors, "
        "shapes, text boxes, images, charts, headers/footers, column widths, number "
        "formats, conditional formatting, speaker notes — and ALWAYS when the user "
        "wants to change a document that already exists. Those tools silently drop "
        "formatting they cannot represent (create_docx loses cell background colors "
        "entirely), so if the user asked for color, layout, or any specific visual "
        "detail, use this tool instead of hoping the simple one carries it through. "
        "\n\n"
        "Contract: your code runs with two names already defined. INPUT_PATH is the "
        "absolute path to a COPY of `source_path` (None when you passed no "
        "source_path); the source file itself is never touched while your code runs, "
        "so open INPUT_PATH, modify, and save. OUTPUT_PATH is where the finished "
        "document must be written "
        "— save to it as the last step (e.g. `doc.save(OUTPUT_PATH)`) or the tool "
        "reports that nothing was produced. Do not write anywhere else and do not "
        "hardcode paths; the destination is chosen outside your code. The destination "
        "MAY be the same file as source_path: the copy is taken before the run, so "
        "read-then-replace is the normal way to revise a file in place. "
        f"{_OFFICE_SCRIPT_LIBRARIES} "
        "\n\n"
        "When editing, call inspect_document on the file FIRST — it reports table "
        "indices and dimensions, sheet ranges and merged cells, and slide shape "
        "inventories, so you address the right cells instead of guessing. "
        "print() a short note about what you changed; the output comes back to you. "
        "\n\n"
        "The user sees your code on an approval card and it runs only after they "
        "approve, so write it to be read: no obfuscation, no unrelated side effects. "
        "If it fails, the error comes back to you — fix it and call the tool again. "
        "If a workspace is connected, `path` is workspace-relative (same as "
        "ws_create_file). If none is connected, never ask the user where to save: "
        "pass destination_path resolved from the user's own words ('in Downloads' -> "
        "'~/Downloads/report.docx', ~ allowed), or omit it to default to their "
        "Documents folder — then tell the user the full path in your reply. "
        f"{_SAME_PATH_RULE}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the output file, e.g. 'report.docx'. Must end in .docx, .xlsx, or .pptx. Used as the suggested filename when no workspace is connected.",
            },
            "code": {
                "type": "string",
                "description": "Python that reads INPUT_PATH (when editing) and writes the finished document to OUTPUT_PATH.",
            },
            "summary": {
                "type": "string",
                "description": "One short line describing what the script does, shown as the header on the user's approval card, e.g. 'shade the header row of table 2 blue'.",
            },
            "source_path": {
                "type": "string",
                "description": "Only when editing an existing document: the .docx/.xlsx/.pptx to open. Workspace-relative when a workspace is connected, otherwise an absolute path (~ allowed). May be the same file as the destination — that is how you revise a document in place. Omit to build a new file from scratch.",
            },
            "destination_path": {
                "type": "string",
                "description": "Only when no workspace is connected: full destination path resolved from the user's words, e.g. '~/Downloads/report.docx' (~ allowed). Omit when the user named no location — defaults to their Documents folder.",
            },
        },
        "required": ["path", "code", "summary"],
    },
}

INSPECT_DOCUMENT_TOOL = {
    "name": "inspect_document",
    "description": (
        "[Workspace] Report the STRUCTURE of an existing .docx/.xlsx/.pptx: for Word, "
        "the page setup, heading outline, and every table with its index, row/column "
        "counts and a sample of cell text; for Excel, each sheet's used range, frozen "
        "panes, merged ranges and first rows; for PowerPoint, the slide size and each "
        "slide's shapes with names, types and positions. Coordinates are reported in "
        "the form the editing libraries use (doc.tables[i].rows[r].cells[c], "
        "wb['Sheet']['B2'], prs.slides[i].shapes[j]), so they can be used directly in "
        "an office_script call. Read-only and immediate — it never modifies the file "
        "and needs no approval. Call this before editing a document you have not "
        "already inspected, so you address the right table, cells, or shapes rather "
        "than guessing. This reports the skeleton, not the prose: to READ a document's "
        "content, use analyze_document or ws_read_file instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "The .docx/.xlsx/.pptx to inspect. Workspace-relative when a workspace is connected, otherwise an absolute path (~ allowed).",
            },
        },
        "required": ["path"],
    },
}

CREATE_DOCX_TOOL = {
    "name": "create_docx",
    "description": (
        "[Workspace] Create a NEW Word (.docx) file from HTML content — the quick path "
        "for ordinary documents. Your HTML is rendered straight into genuine OOXML with "
        "python-docx. Do NOT shell out to pandoc or LibreOffice via ws_run_command "
        "instead; they are not installed. Supported markup, all converted to native "
        "Word formatting: headings (h1-h6), paragraphs (p, div), line and rule breaks "
        "(br, hr), bold/italic/underline/strikethrough (b, strong, i, em, u, s), inline "
        "code (code, tt), preformatted blocks (pre), quotes (blockquote), nested lists "
        "(ul, ol, li, to three levels), text color (a `color` in a style attribute), and "
        "tables (table, thead, tbody, tr, th, td, caption) — including per-cell "
        "background color via `bgcolor` or a `background-color` style, which is honored, "
        "not dropped. Unknown tags stay transparent: their text still renders. "
        "Output automatically follows the app's standard layout (A4 page, 1-inch "
        "margins, Calibri 11pt body) — do not add fonts or page styling yourself. "
        "\n\n"
        "LIMITS — call office_script instead when any of these apply: merged cells, "
        "custom cell borders, set column widths or row heights, images, charts, headers "
        "and footers, page breaks, footnotes, or any styling beyond the markup listed "
        "above. It also only ever creates a NEW file; it cannot modify one that already "
        "exists, so every edit of a document the user already has belongs in "
        "office_script. "
        "If a workspace is connected, `path` is workspace-relative (same as "
        "ws_create_file). If none is connected, never ask the user where to "
        "save: pass destination_path resolved from the user's own words "
        "('in Downloads' -> '~/Downloads/report.docx', ~ allowed), or omit it "
        "to default to their Documents folder — then tell the user the full "
        "path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card. "
        f"{_SAME_PATH_RULE}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the new file, e.g. 'report.docx'. Used as the suggested filename when no workspace is connected.",
            },
            "html_content": {
                "type": "string",
                "description": "The document body as HTML (e.g. '<h1>Title</h1><p>Body text.</p>').",
            },
            "destination_path": {
                "type": "string",
                "description": "Only when no workspace is connected: full destination path resolved from the user's words, e.g. '~/Downloads/report.docx' (~ allowed). Omit when the user named no location — defaults to their Documents folder.",
            },
        },
        "required": ["path", "html_content"],
    },
}

CREATE_PPTX_TOOL = {
    "name": "create_pptx",
    "description": (
        "[Workspace] Create a NEW PowerPoint (.pptx) file, one slide per entry, each "
        "with a title and bulleted body text — the quick path for a plain "
        "title-and-bullets deck. It builds genuine OOXML via python-pptx. Do NOT "
        "shell out to pandoc, LibreOffice, or a raw python-pptx one-liner via "
        "ws_run_command instead. Slides automatically use the standard 16:9 "
        "widescreen size with the Calibri Office theme. "
        "\n\n"
        "LIMITS — call office_script instead when any of these apply. Title plus "
        "bullets is the whole vocabulary here: no colors, fonts, shapes, text boxes, "
        "images, tables, charts, speaker notes, or slide-layout choices, and it "
        "cannot modify an existing deck. Anything visual beyond the default template "
        "needs office_script. "
        "If a workspace is connected, `path` is workspace-relative (same as "
        "ws_create_file). If none is connected, never ask the user where to "
        "save: pass destination_path resolved from the user's own words "
        "('in Downloads' -> '~/Downloads/deck.pptx', ~ allowed), or omit it "
        "to default to their Documents folder — then tell the user the full "
        "path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card. "
        f"{_SAME_PATH_RULE}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the new file, e.g. 'deck.pptx'. Used as the suggested filename when no workspace is connected.",
            },
            "slides": {
                "type": "array",
                "description": "One entry per slide, in order.",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "description": "Slide title"},
                        "bullets": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Bullet points for the slide body, in order.",
                        },
                    },
                    "required": ["title", "bullets"],
                },
            },
            "destination_path": {
                "type": "string",
                "description": "Only when no workspace is connected: full destination path resolved from the user's words, e.g. '~/Downloads/report.docx' (~ allowed). Omit when the user named no location — defaults to their Documents folder.",
            },
        },
        "required": ["path", "slides"],
    },
}

CREATE_XLSX_TOOL = {
    "name": "create_xlsx",
    "description": (
        "[Workspace] Create a NEW Excel (.xlsx) workbook, one worksheet per entry — "
        "the quick path for plain tabular data. It builds a genuine OOXML workbook "
        "via openpyxl. Do NOT shell out to pandoc, LibreOffice, or a raw openpyxl "
        "one-liner via ws_run_command instead. Worksheets automatically get the "
        "standard polish: Calibri 11, bold frozen header row, column widths fitted "
        "to content. "
        "\n\n"
        "LIMITS — call office_script instead when any of these apply. Rows of values "
        "is the whole vocabulary here: no cell fills or fonts, number or date "
        "formats, formulas as formulas, merged cells, borders, conditional "
        "formatting, data validation, or charts, and it cannot modify an existing "
        "workbook. Any of those needs office_script. "
        "If a workspace is connected, `path` is "
        "workspace-relative (same as ws_create_file). If none is connected, never "
        "ask the user where to save: pass destination_path resolved from the "
        "user's own words ('in Downloads' -> '~/Downloads/data.xlsx', ~ allowed), "
        "or omit it to default to their Documents folder — then tell the user the "
        "full path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card. "
        f"{_SAME_PATH_RULE}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the new file, e.g. 'data.xlsx'. Used as the suggested filename when no workspace is connected.",
            },
            "sheets": {
                "type": "array",
                "description": "One entry per worksheet, in order. The first entry becomes the workbook's first sheet.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Worksheet name/tab title"},
                        "rows": {
                            "type": "array",
                            "items": {"type": "array"},
                            "description": "Rows of cell values, in order (e.g. the first row is often a header row).",
                        },
                    },
                    "required": ["name", "rows"],
                },
            },
            "destination_path": {
                "type": "string",
                "description": "Only when no workspace is connected: full destination path resolved from the user's words, e.g. '~/Downloads/report.docx' (~ allowed). Omit when the user named no location — defaults to their Documents folder.",
            },
        },
        "required": ["path", "sheets"],
    },
}

CREATE_PDF_TOOL = {
    "name": "create_pdf",
    "description": (
        "[Workspace] Create a real, simple PDF: an optional title followed by plain-text "
        "paragraphs, one per page-flowing block. This is the standard, correct way to "
        "produce a .pdf in this app — nothing on this machine can render HTML/formatted "
        "content to PDF (no pandoc, no LibreOffice, no HTML-to-PDF filter), so this tool "
        "(built on reportlab) is the ONLY supported path; do not shell out to another "
        "converter via ws_run_command. "
        "This is deliberately simple — plain paragraphs and a title, not a general layout "
        "engine — so it is not the right tool for complex multi-column or richly "
        "formatted documents. Output automatically follows the app's standard "
        "layout (A4 page, 11pt body). If a workspace is connected, `path` is "
        "workspace-relative (same as ws_create_file). If none is connected, never "
        "ask the user where to save: pass destination_path resolved from the "
        "user's own words ('in Downloads' -> '~/Downloads/report.pdf', ~ allowed), "
        "or omit it to default to their Documents folder — then tell the user the "
        "full path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card. "
        f"{_SAME_PATH_RULE}"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Workspace-relative path for the new file, e.g. 'summary.pdf'. Used as the suggested filename when no workspace is connected.",
            },
            "title": {
                "type": "string",
                "description": "Optional document title, rendered at the top in a larger style.",
            },
            "paragraphs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Plain-text paragraphs, in order, each rendered as its own block.",
            },
            "destination_path": {
                "type": "string",
                "description": "Only when no workspace is connected: full destination path resolved from the user's words, e.g. '~/Downloads/report.docx' (~ allowed). Omit when the user named no location — defaults to their Documents folder.",
            },
        },
        "required": ["path", "paragraphs"],
    },
}

DOCUMENT_TOOLS: list[dict] = [
    CREATE_DOCX_TOOL,
    CREATE_PPTX_TOOL,
    CREATE_XLSX_TOOL,
    CREATE_PDF_TOOL,
    OFFICE_SCRIPT_TOOL,
    INSPECT_DOCUMENT_TOOL,
]
