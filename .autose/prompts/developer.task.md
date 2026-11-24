Implement the design for tinydb issue #{{ISSUE_NUMBER}} on the current working tree.

Design (from stage 2):

<design>
{{DESIGN_MD}}
</design>

Specification (from stage 1; the acceptance criteria are what the tests check):

<spec>
{{SPEC_MD}}
</spec>

Retry context (empty on the first attempt; otherwise failing test output or reviewer `code` findings):

<retry_context>
{{RETRY_CONTEXT}}
</retry_context>

You must not create, modify or delete these paths (the diff is checked afterwards):

<forbidden_paths>
{{FORBIDDEN_PATHS}}
</forbidden_paths>

Edit only files under `tinydb/`. Run compileall, mypy and the test suite before answering, then return the `dev_notes.json` object as your final answer.
