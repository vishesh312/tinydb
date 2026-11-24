"""Deterministic stage checks. The orchestrator, not an agent, decides pass/fail (ADR-007).

Each check returns a :class:`CheckResult` with a short, agent-readable ``reason``.
Stage map (DESIGN §2, §8):

* S1  :func:`check_spec`, :func:`blocking_questions`, :func:`check_judge`
* S2  :func:`check_design` (paths exist, AST symbol lookup incl. ``Class.method``, AC coverage)
* S3  :func:`path_violations` over :func:`touched`, :func:`check_build`
* S4  :func:`check_test_map` (``pytest --collect-only``), :func:`run_suite`, :func:`check_types`,
      :func:`fails_on_base`
* S6  :func:`review_problems`
"""
from __future__ import annotations

import ast
import hashlib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config


@dataclass
class CheckResult:
    passed: bool
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)


# --------------------------------------------------------------------------- shell helpers

def run(cmd: list[str], cwd: Path, timeout: int = 600, env: dict[str, str] | None = None) -> tuple[int, str]:
    """Run a command on agent-written code with a scrubbed environment; return ``(exit code, output)``."""
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           env=env if env is not None else config.scrubbed_env())
    except subprocess.TimeoutExpired:
        return 124, f"timeout after {timeout}s: {' '.join(cmd)}"
    return p.returncode, (p.stdout + p.stderr)


def git(repo: Path, *args: str, check: bool = True, env: dict[str, str] | None = None) -> str:
    """Run git with hooks and fsmonitor disabled and a scrubbed env (``env`` overrides, e.g. for push)."""
    p = subprocess.run(["git", *config.GIT_SAFE, *args], cwd=repo, capture_output=True, text=True,
                       env=env if env is not None else config.scrubbed_env())
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p.stdout


def tail(text: str, limit: int = config.RETRY_CONTEXT_BYTES) -> str:
    data = text.encode()
    return text if len(data) <= limit else "...[truncated]...\n" + data[-limit:].decode(errors="replace")


# --------------------------------------------------------------------------- path allowlists

def glob_match(path: str, pattern: str) -> bool:
    """Gitignore-like match: ``**`` spans directories, ``*`` does not; ``**/x`` also matches ``x``."""
    rx = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            rx += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            rx += ".*"
            i += 2
        elif pattern[i] == "*":
            rx += "[^/]*"
            i += 1
        else:
            rx += re.escape(pattern[i])
            i += 1
    return re.fullmatch(rx, path) is not None


def is_noise(path: str) -> bool:
    return any(glob_match(path, p) for p in config.NOISE_PATTERNS)


def path_violations(paths: Iterable[str], allowed: list[str]) -> list[str]:
    """Paths outside ``allowed`` or matching :data:`config.ALWAYS_FORBIDDEN` (ADR-005 layer 4)."""
    bad = []
    for p in paths:
        if any(glob_match(p, f) for f in config.ALWAYS_FORBIDDEN) or not any(glob_match(p, a) for a in allowed):
            bad.append(p)
    return sorted(bad)


def untracked(repo: Path, *pathspec: str) -> list[str]:
    """Untracked files, *including* ignored ones: an agent must not hide a file behind .gitignore or
    .git/info/exclude. By-products are filtered by our own :data:`config.NOISE_PATTERNS` instead."""
    out = git(repo, "ls-files", "--others", "--", *pathspec).splitlines()
    return [p for p in out if p and not is_noise(p)]


def dirty_paths(repo: Path, base: str) -> list[str]:
    """Every path that differs from ``base``: ``git diff --name-only --no-renames base`` plus untracked files."""
    changed = git(repo, "diff", "--name-only", "--no-renames", base).splitlines()
    return sorted({p for p in changed + untracked(repo) if p and not is_noise(p)})


Snapshot = dict[str, "bytes | None"]


def snapshot(repo: Path, base: str) -> Snapshot:
    """Content of every dirty path (``None`` = deleted). Paths not in it are at base content."""
    snap: Snapshot = {}
    for p in dirty_paths(repo, base):
        f = repo / p
        snap[p] = f.read_bytes() if f.is_file() else None
    return snap


def touched(before: Snapshot, after: Snapshot) -> list[str]:
    """Paths an agent changed between two snapshots (so each agent is checked only for its own edits)."""
    return sorted(p for p in set(before) | set(after) if before.get(p, b"\0base") != after.get(p, b"\0base"))


