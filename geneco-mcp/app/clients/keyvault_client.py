"""Azure Key Vault secret reader — OAuth2 client-credentials via msal
against the tenant's Key-Vault-scoped Entra app registration.

This is the first hop of the two-hop credential chain: a tenant's stored
Key Vault credentials fetch that tenant's *real* D365 app registration
(``Crm-Client-Id``/``Crm-Client-Secret``) at call time, so the D365 secret
itself is never at rest in this service — only the vault credential is
(see ``store/crm_resolver.py`` for the caching layer above this).

Mirrors ``clients/d365_connector.py``'s honest-error boundary: every
outbound msal/httpx call and response parse is wrapped so a real failure
raises ``KeyVaultError`` rather than leaking an unhandled exception. The
resolver above translates that into the ``ConnectorError`` the tools
already handle, so a vault outage surfaces as a relayable error and never
a raw 500.
"""

from __future__ import annotations

from typing import Any

import httpx

#: Key Vault data-plane API version used for the secret GET.
API_VERSION = "7.4"

#: OAuth2 scope for the Key Vault data plane.
VAULT_SCOPE = "https://vault.azure.net/.default"

#: Vault secret names holding the tenant's real D365 app registration.
#: Hardcoded deliberately — every tenant's vault uses these exact names
#: (matching geneco-poc's ``crm_client.py``); make them per-tenant columns
#: only if a tenant ever needs different ones.
GENECO_CRM_CLIENT_ID_SECRET = "Crm-Client-Id"
GENECO_CRM_CLIENT_SECRET_SECRET = "Crm-Client-Secret"

#: Vault secret names holding the tenant's SSP (cxchat) Basic Auth pair.
#: Fixed for the same reason as the CRM pair above.
GENECO_SSP_USER_ID_SECRET = "Ssp-User-Id"
GENECO_SSP_PASSWORD_SECRET = "Ssp-Password"


class KeyVaultError(Exception):
    """``status_code``/``secret_name`` are carried structurally rather than
    only in the message so callers that need to explain *why* a vault read
    failed (the credential-verify endpoint) can branch on them instead of
    parsing message text."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        secret_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code
        self.secret_name = secret_name


def describe(exc: KeyVaultError) -> str:
    """A vault failure in the vocabulary a tenant admin can act on.

    Used for the human-readable ``detail`` of the credential-verify
    endpoint, so it names secrets and app registrations but never any
    value.
    """
    where = f"secret {exc.secret_name!r}" if exc.secret_name else "the vault"
    if exc.status_code == 403:
        return (
            f"Key Vault denied access to {where} — the registered kv_client_id app "
            "registration needs Get on secrets in that vault (RBAC role "
            "'Key Vault Secrets User', or a Get access policy)."
        )
    if exc.status_code == 401:
        return (
            "Key Vault rejected the registered vault credentials — check kv_client_id, "
            "kv_client_secret and tenant_id."
        )
    if exc.status_code == 404:
        return (
            f"Key Vault has no {where} — create it in that vault (the names are fixed: "
            f"{GENECO_CRM_CLIENT_ID_SECRET} / {GENECO_CRM_CLIENT_SECRET_SECRET})."
        )
    if exc.status_code is not None:
        # The message for an HTTP failure embeds the vault's own response
        # body; never relay that onward — an upstream body is not ours to
        # vouch for, and this string is shown in a tenant-facing portal.
        return f"Key Vault returned {exc.status_code} for {where}."
    # No status code: our own message for a token, network or parse failure
    # (the actionable part is msal's/httpx's, e.g. an AADSTS code).
    return f"Key Vault check failed: {exc}"


class KeyVaultClient:
    """One instance per resolution, built from a tenant's stored vault
    credentials — stateless across calls, no cached HTTP client."""

    def __init__(
        self, *, vault_url: str, tenant_id: str, client_id: str, client_secret: str
    ) -> None:
        self.vault_url = str(vault_url).rstrip("/")
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._msal_app: Any = None

    @property
    def _authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id}"

    def _get_msal_app(self) -> Any:
        if self._msal_app is None:
            import msal

            self._msal_app = msal.ConfidentialClientApplication(
                client_id=self.client_id,
                client_credential=self.client_secret,
                authority=self._authority,
            )
        return self._msal_app

    def _acquire_token(self) -> str:
        try:
            result = self._get_msal_app().acquire_token_for_client(scopes=[VAULT_SCOPE])
        except Exception as exc:  # msal/network failure — never let this leak as a 500
            raise KeyVaultError(f"key vault token acquisition failed: {exc}") from exc
        if "access_token" not in result:
            raise KeyVaultError(
                f"key vault token acquisition failed: {result.get('error_description', result)}",
                retryable=False,
            )
        return result["access_token"]

    async def get_secret(self, name: str) -> str:
        """Read one secret's current value from the vault."""
        url = f"{self.vault_url}/secrets/{name}"
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    url,
                    params={"api-version": API_VERSION},
                    headers={
                        "Authorization": f"Bearer {self._acquire_token()}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise KeyVaultError(f"key vault request failed: {exc}", secret_name=name) from exc
        if resp.status_code >= 400:
            raise KeyVaultError(
                f"key vault request failed {resp.status_code}: {resp.text[:500]}",
                retryable=resp.status_code >= 500,
                status_code=resp.status_code,
                secret_name=name,
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise KeyVaultError(
                "malformed key vault response (not JSON)", secret_name=name
            ) from exc
        value = data.get("value")
        if not value:
            raise KeyVaultError(
                f"key vault secret {name!r} has no value", retryable=False, secret_name=name
            )
        return value

    async def get_crm_credentials(self) -> tuple[str, str]:
        """The tenant's real D365 app registration, as ``(client_id, client_secret)``."""
        client_id = await self.get_secret(GENECO_CRM_CLIENT_ID_SECRET)
        client_secret = await self.get_secret(GENECO_CRM_CLIENT_SECRET_SECRET)
        return client_id, client_secret

    async def get_ssp_credentials(self) -> tuple[str, str]:
        """The tenant's SSP Basic Auth pair, as ``(username, password)``."""
        username = await self.get_secret(GENECO_SSP_USER_ID_SECRET)
        password = await self.get_secret(GENECO_SSP_PASSWORD_SECRET)
        return username, password
