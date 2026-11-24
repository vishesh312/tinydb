"""Agent invocation: prompt templating, the Claude CLI runner and a scripted fake.

DESIGN §6 / ADR-005 / ADR-011: every agent is ``claude -p --bare`` with an explicit
tool list, auto-approve rules, a per-call budget, a turn cap and a JSON schema. The
orchestrator (not the agent) validates the result again (DESIGN §14).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import jsonschema

from . import config
from .config import RoleConfig

_PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")
_TAG = re.compile(r"</?([a-z][a-z0-9_]*)[\s>]")


class TemplateError(ValueError):
    """A template placeholder was not supplied (fail loudly, never send ``{{X}}`` to a model)."""


def neutralise(value: str, tags: set[str]) -> str:
    """Escape ``<tag`` / ``</tag`` for the template's own fence tags inside a value.

    Untrusted text cannot close the ``<untrusted_issue>`` (etc.) fence it is placed in
    (DESIGN §10 injection; prompts/README.md "Anti-injection").
    """
    if not tags:
        return value
    pattern = re.compile(r"<(/?)(" + "|".join(sorted(map(re.escape, tags))) + r")\b")
    return pattern.sub(lambda m: "&lt;" + m.group(1) + m.group(2), value)


def render_template(template: str, values: dict[str, str]) -> str:
    """Fill ``{{NAME}}`` placeholders in ONE regex pass.

    Values are never re-scanned, so an issue body containing ``{{DIFF}}`` stays literal.
    Raises :class:`TemplateError` if the template uses a name that is not supplied.
    """
    missing = sorted({m.group(1) for m in _PLACEHOLDER.finditer(template)} - set(values))
    if missing:
        raise TemplateError(f"unfilled template placeholders: {', '.join(missing)}")
    tags = {m.group(1) for m in _TAG.finditer(template)}
    return _PLACEHOLDER.sub(lambda m: neutralise(str(values[m.group(1)]), tags), template)


def load_prompts(prompt: str, values: dict[str, str], prompts_dir: Path | None = None,
                 skill_file: Path | None = None) -> tuple[str, str]:
    """Return ``(system_prompt, task)`` for a role. The Analyst's system prompt gets SKILL.md appended."""
    pdir = prompts_dir or config.PROMPTS_DIR
    system = (pdir / f"{prompt}.system.md").read_text()
    if prompt == "analyst":
        system += "\n\n" + (skill_file or config.SKILL_FILE).read_text()
    if _PLACEHOLDER.search(system):
        raise TemplateError(f"system prompt {prompt} must not contain placeholders")
    task = render_template((pdir / f"{prompt}.task.md").read_text(), values)
    return system, task


def load_schema(name: str) -> dict[str, Any]:
    return json.loads((config.SCHEMAS_DIR / f"{name}.schema.json").read_text())


@dataclass
class AgentCall:
    """Everything needed for one agent invocation."""

    role: RoleConfig
    system_prompt: str
    task: str
    schema: dict[str, Any]
    cwd: Path
    log_dir: Path
    label: str
    add_dirs: list[str] = field(default_factory=list)
    allowed_tools: list[str] | None = None   # overrides role.allowed_tools (reviewer probe rules)
    path_prefix: str | None = None           # e.g. venv bin dir so `python` is the project's


@dataclass
class AgentResult:
    """Parsed and validated outcome of one agent call (recorded in the ledger)."""

    ok: bool
    output: dict[str, Any] | None
    error: str | None
    subtype: str
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    num_turns: int = 0
    seconds: float = 0.0
    model: str = ""


class Runner(Protocol):
    def invoke(self, call: AgentCall) -> dict[str, Any]:
        """Run the agent and return the CLI's JSON envelope (possibly synthesised on failure)."""