def restore(repo: Path, base: str, paths: Iterable[str], before: Snapshot) -> None:
    """Undo an agent's edits to ``paths`` (used after a path-allowlist violation)."""
    for p in paths:
        f = repo / p
        if p in before:
            if before[p] is None:
                f.unlink(missing_ok=True)
            else:
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(before[p])  # type: ignore[arg-type]
        elif _exists_in(repo, base, p):
            git(repo, "checkout", base, "--", p)
        else:
            f.unlink(missing_ok=True)


def _exists_in(repo: Path, rev: str, path: str) -> bool:
    return subprocess.run(["git", *config.GIT_SAFE, "cat-file", "-e", f"{rev}:{path}"], cwd=repo,
                          capture_output=True, env=config.scrubbed_env()).returncode == 0


def reset_to_base(repo: Path, base: str) -> None:
    """Return every dirty path under the agent-editable areas (package, tests) to its ``base`` content."""
    areas = (config.TARGET_PACKAGE + "/", "tests/")
    restore(repo, base, [p for p in dirty_paths(repo, base) if p.startswith(areas)], {})


def patch(repo: Path, base: str, prefixes: list[str]) -> str:
    """Unified diff vs ``base`` for paths under ``prefixes``, including untracked files."""
    out = git(repo, "diff", "--binary", "--no-renames", base, "--", *prefixes)
    for p in untracked(repo, *prefixes):
        out += git(repo, "diff", "--no-index", "--binary", "/dev/null", p, check=False)
    return out


def diff_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- S1

def check_spec(spec: dict[str, Any]) -> CheckResult:
    """Structure of ``spec.json`` beyond the schema (DESIGN §2 S1)."""
    problems = []
    acs = spec.get("acceptance_criteria", [])
    reqs = {r["id"] for r in spec.get("requirements", [])}
    srcs = {s["id"] for s in spec.get("source_excerpts", [])}
    if not acs:
        problems.append("the spec has no acceptance criteria")
    seen = set()
    for ac in acs:
        if ac["id"] in seen:
            problems.append(f"{ac['id']} is duplicated")
        seen.add(ac["id"])
        for key in ("given", "when", "then", "observable"):
            if not str(ac.get(key, "")).strip():
                problems.append(f"{ac['id']} has an empty '{key}'")
        if ac.get("requirement") not in reqs:
            problems.append(f"{ac['id']} cites unknown requirement {ac.get('requirement')!r}")
    for r in spec.get("requirements", []):
        for s in r.get("sources", []):
            if s.startswith("SRC-") and s not in srcs:
                problems.append(f"{r['id']} cites unknown source excerpt {s}")
    return CheckResult(not problems, "; ".join(problems))


def blocking_questions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    return [q for q in spec.get("open_questions", []) if q.get("blocking")]


def check_judge(judge: dict[str, Any], spec: dict[str, Any]) -> CheckResult:
    """Every AC must be rated testable by the Judge; a missing verdict counts as untestable."""
    verdicts = {v["ac"]: v for v in judge.get("verdicts", [])}
    bad = []
    for ac in spec.get("acceptance_criteria", []):
        v = verdicts.get(ac["id"])
        if v is None:
            bad.append(f"{ac['id']}: the judge gave no verdict")
        elif not v["testable"]:
            bad.append(f"{ac['id']}: {v['reason']}")
    return CheckResult(not bad, "untestable criteria: " + "; ".join(bad) if bad else "all criteria testable",
                       {"untestable": bad})


# --------------------------------------------------------------------------- S2

