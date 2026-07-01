"""v2-031: AgentShield-скан — статические правила на injection/secret-leak.

ECC `ecc-agentshield`/`skills/security-scan`: конфиги агента (промпты, правила,
MCP-серверы) — такая же поверхность атаки, как код. `scripts/hooks/guard.sh`
уже блокирует деструктивные команды в рантайме (defense-in-depth), но ничего не
проверяет **до** запуска — секрет, случайно попавший в `prompts/*.md`, или
опасная bash-команда, вписанная в `.cursor/rules/*.mdc` как «пример», утекают
молча. Этот модуль — детерминированная (без LLM) статическая проверка; сверху
опционально `security/red_team.py` (adversarial, Opus, opt-in).

Три категории:
  - secret   — похожие на реальные токены/ключи строки в `prompts/`, `.cursor/`.
  - injection — опасные bash-команды без sandbox в `.cursor/` (те же паттерны,
    что `scripts/hooks/guard.sh` блокирует в рантайме — здесь ловим их ещё на
    этапе конфигурации, до того как агент попытается их выполнить).
  - mcp      — секреты/чрезмерно разрешающие флаги в MCP-конфигах
    (`.cursor/mcp.json` / `mcp.json`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

_SCANNED_SUFFIXES = (".md", ".mdc")

# ── secrets ──────────────────────────────────────────────────────────────────
# Реальные форматы токенов провайдеров — низкий false-positive rate.
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("openai-style", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github-pat", re.compile(r"\bghp_[A-Za-z0-9]{36}\b")),
    ("github-fine-grained-pat", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b")),
    ("aws-access-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z\-_]{35}\b")),
    ("private-key-block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
]

# ── injection: опасные bash-команды без sandbox ────────────────────────────
# Тот же чёрный список, что `scripts/hooks/guard.sh` блокирует в рантайме —
# здесь ловим их раньше, на этапе конфигурации (промпт/правило их упоминает
# как «пример» без предупреждения о sandbox).
_INJECTION_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("rm-rf-root", re.compile(r"rm\s+-rf\s+/(?:\s|$)")),
    ("curl-pipe-shell", re.compile(r"curl\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b")),
    ("wget-pipe-shell", re.compile(r"wget\b[^\n|]*\|\s*(?:sudo\s+)?(?:sh|bash|zsh)\b")),
    ("fork-bomb", re.compile(r":\(\)\s*\{\s*:\|\s*:&?\s*\}\s*;\s*:")),
    ("mkfs", re.compile(r"\bmkfs\b")),
    ("dd-raw-device", re.compile(r"\bdd\s+if=")),
    ("redirect-to-block-device", re.compile(r">\s*/dev/sd[a-z]\b")),
    ("chmod-777-root", re.compile(r"chmod\s+-R\s+777\s+/")),
    ("force-push-main", re.compile(r"git\s+push\b[^\n]*--force")),
]

# ── mcp: чрезмерно разрешающие флаги ────────────────────────────────────────
_MCP_PERMISSIVE_KEYS = re.compile(
    r"^(auto[_-]?approve|disable[_-]?all[_-]?approval|trust[_-]?all|"
    r"skip[_-]?confirmation|always[_-]?allow)$",
    re.IGNORECASE,
)

_SEVERITY_ORDER = {"critical": 3, "warning": 2, "info": 1}


@dataclass
class Finding:
    severity: str    # "critical" | "warning" | "info"
    category: str    # "secret" | "injection" | "mcp"
    path: str
    rule: str
    line: int = 0
    snippet: str = ""


@dataclass
class ScanReport:
    root: Path
    findings: list[Finding] = field(default_factory=list)

    @property
    def critical(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "critical"]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == "warning"]

    @property
    def grade(self) -> str:
        """Буквенная оценка A-F по числу/тяжести находок."""
        n_crit, n_warn = len(self.critical), len(self.warnings)
        if n_crit >= 2:
            return "F"
        if n_crit == 1:
            return "D"
        if n_warn > 3:
            return "C"
        if n_warn > 0:
            return "B"
        return "A"

    def markdown(self) -> str:
        lines = [
            "# AgentShield scan report",
            "",
            f"- **root:** `{self.root}`",
            f"- **grade:** **{self.grade}**",
            f"- **findings:** {len(self.findings)} "
            f"({len(self.critical)} critical, {len(self.warnings)} warning)",
            "",
        ]
        if not self.findings:
            lines.append("✅ Ничего не найдено.")
            return "\n".join(lines)
        by_sev = sorted(
            self.findings, key=lambda f: -_SEVERITY_ORDER.get(f.severity, 0),
        )
        lines.append("| severity | category | rule | path:line | snippet |")
        lines.append("|---|---|---|---|---|")
        for f in by_sev:
            snippet = f.snippet.replace("|", "\\|")[:80]
            lines.append(
                f"| {f.severity} | {f.category} | {f.rule} | `{f.path}:{f.line}` | {snippet} |"
            )
        return "\n".join(lines)


def _iter_text_files(directory: Path) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        p for p in directory.rglob("*")
        if p.is_file() and p.suffix in _SCANNED_SUFFIXES
    )


def _scan_text_for_secrets(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _SECRET_PATTERNS:
            m = pattern.search(line)
            if m:
                findings.append(Finding(
                    severity="critical", category="secret", path=str(path),
                    rule=name, line=lineno, snippet=line.strip(),
                ))
    return findings


def _scan_text_for_injection(path: Path, text: str) -> list[Finding]:
    findings: list[Finding] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in _INJECTION_PATTERNS:
            if pattern.search(line):
                findings.append(Finding(
                    severity="warning", category="injection", path=str(path),
                    rule=name, line=lineno, snippet=line.strip(),
                ))
    return findings


def _scan_mcp_config(path: Path) -> list[Finding]:
    """Секреты в `env` MCP-серверов + чрезмерно разрешающие флаги."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    findings: list[Finding] = []
    _walk_mcp_json(data, path, [], findings)
    return findings


