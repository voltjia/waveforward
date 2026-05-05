"""Release manifest parsing and update checks."""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

from waveforward import __version__
from waveforward.core import AgentSyncError

MANIFEST_FORMAT = "waveforward.release_manifest"
LEGACY_MANIFEST_FORMAT = "waveforward.alpha_manifest"
MANIFEST_FORMAT_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class ReleaseWheel:
    """Wheel asset advertised by a release manifest."""

    url: str
    sha256: str


@dataclass(frozen=True)
class UpdateManifest:
    """Validated release manifest."""

    version: str
    commit: str
    wheel: ReleaseWheel


@dataclass(frozen=True)
class UpdateCheckResult:
    """Result of comparing a release manifest to the local installation."""

    current_version: str
    current_commit: str
    latest_version: str
    latest_commit: str
    wheel_url: str
    wheel_sha256: str
    update_available: bool
    reason: str
    verified: bool

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "current_version": self.current_version,
            "current_commit": self.current_commit,
            "latest_version": self.latest_version,
            "latest_commit": self.latest_commit,
            "wheel_url": self.wheel_url,
            "wheel_sha256": self.wheel_sha256,
            "update_available": self.update_available,
            "reason": self.reason,
            "verified": self.verified,
        }


def check_for_update(
    manifest_location: str | Path,
    *,
    current_version: str = __version__,
    current_commit: str = "",
    headers: dict[str, str] | None = None,
    verify_asset: bool = False,
) -> UpdateCheckResult:
    """Compare a release manifest with the current WaveForward version."""

    location = str(manifest_location)
    manifest = load_update_manifest(location, headers=headers)
    wheel_url = resolve_manifest_asset(location, manifest.wheel.url)
    verified = False
    if verify_asset:
        verify_manifest_asset(location, manifest, headers=headers)
        verified = True

    update_available, reason = _compare_release(
        current_version=current_version,
        current_commit=current_commit,
        latest_version=manifest.version,
        latest_commit=manifest.commit,
    )
    return UpdateCheckResult(
        current_version=current_version,
        current_commit=current_commit,
        latest_version=manifest.version,
        latest_commit=manifest.commit,
        wheel_url=wheel_url,
        wheel_sha256=manifest.wheel.sha256,
        update_available=update_available,
        reason=reason,
        verified=verified,
    )


def load_update_manifest(
    location: str | Path,
    *,
    headers: dict[str, str] | None = None,
) -> UpdateManifest:
    """Load and validate a release manifest from a path or URL."""

    try:
        raw = _read_location_bytes(str(location), headers=headers)
        data = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as error:
        raise AgentSyncError("Update manifest must be UTF-8 JSON.") from error
    except json.JSONDecodeError as error:
        raise AgentSyncError(f"Update manifest is invalid JSON: {error.msg}") from error
    return _validate_manifest(data)


def resolve_manifest_asset(manifest_location: str | Path, asset_url: str) -> str:
    """Resolve an asset URL relative to its manifest location."""

    location = str(manifest_location)
    asset = urllib.parse.urlparse(asset_url)
    if asset.scheme in {"http", "https", "file"}:
        return asset_url

    manifest = urllib.parse.urlparse(location)
    if manifest.scheme in {"http", "https", "file"}:
        return urllib.parse.urljoin(location, asset_url)

    return str((Path(location).expanduser().resolve().parent / asset_url).resolve())


def verify_manifest_asset(
    manifest_location: str | Path,
    manifest: UpdateManifest,
    *,
    headers: dict[str, str] | None = None,
) -> str:
    """Return the asset digest after verifying it against the manifest."""

    asset_location = resolve_manifest_asset(manifest_location, manifest.wheel.url)
    digest = _sha256_bytes(_read_location_bytes(asset_location, headers=headers))
    if digest != manifest.wheel.sha256:
        raise AgentSyncError(
            "Update manifest wheel checksum mismatch: "
            f"expected {manifest.wheel.sha256}, got {digest}"
        )
    return digest


