"""One document conversion in a process of its own.

    python -m server.extract.worker SRC EXT FILENAME OUT

Reads the file at SRC, converts it with convert_document, and writes the text
to OUT (atomically, so a killed run leaves no half file). Run by
server/extract/child.py, which kills the process when the upload is cancelled.
"""

import os
import sys

from server.extract import convert_document


def main(argv: list[str]) -> int:
    src, ext, filename, out = argv
    with open(src, "rb") as f:
        content = f.read()
    text = convert_document(content, ext, filename)
    part = out + ".part"
    with open(part, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(part, out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
