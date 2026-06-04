"""
Ingestion pipeline settings.
No Document Intelligence — PDF parsing uses pdfplumber + pymupdf + LLM.
"""
from __future__ import annotations

from functools import lru_cache

from pydantic import AnyHttpUrl, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Azure AI Foundry ──────────────────────────────────────────────────────
    AZURE_FOUNDRY_PROJECT_ENDPOINT: AnyHttpUrl
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str  = "text-embedding-ada-002"
    AZURE_OPENAI_API_VERSION: str           = "2024-08-01-preview"
    # Light LLM for page cleaning + table serialisation — gpt-4o-mini or phi-3-mini
    AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT: str  = "gpt-4o-mini"

    # ── Azure Blob Storage ────────────────────────────────────────────────────
    AZURE_STORAGE_ACCOUNT_NAME: str
    AZURE_STORAGE_CONTAINER_RAW: str        = "raw-documents"
    AZURE_STORAGE_CONTAINER_PROCESSED: str  = "processed-chunks"

    # ── Azure AI Search ───────────────────────────────────────────────────────
    AZURE_SEARCH_ENDPOINT: AnyHttpUrl
    AZURE_SEARCH_API_KEY: SecretStr
    AZURE_SEARCH_INDEX: str                 = "idx-rag"
    AZURE_SEARCH_SEMANTIC_CONFIG: str       = "rag-semantic-config"

    # ── Azure Service Bus ─────────────────────────────────────────────────────
    AZURE_SERVICE_BUS_CONNECTION_STR: SecretStr | None = None  # local dev
    AZURE_SERVICE_BUS_NAMESPACE: str        = ""               # prod (keyless)
    SB_QUEUE_INGESTION: str                 = "ingestion-tasks"
    SB_QUEUE_PROCESSING: str                = "processing-tasks"
    SB_QUEUE_EMBEDDING: str                 = "embedding-tasks"

    # ── Microsoft Graph / SharePoint ──────────────────────────────────────────
    SHAREPOINT_TENANT_ID: str
    SHAREPOINT_CLIENT_ID: str
    SHAREPOINT_CLIENT_SECRET: SecretStr
    SHAREPOINT_WEBHOOK_SECRET: str          = "changeme"
    SHAREPOINT_DEFAULT_SITE_ID: str         = ""

    # ── Processing tuning ─────────────────────────────────────────────────────
    CHILD_CHUNK_MAX_TOKENS: int             = Field(default=200, ge=50,  le=500)
    PARENT_CHUNK_MAX_TOKENS: int            = Field(default=1000, ge=200, le=3000)
    HEADER_FOOTER_MARGIN_PCT: float         = Field(default=0.07, ge=0.02, le=0.15)

    # ── Observability ─────────────────────────────────────────────────────────
    APPLICATIONINSIGHTS_CONNECTION_STRING: str | None = None
    LOG_LEVEL: str = "INFO"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
