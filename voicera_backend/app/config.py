"""
Configuration management for the application.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)


def _require_env(name: str) -> str:
    """
    Read a required environment variable and fail fast with a clear error
    if it is missing, instead of silently falling back to an insecure
    default (see hardening/phase-0-critical-fixes, SEC-04).
    """
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {name}. "
            f"Set it in voicera_backend/.env (see env.example) before starting the app."
        )
    return value


class Settings:
    """Application settings loaded from environment variables."""
    
    # MongoDB Configuration
    # NOTE: user/password/database intentionally have no insecure defaults -
    # this app must not be able to silently connect with well-known
    # admin/admin123-style credentials.
    MONGODB_HOST: str = os.getenv("MONGODB_HOST", "localhost")
    MONGODB_PORT: int = int(os.getenv("MONGODB_PORT", "27017"))
    MONGODB_USER: str = _require_env("MONGODB_USER")
    MONGODB_PASSWORD: str = _require_env("MONGODB_PASSWORD")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "voicera")
    MONGODB_AUTH_SOURCE: str = os.getenv("MONGODB_AUTH_SOURCE", "admin")
    
    # Application Configuration
    API_V1_PREFIX: str = "/api/v1"
    PROJECT_NAME: str = "Voicera Backend API"
    VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"
    # Required - see app/auth.py for why an unset/auto-generated SECRET_KEY
    # is unsafe for anything beyond a single, never-restarted process.
    SECRET_KEY: str = _require_env("SECRET_KEY")
    
    # Mailtrap Configuration
    MAILTRAP_API_TOKEN: str = os.getenv("MAILTRAP_API_TOKEN", "")
    MAILTRAP_FROM_EMAIL: str = os.getenv("MAILTRAP_FROM_EMAIL", "noreply@voicera.com")
    MAILTRAP_FROM_NAME: str = os.getenv("MAILTRAP_FROM_NAME", "Voicera")
    FRONTEND_URL: str = os.getenv("FRONTEND_URL", "http://localhost:3000")  # For reset password link
    
    # Internal API Key for service-to-service communication (bot -> backend)
    INTERNAL_API_KEY: str = os.getenv("INTERNAL_API_KEY", "")
    
    # RAG / Knowledge base — Chroma persistence (default: voicera_backend/rag_system/chroma_data)
    CHROMA_BASE_DIR: str = os.getenv(
        "CHROMA_BASE_DIR",
        str(Path(__file__).resolve().parent.parent / "rag_system" / "chroma_data"),
    )

    # Vobiz API Configuration
    VOBIZ_API_BASE_URL: str = os.getenv("VOBIZ_API_BASE_URL", "https://api.vobiz.ai/api/v1")
    VOBIZ_ACCOUNT_ID: str = os.getenv("VOBIZ_ACCOUNT_ID", "")
    VOBIZ_AUTH_ID: str = os.getenv("VOBIZ_AUTH_ID", "")
    VOBIZ_AUTH_TOKEN: str = os.getenv("VOBIZ_AUTH_TOKEN", "")
    PLIVO_API_BASE_URL: str = os.getenv("PLIVO_API_BASE_URL", "https://api.plivo.com/v1")
    VOICE_SERVER_URL: str = (
        os.getenv("VOICE_SERVER_URL")
        or os.getenv("JOHNAIC_SERVER_URL")
        or "http://localhost:7860"
    )
    BATCH_SCHEDULER_POLL_SECONDS: int = int(os.getenv("BATCH_SCHEDULER_POLL_SECONDS", "5"))
    
    @property
    def mongodb_uri(self) -> str:
        """Build MongoDB connection URI."""
        return (
            f"mongodb://{self.MONGODB_USER}:{self.MONGODB_PASSWORD}"
            f"@{self.MONGODB_HOST}:{self.MONGODB_PORT}/{self.MONGODB_DATABASE}"
            f"?authSource={self.MONGODB_AUTH_SOURCE}"
        )

# Global settings instance
settings = Settings()
