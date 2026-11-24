"""Stage 6: the independent Reviewer and the post-review decision (DESIGN §2 S6, §2.1, ADR-008, ADR-013).

The Reviewer job runs with no GitHub token. It reads only files a token-holding
*prepare* step wrote (issue text, trusted comments, diff), plus its own previous
``review.json``. It never sees spec/design/test report (DESIGN §4) except in the
E1 "friendly" variant, which is an experiment arm.

Find -> Verify (ADR-015): every blocking (``important``) finding of a valid review is re-checked
by a separate read-only Verifier call in a fresh context. Rejected findings move to
``dropped_findings`` and stop blocking; repository review rules come only from the base commit.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import checks, config, render
from .agent import AgentCall, Runner, load_prompts, load_schema, run_agent
from .ledger import Ledger

DIFF_LIMIT = 60_000
#: Optional maintainer review rules, read from the BASE commit only (ADR-015).
RULES_PATH = ".autose/REVIEW.md"
RULES_LIMIT = 8_000


@dataclass
class ReviewInput:
    issue: int
    title: str
    body: str
    maintainer_comments: list[dict[str, str]]
    diff: str
    base_ref: str
    head_sha: str
    round: int = 1
    previous: dict[str, Any] | None = None
    friendly: dict[str, str] | None = None   # SPEC_MD, DESIGN_MD, DEV_NOTES, TEST_REPORT (E1 variant B)
    review_rules: str = ""                    # .autose/REVIEW.md at the base commit ("" if absent)


def base_review_rules(repo: Path, base: str) -> str:
    """``.autose/REVIEW.md`` as it is at the **base** commit, never the PR head (ADR-015); "" if absent.

    Mirrors Claude Ship's rule that REVIEW.md is read "from the base or default branch, never from
    the pull request's own changes", so a PR cannot rewrite the rules it is reviewed against.
    """
    if not base or base.startswith("-"):
        return ""
    text = checks.git(repo, "show", f"{base}:{RULES_PATH}", check=False)
    if len(text) > RULES_LIMIT:
        text = text[:RULES_LIMIT] + f"\n...[{RULES_PATH} truncated at {RULES_LIMIT} chars]"
    return text.strip()


def load_input(in_dir: Path, previous: Path | None, round_no: int) -> ReviewInput:
    """Read the files the prepare step wrote (``issue.json``, ``pr.json``, ``diff.patch``)."""
    issue = json.loads((in_dir / "issue.json").read_text())
    pr = json.loads((in_dir / "pr.json").read_text())
    prev = json.loads(previous.read_text()) if previous and previous.exists() else None
    rules = in_dir / "review_rules.md"   # written by the prepare step from the base commit
    return ReviewInput(issue["number"], issue["title"], issue["body"], issue["maintainer_comments"],
                       (in_dir / "diff.patch").read_text(), pr["base_sha"], pr["head_sha"], round_no, prev,
                       review_rules=rules.read_text() if rules.exists() else "")


def _diff(inp: ReviewInput) -> str:
    return inp.diff if len(inp.diff) <= DIFF_LIMIT else \
        inp.diff[:DIFF_LIMIT] + f"\n...[diff truncated at {DIFF_LIMIT} chars; run `git diff {inp.base_ref}...HEAD`]"


def _comments(inp: ReviewInput) -> str:
    return "\n\n".join(f"@{c['author']}:\n{c['body']}" for c in inp.maintainer_comments)


def _values(inp: ReviewInput, probe_dir: str) -> dict[str, str]:
    prev = ""
    if inp.previous:
        prev = f"Verdict: {inp.previous['verdict']}\n" + render.findings_md(inp.previous, blocking_only=False,
                                                                           include_dropped=True)
    values = {
        "ISSUE_NUMBER": str(inp.issue), "ISSUE_TITLE": inp.title, "ISSUE_BODY": inp.body,
        "MAINTAINER_COMMENTS": _comments(inp),
        "DIFF": _diff(inp), "BASE_REF": inp.base_ref, "PROBE_DIR": probe_dir, "ROUND": str(inp.round),
        "PREVIOUS_FINDINGS": prev, "REVIEW_RULES": inp.review_rules,
    }
    if inp.friendly is not None:
        values.update(inp.friendly)
    return values


def can_reask(spent_usd: float, cap: float, timeout_s: int, deadline: float | None,
              what: str = "re-ask", who: str = "reviewer") -> str | None:
    """Why a re-ask (or verifier call) must be skipped (run cap or job deadline), or ``None`` (ADR-006, ADR-009)."""
    if spent_usd + cap > config.RUN_CAP_USD + 1e-9:
        return f"no {what}: ${spent_usd:.2f} spent + ${cap:.2f} {who} cap exceeds ${config.RUN_CAP_USD:.2f}"
    if deadline and time.time() + timeout_s + config.DEADLINE_SLACK_MIN * 60 > deadline:
        return f"no {what}: not enough job time left"
    return None


def _finding_text(f: dict[str, Any]) -> str:
    loc = f"{f['file']}:{f['line']}" if f.get("line") else f"{f['file']} (no line)"
    return (f"ID: {f['id']}\nSeverity: {checks.severity(f)}\nCategory: {f['category']}\nLocation: {loc}\n"
            f"Claim: {f['description']}\nReviewer's evidence: {f['evidence']}\n"
            f"Reviewer's suggested fix: {f['suggested_fix']}")


def verify_findings(review: dict[str, Any], inp: ReviewInput, repo: Path, out_dir: Path, runner: Runner,
                    ledger: Ledger, spent_usd: float = 0.0, deadline: float | None = None,
                    path_prefix: str | None = None) -> tuple[dict[str, Any], float]:
    """Find -> **Verify** (ADR-015): re-check each blocking finding in its own fresh Verifier call.

    Returns ``(verified_review, cost)``. ``REJECTED`` findings move to ``dropped_findings``;
    ``CONFIRMED``/``PLAUSIBLE`` stay blocking and are marked. Fail-safe: a finding that could
    not be verified (no budget, no time, invalid verifier output) stays blocking as
    ``unverified``. If the reviewer noticed injection attempts, a rejection is not trusted (the
    injected text may target the verifier) and the finding stays as ``plausible``. If every
    blocking finding is dropped the verdict becomes ``approve``, which is still only a COMMENT
    review: a human merges (ADR-013).
    """
    role = config.role("verifier")
    schema = load_schema("verify")
    guarded = bool(review.get("injection_attempts_noticed"))
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    counts = {"confirmed": 0, "plausible": 0, "rejected": 0, "unverified": 0}
    cost = 0.0
    for i, f in enumerate(review.get("findings", []), 1):
        if not checks.is_blocking(f):
            kept.append(f)
            continue
        f = dict(f)
        skip = can_reask(spent_usd + cost, role.budget_usd, role.timeout_s, deadline,
                         what="verification", who="verifier")
        if skip:
            f.update(verification="unverified", verification_reason=skip)
            counts["unverified"] += 1
            kept.append(f)
            ledger.event(stage="S6", event="check", round=inp.round, actor=role.name, passed=None,
                         reason=f"{f['id']}: {skip}")
            continue
        # The id is reviewer-written and appears outside the fence, so only a sanitised form is used there.
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(f["id"]))[:40]
        values = {"ISSUE_NUMBER": str(inp.issue), "ISSUE_TITLE": inp.title, "ISSUE_BODY": inp.body,
                  "MAINTAINER_COMMENTS": _comments(inp), "DIFF": _diff(inp), "BASE_REF": inp.base_ref,
                  "FINDING_ID": safe_id, "FINDING": _finding_text({**f, "id": safe_id})}
        system, task = load_prompts(role.prompt, values)
        call = AgentCall(role=role, system_prompt=system, task=task, schema=schema, cwd=repo,
                         log_dir=out_dir / "logs", label=f"{role.name}-r{inp.round}-{i}-{safe_id}",
                         path_prefix=path_prefix)
        res = run_agent(runner, call)
        cost += res.cost_usd
        out = res.output
        if res.error or out is None or out.get("finding_id") != safe_id:
            why = res.error or (f"verifier answered for {out.get('finding_id')!r}" if out else "no output")
            state, reason = "unverified", f"verifier output unusable ({why}); the finding keeps blocking"
        else:
            state, reason = out["verdict"].lower(), out["reason"].strip()
            if state == "rejected" and guarded:
                state, reason = "plausible", (f"verifier rejected it ({reason}), but the review noticed injection "
                                              "attempts, so a rejection is not trusted and the finding stays")
        counts[state] += 1
        f.update(verification=state, verification_reason=reason)
        (dropped if state == "rejected" else kept).append(f)
        ledger.event(stage="S6", event="check", round=inp.round, attempt=i, actor=role.name,
                     passed=state != "rejected", reason=f"{f['id']}: {state}: {reason}", result=res)
    out_review = {**review, "findings": kept, "dropped_findings": dropped}
    if review["verdict"] == "changes_requested" and not any(checks.is_blocking(f) for f in kept):
        out_review["verdict"] = "approve"
    out_review["verification"] = {"original_verdict": review["verdict"], "counts": counts,
                                  "cost_usd": round(cost, 4)}
    return out_review, cost


def run_review(inp: ReviewInput, repo: Path, out_dir: Path, runner: Runner, probe_dir: Path,
               variant: str = "independent", path_prefix: str | None = None, spent_usd: float = 0.0,
               deadline: float | None = None) -> dict[str, Any]:
    """Run the Reviewer, re-ask once if invalid (budget and time permitting), write review.json/.md/_meta.json.

    ``spent_usd`` is the run's spend before this review (a number passed between jobs, not an artifact).
    """
    repo = repo.resolve()
    for d in (out_dir, probe_dir):
        if d.resolve().is_relative_to(repo):
            raise ValueError(f"{d} must be outside the reviewed checkout {repo}")
    if probe_dir.resolve().is_relative_to(out_dir.resolve()) or out_dir.resolve().is_relative_to(probe_dir.resolve()):
        raise ValueError("the probe dir (agent-writable) must be separate from the artifact dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    probe_dir.mkdir(parents=True, exist_ok=True)
    role = config.role("reviewer" if variant == "independent" else "reviewer_friendly")
    ledger = Ledger(out_dir / "ledger.jsonl", inp.issue)
    system, task = load_prompts(role.prompt, _values(inp, str(probe_dir)))
    allowed = role.allowed_tools + config.reviewer_bash(str(probe_dir))
    schema = load_schema("review")
    review: dict[str, Any] | None = None
    problems: list[str] = []
    cost = 0.0
    skipped = None
    for attempt in (1, 2):
        if attempt == 2:
            skipped = can_reask(spent_usd + cost, role.budget_usd, role.timeout_s, deadline)
            if skipped:
                problems.append(skipped)
                ledger.event(stage="S6", event="stop", round=inp.round, attempt=attempt, passed=False, reason=skipped)
                break
        suffix = "" if attempt == 1 else (
            "\n\nYour previous answer was rejected by the orchestrator's validity rules:\n- "
            + "\n- ".join(problems) + "\nReturn a corrected review.json.")
        call = AgentCall(role=role, system_prompt=system, task=task + suffix, schema=schema, cwd=repo,
                         log_dir=out_dir / "logs", label=f"{role.name}-r{inp.round}-{attempt}",
                         add_dirs=[str(probe_dir)], allowed_tools=allowed, path_prefix=path_prefix)
        res = run_agent(runner, call)
        cost += res.cost_usd
        if res.error or res.output is None:
            problems = [res.error or "no structured output"]
        else:
            problems = checks.review_problems(res.output)
        ledger.event(stage="S6", event="check", round=inp.round, attempt=attempt, actor=role.name,
                     passed=not problems,
                     reason="; ".join(problems) if problems else str(res.output["verdict"]),  # type: ignore[index]
                     result=res)
        if res.output is not None:
            review = res.output
        if not problems:
            break
    valid = review is not None and not problems
    reviewer_verdict = review["verdict"] if review is not None else None
    verification: dict[str, Any] | None = None
    if valid and review is not None and any(checks.is_blocking(f) for f in review["findings"]):
        (out_dir / "review_raw.json").write_text(json.dumps(review, indent=2) + "\n")  # E1: reviewer alone
        if config.verify_enabled():
            review, vcost = verify_findings(review, inp, repo, out_dir, runner, ledger, spent_usd + cost,
                                            deadline, path_prefix)
            cost += vcost
            problems = checks.review_problems(review)  # e.g. approve-after-drops with no command run
            valid = not problems
        else:
            review = {**review, "verification": {"original_verdict": review["verdict"],
                                                 "skipped": "AUTOSE_VERIFY=0"}}
        verification = review["verification"]
    meta = {"issue": inp.issue, "round": inp.round, "head_sha": inp.head_sha, "base_ref": inp.base_ref,
            "variant": variant, "model": role.model, "valid": valid, "problems": problems, "cost_usd": round(cost, 4),
            "reviewer_verdict": reviewer_verdict, "verification": verification,
            "review_rules": bool(inp.review_rules)}
    (out_dir / "review_meta.json").write_text(json.dumps(meta, indent=2) + "\n")
    if review is not None:
        (out_dir / "review.json").write_text(json.dumps(review, indent=2) + "\n")
        note = "" if valid else "Invalid review (" + "; ".join(problems) + "); it does not count as an approval."
        (out_dir / "review.md").write_text(render.review_md(review, inp.round, final=False, note=note,
                                                           invalid=not valid))
    else:
        (out_dir / "review.md").write_text(f"## AutoSE review (round {inp.round}): **INVALID REVIEW (not an approval)**\n" +
                                           render.stop_section("; ".join(problems)))
    return meta


@dataclass
class PostDecision:
    verdict: str        # approve | changes_requested | final | awaiting-human | stopped | stale
    route: str          # S2 | S3 | S4 | ""
    note: str
    post: bool          # whether a PR review is posted


def decide_post(meta: dict[str, Any] | None, review: dict[str, Any] | None, *, final: bool, ci_result: str,
                current_head: str) -> PostDecision:
    """Post-review rules (DESIGN §2.1 S5/S6, §10 stale reviews; ADR-013 routing)."""
    if ci_result != "success":
        return PostDecision("stopped", "", f"CI did not succeed ({ci_result}); stopping. A CI-only failure "
                                           "means the environment differs; a human should look.", False)
    if meta is None:
        return PostDecision("awaiting-human", "", "The review job produced no review.", False)
    if meta.get("head_sha") and current_head and meta["head_sha"] != current_head:
        return PostDecision("stale", "", f"Discarded a review of {meta['head_sha'][:8]}: the PR head is now "
                                         f"{current_head[:8]}.", False)
    if review is None or not meta.get("valid") or checks.review_problems(review):
        # Always posted (as a COMMENT): a reviewer that produced nothing must still be visible on the PR.
        # Before 2026-11-24 a missing review.json meant nothing was posted (results/runs.md, quota runs).
        return PostDecision("awaiting-human", "", "Invalid review after one re-ask; not an approval, no routing.", True)
    if review["verdict"] == "approve":
        if (review.get("verification") or {}).get("original_verdict") == "changes_requested":
            return PostDecision("approve", "", "Verification rejected every blocking finding (approve-equivalent, "
                                               "posted as a COMMENT); a human decides whether to merge.", True)
        return PostDecision("approve", "", "Reviewer approves; a human decides whether to merge.", True)
    route = checks.review_route(review) or ""
    if final:
        return PostDecision("final", "", "Final review: send-backs are used up; a human decides.", True)
    return PostDecision("changes_requested", route, f"Changes requested; routing to {route}.", True)