def _walk_mcp_json(
    node: object, path: Path, breadcrumb: list[str], findings: list[Finding],
) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            trail = [*breadcrumb, str(key)]
            if isinstance(value, str):
                for name, pattern in _SECRET_PATTERNS:
                    if pattern.search(value):
                        findings.append(Finding(
                            severity="critical", category="mcp", path=str(path),
                            rule=f"secret-in-{name}", snippet=".".join(trail),
                        ))
                if _MCP_PERMISSIVE_KEYS.match(str(key)) and value not in ("false", "0"):
                    findings.append(Finding(
                        severity="warning", category="mcp", path=str(path),
                        rule="overly-permissive-flag", snippet=".".join(trail),
                    ))
            elif isinstance(value, bool):
                if _MCP_PERMISSIVE_KEYS.match(str(key)) and value:
                    findings.append(Finding(
                        severity="warning", category="mcp", path=str(path),
                        rule="overly-permissive-flag", snippet=".".join(trail),
                    ))
            else:
                _walk_mcp_json(value, path, trail, findings)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk_mcp_json(item, path, [*breadcrumb, f"[{i}]"], findings)


def scan_directory(root: Path) -> ScanReport:
    """Просканировать `prompts/`, `.cursor/` и MCP-конфиги под `root`.

    `root` — дом harness (где лежат его собственные `prompts/`/`.cursor/`), не
    целевой проект: ADR-0005 6.4 закрывает «безопасность конфигов harness».
    """
    findings: list[Finding] = []

    for directory in (root / "prompts", root / ".cursor"):
        for file_path in _iter_text_files(directory):
            text = file_path.read_text(encoding="utf-8", errors="replace")
            findings.extend(_scan_text_for_secrets(file_path, text))
            findings.extend(_scan_text_for_injection(file_path, text))

    for mcp_path in (root / ".cursor" / "mcp.json", root / "mcp.json"):
        if mcp_path.exists():
            findings.extend(_scan_mcp_config(mcp_path))

    return ScanReport(root=root, findings=findings)
