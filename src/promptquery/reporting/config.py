"""Runtime settings for the reporting bot, read from the environment (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].strip()  # allow trailing "  # comment"
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


def _consensus_mode(value: str) -> str:
    v = str(value).strip().lower()
    return {"1": "off", "2": "always", "0": "off"}.get(v, v if v in {"adaptive", "always", "off"} else "adaptive")


@dataclass
class Settings:
    # DSN of the least-privilege `atlas_reporting` role (see `atlas-bot setup-db`).
    reporting_dsn: str = "postgresql://atlas_reporting:reporting_pw@localhost:55433/atlas"
    # Owner/admin DSN, used only by `atlas-bot setup-db`.
    admin_dsn: str | None = None
    # Tenant slug(s) the bot reports on: "acme", "acme,retail" or "all".
    tenant: str = "acme"
    # LLM spec: "anthropic/claude-sonnet-5", "openai/gpt-4.1", "claude-cli/sonnet" or None (auto).
    llm: str | None = None
    # Optional cheaper/faster model for writing the final answer text (defaults to `llm`).
    fast_llm: str | None = None
    max_rows: int = 1000
    max_repair: int = 3
    statement_timeout_ms: int = 20000
    # Fixes "today" for relative dates ("last month"); defaults to the real date.
    today: date | None = None
    # How many past turns the conversation keeps (the challenge requires 6 sequential prompts).
    max_turns: int = 6
    # Confidence gating / self-consistency:
    #   "adaptive" (default): 1 SQL; a 2nd independent SQL only when result validation reports
    #                         LOW confidence; a 3rd breaks ties when they disagree.
    #   "always": 2 SQL candidates in parallel every time.   "off": 1 SQL, no verification.
    consensus_mode: str = "adaptive"
    # Optional bearer token required by the HTTP API. Empty = no auth (localhost dev only).
    api_token: str | None = None
    # Append one JSON object per turn to this file (observability). Empty = disabled.
    audit_log: Path | None = None
    catalog_cache: Path = field(default_factory=lambda: Path(".cache/atlas_catalog.json"))
    write_answer: bool = True

    @classmethod
    def from_env(cls, env_file: str | Path = ".env") -> "Settings":
        _load_dotenv(Path(env_file))
        e = os.environ
        today = e.get("ATLAS_TODAY")
        # Also accept the generic LLM_PROVIDER / LLM_API_KEY / STRONG_MODEL / FAST_MODEL names.
        provider = (e.get("LLM_PROVIDER") or "").strip().lower()
        if provider and e.get("LLM_API_KEY"):
            key_var = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(provider)
            if key_var:
                os.environ.setdefault(key_var, e["LLM_API_KEY"])
        llm = e.get("ATLAS_LLM") or (f"{provider}/{e['STRONG_MODEL']}" if provider and e.get("STRONG_MODEL") else None)
        fast = e.get("ATLAS_FAST_LLM") or (f"{provider}/{e['FAST_MODEL']}" if provider and e.get("FAST_MODEL") else None)
        return cls(
            reporting_dsn=e.get("ATLAS_REPORTING_DSN", cls.reporting_dsn),
            admin_dsn=e.get("ATLAS_ADMIN_DSN"),
            tenant=e.get("ATLAS_TENANT", cls.tenant),
            llm=llm,
            fast_llm=fast,
            max_rows=int(e.get("ATLAS_MAX_ROWS", cls.max_rows)),
            max_repair=int(e.get("ATLAS_MAX_REPAIR", cls.max_repair)),
            statement_timeout_ms=int(e.get("ATLAS_STATEMENT_TIMEOUT_MS", cls.statement_timeout_ms)),
            today=date.fromisoformat(today) if today else None,
            max_turns=int(e.get("ATLAS_MAX_TURNS", cls.max_turns)),
            consensus_mode=_consensus_mode(e.get("ATLAS_CONSENSUS", cls.consensus_mode)),
            api_token=e.get("ATLAS_API_TOKEN") or None,
            audit_log=Path(e["ATLAS_AUDIT_LOG"]) if e.get("ATLAS_AUDIT_LOG") else None,
            catalog_cache=Path(e.get("ATLAS_CATALOG_CACHE", ".cache/atlas_catalog.json")),
            write_answer=e.get("ATLAS_WRITE_ANSWER", "1") not in {"0", "false", "no"},
        )

    def current_date(self) -> date:
        return self.today or date.today()
