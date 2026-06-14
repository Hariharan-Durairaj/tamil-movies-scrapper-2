"""Environment configuration (infrastructure only).

Runtime-tunable settings (URLs, thresholds, schedule, API keys) live in the
DB `settings` table — see db/settings_store.py — so they can be changed from
the web UI without redeploying.
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    db_host: str = "localhost"
    db_port: int = 5432
    db_name: str = "movie_automator"
    db_user: str = "movie_user"
    db_password: str = "movie_password"
    db_url: str = ""        # optional full override, e.g. sqlite:///dev.db

    port: int = 8585
    data_dir: Path = Path("./data")

    @property
    def database_url(self) -> str:
        if self.db_url:
            return self.db_url
        return (
            f"postgresql+psycopg2://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def posters_dir(self) -> Path:
        return self.data_dir / "posters"

    @property
    def torrents_dir(self) -> Path:
        return self.data_dir / "torr