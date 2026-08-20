"""Deploy identity written to ``/app/.ilha/deploy.json`` (no secrets)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from ilha.config.settings import VERSION


def build_deploy_identity(
    source_manifest: Mapping[str, Any],
    *,
    image_refs: Optional[Mapping[str, str]] = None,
    build: bool = False,
    secrets_profile: Optional[str] = None,
    ilha_version: str = VERSION,
    deployed_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Assemble the public deploy identity payload.

    *source_manifest* is ``DeployPreflight.gather_source_manifest`` output.
    """
    return {
        "git_sha": source_manifest.get("git_sha"),
        "git_branch": source_manifest.get("git_branch"),
        "git_dirty": source_manifest.get("git_dirty"),
        "branch_name": source_manifest.get("branch_name"),
        "deployed_at": deployed_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "ilha_version": ilha_version,
        "image_refs": dict(image_refs or {}),
        "build": bool(build),
        "secrets_profile": secrets_profile,
    }


def write_deploy_identity(path: Path, identity: Mapping[str, Any]) -> None:
    """Write *identity* as JSON (mode 0644, no secrets)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(dict(identity), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o644)


def parse_deploy_identity(raw: str) -> Dict[str, Any]:
    """Parse a remote ``deploy.json`` body. Returns ``{}`` on empty/invalid JSON."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
