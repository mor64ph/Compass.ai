"""Application settings, loaded from .env (see .env.example)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_prefix="",
        extra="ignore",
    )

    # --- provider selection ---
    # gemini | ollama | anthropic | auto. `auto` picks the first configured one,
    # preferring free and keyless over paid - see app/llm/providers/__init__.py.
    compass_llm_provider: str = "auto"
    compass_effort: str = "high"
    compass_llm_cache: bool = True

    # --- Google Gemini (free tier: aistudio.google.com/apikey) ---
    gemini_api_key: str = ""
    # Verified working with structured output on a free-tier key.
    #
    # Two things learned the hard way, both worth knowing before changing this:
    # `gemini-2.5-flash` still appears in /models but 404s for new accounts
    # ("no longer available to new users") - being listed is not being usable,
    # which is why Settings shows the live list. And the free tier allows only
    # **20 requests per day per model**, so when one is spent, switching model
    # is the fix: each carries its own allowance.
    compass_gemini_model: str = "gemini-3.5-flash"

    # --- Ollama (local, no key) ---
    compass_ollama_host: str = "http://localhost:11434"
    # 3B rather than 7B/8B deliberately: on a 4-core laptop with no usable GPU,
    # anything larger takes long enough per generation to be unusable.
    compass_ollama_model: str = "llama3.2:3b"
    # Compass sends 5-8k tokens of profile + résumé + JD. Ollama's default of
    # 2048 would silently truncate the prompt and produce confident nonsense.
    compass_ollama_num_ctx: int = 8192

    # --- Anthropic ---
    # Left unset, the SDK falls back to ANTHROPIC_AUTH_TOKEN or an `ant auth
    # login` profile, so an empty value here is not necessarily an error.
    anthropic_api_key: str = ""
    compass_model: str = "claude-opus-5"

    # --- app ---
    compass_db_url: str = f"sqlite:///{PROJECT_ROOT / 'compass.db'}"
    compass_host: str = "127.0.0.1"
    compass_port: int = 8000
    compass_secret_key: str = "dev-only-change-me"

    # --- embeddings ---
    compass_embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    # Warm the model on a background thread at startup. Worth turning off if you
    # run with --reload and don't want the load repeated on every code change.
    compass_preload_embeddings: bool = True

    # --- google ---
    compass_google_client_secrets_file: str = "./secrets/google_client_secret.json"
    compass_google_client_id: str = ""
    compass_google_client_secret: str = ""
    compass_google_redirect_uri: str = "http://localhost:8000/google/callback"
    compass_gmail_lookback_days: int = 30

    # --- quality gate ---
    compass_quality_window: int = 5
    compass_templated_threshold: float = 0.85

    @property
    def upload_dir(self) -> Path:
        p = PROJECT_ROOT / "data" / "uploads"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def export_dir(self) -> Path:
        p = PROJECT_ROOT / "data" / "exports"
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def google_client_secrets_path(self) -> Path:
        raw = Path(self.compass_google_client_secrets_file)
        return raw if raw.is_absolute() else (PROJECT_ROOT / raw).resolve()

    def google_configured(self) -> bool:
        """True when either the secrets file or the inline pair is available."""
        if self.compass_google_client_id and self.compass_google_client_secret:
            return True
        return self.google_client_secrets_path.is_file()


@lru_cache
def get_settings() -> Settings:
    return Settings()
