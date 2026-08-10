"""Tool schemas for the document-creation tools.

Pure data — no imports, no side effects (mirrors server/executors/tools.py).
Implementations live in server/documents/executors.py.

These four are the standard, correct way to produce their respective file
formats in this app — real .docx/.pptx/.xlsx/.pdf bytes via native macOS
`textutil` / python-pptx / openpyxl / reportlab. Each tool description
repeats that so the model doesn't shell out to pandoc, LibreOffice, or a
raw python-docx one-liner instead (none of which are installed here, and
`ws_run_command` would just fail).
"""

CREATE_DOCX_TOOL = {
    "name": "create_docx",
    "description": (
        "[Workspace] Create a real Word (.docx) file from HTML content. This is the "
        "standard, correct way to produce a .docx in this app — it pipes your HTML "
        "through macOS's native textutil converter to produce genuine OOXML. Do NOT "
        "shell out to pandoc, LibreOffice, or a raw python-docx script via "
        "ws_run_command instead; those are not installed and this tool is the "
        "supported path. Use normal HTML tags for structure — headings (h1-h3), "
        "paragraphs (p), bold/italic (b/i/strong/em), lists (ul/ol/li), tables "
        "(table/tr/td) — textutil converts them to native Word formatting. "
        "Output automatically follows the app's standard layout (A4 page, 1-inch "
        "margins, Calibri 11pt body) — do not add fonts or page styling yourself. "
        "If a workspace is connected, `path` is workspace-relative (same as "
        "ws_create_file). If none is connected, never ask the user where to "
        "save: pass destination_path resolved from the user's own words "
        "('in Downloads' -> '~/Downloads/report.docx', ~ allowed), or omit it "
        "to default to their Documents folder — then tell the user the full "
        "path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card."
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
        "[Workspace] Create a real PowerPoint (.pptx) file, one slide per entry, each "
        "with a title and bulleted body text. This is the standard, correct way to "
        "produce a .pptx in this app — it builds a genuine OOXML presentation via "
        "python-pptx. Do NOT shell out to pandoc, LibreOffice, or a raw python-pptx "
        "one-liner via ws_run_command instead; this tool is the supported path. "
        "Slides automatically use the standard 16:9 widescreen size with the "
        "Calibri Office theme. "
        "If a workspace is connected, `path` is workspace-relative (same as "
        "ws_create_file). If none is connected, never ask the user where to "
        "save: pass destination_path resolved from the user's own words "
        "('in Downloads' -> '~/Downloads/deck.pptx', ~ allowed), or omit it "
        "to default to their Documents folder — then tell the user the full "
        "path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card."
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
        "[Workspace] Create a real Excel (.xlsx) workbook, one worksheet per entry. "
        "This is the standard, correct way to produce a .xlsx in this app — it builds "
        "a genuine OOXML workbook via openpyxl. Do NOT shell out to pandoc, "
        "LibreOffice, or a raw openpyxl one-liner via ws_run_command instead; this "
        "tool is the supported path. Worksheets automatically get the standard "
        "polish: Calibri 11, bold frozen header row, column widths fitted to "
        "content. If a workspace is connected, `path` is "
        "workspace-relative (same as ws_create_file). If none is connected, never "
        "ask the user where to save: pass destination_path resolved from the "
        "user's own words ('in Downloads' -> '~/Downloads/data.xlsx', ~ allowed), "
        "or omit it to default to their Documents folder — then tell the user the "
        "full path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card."
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
        "(built on reportlab) is the ONLY supported path; do not attempt textutil for PDF "
        "(it cannot produce one) or shell out to another converter via ws_run_command. "
        "This is deliberately simple — plain paragraphs and a title, not a general layout "
        "engine — so it is not the right tool for complex multi-column or richly "
        "formatted documents. Output automatically follows the app's standard "
        "layout (A4 page, 11pt body). If a workspace is connected, `path` is "
        "workspace-relative (same as ws_create_file). If none is connected, never "
        "ask the user where to save: pass destination_path resolved from the "
        "user's own words ('in Downloads' -> '~/Downloads/report.pdf', ~ allowed), "
        "or omit it to default to their Documents folder — then tell the user the "
        "full path in your reply. The document is built immediately and the user "
        "confirms the exact path on the approval card."
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
]
