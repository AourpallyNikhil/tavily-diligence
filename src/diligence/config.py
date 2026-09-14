"""Runtime configuration.

The inference provider sits behind an OpenAI-compatible base URL. This project
runs on OpenRouter by default; pointing it at Nebius Token Factory (or any other
OpenAI-compatible endpoint) is an environment change, not a code change.

Model tiering is a deliberate cost decision. The verifier runs once per claim and
is the highest-volume call in the system, but its job is narrow -- decide whether
one passage entails one sentence -- so it runs on a small cheap model. The writer
runs once per dimension and does the harder synthesis work.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_WRITER = "google/gemini-2.5-flash"
DEFAULT_VERIFIER = "qwen/qwen3-30b-a3b-instruct-2507"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    tavily_api_key: str
    llm_api_key: str
    base_url: str
    writer_model: str
    verifier_model: str
    max_results_per_query: int = 5

    @classmethod
    def from_env(cls) -> "Config":
        tavily = os.getenv("TAVILY_API_KEY")
        if not tavily:
            raise ConfigError(
                "Missing TAVILY_API_KEY. Create one at https://app.tavily.com and put it in .env"
            )

        # Accept either an OpenRouter key or a Nebius key, whichever is present.
        llm = os.getenv("OPENROUTER_API_KEY") or os.getenv("NEBIUS_API_KEY")
        if not llm:
            raise ConfigError(
                "Missing OPENROUTER_API_KEY (or NEBIUS_API_KEY). See README for setup."
            )

        base_url = os.getenv("LLM_BASE_URL") or (
            DEFAULT_BASE_URL
            if os.getenv("OPENROUTER_API_KEY")
            else "https://api.studio.nebius.com/v1"
        )

        return cls(
            tavily_api_key=tavily,
            llm_api_key=llm,
            base_url=base_url,
            writer_model=os.getenv("WRITER_MODEL", DEFAULT_WRITER),
            verifier_model=os.getenv("VERIFIER_MODEL", DEFAULT_VERIFIER),
            max_results_per_query=int(os.getenv("MAX_RESULTS", "5")),
        )
