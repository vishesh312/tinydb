Write the tests for tinydb issue #{{ISSUE_NUMBER}}. The only file you may create or edit is `{{TEST_FILE}}`.

Acceptance criteria (from the spec; derive every expected value from these):

<acceptance_criteria>
{{ACCEPTANCE_CRITERIA_MD}}
</acceptance_criteria>

Public API / test hooks (from the design):

<public_api>
{{PUBLIC_API_MD}}
</public_api>

Contested tests (empty unless the Developer contested a test; each entry has the test id and the Developer's reason):

<contested_tests>
{{CONTESTED_TESTS}}
</contested_tests>

Reviewer findings about your tests (address them; revise the test file accordingly). This is `none` unless the independent reviewer sent the PR back to the testing stage:

<review_findings>
{{REVIEW_FINDINGS}}
</review_findings>

Write at least one test per criterion named `test_ac<N>_<slug>`, run `python -m pytest {{TEST_FILE}} -q` and `python -m pytest -q`, then return the `test_map.json` object as your final answer.
