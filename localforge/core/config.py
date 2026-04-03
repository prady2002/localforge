"""Configuration module for localforge."""

from __future__ import annotations

import enum
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings


class ModelProfile(str, enum.Enum):
    """Model size profile controlling context and retrieval parameters."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass(frozen=True)
class ModelProfileSettings:
    """Derived settings based on the selected model profile."""

    context_window: int
    reasoning_depth: int
    retrieval_limit: int
    chunk_size: int


_PROFILE_MAP: dict[ModelProfile, ModelProfileSettings] = {
    ModelProfile.SMALL: ModelProfileSettings(
        context_window=4096,
        reasoning_depth=2,
        retrieval_limit=5,
        chunk_size=512,
    ),
    ModelProfile.MEDIUM: ModelProfileSettings(
        context_window=8192,
        reasoning_depth=4,
        retrieval_limit=10,
        chunk_size=1024,
    ),
    ModelProfile.LARGE: ModelProfileSettings(
        context_window=32768,
        reasoning_depth=8,
        retrieval_limit=20,
        chunk_size=2048,
    ),
}


def get_model_profile_settings(profile: ModelProfile) -> ModelProfileSettings:
    """Return the ``ModelProfileSettings`` for the given *profile*."""
    return _PROFILE_MAP[profile]


class LocalForgeConfig(BaseSettings):
    """Central configuration for localforge.

    Values are resolved in order:
      1. Environment variables prefixed with ``LOCALFORGE_``
      2. ``.localforge/config.yml`` in the repo root
      3. Defaults defined here
    """

    model_name: str = Field(
        default="qwen2.5-coder:7b",
        description="Ollama model tag to use for generation.",
    )
    ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Base URL of the Ollama HTTP API.",
    )
    max_context_tokens: int = Field(
        default=4096,
        description="Maximum number of tokens to include in the LLM context window.",
    )
    max_iterations: int = Field(
        default=50,
        description="Maximum agent loop iterations before forced stop.",
    )
    repo_path: str = Field(
        default=".",
        description="Path to the repository root.",
    )
    index_db_path: str = Field(
        default=".localforge/index.db",
        description="Path to the SQLite index database.",
    )
    auto_approve: bool = Field(
        default=False,
        description="Automatically approve all proposed patches without user confirmation.",
    )
    dry_run: bool = Field(
        default=False,
        description="When True, show planned patches without writing to disk.",
    )
    log_level: str = Field(
        default="INFO",
        description="Logging verbosity (DEBUG, INFO, WARNING, ERROR, CRITICAL).",
    )
    model_profile: ModelProfile = Field(
        default=ModelProfile.SMALL,
        description="Model size profile (small, medium, large).",
    )

    model_config = {
        "env_prefix": "LOCALFORGE_",
        "env_file": ".env",
        "extra": "ignore",
    }


def load_config(repo_path: str = ".") -> LocalForgeConfig:
    """Load configuration from ``.localforge/config.yml`` if it exists, else defaults.

    Parameters
    ----------
    repo_path:
        Root of the repository to search for the config file.

    Returns
    -------
    LocalForgeConfig
        Fully-resolved configuration instance.
    """
    config_path = Path(repo_path) / ".localforge" / "config.yml"
    overrides: dict[str, Any] = {}

    if config_path.is_file():
        with open(config_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
            if isinstance(raw, dict):
                overrides = raw

    return LocalForgeConfig(**overrides)
