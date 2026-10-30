from dataclasses import dataclass, field
from pathlib import Path
import os

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: ROOT / "runtime")
    database_url: str = ""
    provider: str = "demo"
    deepseek_api_key: str = ""
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    embedding_provider: str = "hash"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    max_repo_files: int = 1500
    max_repo_bytes: int = 15_000_000
    max_file_bytes: int = 300_000
    request_timeout: float = 60.0
    max_rounds: int = 3
    max_tool_calls: int = 10
    max_context_chars: int = 22_000
    max_output_tokens: int = 1600
    github_token: str = ""
    allow_local_import: bool = False

    def __post_init__(self):
        self.data_dir = Path(self.data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if not self.database_url:
            self.database_url = "sqlite:///" + (self.data_dir / "repopilot.db").as_posix()
        if self.provider not in {"demo", "deepseek"}:
            raise ValueError("MODEL_PROVIDER must be demo or deepseek")

    @classmethod
    def from_env(cls):
        load_dotenv(ROOT / ".env", override=False)
        return cls(
            data_dir=Path(os.getenv("REPOPILOT_DATA_DIR", str(ROOT / "runtime"))),
            database_url=os.getenv("DATABASE_URL", ""),
            provider=os.getenv("MODEL_PROVIDER", "demo"),
            deepseek_api_key=os.getenv("DEEPSEEK_API_KEY", ""),
            deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            deepseek_model=os.getenv("DEEPSEEK_MODEL", "deepseek-flash"),
            embedding_provider=os.getenv("EMBEDDING_PROVIDER", "hash"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "sentence-transformers/all-MiniLM-L6-v2"),
            embedding_base_url=os.getenv("EMBEDDING_BASE_URL", ""),
            embedding_api_key=os.getenv("EMBEDDING_API_KEY", ""),
            github_token=os.getenv("GITHUB_TOKEN", ""),
            allow_local_import=os.getenv("ALLOW_LOCAL_IMPORT", "false").lower() == "true",
        )
