"""Static configuration: roles, models, budgets, caps, path allowlists.

Sources: DESIGN.md §6 (tools/permissions), §7 (models, per-call caps, run cap),
§10 (attempt caps, timeouts, deadline), ADR-005, ADR-006, ADR-009.

Every per-role value can be overridden from the environment, e.g.
``AUTOSE_MODEL_REVIEWER``, ``AUTOSE_EFFORT_DEVELOPER``, ``AUTOSE_BUDGET_TESTER``,
``AUTOSE_MAX_TURNS_ANALYST``, ``AUTOSE_TIMEOUT_DEVELOPER`` (seconds).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
#: In this repo the package lives in ``workflow/autose``; in the fork in ``.autose/autose``.
#: Prompts and skills sit next to the package in both layouts.
ROOT_DIR = PACKAGE_DIR.parent
SCHEMAS_DIR = PACKAGE_DIR / "schemas"
PROMPTS_DIR = Path(os.environ.get("AUTOSE_PROMPTS_DIR", ROOT_DIR / "prompts"))
SKILL_FILE = Path(os.environ.get("AUTOSE_SKILL_FILE", ROOT_DIR / "skills" / "requirements-spec" / "SKILL.md"))

#: The project package the bot may change (tinydb; ADR-012).
TARGET_PACKAGE = os.environ.get("AUTOSE_TARGET_PACKAGE", "tinydb")
DEFAULT_REPO = os.environ.get("GITHUB_REPOSITORY", "vishesh312/tinydb")
BASE_BRANCH = os.environ.get("AUTOSE_BASE_BRANCH", "main")
TRIGGER_LABEL = "autose"
BOT_NAME = os.environ.get("AUTOSE_BOT_NAME", "github-actions[bot]")
BOT_EMAIL = os.environ.get("AUTOSE_BOT_EMAIL", "41898282+github-actions[bot]@users.noreply.github.com")

# --- Caps (DESIGN §2.1, §10) -------------------------------------------------
SPEC_ATTEMPTS = 2
DESIGN_ATTEMPTS = 2
DEV_ATTEMPTS = 3          # per design
TESTER_ATTEMPTS = 2       # per design
DESIGN_REVISITS = 1       # in-build S4 -> S2 loop-backs
MAX_SEND_BACKS = 2        # review send-backs (ADR-013)
RETRY_CONTEXT_BYTES = 4096


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


RUN_CAP_USD = _env_float("AUTOSE_RUN_CAP_USD", 10.0)
#: ADR-009: no new agent call if the job has less than (agent timeout + this margin) left.
DEADLINE_SLACK_MIN = _env_float("AUTOSE_DEADLINE_SLACK_MIN", 2.0)

# --- Path allowlists (DESIGN §2 S2-S4, ADR-005 §4) ---------------------------
#: Paths that no agent may ever change.
ALWAYS_FORBIDDEN = [
    ".github/**", ".autose/**", "pyproject.toml", "setup.py", "setup.cfg", "poetry.lock",
    "uv.lock", "pytest.ini", "mypy.ini", "tox.ini", ".coveragerc", "conftest.py",
    "**/conftest.py", "MANIFEST.in", "ARCHITECTURE.md", "CLAUDE.md", "**/CLAUDE.md", ".claude/**",
]
#: Build/test by-products that are never counted as an agent's edit.
NOISE_PATTERNS = ["**/__pycache__/**", "**/.pytest_cache/**", "**/.mypy_cache/**", ".coverage", ".coverage.*",
                  "*.egg-info/**", "**/*.egg-info/**", ".venv/**", "coverage.xml", "htmlcov/**",
                  "**/*.pyc"]


def tester_file(issue: int) -> str:
    """The single file the Tester owns (DESIGN §2 S4)."""
    return f"tests/test_autose_issue_{issue}.py"


def package_glob() -> str:
    return f"{TARGET_PACKAGE}/**"


# --- Roles (DESIGN §6, §7) ---------------------------------------------------
READ_TOOLS = ["Read", "Grep", "Glob"]
WRITE_TOOLS = ["Read", "Grep", "Glob", "Edit", "Write", "Bash"]


@dataclass(frozen=True)
class RoleConfig:
    """How one agent role is invoked (one row of DESIGN §6 and §7)."""

    name: str
    model: str
    effort: str
    tools: list[str]
    allowed_tools: list[str]
    budget_usd: float
    max_turns: int
    timeout_s: int = 20 * 60
    schema: str = ""
    prompt: str = ""
    extra_args: list[str] = field(default_factory=list)
    backend: str = ""   # claude | codex | agy (ADR-014); filled in by role()


SONNET = "claude-sonnet-5-5"
OPUS = "claude-opus-5-5"
HAIKU = "claude-haiku-5-5"

_DEV_BASH = ["Bash(python -m pytest *)", "Bash(python -m mypy *)", "Bash(python -m compileall *)",
             "Bash(git diff *)", "Bash(git status *)", "Bash(git diff)", "Bash(git status)"]
_TEST_BASH = ["Bash(python -m pytest *)"]
_REVIEW_BASH_STATIC = ["Bash(python -m pytest *)", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)"]


def reviewer_bash(probe_dir: str) -> list[str]:
    """Reviewer Bash rules (DESIGN §6): pytest, probes under the probe dir, git read commands."""
    probe = probe_dir.rstrip("/")
    return _REVIEW_BASH_STATIC + [f"Bash(python {probe}/*)", f"Bash(python -m pytest {probe}/*)"]


_ROLES: dict[str, RoleConfig] = {
    "analyst": RoleConfig("analyst", SONNET, "medium", READ_TOOLS, READ_TOOLS, 0.75, 30, schema="spec"),
    "judge": RoleConfig("judge", HAIKU, "low", [], [], 0.05, 3, timeout_s=5 * 60, schema="judge"),
    "architect": RoleConfig("architect", SONNET, "medium", READ_TOOLS, READ_TOOLS, 0.75, 40, schema="design"),
    "developer": RoleConfig("developer", SONNET, "high", WRITE_TOOLS, READ_TOOLS + ["Edit", "Write"] + _DEV_BASH,
                            1.50, 60, schema="dev_notes"),
    "tester": RoleConfig("tester", SONNET, "medium", WRITE_TOOLS, READ_TOOLS + ["Edit", "Write"] + _TEST_BASH,
                         1.00, 50, schema="test_map"),
    # Reviewer Bash rules depend on the probe dir; filled in by reviewer_bash() at call time.
    "reviewer": RoleConfig("reviewer", OPUS, "high", ["Read", "Grep", "Glob", "Write", "Bash"],
                           READ_TOOLS + ["Write"], 2.00, 60, schema="review"),
    # E1 variant B: same tools, same schema, builder model (experiment/variants.md).
    "reviewer_friendly": RoleConfig("reviewer_friendly", SONNET, "high", ["Read", "Grep", "Glob", "Write", "Bash"],
                                    READ_TOOLS + ["Write"], 2.00, 60, schema="review"),
    # Find -> Verify (ADR-015): re-checks one blocking reviewer finding in a fresh, read-only context.
    "verifier": RoleConfig("verifier", OPUS, "medium", READ_TOOLS, READ_TOOLS, 0.50, 25, schema="verify"),
    # E2 variant B: Developer + Tester in one context.
    "builder": RoleConfig("builder", SONNET, "high", WRITE_TOOLS, READ_TOOLS + ["Edit", "Write"] + _DEV_BASH,
                          2.50, 80, schema="builder"),
}


#: ADR-014: the agent harness is chosen per role. ``codex`` (ChatGPT subscription) is the default.
#: Override globally with ``AUTOSE_BACKEND`` or per role with ``AUTOSE_BACKEND_<ROLE>``.
BACKENDS = ("claude", "codex", "agy")
DEFAULT_BACKEND = "codex"
BUILDER_GPT = "gpt-6.1-sol"     # builders: the strongest Codex model on the subscription
SMALL_GPT = "gpt-6-luna"        # judge: small and fast
REVIEW_GPT = "gpt-6-astra"      # reviewer/verifier: a *different* model from the builders (ADR-008)
#: Default model per backend and role. ``claude`` keeps the original §7 table.
BACKEND_MODELS: dict[str, dict[str, str]] = {
    # First live run (2026-11-24): gpt-6-luna as judge failed a testable spec twice on test-layout
    # nitpicks, so the judge uses the builder model at low effort (results/runs.md, ADR-014 note).
    "codex": {"judge": BUILDER_GPT, "reviewer": REVIEW_GPT, "verifier": REVIEW_GPT, "*": BUILDER_GPT},
    "agy": {"judge": "gemini-3.8-flash-low", "reviewer": "claude-opus-4-6-thinking",
            "verifier": "claude-opus-4-6-thinking", "*": "gemini-3.8-flash-high"},
}


def backend_for(name: str) -> str:
    b = os.environ.get(f"AUTOSE_BACKEND_{name.upper()}") or os.environ.get("AUTOSE_BACKEND") or DEFAULT_BACKEND
    if b not in BACKENDS:
        raise ValueError(f"unknown backend {b!r} for role {name} (expected one of {BACKENDS})")
    return b


def role(name: str) -> RoleConfig:
    """Return the role config with ``AUTOSE_<FIELD>_<ROLE>`` environment overrides applied."""
    base = _ROLES[name]
    key = name.upper()

    def env(field_name: str, default: object) -> str:
        # Empty values count as unset: Actions passes `${{ vars.X }}` as "" when X is undefined.
        return os.environ.get(f"AUTOSE_{field_name}_{key}") or str(default)

    backend = backend_for(name)
    table = BACKEND_MODELS.get(backend)
    default_model = (table.get(name) or table["*"]) if table else base.model
    return replace(
        base,
        backend=backend,
        prompt=base.prompt or name,
        model=env("MODEL", default_model),
        effort=env("EFFORT", base.effort),
        budget_usd=float(env("BUDGET", base.budget_usd)),
        max_turns=int(env("MAX_TURNS", base.max_turns)),
        timeout_s=int(env("TIMEOUT", base.timeout_s)),
    )


ROLE_NAMES = list(_ROLES)

#: ADR-006 reservation: the agent caps a rework at each route would spend at minimum.
REWORK_ROLES = {"S2": ["architect", "developer", "tester"], "S3": ["developer", "tester"], "S4": ["tester"]}


def verify_enabled() -> bool:
    """The review verification pass (ADR-015) runs unless ``AUTOSE_VERIFY=0``."""
    return (os.environ.get("AUTOSE_VERIFY") or "1").strip() != "0"


def review_reserve() -> float:
    """Budget build/rework must leave untouched: one Reviewer cap plus one Verifier cap (ADR-006, ADR-015).

    The Verifier cap is included only while verification is enabled. Only one verifier call is
    reserved; further ones (one per blocking finding) run only if they still fit under the run cap,
    and a finding that could not be verified keeps blocking. The invalid-review re-ask is *not*
    reserved either: it runs only if it still fits, otherwise the review stays invalid and the run
    ends ``awaiting-human`` (ADR-013). Reserving it too would make the first ``approach`` send-back
    unaffordable after a typical run ($2.85 + $7.75).
    """
    return role("reviewer").budget_usd + (role("verifier").budget_usd if verify_enabled() else 0.0)


#: Variables never passed to agents or to subprocesses the orchestrator runs on agent-written code
#: (DESIGN §6, ADR-005 layer 2): tokens, and Actions files that let a process set env/outputs/PATH.
SECRET_ENV = {"GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN", "GITHUB_ENV",
              "GITHUB_PATH", "GITHUB_OUTPUT", "GITHUB_STEP_SUMMARY", "GITHUB_STATE"}


def scrubbed_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """``os.environ`` minus :data:`SECRET_ENV` and every ``ACTIONS_*`` runtime token/URL."""
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV and not k.startswith("ACTIONS_")}
    env.update(extra or {})
    return env


#: Every orchestrator git command ignores repo-controlled hooks and fsmonitor (agent-writable .git/config).
GIT_SAFE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
