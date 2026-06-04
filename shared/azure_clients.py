"""
Azure client factories — ingestion pipeline.

Auth:
  - Foundry / OpenAI : AzureCliCredential locally, ManagedIdentity in ACA
  - AI Search        : API key (Contributor access is enough locally)
  - Blob Storage     : AzureCliCredential / ManagedIdentity
  - Service Bus      : connection string locally, ManagedIdentity in ACA

No Document Intelligence — PDF parsing is done natively with pdfplumber + pymupdf.
"""
from __future__ import annotations

import os
from functools import lru_cache

from azure.core.credentials import AzureKeyCredential
from azure.identity import AzureCliCredential, ManagedIdentityCredential
from azure.search.documents import SearchClient
from azure.search.documents.indexes import SearchIndexClient
from azure.storage.blob import BlobServiceClient
from openai import AzureOpenAI

from shared.config import settings


def _credential():
    if os.getenv("RUNNING_IN_AZURE"):
        return ManagedIdentityCredential()
    return AzureCliCredential()


@lru_cache(maxsize=1)
def get_openai_client() -> AzureOpenAI:
    """
    Returns AzureOpenAI client. Tries AIProjectClient.inference first,
    falls back to direct endpoint if inference attr not available.
    """
    import logging, re
    logger = logging.getLogger(__name__)
    endpoint = str(settings.AZURE_FOUNDRY_PROJECT_ENDPOINT)

    try:
        from azure.ai.projects import AIProjectClient
        client = AIProjectClient(endpoint=endpoint, credential=_credential())
        if hasattr(client, "inference"):
            return client.inference.get_azure_openai_client(
                api_version=settings.AZURE_OPENAI_API_VERSION
            )
        logger.warning("azure-ai-projects .inference unavailable — using direct endpoint")
    except Exception as exc:
        logger.warning("AIProjectClient failed: %s", exc)

    base_endpoint = re.sub(r"/api/projects/[^/]+/?$", "/", endpoint)
    if not base_endpoint.endswith("/"):
        base_endpoint += "/"
    cred = _credential()
    return AzureOpenAI(
        azure_endpoint=base_endpoint,
        azure_ad_token_provider=lambda: cred.get_token("https://cognitiveservices.azure.com/.default").token,
        api_version=settings.AZURE_OPENAI_API_VERSION,
    )


@lru_cache(maxsize=1)
def get_blob_service_client() -> BlobServiceClient:
    return BlobServiceClient(
        account_url=f"https://{settings.AZURE_STORAGE_ACCOUNT_NAME}.blob.core.windows.net",
        credential=_credential(),
    )


@lru_cache(maxsize=1)
def get_search_client() -> SearchClient:
    return SearchClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        index_name=settings.AZURE_SEARCH_INDEX,
        credential=AzureKeyCredential(
            settings.AZURE_SEARCH_API_KEY.get_secret_value()
        ),
    )


@lru_cache(maxsize=1)
def get_search_index_client() -> SearchIndexClient:
    return SearchIndexClient(
        endpoint=str(settings.AZURE_SEARCH_ENDPOINT),
        credential=AzureKeyCredential(
            settings.AZURE_SEARCH_API_KEY.get_secret_value()
        ),
    )


def get_service_bus_client():
    """New instance per use — always use as async context manager."""
    from azure.servicebus.aio import ServiceBusClient as AsyncSBClient
    conn_str = (
        settings.AZURE_SERVICE_BUS_CONNECTION_STR.get_secret_value()
        if settings.AZURE_SERVICE_BUS_CONNECTION_STR
        else None
    )
    if conn_str:
        return AsyncSBClient.from_connection_string(conn_str)
    return AsyncSBClient(
        fully_qualified_namespace=settings.AZURE_SERVICE_BUS_NAMESPACE,
        credential=_credential(),
    )