def find_symbol(source: str, dotted: str) -> bool:
    """AST lookup of a module-level function/class/assignment or ``Class.method`` / ``Class.attr``."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    body: list[ast.stmt] = tree.body
    parts = dotted.split(".")
    for i, name in enumerate(parts):
        node = next((n for n in body if _defines(n, name)), None)
        if node is None:
            return False
        if i < len(parts) - 1:
            if not isinstance(node, ast.ClassDef):
                return False
            body = node.body
    return True


def _defines(node: ast.stmt, name: str) -> bool:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return node.name == name
    if isinstance(node, ast.Assign):
        return any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
    if isinstance(node, ast.AnnAssign):
        return isinstance(node.target, ast.Name) and node.target.id == name
    if isinstance(node, (ast.If, ast.Try)):  # e.g. `if TYPE_CHECKING:` blocks, overload stubs
        return any(_defines(n, name) for n in node.body)
    return False


#: Symbols that mean "the module itself" rather than a definition inside it.
_MODULE_SYMBOLS = {"", "<module>", "*", "module"}


def check_design(design: dict[str, Any], spec: dict[str, Any], repo: Path) -> CheckResult:
    """DESIGN §2 S2: ≥1 change, paths exist or are new, ``modify`` symbols exist, AC coverage, tinydb/** only."""
    problems = []
    changes = design.get("changes", [])
    if not changes:
        problems.append("the design lists no changes")
    for c in changes:
        path, sym = c["path"], c["symbol"].strip()
        if path_violations([path], [config.package_glob()]):
            problems.append(f"{path}: only {config.package_glob()} may be changed (tests belong to the Tester)")
            continue
        f = repo / path
        if not f.is_file():
            if not (c["new_file"] or c["kind"] == "new"):
                problems.append(f"{path} does not exist and is not marked new")
            continue
        if c["kind"] == "modify" and sym not in _MODULE_SYMBOLS and not find_symbol(f.read_text(), sym):
            problems.append(f"{path}: symbol {sym!r} (kind modify) not found by AST lookup")
    covered = {a for c in changes for a in c.get("acs", [])}
    for ac in spec.get("acceptance_criteria", []):
        if ac["id"] not in covered:
            problems.append(f"{ac['id']} is not covered by any change")
    return CheckResult(not problems, "; ".join(problems))


# --------------------------------------------------------------------------- S3

def check_build(repo: Path, python: str) -> CheckResult:
    """'The code builds' = compileall + import + mypy on the package (DESIGN §2 S3, ADR-007)."""
    pkg = config.TARGET_PACKAGE
    for cmd in ([python, "-m", "compileall", "-q", pkg], [python, "-c", f"import {pkg}"],
                [python, "-m", "mypy", pkg]):
        code, out = run(cmd, repo)
        if code != 0:
            return CheckResult(False, f"`{' '.join(cmd[1:])}` failed:\n{tail(out)}", {"command": cmd})
    return CheckResult(True, "compileall, import and mypy passed")


# --------------------------------------------------------------------------- S4

def collect_tests(repo: Path, python: str, test_file: str) -> tuple[set[str], str]:
    """Node ids collected by ``pytest --collect-only -q`` for one file (plus raw output)."""
    code, out = run([python, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", "-o", "addopts=",
                     test_file], repo)
    ids = {line.strip() for line in out.splitlines() if "::" in line and not line.startswith(("ERROR", "FAILED"))}
    return (ids if code == 0 else set()), out


def _strip_params(test_id: str) -> str:
    return test_id.split("[", 1)[0]


def check_test_map(test_map: dict[str, Any], spec: dict[str, Any], collected: set[str],
                   expected_file: str, collect_output: str = "") -> CheckResult:
    """Every AC maps to ≥1 *collected* test id in the Tester's file (DESIGN §2 S4)."""
    problems = []
    if test_map.get("test_file") != expected_file:
        problems.append(f"test_file must be {expected_file}")
    if not collected:
        problems.append(f"no tests collected from {expected_file}:\n{tail(collect_output, 2000)}")
    known = {_strip_params(t) for t in collected}
    mapping = {m["ac"]: m["tests"] for m in test_map.get("mapping", [])}
    for ac in spec.get("acceptance_criteria", []):
        tests = mapping.get(ac["id"], [])
        real = [t for t in tests if _strip_params(t) in known]
        if not real:
            problems.append(f"{ac['id']} has no collected test (mapped: {tests or 'none'})")
    return CheckResult(not problems, "; ".join(problems))


@dataclass
class SuiteResult:
    passed: bool
    failing: list[str]
    counts: dict[str, int]
    output: str


_SUMMARY = re.compile(r"^(FAILED|ERROR) (\S+)")


def run_suite(repo: Path, python: str, args: list[str] | None = None) -> SuiteResult:
    """Full suite with ``pytest -rfE`` (failing ids) and junit XML (counts) for test_report.md."""
    with tempfile.TemporaryDirectory() as td:
        junit = Path(td) / "junit.xml"
        code, out = run([python, "-m", "pytest", "-rfE", "-p", "no:cacheprovider", f"--junitxml={junit}",
                         *(args or [])], repo, timeout=900)
        counts = _junit_counts(junit)
    failing = sorted({m.group(2) for line in out.splitlines() if (m := _SUMMARY.match(line))})
    return SuiteResult(code == 0, failing, counts, out)


def _junit_counts(path: Path) -> dict[str, int]:
    import xml.etree.ElementTree as ET

    if not path.exists():
        return {}
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root)
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    for s in suites:
        for k in totals:
            totals[k] += int(s.get(k, 0))
    return totals


def check_types(repo: Path, python: str) -> CheckResult:
    """``mypy tinydb tests``, mirroring upstream CI's ``pytest --mypy -m mypy tinydb tests`` (DESIGN §2 S4)."""
    code, out = run([python, "-m", "mypy", config.TARGET_PACKAGE, "tests"], repo)
    return CheckResult(code == 0, "mypy tinydb tests passed" if code == 0 else f"mypy failed:\n{tail(out)}")


def fails_on_base(repo: Path, base: str, test_file: str, python: str) -> CheckResult:
    """Anti-vacuous-test check: ≥1 new test must fail with the implementation reverted (ADR-007).

    A temporary ``git worktree`` at ``base`` (so every non-test change is reverted) gets
    only the new test file copied in; only that file is run.
    """
    tmp = Path(tempfile.mkdtemp(prefix="autose-base-"))
    wt = tmp / "base"
    try:
        git(repo, "worktree", "add", "--detach", str(wt), base)
        dest = wt / test_file
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / test_file, dest)
        code, where = run([python, "-c", f"import {config.TARGET_PACKAGE}; print({config.TARGET_PACKAGE}.__file__)"], wt)
        if code != 0 or not Path(where.strip().splitlines()[-1]).resolve().is_relative_to(wt.resolve()):
            return CheckResult(False, f"could not isolate the base checkout (imported {where.strip()})")
        code, out = run([python, "-m", "pytest", "-rfE", "-p", "no:cacheprovider", "-o", "addopts=", test_file], wt)
        failing = sorted({m.group(2) for line in out.splitlines() if (m := _SUMMARY.match(line))})
        if code in (1, 2):  # tests failed, or the file cannot import base code (= behaviour missing)
            return CheckResult(True, f"{len(failing) or 'all'} new test(s) fail on base", {"failing_on_base": failing})
        if code == 0:
            return CheckResult(False, "no new test fails on the base commit: the tests do not detect the change "
                                      "(they would pass without it)", {"failing_on_base": []})
        return CheckResult(False, f"pytest on base exited {code}:\n{tail(out, 2000)}")
    finally:
        git(repo, "worktree", "remove", "--force", str(wt), check=False)
        shutil.rmtree(tmp, ignore_errors=True)
        git(repo, "worktree", "prune", check=False)


# --------------------------------------------------------------------------- S6

#: Legacy severities (review.json before ADR-015) map onto the Claude Ship vocabulary.
LEGACY_SEVERITY = {"blocking": "important", "minor": "nit"}


def severity(finding: dict[str, Any]) -> str:
    """``important`` | ``pre_existing`` | ``nit``; old ``blocking``/``minor`` artifacts are read as important/nit."""
    sev = str(finding.get("severity", ""))
    return LEGACY_SEVERITY.get(sev, sev)


def is_blocking(finding: dict[str, Any]) -> bool:
    """Only ``important`` findings (introduced by this PR) block; ``pre_existing`` and ``nit`` never do.

    Findings the verification pass rejected are moved to ``dropped_findings`` and are not in
    ``findings`` at all (ADR-015), so they never reach this predicate.
    """
    return severity(finding) == "important"


def review_problems(review: dict[str, Any]) -> list[str]:
    """Evidence/validity rules for review.json (DESIGN §2 S6, §2.1; ADR-007, ADR-013, ADR-015)."""
    problems = []
    blocking = [f for f in review.get("findings", []) if is_blocking(f)]
    if review["verdict"] == "approve":
        ran = [c for c in review.get("checks_performed", []) if c["command"].strip()]
        if not ran:
            problems.append("approve requires at least one command in checks_performed")
        if blocking:
            problems.append("approve must not carry blocking (important) findings")
    elif not blocking:
        problems.append("changes_requested requires at least one blocking (important) finding")
    for f in review.get("findings", []):
        if not f["file"].strip():
            problems.append(f"{f['id']}: finding has no file")
        if not f["evidence"].strip():
            problems.append(f"{f['id']}: finding has no evidence")
    return problems


def review_route(review: dict[str, Any]) -> str | None:
    """Only blocking (important) findings route: approach → S2, else code → S3, else tests → S4 (ADR-013)."""
    if review["verdict"] != "changes_requested":
        return None
    cats = {f["category"] for f in review.get("findings", []) if is_blocking(f)}
    for cat, stage in (("approach", "S2"), ("code", "S3"), ("tests", "S4")):
        if cat in cats:
            return stage
    return None


def committed_paths(repo: Path, base: str) -> list[str]:
    """Paths changed between ``base`` and ``HEAD`` (the final pre-push gate, DESIGN §10)."""
    return sorted(p for p in git(repo, "diff", "--name-only", "--no-renames", base, "HEAD").splitlines() if p)
