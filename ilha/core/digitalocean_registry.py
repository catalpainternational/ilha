"""DigitalOcean Container Registry API client (no doctl dependency)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional
from urllib.parse import quote

import requests

from ilha.exceptions import ilhaCommandError

API_BASE_URL = "https://api.digitalocean.com/v2"


class DigitalOceanRegistryClient:
    """Minimal DOCR client for image list/push/destroy workflows."""

    def __init__(self, api_token: str):
        self.api_token = api_token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        ok_statuses: Optional[set[int]] = None,
    ) -> requests.Response:
        url = f"{API_BASE_URL}{endpoint}"
        try:
            response = self.session.request(method, url, params=params, timeout=30)
        except requests.exceptions.RequestException as exc:
            raise ilhaCommandError(f"DigitalOcean registry API request failed: {exc}") from exc

        allowed = ok_statuses or {200, 201, 204}
        if response.status_code not in allowed:
            detail = (response.text or "").strip() or response.reason
            if response.status_code == 401:
                raise ilhaCommandError("DigitalOcean API authentication failed. Check your API token.")
            raise ilhaCommandError(
                f"DigitalOcean registry API error ({response.status_code}): {detail[:500]}"
            )
        return response

    def _collect_paginated(self, endpoint: str, key: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page = 1
        per_page = 200
        while True:
            sep = "&" if "?" in endpoint else "?"
            response = self._request(
                "GET",
                f"{endpoint}{sep}page={page}&per_page={per_page}",
            )
            try:
                data = response.json()
            except ValueError as exc:
                raise ilhaCommandError(f"Invalid JSON from DigitalOcean registry API: {exc}") from exc
            batch = data.get(key) or []
            if not isinstance(batch, list):
                break
            if not batch:
                break
            items.extend(row for row in batch if isinstance(row, dict))
            pages = (data.get("links") or {}).get("pages") or {}
            if not pages.get("next"):
                break
            page += 1
        return items

    @staticmethod
    def _encode_repo(repository: str) -> str:
        return quote(str(repository).strip().strip("/"), safe="")

    @staticmethod
    def _encode_registry(registry: str) -> str:
        return quote(str(registry).strip(), safe="")

    def list_repositories(self, registry: str) -> List[str]:
        """List repository names in a registry."""
        registry = str(registry).strip()
        rows = self._collect_paginated(
            f"/registry/{self._encode_registry(registry)}/repositoriesV2",
            "repositories",
        )
        names: List[str] = []
        for row in rows:
            name = str(row.get("name") or "").strip()
            if name:
                names.append(name)
        return names

    def list_tags(self, registry: str, repository: str) -> List[Dict[str, Any]]:
        """List tags for one repository."""
        registry = str(registry).strip()
        repo = self._encode_repo(repository)
        try:
            return self._collect_paginated(
                f"/registry/{self._encode_registry(registry)}/repositories/{repo}/tags",
                "tags",
            )
        except ilhaCommandError as exc:
            message = str(exc).lower()
            if "404" in message or "repository not found" in message:
                return []
            raise

    def delete_tag(self, registry: str, repository: str, tag: str) -> None:
        """Delete one repository tag."""
        registry = str(registry).strip()
        repo = self._encode_repo(repository)
        tag_encoded = quote(str(tag).strip(), safe="")
        self._request(
            "DELETE",
            f"/registry/{self._encode_registry(registry)}/repositories/{repo}/tags/{tag_encoded}",
            ok_statuses={204},
        )

    def docker_credentials(
        self,
        registry: str,
        *,
        read_write: bool = True,
        expiry_seconds: int = 3600,
    ) -> Dict[str, str]:
        """Return username/password for ``docker login`` against DOCR."""
        registry = str(registry).strip()
        params: Dict[str, Any] = {
            "read_write": "true" if read_write else "false",
            "expiry_seconds": expiry_seconds,
        }
        response = self._request(
            "GET",
            "/registry/docker-credentials",
            params=params,
        )
        payload = response.json()

        auths = payload.get("auths") if isinstance(payload, dict) else None
        if not isinstance(auths, dict):
            raise ilhaCommandError("Unexpected docker credentials response from DigitalOcean API")

        entry = auths.get("registry.digitalocean.com")
        if not isinstance(entry, dict):
            # Some responses nest under the registry hostname key only.
            for value in auths.values():
                if isinstance(value, dict) and (
                    (value.get("username") and value.get("password")) or value.get("auth")
                ):
                    entry = value
                    break
        if not isinstance(entry, dict):
            raise ilhaCommandError("Docker credentials missing registry.digitalocean.com entry")

        username = str(entry.get("username") or "").strip()
        password = str(entry.get("password") or "").strip()
        if not username or not password:
            auth_blob = str(entry.get("auth") or "").strip()
            if auth_blob:
                import base64

                try:
                    decoded = base64.b64decode(auth_blob).decode("utf-8")
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ilhaCommandError(f"Could not decode docker credentials auth blob: {exc}") from exc
                username, _, password = decoded.partition(":")
                username = username.strip()
                password = password.strip()
        if not username or not password:
            raise ilhaCommandError("Docker credentials response missing username or password")
        return {"username": username, "password": password}

    def docker_login(self, registry: str, *, read_write: bool = True, expiry_seconds: int = 3600) -> None:
        """Configure local Docker client for DOCR using API credentials."""
        import subprocess

        creds = self.docker_credentials(
            registry,
            read_write=read_write,
            expiry_seconds=expiry_seconds,
        )
        result = subprocess.run(
            [
                "docker",
                "login",
                "registry.digitalocean.com",
                "-u",
                creds["username"],
                "--password-stdin",
            ],
            input=creds["password"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ilhaCommandError(f"docker login failed: {detail or 'unknown error'}")
