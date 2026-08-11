"""Skill GC — propose / apply quarantine for unused catalog skills.

Never deletes skills mid-run. Apply requires explicit ``--approve``.
Quarantine sets ``status=quarantined`` in ``skills/catalog.json`` so
``select_skills`` skips them (already filters ``status != active``).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.skills.catalog import CatalogSkill, SkillCatalog, load_catalog
from harness.store.repository import Store

QUARANTINED = "quarantined"
ACTIVE = "active"


@dataclass(frozen=True, slots=True)
class SkillUsageStat:
    id: str
    path: str
    status: str
    invocation: str
    select_hits: int
    mention_hits: int
    in_pack: bool
    propose_quarantine: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GcProposal:
    generated_at: str
    catalog_path: str
    skills: list[SkillUsageStat] = field(default_factory=list)
    quarantine_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "catalog_path": self.catalog_path,
            "quarantine_ids": list(self.quarantine_ids),
            "skills": [s.to_dict() for s in self.skills],
        }


def _pack_ids(catalog: SkillCatalog) -> set[str]:
    out: set[str] = set()
    for ids in catalog.packs.values():
        out.update(ids)
    return out


def _count_select_hits(store: Store, skill: CatalogSkill) -> int:
    """Count dispatches whose skill_paths JSON contains this skill path or id."""
    needles = [n for n in (skill.path, skill.id) if n]
    if not needles:
        return 0
    total = 0
    # skill_paths is a JSON array string; LIKE is a cheap proxy (same as stocktake).
    for needle in needles:
        total += store.count_dispatches_with_skill(needle)
    return total


def _count_mentions(store: Store, skill: CatalogSkill) -> int:
    needle = skill.id
    if not needle:
        return 0
    return store.count_agent_events_matching(needle)


def propose_quarantine(
    catalog: SkillCatalog,
    store: Store,
    *,
    catalog_path: Path,
    max_selects: int = 0,
    include_human_only: bool = False,
    protect_pack_core: bool = True,
) -> GcProposal:
    """Build quarantine proposal from catalog + dispatch/select usage.

    Default: propose skills with ``select_hits <= max_selects`` that are not
    in worker/reviewer/orch core packs (unless ``protect_pack_core=False``).
    Human-only skills are excluded unless ``include_human_only``.
    Already quarantined skills are listed but not re-proposed.
    """
    human_only = set(catalog.packs.get("human_only") or [])
    core_ids: set[str] = set()
    if protect_pack_core:
        for key in ("worker_core", "reviewer_core", "orch_core"):
            core_ids.update(catalog.packs.get(key) or [])
    packed = _pack_ids(catalog)

    skills: list[SkillUsageStat] = []
    quarantine_ids: list[str] = []

    for skill in catalog.skills:
        selects = _count_select_hits(store, skill)
        mentions = _count_mentions(store, skill)
        reasons: list[str] = []
        propose = False

        if skill.status == QUARANTINED:
            reasons.append("already quarantined")
        elif skill.id in human_only and not include_human_only:
            reasons.append("human_only pack (skipped)")
        elif skill.id in core_ids:
            reasons.append("core pack protected")
        elif selects <= max_selects:
            propose = True
            reasons.append(f"select_hits={selects} <= {max_selects}")
            if mentions == 0:
                reasons.append("zero agent_events mentions")
            if skill.id not in packed:
                reasons.append("not in any pack")
        else:
            reasons.append(f"select_hits={selects} above threshold")

        if propose:
            quarantine_ids.append(skill.id)

        skills.append(
            SkillUsageStat(
                id=skill.id,
                path=skill.path,
                status=skill.status,
                invocation=skill.invocation,
                select_hits=selects,
                mention_hits=mentions,
                in_pack=skill.id in packed,
                propose_quarantine=propose,
                reasons=tuple(reasons),
            )
        )

    return GcProposal(
        generated_at=datetime.now(tz=UTC).isoformat(),
        catalog_path=str(catalog_path),
        skills=skills,
        quarantine_ids=quarantine_ids,
    )


def apply_quarantine(
    catalog_path: Path,
    skill_ids: list[str],
    *,
    approve: bool,
    note: str = "",
) -> dict[str, Any]:
    """Set ``status=quarantined`` for given ids. Requires ``approve=True``.

    Does **not** delete files. Never mutates catalog without human flag.
    """
    if not approve:
        return {
            "ok": False,
            "error": "refused: pass --approve to quarantine (never auto mid-run)",
            "would_quarantine": list(skill_ids),
        }
    if not catalog_path.is_file():
        return {"ok": False, "error": f"catalog not found: {catalog_path}"}

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    want = {s.strip() for s in skill_ids if s.strip()}
    if not want:
        return {"ok": False, "error": "no skill ids provided"}

    changed: list[str] = []
    missing: list[str] = []
    found: set[str] = set()
    for raw in data.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or "")
        if sid not in want:
            continue
        found.add(sid)
        if str(raw.get("status") or ACTIVE) != QUARANTINED:
            raw["status"] = QUARANTINED
            raw["quarantined_at"] = datetime.now(tz=UTC).isoformat()
            if note:
                raw["quarantine_note"] = note
            changed.append(sid)

    missing = sorted(want - found)
    data["updated_at"] = datetime.now(tz=UTC).isoformat()
    catalog_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "changed": changed,
        "already_quarantined": sorted(found - set(changed)),
        "missing": missing,
        "catalog": str(catalog_path),
    }


def restore_active(
    catalog_path: Path,
    skill_ids: list[str],
    *,
    approve: bool,
) -> dict[str, Any]:
    """Restore quarantined skills to ``active``. Requires ``--approve``."""
    if not approve:
        return {
            "ok": False,
            "error": "refused: pass --approve to restore",
            "would_restore": list(skill_ids),
        }
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    want = {s.strip() for s in skill_ids if s.strip()}
    changed: list[str] = []
    for raw in data.get("skills") or []:
        if not isinstance(raw, dict):
            continue
        sid = str(raw.get("id") or "")
        if sid in want and str(raw.get("status")) == QUARANTINED:
            raw["status"] = ACTIVE
            raw.pop("quarantined_at", None)
            raw.pop("quarantine_note", None)
            changed.append(sid)
    data["updated_at"] = datetime.now(tz=UTC).isoformat()
    catalog_path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"ok": True, "changed": changed, "catalog": str(catalog_path)}


def render_proposal_markdown(proposal: GcProposal) -> str:
    lines = [
        "# Skill GC proposal",
        "",
        f"- **generated:** {proposal.generated_at}",
        f"- **catalog:** `{proposal.catalog_path}`",
        f"- **propose quarantine:** {len(proposal.quarantine_ids)}",
        "",
        "Apply only with human approval:",
        "",
        "```bash",
        "harness skills gc apply --approve --ids "
        + ",".join(proposal.quarantine_ids or ["<id>"]),
        "```",
        "",
        "| id | status | selects | mentions | propose? | reasons |",
        "|---|---|---|---|---|---|",
    ]
    for s in proposal.skills:
        mark = "yes" if s.propose_quarantine else "no"
        reasons = "; ".join(s.reasons).replace("|", "\\|")
        lines.append(
            f"| `{s.id}` | {s.status} | {s.select_hits} | {s.mention_hits} | "
            f"{mark} | {reasons} |"
        )
    lines.append("")
    lines.append(
        "Never auto-delete mid-run. Quarantine only flips catalog `status`; "
        "skill files stay on disk."
    )
    lines.append("")
    return "\n".join(lines)


def load_catalog_for_gc(path: Path) -> SkillCatalog:
    return load_catalog(path)
