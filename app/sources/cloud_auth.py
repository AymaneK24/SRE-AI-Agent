"""Sign-in for managed Prometheus services on AWS, Azure, and Google."""
from __future__ import annotations

import base64
import json
import time
from typing import Any, AsyncGenerator

import httpx


class AwsSigV4Auth(httpx.Auth):
    """Signs requests for Amazon Managed Service for Prometheus."""

    requires_request_body = True

    def __init__(self, region: str, access_key: str = "", secret_key: str = "", session_token: str = "", service: str = "aps") -> None:
        import boto3

        self.region = region
        self.service = service
        session = boto3.Session(
            aws_access_key_id=access_key or None, aws_secret_access_key=secret_key or None,
            aws_session_token=session_token or None, region_name=region,
        )
        self.credentials = session.get_credentials()

    def auth_flow(self, request: httpx.Request):
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        if self.credentials is None:
            raise httpx.TransportError("No AWS credentials were found.")
        signed = AWSRequest(method=request.method, url=str(request.url), data=request.content,
                            headers={"Host": request.url.host, "Content-Type": request.headers.get("Content-Type", "")})
        SigV4Auth(self.credentials.get_frozen_credentials(), self.service, self.region).add_auth(signed)
        for key, value in signed.headers.items():
            request.headers[key] = value
        yield request


class _TokenAuth(httpx.Auth):
    def __init__(self) -> None:
        self._token = ""
        self._expires = 0.0

    async def _fetch(self) -> tuple[str, float]:
        raise NotImplementedError

    async def token(self) -> str:
        if time.time() >= self._expires - 60:
            self._token, lifetime = await self._fetch()
            self._expires = time.time() + lifetime
        return self._token

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, Any]:
        request.headers["Authorization"] = f"Bearer {await self.token()}"
        yield request


class AzureClientAuth(_TokenAuth):
    """Microsoft Entra client-credentials sign-in, used for Azure Monitor and managed Prometheus."""

    def __init__(self, tenant_id: str, client_id: str, client_secret: str, scope: str, login_url: str = "https://login.microsoftonline.com") -> None:
        super().__init__()
        self.tenant_id, self.client_id, self.client_secret = tenant_id, client_id, client_secret
        self.scope = scope
        self.login_url = login_url.rstrip("/")

    async def _fetch(self) -> tuple[str, float]:
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(f"{self.login_url}/{self.tenant_id}/oauth2/v2.0/token", data={
                "grant_type": "client_credentials", "client_id": self.client_id,
                "client_secret": self.client_secret, "scope": self.scope,
            })
        if response.status_code != 200:
            raise httpx.TransportError("Microsoft Entra rejected these credentials. Check the tenant, client ID, and secret.")
        data = response.json()
        return data["access_token"], float(data.get("expires_in", 3600))


class GoogleServiceAccountAuth(_TokenAuth):
    """Google service-account sign-in, used for Google Managed Service for Prometheus."""

    def __init__(self, service_account_json: str, scope: str = "https://www.googleapis.com/auth/monitoring.read") -> None:
        super().__init__()
        self.account = json.loads(service_account_json)
        self.scope = scope

    async def _fetch(self) -> tuple[str, float]:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding

        now = int(time.time())
        token_uri = self.account.get("token_uri", "https://oauth2.googleapis.com/token")

        def encode(value: dict[str, Any]) -> bytes:
            return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")

        body = encode({"alg": "RS256", "typ": "JWT"}) + b"." + encode({
            "iss": self.account["client_email"], "scope": self.scope, "aud": token_uri, "iat": now, "exp": now + 3600,
        })
        key = serialization.load_pem_private_key(self.account["private_key"].encode(), password=None)
        signature = key.sign(body, padding.PKCS1v15(), hashes.SHA256())
        assertion = body + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
        async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
            response = await client.post(token_uri, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion.decode(),
            })
        if response.status_code != 200:
            raise httpx.TransportError("Google rejected this service account.")
        data = response.json()
        return data["access_token"], float(data.get("expires_in", 3600))
