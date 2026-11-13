"""Configuration.

Read once, at the process entry point, never scattered through the codebase.
Defaults are chosen so that a fresh clone runs end to end with no environment
at all: the LLM provider falls back to the offline reasoner and the store
falls back to a file-backed SQLite database.

A ``.env`` file at the repository root is read before anything else, with real
environment variables always winning over it. There is no dependency for this
-- it is thirty lines -- because the alternative is telling a new contributor
to export four variables in every shell they open, which they will get wrong
once and then debug for twenty minutes.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Read ``.env`` into the process environment. Never overwrites a real one.

    Deliberately small: ``KEY=value``, ``#`` comments, optional ``export``
    prefix, optional matching quotes. Anything more elaborate belongs in a
    real configuration system rather than in a file people paste secrets into.
    """
    target = path or (REPO_ROOT / ".env")
    loaded: dict[str, str] = {}
    try:
        if not target.is_file():
            return loaded
        for raw in target.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not name:
                continue
            loaded[name] = value
            # A variable exported in the shell beats the file, so a one-off
            # override does not require editing (and later committing) .env.
            os.environ.setdefault(name, value)
    except OSError:
        return loaded
    return loaded


def _env(name: str, default: str) -> str:
    return os.environ.get("DS_" + name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class DetectionSettings:
    """Conformal calibration knobs. See docs/04-detection-ml.md section 4."""

    # Target false-positive rate per asset tier. Higher-value assets get more
    # sensitivity because the cost of missing a tier-1 regression dwarfs the
    # cost of one extra look from a human.
    alpha_by_tier: dict[int, float] = field(
        default_factory=lambda: {1: 0.010, 2: 0.005, 3: 0.002, 4: 0.002}
    )
    calibration_window: int = 200
    calibration_half_life_days: float = 14.0
    min_history_cycles: int = 30
    consecutive_breaches: int = 2
    baseline_cycles: int = 60

    # A score must clear the conformal threshold by this factor to count as a
    # breach. Pure hysteresis: because a breaching point is withheld from the
    # calibration set, a series that grazes its threshold would otherwise sit
    # there breaching indefinitely. Requiring a margin means noise near the
    # boundary settles and a real regression -- which is never marginal --
    # still fires immediately.
    breach_margin: float = 1.15

    # The consecutive-breach rule is per signal, because the signals differ in
    # specificity. A schema hash change is a fact and fires on sight. A
    # multivariate isolation score says only "this combination is unusual",
    # which is true of plenty of legitimate days, so it has to hold for
    # longer before it is worth a human's attention.
    breaches_by_signal: dict[str, int] = field(
        default_factory=lambda: {
            "SCHEMA": 1,
            "MULTIVARIATE": 4,
            "NEW_CATEGORY": 3,
            "FRESHNESS": 2,
        }
    )

    # Practical-significance floors, in each signal's own nonconformity units.
    # An anomaly needs BOTH a conformal breach and a movement large enough to
    # matter, and this is the second bar.
    #
    # These are hand-set per signal, which is uncomfortable: it is the last
    # piece of per-signal tuning in a system whose central claim is that
    # conformal calibration removes the need for it. Replacing them with a
    # figure derived from each cohort's own score distribution was tried three
    # ways and all three measured worse -- see DetectorEnsemble.floor_mode and
    # tools/design/floor_sweep.py. They are kept because they win, and the
    # discomfort is recorded rather than resolved.
    #
    # The reason the derivation failed is worth knowing before trying again: a
    # quantile of the cohort's scores is not independent of a threshold that is
    # also a quantile of those scores. A floor that asks a genuinely different
    # question has to be expressed in the column's own units, not in its rank.
    min_effect_by_signal: dict[str, float] = field(
        default_factory=lambda: {
            "VOLUME": 2.0,
            "FRESHNESS": 3.0,
            "NULL_RATE": 2.0,
            "REFERENTIAL": 2.0,
            "CARDINALITY": 2.5,
            "NUMERIC_DRIFT": 0.6,
            "STRING_LENGTH": 0.6,
            "CATEGORICAL_DRIFT": 1.5,
            "NEW_CATEGORY": 2.0,
            "MULTIVARIATE": 3.0,
            "SCHEMA": 0.0,
        }
    )


@dataclass(frozen=True)
class CorrelationSettings:
    window_minutes: int = 15
    link_minutes: int = 10
    max_lineage_distance: int = 3
    dedup_hours: int = 24

    # Alert-fatigue control (docs/01 US-7). A single anomaly on a single
    # column, with nothing else in its lineage neighbourhood moving, is an
    # observation rather than an incident. Real regressions corroborate: they
    # persist across cycles or they move more than one signal. The exceptions
    # below are facts rather than statistics, and fire alone.
    min_cluster_size: int = 2
    always_raise_signals: tuple[str, ...] = ("SCHEMA",)
    always_raise_tier: int = 1


@dataclass(frozen=True)
class AgentBudget:
    """Hard caps from docs/05 section 6. A breach terminates the run."""

    max_transitions: int = 12
    max_tool_calls: int = 25
    max_usd: float = 0.50
    max_wall_seconds: float = 180.0
    max_hypothesis_rounds: int = 2


@dataclass(frozen=True)
class Settings:
    env: str = field(default_factory=lambda: _env("ENV", "local"))
    data_dir: Path = field(default_factory=lambda: Path(_env("DATA_DIR", str(REPO_ROOT / "var"))))
    db_path: Path = field(
        default_factory=lambda: Path(_env("DB_PATH", str(REPO_ROOT / "var" / "datasentry.db")))
    )
    warehouse_path: Path = field(
        default_factory=lambda: Path(_env("WAREHOUSE_PATH", str(REPO_ROOT / "var" / "warehouse.db")))
    )
    profile_salt: str = field(default_factory=lambda: _env("PROFILE_SALT", "datasentry-local-salt"))
    profiler_version: str = "profiler/1.2.0"
    detector_version: str = "detectors/1.4.0"
    policy_version: str = "policy/2026.08.1"

    # Reasoning.
    #
    # `auto` resolves groq -> gemini -> heuristic, free tiers
    # first, so one environment variable is the whole setup and a clone with
    # none of them still runs the full loop deterministically.
    llm_provider: str = field(default_factory=lambda: _env("LLM_PROVIDER", "auto"))

    # Empty means "whatever this provider's default pair is", which is what
    # you want when switching provider -- you should not have to know Groq's
    # model ids to try Groq. Setting either one pins it across providers.
    llm_cheap_model: str = field(default_factory=lambda: _env("LLM_CHEAP_MODEL", ""))
    llm_frontier_model: str = field(default_factory=lambda: _env("LLM_FRONTIER_MODEL", ""))

    groq_api_key: str = field(default_factory=lambda: os.environ.get("GROQ_API_KEY", ""))
    gemini_api_key: str = field(
        default_factory=lambda: os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )

    # Serving.
    #
    # PaaS platforms (Render, Railway, Fly, Cloud Run) inject a bare `PORT`
    # and expect the process to bind 0.0.0.0. DS_PORT still wins locally, so
    # the same image runs unchanged in both places.
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1"))
    port: int = field(
        default_factory=lambda: _env_int("PORT", int(os.environ.get("PORT", "8787") or 8787))
    )

    # Comma-separated origins allowed to call the API cross-site. Empty means
    # same-origin only, which is the deployed shape: the console is served by
    # this process. A split deployment sets DS_CORS_ORIGINS to the web origin.
    cors_origins: str = field(default_factory=lambda: _env("CORS_ORIGINS", ""))

    seed: int = field(default_factory=lambda: _env_int("SEED", 20260829))
    log_json: bool = field(default_factory=lambda: _env_bool("LOG_JSON", False))

    detection: DetectionSettings = field(default_factory=DetectionSettings)
    correlation: CorrelationSettings = field(default_factory=CorrelationSettings)
    budget: AgentBudget = field(default_factory=AgentBudget)

    def alpha_for_tier(self, tier: int) -> float:
        return self.detection.alpha_by_tier.get(tier, 0.002)

    def breaches_for(self, signal: str) -> int:
        return self.detection.breaches_by_signal.get(
            signal, self.detection.consecutive_breaches
        )

    def min_effect_for(self, signal: str) -> float:
        return self.detection.min_effect_by_signal.get(signal, 0.0)

    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


_SETTINGS: Settings | None = None


def settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        load_dotenv()
        _SETTINGS = Settings()
        _SETTINGS.ensure_dirs()
    return _SETTINGS


def reset_settings() -> None:
    """Test hook: drop the memoised instance so env changes take effect."""
    global _SETTINGS
    _SETTINGS = None