def _strip_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\s*\n(.*)\n```\s*$", text, re.S)
    return m.group(1) if m else text


def parse_envelope(env: dict[str, Any], schema: dict[str, Any], model: str = "", seconds: float = 0.0,
                   fallback_cost: float = 0.0) -> AgentResult:
    """Turn a ``--output-format json`` envelope into an :class:`AgentResult`.

    Defensive: falls back to JSON-parsing ``result`` (fences stripped) when
    ``structured_output`` is missing, then validates against the schema. An envelope
    without ``total_cost_usd`` (timeout, crash, unparseable output) is charged
    ``fallback_cost``, the role's full per-call cap, so the run cap stays conservative.
    """
    usage = env.get("usage") or {}
    cost = env.get("total_cost_usd")
    res = AgentResult(
        ok=False, output=None, error=None, subtype=str(env.get("subtype", "unknown")),
        cost_usd=float(cost) if cost is not None else fallback_cost,
        tokens_in=int(usage.get("input_tokens") or 0), tokens_out=int(usage.get("output_tokens") or 0),
        cache_read=int(usage.get("cache_read_input_tokens") or 0),
        cache_creation=int(usage.get("cache_creation_input_tokens") or 0),
        num_turns=int(env.get("num_turns") or 0),
        seconds=float(env.get("duration_ms") or 0) / 1000 or seconds, model=model,
    )
    output = env.get("structured_output")
    if output is None and isinstance(env.get("result"), str):
        try:
            output = json.loads(_strip_fences(env["result"]))
        except json.JSONDecodeError:
            output = None
    if env.get("is_error") or res.subtype != "success":
        res.error = f"agent ended with {res.subtype}: {str(env.get('result', ''))[:500]}"
        # A structured answer may still exist (e.g. max-turns after answering); keep it only if valid.
    if output is None:
        res.error = res.error or "no structured output"
        return res
    try:
        jsonschema.validate(output, schema)
    except jsonschema.ValidationError as exc:
        res.error = (res.error + "; " if res.error else "") + f"schema violation: {exc.message} at {list(exc.absolute_path)}"
        res.output = output if isinstance(output, dict) else None
        return res
    res.output = output
    res.ok = res.error is None
    return res


def agent_env(path_prefix: str | None = None) -> dict[str, str]:
    """:func:`config.scrubbed_env` (no GitHub tokens, no ``ACTIONS_*``, no ``GITHUB_ENV``/``OUTPUT``/…)."""
    env = config.scrubbed_env()
    env["DISABLE_UPDATES"] = "1"
    env["DISABLE_AUTOUPDATER"] = "1"
    if path_prefix:
        env["PATH"] = path_prefix + os.pathsep + env.get("PATH", "")
    return env


SANDBOX_SETTINGS = {"sandbox": {"enabled": True, "failIfUnavailable": True,
                                "network": {"allowedDomains": []},
                                "credentials": {"envVars": [{"name": "ANTHROPIC_API_KEY", "mode": "deny"}]}}}
class ClaudeRunner:
    """Runs the pinned Claude Code CLI headless (ADR-011)."""

    def __init__(self, executable: str = "claude") -> None:
        self.executable = executable

    def argv(self, call: AgentCall, system_file: Path) -> list[str]:
        r = call.role
        # The task always goes over stdin: no E2BIG on large diffs, no prompt text in `ps` output.
        args = [self.executable, "-p", "--bare", "--restricted", "--strict-mcp-config", "--no-session-persistence", "--model", r.model, "--effort", r.effort,
                 "--tools", ",".join(r.tools), "--permission-mode", "dontAsk",
                 "--max-budget-usd", f"{r.budget_usd:.2f}", "--max-turns", str(r.max_turns),
                 "--output-format", "json", "--json-schema", json.dumps(call.schema, separators=(",", ":")),
                 "--append-system-prompt-file", str(system_file)]
        for d in call.add_dirs:
            args += ["--add-dir", d]
        if os.environ.get("AUTOSE_SANDBOX") == "1":
            args += ["--settings", json.dumps(SANDBOX_SETTINGS)]
        args += r.extra_args
        allowed = call.allowed_tools if call.allowed_tools is not None else r.allowed_tools
        if allowed:  # variadic flag goes last so nothing after it is swallowed
            args += ["--allowedTools", *allowed]
        return args

    def invoke(self, call: AgentCall) -> dict[str, Any]:
        call.log_dir.mkdir(parents=True, exist_ok=True)
        system_file = call.log_dir / f"{call.label}.system.md"
        system_file.write_text(call.system_prompt)
        (call.log_dir / f"{call.label}.task.md").write_text(call.task)
        argv = self.argv(call, system_file)
        try:
            proc = subprocess.run(argv, cwd=call.cwd, env=agent_env(call.path_prefix), input=call.task,
                                  capture_output=True, text=True, timeout=call.role.timeout_s)
        except subprocess.TimeoutExpired:
            return {"is_error": True, "subtype": "error_timeout", "result": f"killed after {call.role.timeout_s}s"}
        except FileNotFoundError:
            return {"is_error": True, "subtype": "error_no_cli", "result": f"{self.executable} not found"}
        except OSError as exc:  # e.g. E2BIG, permission denied
            return {"is_error": True, "subtype": "error_os", "result": f"could not start {self.executable}: {exc}"}
        (call.log_dir / f"{call.label}.stdout.json").write_text(proc.stdout)
        (call.log_dir / f"{call.label}.stderr.txt").write_text(proc.stderr[-20000:])
        try:  # parse stdout even on non-zero exit: budget/turn errors still emit an envelope
            env = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
        except (json.JSONDecodeError, IndexError):
            env = {}
        if not isinstance(env, dict) or not env:
            return {"is_error": True, "subtype": "error_unparseable",
                    "result": f"exit {proc.returncode}: {proc.stderr[-500:]}"}
        return env


class FakeRunner:
    """Deterministic scripted agent for zero-cost tests (no network, no CLI).

    ``fixture_dir/script.json`` maps a role to a queue of steps::

        {"developer": [{"output": "dev1.json", "patch": "impl_buggy.patch", "cost_usd": 0.1}],
         "reviewer":  [{"output": {...inline...}}, {"subtype": "error_max_turns"}]}

    ``patch`` is applied to the agent's cwd with ``git apply`` to simulate edits; ``write``
    maps relative paths to fixture files copied in verbatim. Every call is recorded in
    :attr:`calls` so tests can assert what each agent was shown (ADR-003 hand-offs).
    """

    def __init__(self, fixture_dir: Path) -> None:
        self.dir = Path(fixture_dir)
        self.script: dict[str, list[dict[str, Any]]] = json.loads((self.dir / "script.json").read_text())
        self.calls: list[AgentCall] = []

    def invoke(self, call: AgentCall) -> dict[str, Any]:
        self.calls.append(call)
        call.log_dir.mkdir(parents=True, exist_ok=True)
        (call.log_dir / f"{call.label}.task.md").write_text(call.task)
        queue = self.script.get(call.role.prompt) or self.script.get(call.role.name) or []
        if not queue:
            return {"is_error": True, "subtype": "error_during_execution", "result": f"fake: no step for {call.role.name}"}
        step = queue.pop(0)
        if "patch" in step:
            subprocess.run(["git", *config.GIT_SAFE, "apply", "--whitespace=nowarn", str(self.dir / step["patch"])],
                           cwd=call.cwd, check=True)
        for rel, src in step.get("write", {}).items():
            dest = call.cwd / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(self.dir / src, dest)
        out = step.get("output")
        if isinstance(out, str):
            out = json.loads((self.dir / out).read_text())
        env: dict[str, Any] = {"is_error": step.get("subtype", "success") != "success",
                               "subtype": step.get("subtype", "success"), "result": step.get("result", ""),
                               "total_cost_usd": step.get("cost_usd", 0.01), "num_turns": 1, "duration_ms": 5,
                               "usage": {"input_tokens": 100, "output_tokens": 50}}
        if out is not None:
            env["structured_output"] = out
        return env


def run_agent(runner: Runner, call: AgentCall) -> AgentResult:
    """Invoke the agent and parse + validate its envelope."""
    start = time.monotonic()
    env = runner.invoke(call)
    return parse_envelope(env, call.schema, model=call.role.model, seconds=time.monotonic() - start,
                          fallback_cost=call.role.budget_usd)


def scratch_dir(prefix: str) -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))
