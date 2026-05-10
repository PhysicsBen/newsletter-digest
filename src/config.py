from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # LLM
    gemini_api_key: str = ""
    llm_model: str = "gemini/gemini-3-flash-preview"
    llm_thinking_level: str = "low"
    prompt_version: str = "v1.0"

    # Gmail
    gmail_label: str = "AI Newsletters"
    gmail_credentials_path: str = "credentials.json"
    gmail_token_path: str = "token.json"

    # Database
    database_url: str = "sqlite:///data/newsletter.db"

    # Embeddings
    embedding_model: str = "BAAI/bge-m3"

    # Pipeline
    staleness_days: int = 30
    dedup_similarity_threshold: float = 0.87
    topic_similarity_threshold: float = 0.82
    token_budget: int = 6000


settings = Settings()
