# Search tool package — ripgrep-powered file and content search.
#
# Modules:
#   engine.py  — ripgrep binary detection, low-level subprocess interface
#   grep.py    — ws_grep executor (content search)
#   glob.py    — ws_glob executor (file path search)

# Import executors to trigger @register_executor side effects.
import server.search.glob  # noqa: F401
import server.search.grep  # noqa: F401
