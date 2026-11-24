"""The deterministic state machine S1-S5 plus rework entry points (ADR-002, DESIGN §2.1).

Agents only produce artifacts; :func:`next_stage` turns a check outcome plus counters
into the next stage. Every transition is written to ``ledger.jsonl``; counters live in
``state.json``.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import checks, config, render
from .agent import AgentCall, AgentResult, Runner, load_prompts, load_schema, run_agent
from .ledger import Ledger, State

# --------------------------------------------------------------------------- pure routing


@dataclass
class Counters:
    """Attempt counters relevant to one routing decision (DESIGN §2.1 'Counter used')."""

    spec_attempts: int = 0
    design_attempts: int = 0
    dev_attempts: int = 0
    tester_attempts: int = 0
    design_revisits: int = 0
    can_revisit_design: bool = True   # False when S2 is frozen (E2 `local --stages 3-4`)


def _exhausted_dev(c: Counters) -> str:
    return "S2" if c.can_revisit_design and c.design_revisits < config.DESIGN_REVISITS else "stop"


def next_stage(stage: str, outcome: str, c: Counters) -> str:
    """DESIGN §2.1 routing table as a pure function.

    ``outcome`` is one of ``pass``, ``fail``, ``blocking`` (S1), ``tester_fail``, ``suite_fail``,
    ``contested`` (S4) or ``no_progress`` (S3/S4). Returns a stage id, ``stop`` or ``needs-info``.
    """
    if outcome == "pass":
        return {"S1": "S2", "S2": "S3", "S3": "S4", "S4": "S5", "S5": "S6"}[stage]
    if outcome == "no_progress":
        return _exhausted_dev(c)
    if stage == "S1":
        if outcome == "blocking":
            return "needs-info"
        return "S1" if c.spec_attempts < config.SPEC_ATTEMPTS else "stop"
    if stage == "S2":
        return "S2" if c.design_attempts < config.DESIGN_ATTEMPTS else "stop"
    if stage == "S3" or (stage == "S4" and outcome == "suite_fail"):
        return "S3" if c.dev_attempts < config.DEV_ATTEMPTS else _exhausted_dev(c)
    if stage == "S4" and outcome == "tester_fail":
        return "S4" if c.tester_attempts < config.TESTER_ATTEMPTS else "stop"
    if stage == "S4" and outcome == "contested":
        return "S4"
    return "stop"  # S5 failures (after the one gh retry) and anything unknown


@dataclass(frozen=True)
class Fingerprint:
    diff: str
    failing: frozenset[str] = frozenset()


def no_progress(prev: Fingerprint | None, cur: Fingerprint) -> bool:
    """Same diff hash, or the same non-empty failing test set, as the previous attempt (DESIGN §2 S4)."""
    if prev is None:
        return False
    return prev.diff == cur.diff or (bool(cur.failing) and prev.failing == cur.failing)


def rework_affordable(spent: float, route: str) -> tuple[bool, float]:
    """ADR-006 reservation: spent + the route's agent caps + the review reserve must fit the run cap.

    The reserve is one Reviewer cap (:func:`config.review_reserve`), the same amount
    :meth:`Pipeline.call` keeps free, so a rework that starts can also be reviewed.
    """
    need = sum(config.role(r).budget_usd for r in config.REWORK_ROLES[route]) + config.review_reserve()
    return spent + need <= config.RUN_CAP_USD + 1e-9, need


# --------------------------------------------------------------------------- pipeline


class Stop(Exception):
    def __init__(self, stage: str, reason: str, status: str = "stopped") -> None:
        super().__init__(reason)
        self.stage, self.reason, self.status = stage, reason, status


@dataclass
class IssueInput:
    number: int
    title: str
    body: str
    maintainer_comments: list[dict[str, str]] = field(default_factory=list)


#: Artifact that receives ``## Stop reason`` for each stage (DESIGN §10).
STOP_ARTIFACT = {"S1": "spec.md", "S2": "design.md", "S3": "test_report.md", "S4": "test_report.md",
                 "S5": "pr.md", "S6": "review.md"}


class Pipeline:
    """Runs stages for one issue inside one job (ADR-002 level 1)."""

    def __init__(self, issue: IssueInput, repo: Path, home: Path, runner: Runner, python: str,
                 variant: str = "split", can_revisit_design: bool = True,
                 on_update: Callable[["Pipeline"], None] | None = None, path_prefix: str | None = None,
                 deadline: float | None = None, reserve_usd: float = 0.0) -> None:
        self.issue = issue
        self.repo = repo.resolve()
        if home.resolve().is_relative_to(self.repo):
            # ADR-003: agents' file tools are confined to the repo (--restricted); artifacts must live outside.
            raise ValueError(f"AUTOSE_HOME {home} must be outside the working tree {self.repo}")
        self.reserve_usd = reserve_usd
        self.dir = home / f"issue-{issue.number}"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runner, self.python, self.variant = runner, python, variant
        self.can_revisit_design = can_revisit_design
        self.on_update = on_update
        self.path_prefix = path_prefix
        self.deadline = deadline
        sp = self.dir / "state.json"
        self.state = State.load(sp) if sp.exists() else State(issue=issue.number, branch=f"autose/issue-{issue.number}")
        self.ledger = Ledger(self.dir / "ledger.jsonl", issue.number)
        self.test_file = config.tester_file(issue.number)
        self.report_rows: list[dict[str, Any]] = []
        self.warnings: list[str] = []
        self.last_suite: dict[str, Any] | None = None
        self.last_fob: dict[str, Any] | None = None

    # ------------------------------------------------------------ helpers
    def save(self) -> None:
        self.state.save(self.dir / "state.json")
        if self.on_update:
            try:
                self.on_update(self)
            except Exception as exc:  # status reporting must never kill the run
                print(f"warning: status update failed: {exc}")

    def write(self, name: str, content: str | dict[str, Any]) -> Path:
        p = self.dir / name
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=2) + "\n")
        return p

    def read_json(self, name: str) -> dict[str, Any] | None:
        p = self.dir / name
        return json.loads(p.read_text()) if p.exists() else None

    def counters(self) -> Counters:
        a = self.state.attempts
        return Counters(a.get("S1", 0), a.get("S2", 0), a.get("S3", 0), a.get("S4", 0),
                        self.state.design_revisits, self.can_revisit_design)

    def bump(self, key: str) -> int:
        self.state.attempts[key] = self.state.attempts.get(key, 0) + 1
        return self.state.attempts[key]

    def check(self, stage: str, passed: bool, reason: str, actor: str = "orchestrator") -> None:
        self.state.stage = stage
        self.ledger.event(stage=stage, event="check", round=self.state.round,
                          attempt=self.state.attempts.get(stage, 0), actor=actor, passed=passed, reason=reason[:2000])
        self.save()

    def route(self, stage: str, to: str, reason: str) -> None:
        self.ledger.event(stage=stage, event="route", round=self.state.round, reason=f"-> {to}: {reason[:500]}")

    def stop(self, stage: str, reason: str, status: str = "stopped") -> Stop:
        """Record a stop, write ``## Stop reason`` into the stage artifact, return the exception to raise."""
        self.state.status, self.state.stop_reason, self.state.stage = status, reason, stage
        self.ledger.event(stage=stage, event="stop", round=self.state.round, passed=False, reason=reason[:2000])
        if stage in ("S3", "S4"):
            self.write_report()
        art = self.dir / STOP_ARTIFACT[stage]
        art.write_text((art.read_text() if art.exists() else f"# {STOP_ARTIFACT[stage]}\n")
                       + render.stop_section(reason))
        self.save()
        return Stop(stage, reason, status)

    def call(self, role_name: str, stage: str, values: dict[str, str], label: str,
             add_dirs: list[str] | None = None, allowed: list[str] | None = None,
             task_suffix: str = "") -> AgentResult:
        """One agent call guarded by the run cap and job deadline (ADR-006, ADR-009).

        ``reserve_usd`` (build/rework: one Reviewer cap + one re-ask) is never spent here.
        """
        role = config.role(role_name)
        if self.state.cost_usd + role.budget_usd + self.reserve_usd > config.RUN_CAP_USD + 1e-9:
            raise self.stop(stage, f"run cost cap: ${self.state.cost_usd:.2f} spent + ${role.budget_usd:.2f} "
                                   f"{role_name} cap + ${self.reserve_usd:.2f} review reserve would exceed "
                                   f"${config.RUN_CAP_USD:.2f}")
        if self.deadline and time.time() + role.timeout_s + config.DEADLINE_SLACK_MIN * 60 > self.deadline:
            raise self.stop(stage, "job deadline: not enough time left for another agent call")
        system, task = load_prompts(role.prompt, values)
        call = AgentCall(role=role, system_prompt=system, task=task + task_suffix, schema=load_schema(role.schema),
                         cwd=self.repo, log_dir=self.dir / "logs", label=label, add_dirs=add_dirs or [],
                         allowed_tools=allowed, path_prefix=self.path_prefix)
        res = run_agent(self.runner, call)
        self.state.cost_usd = round(self.state.cost_usd + res.cost_usd, 4)
        self.ledger.event(stage=stage, event="start", round=self.state.round,
                          attempt=self.state.attempts.get(stage, 0), actor=role_name, passed=res.ok,
                          reason=res.error or "ok", result=res)
        self.save()
        return res

    # ------------------------------------------------------------ S1
    def requirements(self) -> dict[str, Any]:
        """S1: Analyst + structure check + Judge; blocking questions stop with needs-info."""
        feedback = ""
        while True:
            n = self.bump("S1")
            comments = "\n\n".join(f"@{c['author']}:\n{c['body']}" for c in self.issue.maintainer_comments)
            res = self.call("analyst", "S1", {
                "ISSUE_NUMBER": str(self.issue.number), "ISSUE_TITLE": self.issue.title,
                "ISSUE_BODY": self.issue.body, "MAINTAINER_COMMENTS": comments, "PREVIOUS_FEEDBACK": feedback,
            }, f"analyst-{n}")
            spec, judge = res.output, None
            if res.ok and spec is not None:
                self.write("spec.json", spec)
                result = checks.check_spec(spec)
                if result.passed and checks.blocking_questions(spec):
                    self.write("spec.md", render.spec_md(spec))
                    self.write("questions.md", render.questions_comment(spec))
                    self.check("S1", False, "blocking open questions")
                    raise self.stop("S1", "The issue has blocking open questions; they were posted on the issue.",
                                    status="needs-info")
                if result.passed:
                    jres = self.call("judge", "S1", {"ACCEPTANCE_CRITERIA_JSON": json.dumps(
                        spec["acceptance_criteria"], indent=2)}, f"judge-{n}")
                    judge = jres.output
                    result = checks.check_judge(judge, spec) if jres.ok and judge else \
                        checks.CheckResult(False, f"judge failed: {jres.error}")
                    if judge:
                        self.write("judge.json", judge)
            else:
                result = checks.CheckResult(False, f"invalid analyst output: {res.error}")
            self.write("spec.md", render.spec_md(spec, judge, result.reason) if res.ok and spec else
                       f"# Spec: issue #{self.issue.number}\n\nNo valid spec was produced.\n")
            self.check("S1", result.passed, result.reason or "spec checks passed")
            if result.passed:
                assert spec is not None
                return spec
            nxt = next_stage("S1", "fail", self.counters())
            self.route("S1", nxt, result.reason)
            if nxt == "stop":
                raise self.stop("S1", f"Spec check failed after {n} attempts: {result.reason}")
            feedback = result.reason

    # ------------------------------------------------------------ S2
    def design(self, spec: dict[str, Any], loopback: str = "") -> dict[str, Any]:
        """S2: Architect + design checker (paths, AST symbols, AC coverage, tinydb/** only)."""
        self.state.attempts["S2"] = 0
        feedback = ""
        spec_text = (self.dir / "spec.md").read_text()
        while True:
            n = self.bump("S2")
            res = self.call("architect", "S2", {"ISSUE_NUMBER": str(self.issue.number), "SPEC_MD": spec_text,
                                                "LOOPBACK_CONTEXT": loopback, "PREVIOUS_FEEDBACK": feedback},
                            f"architect-r{self.state.round}-{n}")
            if res.ok and res.output:
                design = res.output
                self.write("design.json", design)
                result = checks.check_design(design, spec, self.repo)
                self.write("design.md", render.design_md(design, result.reason or "design checks passed"))
            else:
                result = checks.CheckResult(False, f"invalid architect output: {res.error}")
                self.write("design.md", "# Design\n\nNo valid design was produced.\n")
            self.check("S2", result.passed, result.reason or "design checks passed")
            if result.passed:
                # A new design resets the Developer and Tester counters (DESIGN §2.1).
                self.state.attempts["S3"] = self.state.attempts["S4"] = 0
                return design
            nxt = next_stage("S2", "fail", self.counters())
            self.route("S2", nxt, result.reason)
            if nxt == "stop":
                raise self.stop("S2", f"Design check failed after {n} attempts: {result.reason}")
            feedback = result.reason

    # ------------------------------------------------------------ S3/S4 shared
    def pkg_patch(self) -> str:
        text = checks.patch(self.repo, self.state.base_sha, [config.TARGET_PACKAGE])
        self.write("diff.patch", text)
        return text

    def forbidden_text(self, allowed: list[str]) -> str:
        return ("Anything outside: " + ", ".join(allowed) + "\nIn particular: tests/** (except the files above), "
                + ", ".join(config.ALWAYS_FORBIDDEN))

    def record(self, actor: str, check: str, passed: bool, detail: str) -> None:
        self.report_rows.append({"actor": actor, "check": check, "passed": passed, "detail": detail})

    def write_report(self) -> None:
        self.write("test_report.md", render.report_md(self.issue.number, self.report_rows,
                                                      self.read_json("test_map.json"), self.last_suite,
                                                      self.last_fob, self.warnings))

    def design_texts(self) -> tuple[str, str]:
        return (self.dir / "design.md").read_text(), (self.dir / "spec.md").read_text()

    def developer_attempt(self, retry: str) -> tuple[checks.CheckResult, Fingerprint]:
        """One Developer attempt and the S3 check (allowlist, non-empty diff, build)."""
        n = self.bump("S3")
        design_text, spec_text = self.design_texts()
        allowed = [config.package_glob()]
        before = checks.snapshot(self.repo, self.state.base_sha)
        res = self.call("developer", "S3", {
            "ISSUE_NUMBER": str(self.issue.number), "DESIGN_MD": design_text, "SPEC_MD": spec_text,
            "RETRY_CONTEXT": retry, "FORBIDDEN_PATHS": self.forbidden_text(allowed)},
            f"developer-r{self.state.round}-d{self.state.design_revisits}-{n}")
        after = checks.snapshot(self.repo, self.state.base_sha)
        if res.output:
            self.write("dev_notes.json", res.output)
        elif not res.ok:
            self.warnings.append(f"developer attempt {n}: {res.error}")
        result = self._s3_check(before, after, allowed, "developer")
        return result, Fingerprint(checks.diff_hash(self.pkg_patch()))

    def _s3_check(self, before: checks.Snapshot, after: checks.Snapshot, allowed: list[str],
                  actor: str) -> checks.CheckResult:
        changed = checks.touched(before, after)
        bad = checks.path_violations(changed, allowed)
        if bad:
            checks.restore(self.repo, self.state.base_sha, bad, before)
            result = checks.CheckResult(False, f"touched paths outside the allowlist (reverted): {', '.join(bad)}")
        elif not self.pkg_patch().strip():
            result = checks.CheckResult(False, f"the diff under {config.TARGET_PACKAGE}/ is empty")
        else:
            result = checks.check_build(self.repo, self.python)
        design = self.read_json("design.json") or {}
        planned = {c["path"] for c in design.get("changes", [])}
        extra = [p for p in changed if p.startswith(config.TARGET_PACKAGE + "/") and p not in planned]
        if extra:
            self.warnings.append(f"{actor} changed files not in the design: {', '.join(extra)}")
        self.record(actor, "S3 build", result.passed, result.reason)
        self.check("S3", result.passed, result.reason, actor=actor)
        return result

    def tester_attempt(self, spec: dict[str, Any], context: str, findings: str = "") -> checks.CheckResult:
        """One Tester attempt and the Tester's own check (allowlist, AC mapping, collection, fails-on-base)."""
        n = self.bump("S4")
        design = self.read_json("design.json") or {}
        before = checks.snapshot(self.repo, self.state.base_sha)
        res = self.call("tester", "S4", {
            "ISSUE_NUMBER": str(self.issue.number), "TEST_FILE": self.test_file,
            "ACCEPTANCE_CRITERIA_MD": render.acceptance_md(spec), "PUBLIC_API_MD": render.public_api_md(design),
            "CONTESTED_TESTS": context, "REVIEW_FINDINGS": findings or "none"}, f"tester-r{self.state.round}-d{self.state.design_revisits}-{n}")
        after = checks.snapshot(self.repo, self.state.base_sha)
        changed = checks.touched(before, after)
        bad = checks.path_violations(changed, [self.test_file])
        if bad:
            checks.restore(self.repo, self.state.base_sha, bad, before)
            result = checks.CheckResult(False, f"touched paths other than {self.test_file} (reverted): "
                                               f"{', '.join(bad)}")
        elif not res.output:
            result = checks.CheckResult(False, f"invalid tester output: {res.error}")
        else:
            self.write("test_map.json", res.output)
            result = self._tests_own_check(spec, res.output)
        self.record("tester", "S4 tester check", result.passed, result.reason)
        self.check("S4", result.passed, result.reason, actor="tester")
        return result

    def _tests_own_check(self, spec: dict[str, Any], test_map: dict[str, Any]) -> checks.CheckResult:
        if not (self.repo / self.test_file).is_file():
            return checks.CheckResult(False, f"{self.test_file} does not exist")
        collected, out = checks.collect_tests(self.repo, self.python, self.test_file)
        mapped = checks.check_test_map(test_map, spec, collected, self.test_file, out)
        if not mapped.passed:
            return mapped
        fob = checks.fails_on_base(self.repo, self.state.base_sha, self.test_file, self.python)
        self.last_fob = {"passed": fob.passed, "reason": fob.reason}
        return fob if not fob.passed else checks.CheckResult(True, f"AC mapping complete; {fob.reason}")

    def suite_check(self) -> tuple[checks.CheckResult, frozenset[str], bool]:
        """Full suite + ``mypy tinydb tests``. Returns (result, failing ids, tester_at_fault)."""
        suite = checks.run_suite(self.repo, self.python)
        self.last_suite = {"passed": suite.passed, "failing": suite.failing, "counts": suite.counts}
        if not suite.passed:
            ids = ", ".join(suite.failing) or "(see output)"
            r = checks.CheckResult(False, f"full suite failed; failing: {ids}\n{checks.tail(suite.output)}")
            self.record("orchestrator", "S4 suite", False, f"failing: {ids}")
            self.check("S4", False, r.reason)
            return r, frozenset(suite.failing), False
        types = checks.check_types(self.repo, self.python)
        tester_fault = (not types.passed) and self.test_file in types.reason \
            and f"{config.TARGET_PACKAGE}/" not in types.reason
        self.record("orchestrator", "S4 suite + mypy", types.passed, types.reason if not types.passed else
                    f"suite passed {json.dumps(suite.counts)}; mypy passed")
        self.check("S4", types.passed, types.reason if not types.passed else "full suite and mypy passed")
        return types, frozenset({"<mypy>"} if not types.passed else set()), tester_fault

    def failure_summary(self, reason: str) -> str:
        text = f"# Failure summary (issue #{self.issue.number})\n\n{checks.tail(reason)}\n"
        self.write("failure_summary.md", text)
        return text

    def revisit_design(self, spec: dict[str, Any], reason: str) -> None:
        """S4 -> S2 once with failure_summary.md; the new design starts from a clean tree."""
        self.state.design_revisits += 1
        loop = (self.dir / "design.md").read_text() + "\n\n" + self.failure_summary(reason)
        checks.reset_to_base(self.repo, self.state.base_sha)  # the new design is checked against base
        self.design(spec, loop)

    # ------------------------------------------------------------ S3 -> S4 loop (split variant)
    def implement_and_test(self, spec: dict[str, Any], entry: str = "S3", retry: str = "",
                           tests_ready: bool = False, tester_context: str = "", review_findings: str = "") -> None:
        """Run S3/S4 until the S4 check passes, or raise :class:`Stop` (DESIGN §2.1)."""
        if self.variant == "single":
            return self.build_single(spec, retry)
        need_dev = entry == "S3"
        prev: Fingerprint | None = None
        while True:
            if need_dev:
                result, fp = self.developer_attempt(retry)
                if not result.passed:
                    outcome = "no_progress" if no_progress(prev, fp) else "fail"
                    prev = fp
                    nxt = next_stage("S3", outcome, self.counters())
                    if self._follow(nxt, "S3", spec, result.reason):
                        need_dev, tests_ready, retry, prev = True, False, "", None
                        continue
                    retry = result.reason
                    continue
                notes = self.read_json("dev_notes.json") or {}
                fresh = [c for c in notes.get("contested_tests", []) if c["test_id"] not in self.state.contested]
                if fresh and tests_ready:  # the Tester re-checks each contested test once
                    self.state.contested += [c["test_id"] for c in fresh]
                    self.route("S4", "S4", "developer contested tests")
                    tests_ready, tester_context = False, "\n".join(f"- {c['test_id']}: {c['reason']}" for c in fresh)
            need_dev = True
            if not tests_ready:
                result = self.tester_attempt(spec, tester_context, review_findings)
                if not result.passed:
                    nxt = next_stage("S4", "tester_fail", self.counters())
                    self.route("S4", nxt, result.reason)
                    if nxt == "stop":
                        raise self.stop("S4", f"Tester check failed after {self.state.attempts['S4']} attempts: "
                                              f"{result.reason}")
                    tester_context, need_dev = f"Your previous attempt failed the check:\n{result.reason}", False
                    continue
                tests_ready, tester_context, review_findings = True, "", ""
            else:  # kept tests: re-run fails-on-base against the current base (DESIGN §2.1 code send-back)
                fob = checks.fails_on_base(self.repo, self.state.base_sha, self.test_file, self.python)
                self.last_fob = {"passed": fob.passed, "reason": fob.reason}
                self.record("orchestrator", "S4 fails-on-base (kept tests)", fob.passed, fob.reason)
                if not fob.passed:  # a Tester-own check failure: S4 retry (DESIGN §2.1)
                    self.check("S4", False, fob.reason)
                    nxt = next_stage("S4", "tester_fail", self.counters())
                    self.route("S4", nxt, fob.reason)
                    if nxt == "stop":
                        raise self.stop("S4", f"Tester attempts exhausted: {fob.reason}")
                    tests_ready, need_dev = False, False
                    tester_context = f"Your kept tests failed the orchestrator's check:\n{fob.reason}"
                    continue
            result, failing, tester_fault = self.suite_check()
            self.write_report()
            if result.passed:
                self.state.status = "tests-passed"
                self.save()
                return
            if tester_fault:
                tests_ready, need_dev = False, False
                tester_context = f"Your test file fails type checking:\n{result.reason}"
                nxt = next_stage("S4", "tester_fail", self.counters())
                if nxt == "stop":
                    raise self.stop("S4", f"Tester attempts exhausted: {result.reason}")
                continue
            fp = Fingerprint(checks.diff_hash(self.pkg_patch()), failing)
            outcome = "no_progress" if no_progress(prev, fp) else "suite_fail"
            prev = fp
            nxt = next_stage("S4", outcome, self.counters())
            if self._follow(nxt, "S4", spec, result.reason):
                need_dev, tests_ready, retry, prev = True, False, "", None
                continue
            retry = checks.tail(result.reason)

    def _follow(self, nxt: str, stage: str, spec: dict[str, Any], reason: str) -> bool:
        """Apply a non-retry route. Returns True after a design revisit; raises on stop."""
        self.route(stage, nxt, reason)
        if nxt == "stop":
            raise self.stop(stage, f"Stopped after {self.state.attempts.get('S3', 0)} Developer attempts "
                                   f"and {self.state.design_revisits} design revisit(s): {checks.tail(reason, 1500)}")
        if nxt == "S2":
            self.revisit_design(spec, reason)
            return True
        return False

    # ------------------------------------------------------------ E2 variant B: one Builder
    def build_single(self, spec: dict[str, Any], retry: str = "") -> None:
        """S3+S4 by one Builder agent; same checks as the split variant (experiment/variants.md E2)."""
        allowed = [config.package_glob(), self.test_file]
        prev: Fingerprint | None = None
        while True:
            n = self.bump("S3")
            design_text, spec_text = self.design_texts()
            before = checks.snapshot(self.repo, self.state.base_sha)
            res = self.call("builder", "S3", {
                "ISSUE_NUMBER": str(self.issue.number), "DESIGN_MD": design_text, "SPEC_MD": spec_text,
                "TEST_FILE": self.test_file, "RETRY_CONTEXT": retry,
                "FORBIDDEN_PATHS": self.forbidden_text(allowed)}, f"builder-d{self.state.design_revisits}-{n}")
            after = checks.snapshot(self.repo, self.state.base_sha)
            if res.output:
                self.write("dev_notes.json", res.output["dev_notes"])
                self.write("test_map.json", res.output["test_map"])
            result = self._s3_check(before, after, allowed, "builder")
            failing: frozenset[str] = frozenset()
            if result.passed:
                result = self._tests_own_check(spec, res.output["test_map"]) if res.output else \
                    checks.CheckResult(False, f"invalid builder output: {res.error}")
                self.record("builder", "S4 test check", result.passed, result.reason)
                self.check("S4", result.passed, result.reason, actor="builder")
            if result.passed:
                result, failing, _ = self.suite_check()
            self.write_report()
            if result.passed:
                self.state.status = "tests-passed"
                self.save()
                return
            fp = Fingerprint(checks.diff_hash(self.pkg_patch()), failing)
            outcome = "no_progress" if no_progress(prev, fp) else "suite_fail"
            prev = fp
            nxt = next_stage("S4", outcome, self.counters())
            if self._follow(nxt, "S4", spec, result.reason):
                retry, prev = "", None
                continue
            retry = checks.tail(result.reason)

    # ------------------------------------------------------------ entry points
    def run_build(self, stages: tuple[int, int] = (1, 4)) -> None:
        """S1..S4 for a fresh pipeline (``build`` and ``local``). Release is done by the caller."""
        self.state.status = "running"
        if not self.state.base_sha:
            self.state.base_sha = checks.git(self.repo, "rev-parse", "HEAD").strip()
        self.save()
        lo, hi = stages
        if lo <= 1:
            spec = self.requirements()
        else:
            spec = self._frozen("spec.json")
        if hi < 2:
            return
        if lo <= 2:
            self.design(spec)
        else:
            self._frozen("design.json")
            self.write("spec.md", render.spec_md(spec))
            self.write("design.md", render.design_md(self.read_json("design.json") or {}))
        if hi < 3:
            return
        self.implement_and_test(spec)

    def _frozen(self, name: str) -> dict[str, Any]:
        data = self.read_json(name)
        if data is None:
            raise self.stop("S1" if name == "spec.json" else "S2", f"frozen {name} missing in {self.dir}")
        return data

    def run_rework(self, route: str, review: dict[str, Any]) -> None:
        """Rework after a review send-back (ADR-013): S2, S3 or S4, then on to S4's check."""
        spec = self._frozen("spec.json")
        self.state.round += 1
        self.state.send_backs += 1
        self.state.status = "running"
        self.report_rows = []
        if route == "S2":
            loop = (self.dir / "design.md").read_text() + "\n\nReviewer findings (approach):\n" + \
                render.findings_md(review, {"approach"})
            checks.reset_to_base(self.repo, self.state.base_sha)  # design against base; Tester regenerates tests
            self.design(spec, loop)
            self.implement_and_test(spec)
        elif route == "S3":
            self.state.attempts["S3"] = 0
            self.implement_and_test(spec, entry="S3", tests_ready=True,
                                    retry="Reviewer findings (code):\n" + render.findings_md(review, {"code"}))
        else:
            self.state.attempts["S4"] = 0
            self.implement_and_test(spec, entry="S4", tests_ready=False,
                                    review_findings=render.findings_md(review, {"tests"}))

    # ------------------------------------------------------------ S5 (deterministic)
    def commit(self, message: str) -> str:
        """Commit only allowlisted paths with the bot identity; return HEAD.

        ``git reset`` first, so nothing an agent staged (``git add``) outside the allowlist rides along.
        """
        checks.git(self.repo, "reset", "-q")
        paths = [config.TARGET_PACKAGE]
        if (self.repo / self.test_file).exists() or checks.git(self.repo, "ls-files", "--", self.test_file).strip():
            paths.append(self.test_file)
        checks.git(self.repo, "add", "-A", "--", *paths)
        if checks.git(self.repo, "diff", "--cached", "--name-only").strip():
            checks.git(self.repo, "-c", f"user.name={config.BOT_NAME}", "-c", f"user.email={config.BOT_EMAIL}",
                       "commit", "-q", "-m", message)
        self.state.head_sha = checks.git(self.repo, "rev-parse", "HEAD").strip()
        self.save()
        return self.state.head_sha
