"""Settings, read from the environment and .env."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    # Crustdata
    crustdata_api_key: str = ""
    crustdata_api_version: str = "2025-11-01"
    crustdata_base_url: str = "https://api.crustdata.com"
    crustdata_mode: str = "sim"  # sim | live | record | replay
    crustdata_timeout_s: float = 8.0
    sim_latency: bool = True

    # Claude
    anthropic_api_key: str = ""
    anthropic_workspace_id: str = ""
    vendor_persona: str = "crustdata"
    profile_builder: str = "basic"  # basic | react
    fast_path: bool = True  # A clear name match skips the agent. Off runs the agent on everyone.
    profile_timeout_s: float = 240.0  # One agent run, from when its turn starts. Queuing doesn't count.
    report_queue_max: int = 3  # Signups waiting behind the running agent, before we stop queuing them.
    llm_model: str = "claude-opus-5-5"
    llm_timeout_s: float = 25.0  # The output now includes a starter spec, so it runs longer.

    # Delivery. Both are off by default: nothing leaves the app until someone opts in.
    email_api_key: str = ""
    email_from: str = ""
    email_live: bool = False
    email_allowlist: str = ""
    email_redirect_to: str = ""  # Testing on real profiles: every send goes here instead.
    slack_webhook_url: str = ""
    slack_live: bool = False

    # App
    app_base_url: str = "http://localhost:8000"
    demo_password: str = ""
    db_path: str = str(ROOT / "cache" / "crustflow.sqlite")
    replay_dir: str = str(ROOT / "cache" / "replay")
    pipeline_timeout_s: float = 1100.0  # Room for a full queue of agent runs. Each step has its own limit.
    # Comma-separated emails for the dashboard's demo buttons. Empty uses the simulator's cast.
    demo_signups: str = ""
    matrix_signups: str = ""

    # Budgets, in credits, summed from X-Credits-Used
    budget_initial: float = 1.0
    budget_standard: float = 15.0
    sandbox_pill_limit: int = 3
    global_credit_cap: float = 2000.0

    # Founder watch. Watches commit the account to recurring spend, so on the real API nothing is
    # created until WATCH_LIVE is on. The simulator always creates them.
    watch_founders: bool = True
    watch_live: bool = False
    watch_every_hours: int = 24  # How often Crustdata checks each watch. Price doesn't depend on it.
    watch_poll_s: float = 3600.0  # How often we read new runs. 0 means only the dashboard's button.
    watch_confidence_min: float = 0.85  # Above a name-only match: never watch someone who may be a namesake.
    watch_startup_max_headcount: int = 500
    signal_confidence_min: float = 0.7
    signal_email_cooldown_days: float = 7.0

    # Decision thresholds
    match_confidence_min: float = 0.8
    classify_confidence_min: float = 0.6
    high_fit_min_headcount: int = 10
    high_fit_min_openings: int = 3

    @property
    def uses_simulator(self) -> bool:
        return self.crustdata_mode == "sim"

    @property
    def allowlist(self) -> set[str]:
        return {a.strip().lower() for a in self.email_allowlist.split(",") if a.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
