from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # LLM
    gemini_api_key: str = ""
    llm_model: str = "gemini/gemini-3.1-flash-lite"
    llm_thinking_level: str = "low"  # Gemini only; ignored for other providers
    llm_temperature: float | None = None  # None = use provider default
    ollama_base_url: str = "http://localhost:11434"  # Only used when llm_model starts with ollama/
    prompt_version: str = "v1.0"

    # Gmail
    gmail_label: str = "AI Newsletters"
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"
    digest_recipient_email: str = ""  # Where to send digests; set in .env

    # Database
    database_url: str = "sqlite:///data/newsletter.db"

    # Embeddings
    embedding_model: str = "BAAI/bge-small-en-v1.5"

    # Pipeline
    staleness_days: int = 30
    dedup_similarity_threshold: float = 0.87
    topic_similarity_threshold: float = 0.82
    digest_significance_min_score: float = 5.0  # topics below this are excluded from the digest
    token_budget: int = 6000
    llm_concurrency: int = 10  # parallel LLM workers (Gemini); use 1 for Ollama
    llm_retries: int = 5  # retries on transient errors (503, 429) with exponential backoff


settings = Settings()
