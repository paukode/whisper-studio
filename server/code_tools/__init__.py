"""Code tools: the linters and language servers the app runs for the assistant
and for the code editor.

- ``commands``: how each tool is launched. The status probe, the assistant's
  ``lsp_diagnostics``, the check after every Python write and the editor's
  language-server proxy all take their command from here, so what the Code
  tools page reports is what actually runs.
- ``ruff``: Python checks for the assistant (diagnostics and the post-write
  check, fix and format).
- ``eslint``: JS/TS checks for the assistant with the workspace's own ESLint.
- ``turn_checks``: the ``code_checks`` switch (off by default) that every run on
  the assistant's code is behind, and the checks a chat turn can end with.
- ``status``: ``GET /api/code-tools/status`` for Settings > Tools and
  automation > Code tools.
"""