def download_update_wheel(
    manifest_location: str | Path,
    output_dir: str | Path,
    *,
    headers: dict[str, str] | None = None,
) -> Path:
    """Download or copy the manifest wheel after verifying its SHA-256."""

    location = str(manifest_location)
    manifest = load_update_manifest(location, headers=headers)
    asset_location = resolve_manifest_asset(location, manifest.wheel.url)
    content = _read_location_bytes(asset_location, headers=headers)
    digest = _sha256_bytes(content)
    if digest != manifest.wheel.sha256:
        raise AgentSyncError(
            "Update manifest wheel checksum mismatch: "
            f"expected {manifest.wheel.sha256}, got {digest}"
        )

    filename = Path(urllib.parse.urlparse(asset_location).path).name
    if not filename:
        filename = "waveforward-update.whl"
    if not filename.endswith(".whl"):
        raise AgentSyncError("Update manifest wheel URL must end with .whl.")
    destination = Path(output_dir) / filename
    destination.write_bytes(content)
    return destination


def _validate_manifest(data: Any) -> UpdateManifest:
    if not isinstance(data, dict):
        raise AgentSyncError("Update manifest must be a JSON object.")
    if data.get("format") not in {MANIFEST_FORMAT, LEGACY_MANIFEST_FORMAT}:
        raise AgentSyncError("Update manifest format is not supported.")
    if data.get("format_version") != MANIFEST_FORMAT_VERSION:
        raise AgentSyncError("Update manifest format version is not supported.")

    version = str(data.get("version") or "").strip()
    commit = str(data.get("commit") or "").strip()
    wheel = data.get("wheel")
    if not version:
        raise AgentSyncError("Update manifest version is missing.")
    if not isinstance(wheel, dict):
        raise AgentSyncError("Update manifest wheel entry is missing.")

    wheel_url = str(wheel.get("url") or "").strip()
    wheel_sha256 = str(wheel.get("sha256") or "").strip().lower()
    if not wheel_url:
        raise AgentSyncError("Update manifest wheel URL is missing.")
    if not re.fullmatch(r"[0-9a-f]{64}", wheel_sha256):
        raise AgentSyncError("Update manifest wheel SHA-256 is invalid.")

    return UpdateManifest(
        version=version,
        commit=commit,
        wheel=ReleaseWheel(url=wheel_url, sha256=wheel_sha256),
    )


def _read_location_bytes(
    location: str,
    *,
    headers: dict[str, str] | None = None,
) -> bytes:
    parsed = urllib.parse.urlparse(location)
    if parsed.scheme in {"http", "https", "file"}:
        request = urllib.request.Request(location, headers=headers or {})
        try:
            with urllib.request.urlopen(
                request, timeout=DEFAULT_TIMEOUT_SECONDS
            ) as handle:
                return handle.read()
        except OSError as error:
            raise AgentSyncError(
                f"Update manifest URL is unavailable: {error}"
            ) from error
    if parsed.scheme:
        raise AgentSyncError(f"Unsupported update manifest URL scheme: {parsed.scheme}")
    path = Path(location).expanduser()
    if not path.is_file():
        raise AgentSyncError(f"Update manifest path does not exist: {path}")
    return path.read_bytes()


def _compare_release(
    *,
    current_version: str,
    current_commit: str,
    latest_version: str,
    latest_commit: str,
) -> tuple[bool, str]:
    version_order = _compare_versions(latest_version, current_version)
    if version_order > 0:
        return True, "latest version is newer"
    if version_order < 0:
        return False, "current version is newer"

    current_commit = current_commit.strip()
    latest_commit = latest_commit.strip()
    if current_commit and latest_commit and current_commit != latest_commit:
        return True, "same version has a different release commit"
    return False, "current installation is up to date"


def _compare_versions(left: str, right: str) -> int:
    left_parts = _parse_version(left)
    right_parts = _parse_version(right)
    if left_parts is None or right_parts is None:
        return 0 if left == right else -1
    return (left_parts > right_parts) - (left_parts < right_parts)


def _parse_version(value: str) -> tuple[int, ...] | None:
    match = re.fullmatch(r"v?(\d+(?:\.\d+)*)(?:[-+].*)?", value.strip())
    if not match:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def _sha256_bytes(content: bytes) -> str:
    digest = sha256()
    digest.update(content)
    return digest.hexdigest()
