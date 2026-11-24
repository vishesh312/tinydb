"""GitHub access through the ``gh`` CLI. Only deterministic code calls this (ADR-005).

Every call passes ``--repo`` (DESIGN §10.1) so a fork never defaults to the upstream repo.
The token comes from the job's ``GH_TOKEN``; agent subprocesses never see it.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from . import config

STATUS_MARKER = "<!-- autose:status -->"
TRACE_MARKER = "<!-- autose:trace -->"
WRITE_PERMISSIONS = {"admin", "maintain", "write"}
LABELS = {
    "autose:needs-info": "d4c5f9", "autose:failed": "b60205", "autose:awaiting-human": "0e8a16",
    "autose:review-approved": "0e8a16", "autose:review-changes-requested": "fbca04",
}

T = TypeVar("T")


class GitHubError(RuntimeError):
    pass


def retry_once(fn: Callable[[], T], delay: float = 10.0) -> T:
    """S5 rule (DESIGN §2.1): a failed push / PR call is retried once after 10 s."""
    try:
        return fn()
    except (GitHubError, RuntimeError):
        time.sleep(delay)
        return fn()


@dataclass
class Issue:
    number: int
    title: str
    body: str
    maintainer_comments: list[dict[str, str]]


class GitHub:
    def __init__(self, repo: str = config.DEFAULT_REPO, gh: str = "gh") -> None:
        self.repo = repo
        self.gh = gh
        self._perm_cache: dict[str, str] = {}

    # -- plumbing
    def _run(self, *args: str, input_text: str | None = None) -> str:
        p = subprocess.run([self.gh, *args], capture_output=True, text=True, input=input_text)
        if p.returncode != 0:
            raise GitHubError(f"gh {' '.join(args[:3])} failed: {p.stderr.strip()[:500]}")
        return p.stdout

    def api(self, path: str, *args: str, input_json: Any = None) -> Any:
        extra = ["--input", "-"] if input_json is not None else []
        out = self._run("api", path, *args, *extra,
                        input_text=json.dumps(input_json) if input_json is not None else None)
        return json.loads(out) if out.strip() else None

    def api_paginated(self, path: str) -> list[Any]:
        out = self._run("api", "--paginate", "--slurp", path)
        pages = json.loads(out) if out.strip() else []
        return [item for page in pages for item in page]

    # -- people
    def permission(self, user: str) -> str:
        """Collaborator permission via the collaborators API, not author_association (ADR-010)."""
        if user not in self._perm_cache:
            try:
                data = self.api(f"repos/{self.repo}/collaborators/{user}/permission")
                self._perm_cache[user] = data.get("permission", "none")
            except GitHubError as exc:
                if not _is_404(exc):
                    raise  # fail loudly: an API outage must not look like "no permission" or "trusted"
                self._perm_cache[user] = "none"
        return self._perm_cache[user]

    def can_write(self, user: str) -> bool:
        return self.permission(user) in WRITE_PERMISSIONS

    # -- issues
    def issue(self, number: int) -> Issue:
        data = self.api(f"repos/{self.repo}/issues/{number}")
        comments = self.api_paginated(f"repos/{self.repo}/issues/{number}/comments")
        trusted = [
            {"author": c["user"]["login"], "body": c["body"] or ""} for c in comments
            if c["user"].get("type") != "Bot" and STATUS_MARKER not in (c["body"] or "")
            and self.can_write(c["user"]["login"])
        ]
        return Issue(number, data["title"], data.get("body") or "", trusted)

    def bot_comment(self, number: int, marker: str) -> dict[str, Any] | None:
        """The latest comment carrying ``marker`` that the bot itself wrote (others can copy the marker)."""
        found = None
        for c in self.api_paginated(f"repos/{self.repo}/issues/{number}/comments"):
            if marker in (c.get("body") or "") and c["user"].get("login") == config.BOT_NAME:
                found = c
        return found

    def upsert_comment(self, number: int, marker: str, body: str) -> None:
        """Create or edit in place the bot's one comment carrying ``marker`` (sticky status, DESIGN §5)."""
        body = body[:65000]
        c = self.bot_comment(number, marker)
        if c:
            self.api(f"repos/{self.repo}/issues/comments/{c['id']}", "-X", "PATCH", input_json={"body": body})
        else:
            self.comment(number, body)

    def comment(self, number: int, body: str) -> None:
        self.api(f"repos/{self.repo}/issues/{number}/comments", "-X", "POST", input_json={"body": body[:65000]})

    def ensure_label(self, name: str) -> None:
        try:
            self._run("label", "create", name, "--repo", self.repo, "--force", "--color", LABELS.get(name, "ededed"))
        except GitHubError:
            pass

    def add_label(self, number: int, name: str) -> None:
        self.ensure_label(name)
        self.api(f"repos/{self.repo}/issues/{number}/labels", "-X", "POST", input_json={"labels": [name]})

    def remove_label(self, number: int, name: str) -> None:
        try:
            self._run("api", "-X", "DELETE", f"repos/{self.repo}/issues/{number}/labels/{name}")
        except GitHubError:
            pass  # not present

    def non_bot_commits(self, base: str, branch: str) -> list[str] | None:
        """Commits on ``branch`` (vs ``base``) not authored by the bot; ``None`` if the branch is absent.

        DESIGN §10: never force-push over a person's work on ``autose/issue-<N>``.
        """
        try:
            data = self.api(f"repos/{self.repo}/compare/{base}...{branch}")
        except GitHubError as exc:
            if _is_404(exc):
                return None
            raise
        out = []
        for c in data.get("commits", []):
            login = (c.get("author") or {}).get("login", "")
            email = c["commit"]["author"].get("email", "")
            if login != config.BOT_NAME and email != config.BOT_EMAIL:
                out.append(f"{c['sha'][:8]} by {login or email}")
        return out

    # -- pull requests
    def pr(self, number: int) -> dict[str, Any]:
        out = self._run("pr", "view", str(number), "--repo", self.repo, "--json",
                        "number,state,url,title,body,headRefName,headRefOid,baseRefName,baseRefOid,"
                        "isCrossRepository,author")
        return json.loads(out)

    def find_open_pr(self, branch: str) -> dict[str, Any] | None:
        out = self._run("pr", "list", "--repo", self.repo, "--head", branch, "--state", "open",
                        "--json", "number,url")
        prs = json.loads(out)
        return prs[0] if prs else None

    def create_or_update_pr(self, branch: str, title: str, body: str) -> dict[str, Any]:
        """Open the PR for ``branch`` or update the existing one (ADR-004 restart semantics)."""
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as fh:
            fh.write(body[:65000])
        existing = self.find_open_pr(branch)
        if existing:
            self._run("pr", "edit", str(existing["number"]), "--repo", self.repo, "--title", title,
                      "--body-file", fh.name)
        else:
            self._run("pr", "create", "--repo", self.repo, "--base", config.BASE_BRANCH, "--head", branch,
                      "--title", title, "--body-file", fh.name)
        Path(fh.name).unlink(missing_ok=True)
        found = self.find_open_pr(branch)
        if not found:
            raise GitHubError(f"no open PR for {branch} after create/update")
        return self.pr(found["number"])

    def append_pr_body(self, number: int, text: str) -> None:
        body = self.pr(number).get("body") or ""
        self.api(f"repos/{self.repo}/pulls/{number}", "-X", "PATCH", input_json={"body": (body + text)[:65000]})

    def post_review(self, number: int, body: str, commit_id: str) -> None:
        """Verdict as a COMMENT review; the header carries APPROVE/REQUEST_CHANGES (DESIGN §13)."""
        self.api(f"repos/{self.repo}/pulls/{number}/reviews", "-X", "POST",
                 input_json={"event": "COMMENT", "body": body[:65000], "commit_id": commit_id})

    def set_status(self, sha: str, state: str, context: str, description: str, target_url: str = "") -> None:
        payload = {"state": state, "context": context, "description": description[:140]}
        if target_url:
            payload["target_url"] = target_url
        self.api(f"repos/{self.repo}/statuses/{sha}", "-X", "POST", input_json=payload)


def _is_404(exc: Exception) -> bool:
    return "404" in str(exc) or "Not Found" in str(exc)


def run_url() -> str:
    import os

    server, repo, run_id = (os.environ.get(k, "") for k in ("GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "GITHUB_RUN_ID"))
    return f"{server}/{repo}/actions/runs/{run_id}" if run_id else ""
