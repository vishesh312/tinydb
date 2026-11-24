Implement the design for tinydb issue #{{ISSUE_NUMBER}} and write its tests in `{{TEST_FILE}}`.

Design (from stage 2):

<design>
{{DESIGN_MD}}
</design>

Specification (from stage 1; derive every expected test value from its acceptance criteria):

<spec>
{{SPEC_MD}}
</spec>

Retry context (empty on the first attempt; otherwise failing test output or reviewer findings):

<retry_context>
{{RETRY_CONTEXT}}
</retry_context>

You must not create, modify or delete these paths (the diff is checked afterwards):

<forbidden_paths>
{{FORBIDDEN_PATHS}}
</forbidden_paths>

Edit only files under `tinydb/` and the test file `{{TEST_FILE}}`. Run compileall, mypy, your tests and the full suite, then return `{"dev_notes": {...}, "test_map": {...}}` as your final answer.
