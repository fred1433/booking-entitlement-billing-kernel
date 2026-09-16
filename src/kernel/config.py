"""Configuration, read from the environment. No secret is committed here."""

from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_DATABASE_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/kernel_dev"


@dataclass(frozen=True)
class Settings:
    database_url: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(database_url=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
