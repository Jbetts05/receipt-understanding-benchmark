"""Authentication helpers shared by Azure provider clients."""

from __future__ import annotations

from collections.abc import Generator
from threading import Lock

import httpx
from azure.core.credentials import TokenCredential
from azure.identity import get_bearer_token_provider

AZURE_AI_FOUNDRY_SCOPE = "https://ai.azure.com/.default"
AZURE_COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


class RefreshingBearerTokenAuth(httpx.Auth):
    """Attach a cached, automatically refreshed Entra bearer token to each request."""

    def __init__(self, credential: TokenCredential, scope: str) -> None:
        self._token_provider = get_bearer_token_provider(credential, scope)
        self._provider_lock = Lock()

    def auth_flow(
        self, request: httpx.Request
    ) -> Generator[httpx.Request, httpx.Response, None]:
        # The Azure token provider owns expiry-aware caching. Serializing access
        # prevents concurrent requests from racing a refresh on a shared client.
        with self._provider_lock:
            token = self._token_provider()
        if not token:
            raise RuntimeError("Azure credential returned an empty bearer token")
        request.headers["Authorization"] = f"Bearer {token}"
        yield request
