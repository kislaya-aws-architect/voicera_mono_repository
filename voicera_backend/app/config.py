"""
Configuration management for the application.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
env_path = Path(__file__).parent.parent / ".env"
load_dotenv(dotenv_path=env_path)

class Settings:
    """Application settings loaded from environment variables."""
    
    # MongoDB Configuration
    MONGODB_HOST: str = os.getenv("MONGODB_HOST", "localhost")
    MONGODB_PORT: int = int(os.getenv("MONGODB_PORT", "27017"))
    MONGODB_USER: str = os.getenv("MONGODB_USER", "admin")
    MONGODB_PASSWORD: str = os.getenv("MONGODB_PASSWORD", "admin123")
    MONGODB_DATABASE: str = os.getenv("MONGODB_DATABASE", "voicera")
    MONGODB_AUTH_SOURCE: str = os.getenv("MONGODB_AUTH_SOURCE", "admin")
    
    # Application Configuration
    API_V1_PREFIX: str = "/api/v1"
    PROJECT_NAME: str = "Voicera Backend API"
    VERSION: str = "1.0.0"
    DEBUG: bool = os.getenv("DEBUG", "False").lower() == "true"
    SECRET_KEY: str = os.getenv("SECRET_KEY", "")  # Should be set in .env for production
    
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

    # --- Sajag / Glific webhook (SaveLIFE Foundation WhatsApp integration) ---
    # PROVISIONAL: single shared-secret model for the POC (one partner org, SLF).
    # If VoicERA ever needs to support more than one Glific-backed partner, this should
    # move to the existing Integrations collection pattern used for Vobiz
    # (see app/services/integration_service.py + app/services/vobiz.py) instead of a
    # single global env var.
    SAJAG_GLIFIC_WEBHOOK_SECRET: str = os.getenv("SAJAG_GLIFIC_WEBHOOK_SECRET", "")

    # ai4bharat_stt_server exposes a plain REST POST /transcribe (audio_b64, language_id).
    # Point this at the STT server used for the Sajag pipeline (same instance already
    # running for the telephony flow is fine).
    SAJAG_STT_SERVER_URL: str = os.getenv("SAJAG_STT_SERVER_URL", "http://localhost:8001")

    # Dedicated English->Hindi NMT service (IndicTrans2), separate from the LLM
    # server used for hazard classification — different model, different contract.
    # Added 2026-07-20 after general-purpose chat LLMs (Qwen2.5 3B/7B via Ollama)
    # produced fluent but factually wrong Hindi; IndicTrans2 is purpose-built for
    # translation and verified accurate on the same test sentence.
    SAJAG_TRANSLATION_SERVER_URL: str = os.getenv("SAJAG_TRANSLATION_SERVER_URL", "http://localhost:8004")

    # llm_server runs a vLLM OpenAI-compatible server (Qwen3-8B by default).
    # Used for hazard classification / tagging AND Hindi translation of the
    # transcribed report text (see sajag_pipeline.translate_to_hindi) — same
    # endpoint, two different prompts. If this is down, both steps no-op/log.
    SAJAG_LLM_SERVER_URL: str = os.getenv("SAJAG_LLM_SERVER_URL", "http://localhost:8003")
    SAJAG_LLM_MODEL: str = os.getenv("SAJAG_LLM_MODEL", "Qwen/Qwen3-8B")

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
