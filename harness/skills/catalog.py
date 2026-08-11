"""Curated skill catalog (Phase 0.5 foundation).

Deterministic select of 1–3 skill paths for a dispatch. Mid-run remote install
is forbidden by policy; catalog is local JSON only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CatalogSkill:
    id: str
    path: str
    invocation: str
    roles: tuple[str, ...]
    triggers: tuple[str, ...]
    status: str = "active"
    max_body_lines: int | None = None


@dataclass(frozen=True)
class SkillCatalog:
    version: int
    updated_at: str
    policy: dict[str, Any]
    packs: dict[str, list[str]]
    skills: tuple[CatalogSkill, ...]

    @property
    def max_inject(self) -> int:
        raw = self.policy.get("max_inject_per_dispatch", 3)
        try:
            return max(1, int(raw))
        except (TypeError, ValueError):
            return 3


def default_catalog_path(harness_root: Path) -> Path:
    return harness_root / "skills" / "catalog.json"


def load_catalog(path: Path) -> SkillCatalog:
    data = json.loads(path.read_text(encoding="utf-8"))
    skills: list[CatalogSkill] = []
    for raw in data.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        skills.append(
            CatalogSkill(
                id=str(raw["id"]),
                path=str(raw.get("path") or ""),
                invocation=str(raw.get("invocation") or "model"),
                roles=tuple(str(r) for r in (raw.get("roles") or [])),
                triggers=tuple(str(t) for t in (raw.get("triggers") or [])),
                status=str(raw.get("status") or "active"),
                max_body_lines=(
                    int(raw["max_body_lines"])
                    if raw.get("max_body_lines") is not None
                    else None
                ),
            )
        )
    return SkillCatalog(
        version=int(data.get("version") or 1),
        updated_at=str(data.get("updated_at") or ""),
        policy=dict(data.get("policy") or {}),
        packs={
            str(k): [str(x) for x in (v or [])]
            for k, v in (data.get("packs") or {}).items()
        },
        skills=tuple(skills),
    )


def _by_id(catalog: SkillCatalog) -> dict[str, CatalogSkill]:
    return {s.id: s for s in catalog.skills}


def select_skills(
    catalog: SkillCatalog,
    *,
    role: str,
    stage: str = "",
    gate_fail: bool = False,
    limit: int | None = None,
) -> list[str]:
    """Deterministic 1..N skill *paths* for injection (human_only excluded).

    Ranking: pack membership for role → trigger match on stage → id order.
    """
    cap = limit if limit is not None else catalog.max_inject
    role_n = role.strip().lower()
    stage_n = stage.strip().lower()
    pack_key = {
        "worker": "worker_core",
        "reviewer": "reviewer_core",
        "orchestrator": "orch_core",
        "planner": "orch_core",
        "sub-orchestrator": "orch_core",
        "sub_orchestrator": "orch_core",
    }.get(role_n, "worker_core")
    pack_ids = list(catalog.packs.get(pack_key) or [])
    human_only = set(catalog.packs.get("human_only") or [])
    index = _by_id(catalog)

    candidates: list[tuple[int, str, CatalogSkill]] = []
    for sid in pack_ids:
        if sid in human_only:
            continue
        skill = index.get(sid)
        if skill is None or skill.status != "active":
            continue
        if skill.invocation == "human":
            continue
        if role_n and skill.roles and role_n not in {r.lower() for r in skill.roles}:
            # orch pack skills may list roles=["orch"]; allow pack override
            if sid not in pack_ids:
                continue
        score = 0
        if stage_n and any(stage_n in t.lower() or t.lower() in stage_n for t in skill.triggers):
            score += 10
        if gate_fail and any(
            t.lower() in {"debug", "fix", "systematic-debugging"} for t in skill.triggers
        ):
            score += 5
        if gate_fail and skill.id in {"systematic-debugging", "anti-hallucination"}:
            score += 3
        candidates.append((score, skill.id, skill))

    candidates.sort(key=lambda x: (-x[0], x[1]))
    out: list[str] = []
    for _score, _sid, skill in candidates:
        if not skill.path:
            continue
        if skill.path not in out:
            out.append(skill.path)
        if len(out) >= cap:
            break
    return out


def role_for_dispatch_kind(kind: str) -> str:
    """Map TaskTool dispatch kind → catalog role key."""
    mapping = {
        "worker": "worker",
        "reviewer": "reviewer",
        "goal-judge": "reviewer",
        "sub-orchestrator": "orchestrator",
        "sprint-contract": "orchestrator",
        "orchestrator": "orchestrator",
        "planner": "orchestrator",
        "gate": "worker",
    }
    return mapping.get(kind.strip().lower(), "worker")


def select_for_dispatch(
    harness_root: Path,
    *,
    kind: str,
    stage: str = "",
    gate_fail: bool = False,
    catalog_path: Path | None = None,
) -> list[str]:
    """Load catalog from harness root and select 1–3 paths for a dispatch."""
    path = catalog_path or default_catalog_path(harness_root)
    if not path.is_file():
        return []
    catalog = load_catalog(path)
    return select_skills(
        catalog,
        role=role_for_dispatch_kind(kind),
        stage=stage,
        gate_fail=gate_fail,
    )
