/* Architecture - attachment extraction (type-aware file to Markdown router) */
WSDiagram.mount("extraction-diagram", {
  title: "Attachment extraction router",
  grid: { nodeW: 168, nodeH: 60, gapX: 52, gapY: 38 },
  groups: {
    transport: { label: "Upload" },
    server: { label: "Extract" },
    local: { label: "On-device" },
    external: { label: "Bedrock" }
  },
  nodes: [
    { id: "up", group: "transport", col: 0, row: 1.5, label: "Upload", sub: "/api/upload", desc: "A file is POSTed to /api/upload (server/attachments.py). Size-gated, then routed by extension and content type." },
    { id: "md", group: "server", col: 1, row: 1.5, label: "MarkItDown", sub: "to Markdown", desc: "MarkItDown runs first on every non-image file, converting PDF text layers, DOCX, PPTX, HTML, CSV, and more to Markdown. Its output is reused by the specializers." },
    { id: "scanned", group: "server", col: 2, row: 0, label: "Scanned PDF?", sub: "< 100 chars text", desc: "If the PDF's extracted text is under 100 non-whitespace chars it is treated as scanned and rasterized for OCR (server/extract/pdf.py)." },
    { id: "ocr", group: "local", col: 3, row: 0, label: "OCR", sub: "Apple Vision", desc: "Rasterized pages are OCR'd. Apple Vision (ocrmac) runs first: native macOS, on-device, fast, free, private." },
    { id: "haiku", group: "external", kind: "external", col: 5, row: 0, label: "Haiku OCR", sub: "fallback", desc: "Only if Apple Vision fails or is unavailable AND AWS credentials resolve, Bedrock Claude Haiku vision transcribes the pages." },
    { id: "sheet", group: "server", col: 2, row: 1, label: "Spreadsheet sample", sub: "schema + rows", desc: "Large spreadsheets (MarkItDown output over 60K chars) collapse to each sheet's dimensions, header, and first 20 rows (server/extract/sheet.py)." },
    { id: "img", group: "local", col: 2, row: 2, label: "Image OCR", sub: "Apple Vision", desc: "Uploaded images are OCR'd to text for text-only models and sized for OCR-friendly vision input (server/extract/image.py)." },
    { id: "media", group: "local", col: 2, row: 3, label: "Audio / video", sub: "transcribe + frames", desc: "Uploaded audio and video are transcribed on-device with mlx-whisper and speaker-labeled via ECAPA. Video also samples keyframes: their on-screen text is Apple Vision OCR'd and the frames are retained for vision models (server/extract/media.py)." },
    { id: "out", group: "server", col: 5, row: 1.5, label: "Markdown to chat", sub: "injected", desc: "The Markdown, plus a heading outline, is stored and injected into the chat turn. Vision models also get the raw image blocks." }
  ],
  edges: [
    { from: "up", to: "md" },
    { from: "md", to: "scanned" },
    { from: "scanned", to: "ocr", label: "yes" },
    { from: "ocr", to: "haiku", label: "if unavailable" },
    { from: "md", to: "sheet", label: "xlsx" },
    { from: "md", to: "img", label: "image" },
    { from: "md", to: "media", label: "audio/video" },
    { from: "md", to: "out", label: "text ok" },
    { from: "ocr", to: "out" },
    { from: "sheet", to: "out" },
    { from: "img", to: "out" },
    { from: "media", to: "out" }
  ]
});
