"""The single bag of collaborators a node is allowed to touch.

Nodes are pure functions of ``(TurnState, RunContext)``. Everything they may
reach -- config, semantic retriever, warehouse repo, trace sink, checkpoints --
arrives through this dataclass, which is what makes a node trivially testable:
a fake context with two stub objects is enough, no patching of module globals.

The fields are exactly the ones pinned in the implementation contract (2.2);
the optional ones default to ``None`` and the engine is required to work when
they are missing (no checkpoint, no cache, no model).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.config import Settings
from app.core.protocols import Inferencer, LLMProvider, Repo, Retriever, Trace
from app.harness.checkpoint import Checkpoint, SemanticCache


@dataclass
class RunContext:
    """Per-turn dependency container handed to every node and to the engine."""

    settings: Settings
    provider: LLMProvider | None
    retriever: Retriever
    repo: Repo
    trace: Trace
    inferencer: Inferencer | None = None
    checkpoint: Checkpoint | None = None
    cache: SemanticCache | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def get(self, key: str, default: Any = None) -> Any:
        """Convenience read from ``extras`` for ad-hoc wiring during development."""
        return self.extras.get(key, default)
