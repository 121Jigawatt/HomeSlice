from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ORCASERVER_")

    config_dir: Path = Path("/config/orcaserver")
    data_dir: Path = Path("/data")
    # Shared with the streamed GUI so both see the same presets.
    orca_datadir: Path = Path("/config/.config/OrcaSlicer")
    frontend_dist: Path | None = None
    secret_key_file: Path | None = None

    cookie_secure: bool = True
    session_ttl_hours: int = 24 * 7
    session_idle_hours: int = 24
    login_max_failures: int = 5
    login_window_s: int = 900
    min_password_length: int = 12

    max_upload_mb: int = 500
    slice_timeout_s: int = 900
    slice_workers: int = 1
    slice_memory_mb: int = 6144

    github_repo: str = "OrcaSlicer/OrcaSlicer"
    github_token: str | None = None
    allow_unverified_updates: bool = False
    update_check_interval_h: int = 6
    keep_versions: int = 3

    enable_api_docs: bool = False

    @property
    def library_dir(self) -> Path:
        return self.data_dir / "library"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def tmp_dir(self) -> Path:
        return self.data_dir / "tmp"

    @property
    def orca_dir(self) -> Path:
        return self.data_dir / "orca"

    @property
    def db_path(self) -> Path:
        return self.config_dir / "orcaserver.db"

    def ensure_dirs(self) -> None:
        for d in (self.config_dir, self.library_dir, self.jobs_dir, self.tmp_dir, self.orca_dir):
            d.mkdir(parents=True, exist_ok=True)
