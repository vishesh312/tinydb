"""Run state (``state.json``) and the append-only event ledger (``ledger.jsonl``).

DESIGN §5 and §14, ADR-004: state lives in ``$AUTOSE_HOME/issue-<N>/`` inside a job
and travels between jobs as a workflow artifact.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .agent import AgentResult


@dataclass
class State:
    """``state.json`` (DESIGN §14)."""

    issue: int
    branch: str = ""
    base_sha: str = ""
    head_sha: str = ""
    pr: int | None = None
    round: int = 0
    stage: str = "S1"
    attempts: dict[str, int] = field(default_factory=dict)
    design_revisits: int = 0
    send_backs: int = 0
    cost_usd: float = 0.0
    status: str = "running"  # running | stopped | needs-info | awaiting-human | tests-passed | pr-open
    stop_reason: str | None = None
    contested: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> "State":
        data = json.loads(path.read_text())
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2) + "\n")


class Ledger:
    """Appends one JSON line per event (DESIGN §14 ledger.jsonl)."""

    def __init__(self, path: Path, issue: int, run_id: str | None = None) -> None:
        self.path = path
        self.issue = issue
        self.run_id = run_id or os.environ.get("GITHUB_RUN_ID") or time.strftime("local-%Y%m%dT%H%M%S")

    def event(self, *, stage: str, event: str, round: int = 0, attempt: int = 0, actor: str = "orchestrator",
              passed: bool | None = None, reason: str = "", result: AgentResult | None = None,
              seconds: float = 0.0) -> dict[str, Any]:
        rec: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "run_id": self.run_id, "issue": self.issue,
            "round": round, "stage": stage, "attempt": attempt, "actor": actor,
            "model": result.model if result else None, "event": event, "passed": passed, "reason": reason,
            "tokens_in": result.tokens_in if result else 0, "tokens_out": result.tokens_out if result else 0,
            "cache_read": result.cache_read if result else 0, "cache_write": result.cache_creation if result else 0,
            "cost_usd": round_cost(result.cost_usd) if result else 0.0,
            "seconds": round_cost(result.seconds if result else seconds),
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec) + "\n")
        return rec

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]


def round_cost(x: float) -> float:
    return round(float(x), 4)


def ledger_cost(path: Path) -> float:
    """Total ``cost_usd`` in a ledger file (used to carry reviewer spend into rework)."""
    if not path.exists():
        return 0.0
    return sum(json.loads(line).get("cost_usd", 0.0) for line in path.read_text().splitlines() if line.strip())
