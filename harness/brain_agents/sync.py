"""Sync human ``/home/brain`` → agent layer without mutating the canon."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from harness.brain_agents.cards import BrainCard, write_card

DEFAULT_CANON = Path(os.environ.get("HARNESS_BRAIN_CANON", "/home/brain"))
DEFAULT_AGENT_ROOT = Path(os.environ.get("HARNESS_BRAIN_ROOT", "/home/brain-agents"))

# Progressive sync: key dirs only (not full dump of every attachment).
SYNC_DIRS = (
    "decisions",
    "architecture",
    "incidents",
    "templates",
    "integrations",
)

# Cap cards generated per kind so first sync stays light.
_MAX_CARDS_PER_KIND = 40


@dataclass(slots=True)
class SyncResult:
    agent_root: Path
    canon: Path
    synced_dirs: list[str] = field(default_factory=list)
    cards_written: int = 0
    index_path: Path | None = None
    skipped: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def sync_brain_agents(
    *,
    canon: Path | None = None,
    agent_root: Path | None = None,
    dirs: tuple[str, ...] | None = None,
    max_cards_per_kind: int = _MAX_CARDS_PER_KIND,
) -> SyncResult:
    """Clone selected canon dirs + generate ``cards/`` + ``index.json``.

    Never writes into ``canon``. Creates ``lessons/`` writable by agents.
    """
    src = (canon or DEFAULT_CANON).expanduser().resolve()
    dst = (agent_root or DEFAULT_AGENT_ROOT).expanduser().resolve()
    result = SyncResult(agent_root=dst, canon=src)

    if src == dst:
        result.errors.append("canon and agent_root must differ — refusing sync")
        return result
    if not src.is_dir():
        result.errors.append(f"canon missing: {src}")
        return result
    # Hard guard: never treat human vault as writable target.
    if _is_human_canon(dst):
        result.errors.append(f"refusing to use human canon as agent root: {dst}")
        return result

    dst.mkdir(parents=True, exist_ok=True)
    _write_readme(dst, src)

    selected = dirs or SYNC_DIRS
    for name in selected:
        src_dir = src / name
        if not src_dir.is_dir():
            result.skipped.append(name)
            continue
        dst_dir = dst / name
        _rsync_tree(src_dir, dst_dir)
        result.synced_dirs.append(name)

    # Agent-writable lessons (do not wipe existing agent lessons).
    (dst / "lessons").mkdir(parents=True, exist_ok=True)

    cards_dir = dst / "cards"
    cards_dir.mkdir(parents=True, exist_ok=True)
    cards_meta: list[dict[str, object]] = []
    for kind, rel in (
        ("adr", "decisions"),
        ("architecture", "architecture"),
        ("incident", "incidents"),
        ("template", "templates"),
    ):
        folder = dst / rel
        if not folder.is_dir():
            continue
        count = 0
        for path in sorted(folder.glob("*.md")):
            if path.name.startswith("_") or path.name.lower() == "readme.md":
                continue
            if count >= max_cards_per_kind:
                break
            card = _card_from_markdown(path, kind=kind, agent_root=dst)
            write_card(cards_dir, card)
            cards_meta.append(
                {
                    "id": card.id,
                    "title": card.title,
                    "kind": card.kind,
                    "path": card.path,
                    "tags": card.tags,
                }
            )
            count += 1
            result.cards_written += 1

    index = {
        "schema_version": 1,
        "synced_at": datetime.now(UTC).isoformat(),
        "canon": str(src),
        "agent_root": str(dst),
        "synced_dirs": result.synced_dirs,
        "cards": cards_meta,
        "policy": {
            "human_canon_writable": False,
            "agent_writeback": str(dst / "lessons"),
            "pointers_only_in_contextpack": True,
        },
    }
    index_path = dst / "index.json"
    index_path.write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result.index_path = index_path
    return result


def is_human_canon(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == Path("/home/brain").resolve() or (
        resolved.name == "brain" and resolved.parent == Path("/home")
    )


# Back-compat alias for private callers.
_is_human_canon = is_human_canon


def _rsync_tree(src: Path, dst: Path) -> None:
    """Copy tree replacing destination contents for that dir only."""
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, dirs_exist_ok=False)


def _write_readme(agent_root: Path, canon: Path) -> None:
    text = f"""# brain-agents (agent layer)

Synced from human canon: `{canon}`

**Rules**
- Human `/home/brain` is read-only for agents — never auto-write there.
- Write lessons / cards proposals here (`lessons/`, `cards/`).
- ContextPack injects **pointers** only (paths + titles), not vault dumps.
- Re-sync: `harness brain sync`

Generated by Harness V4 Phase 3.
"""
    (agent_root / "README.md").write_text(text, encoding="utf-8")


def _card_from_markdown(path: Path, *, kind: str, agent_root: Path) -> BrainCard:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    title = path.stem
    summary = ""
    tags: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            break
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("tags:"):
            # YAML-ish: tags: [a, b] or tags: [moc, adr]
            raw = stripped.split(":", 1)[1].strip().strip("[]")
            tags = [t.strip().strip("'\"") for t in raw.split(",") if t.strip()]
            break
    # First non-empty prose line after title as summary.
    seen_title = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("---") or stripped.startswith("created:"):
            continue
        if stripped.startswith("#"):
            seen_title = True
            continue
        if stripped.startswith("tags:") or stripped.startswith("status:"):
            continue
        if seen_title or title:
            summary = stripped[:240]
            break
    return BrainCard(
        id=path.stem,
        title=title,
        kind=kind,
        path=str(path.resolve()),
        summary=summary,
        tags=tags,
        source="canon",
    )
