#!/bin/sh
set -eu

PREFIX="${PREFIX:-$HOME/.local}"
PYTHON="${PYTHON:-python3}"
SOURCE=""
REPO=""
REF=""
ARCHIVE=""
MANIFEST=""
MANIFEST_TMP_DIR=""

usage() {
  cat <<'USAGE'
Usage: scripts/install.sh [options]

Install WaveForward into an isolated Python virtual environment.

Options:
  --prefix DIR       Install prefix. Defaults to $HOME/.local.
  --python COMMAND   Python command. Defaults to python3.
  --source DIR       Install from a local checkout.
  --archive PATH     Install from a local source archive or package URL.
  --manifest PATH    Install from a release manifest path or URL.
  --repo URL         Install from a Git repository URL.
  --ref REF          Git ref for --repo installs.
  -h, --help         Show this help.

Examples:
  scripts/install.sh --source .
  scripts/install.sh --archive waveforward-0.1.0.tar.gz
  scripts/install.sh --archive https://example.com/waveforward-0.1.0.tar.gz
  scripts/install.sh --manifest waveforward-release-manifest.json
  scripts/install.sh --manifest https://example.com/waveforward-release-manifest.json
  scripts/install.sh --repo https://github.com/voltjia/waveforward.git --ref main

Environment:
  WAVEFORWARD_INSTALL_WF_ALIAS=0      Do not create the wf shortcut.
  WAVEFORWARD_INSTALL_WF_ALIAS=force  Replace an existing wf command.
USAGE
}

cleanup() {
  if [ -n "$MANIFEST_TMP_DIR" ]; then
    rm -rf "$MANIFEST_TMP_DIR"
  fi
}
trap cleanup EXIT INT TERM

while [ "$#" -gt 0 ]; do
  case "$1" in
    --prefix)
      PREFIX="$2"
      shift 2
      ;;
    --python)
      PYTHON="$2"
      shift 2
      ;;
    --source)
      SOURCE="$2"
      shift 2
      ;;
    --archive)
      ARCHIVE="$2"
      shift 2
      ;;
    --manifest)
      MANIFEST="$2"
      shift 2
      ;;
    --repo)
      REPO="$2"
      shift 2
      ;;
    --ref)
      REF="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "error: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

SELECTED=0
[ -n "$SOURCE" ] && SELECTED=$((SELECTED + 1))
[ -n "$REPO" ] && SELECTED=$((SELECTED + 1))
[ -n "$ARCHIVE" ] && SELECTED=$((SELECTED + 1))
[ -n "$MANIFEST" ] && SELECTED=$((SELECTED + 1))
if [ "$SELECTED" -gt 1 ]; then
  echo "error: use only one of --source, --repo, --archive, or --manifest" >&2
  exit 2
fi

if [ "$SELECTED" -eq 0 ]; then
  SOURCE="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
fi

"$PYTHON" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit("error: WaveForward requires Python 3.12 or newer")
PY
PYTHON_BIN="$(command -v "$PYTHON" 2>/dev/null || printf '%s' "$PYTHON")"

INSTALL_ROOT="$PREFIX/share/waveforward"
VENV="$INSTALL_ROOT/venv"
BIN_DIR="$PREFIX/bin"
WRAPPER="$BIN_DIR/waveforward"
WF_WRAPPER="$BIN_DIR/wf"
USE_UV=0

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  else
    echo "error: sha256sum or shasum is required to verify archive checksums" >&2
    exit 1
  fi
}

verify_archive_checksum() {
  target="$1"
  case "$target" in
    http://*|https://*)
      return 0
      ;;
  esac
  if [ ! -f "$target" ]; then
    return 0
  fi

  target_dir="$(dirname -- "$target")"
  target_name="$(basename -- "$target")"
  checksum_file=""
  if [ -f "$target.sha256" ]; then
    checksum_file="$target.sha256"
  elif [ -f "$target_dir/SHA256SUMS" ] \
    && awk -v name="$target_name" '$2 == name || $2 == "*" name { found = 1 } END { exit(found ? 0 : 1) }' "$target_dir/SHA256SUMS"; then
    checksum_file="$target_dir/SHA256SUMS"
  fi

  if [ -z "$checksum_file" ]; then
    return 0
  fi

  expected="$(awk -v name="$target_name" 'NF == 1 { print $1; exit } $2 == name || $2 == "*" name { print $1; exit }' "$checksum_file")"
  if [ -z "$expected" ]; then
    echo "error: checksum file does not contain $target_name: $checksum_file" >&2
    exit 1
  fi
  actual="$(sha256_file "$target")"
  if [ "$actual" != "$expected" ]; then
    echo "error: checksum mismatch for $target" >&2
    echo "expected: $expected" >&2
    echo "actual:   $actual" >&2
    exit 1
  fi
  echo "Verified checksum: $target"
}

resolve_manifest_archive() {
  MANIFEST_TMP_DIR="$(mktemp -d)"
  "$PYTHON" - "$1" "$MANIFEST_TMP_DIR" <<'PY'
from __future__ import annotations

import hashlib
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

manifest_ref = sys.argv[1]
tmp_dir = Path(sys.argv[2])


def read_ref(ref: str) -> tuple[bytes, str]:
    parsed = urllib.parse.urlparse(ref)
    if parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(ref, timeout=30) as response:
            return response.read(), response.geturl()
    path = Path(ref)
    return path.read_bytes(), path.resolve().as_uri()


def fetch_release_file(base: str, value: str, expected_sha256: str) -> Path:
    parsed = urllib.parse.urlparse(value)
    source = value if parsed.scheme else urllib.parse.urljoin(base, value)
    source_parsed = urllib.parse.urlparse(source)
    filename = Path(source_parsed.path).name or "waveforward-release.whl"
    output = tmp_dir / filename
    if source_parsed.scheme in {"http", "https"}:
        with urllib.request.urlopen(source, timeout=60) as response:
            output.write_bytes(response.read())
    elif source_parsed.scheme == "file":
        path = Path(urllib.request.url2pathname(source_parsed.path))
        if path.is_dir():
            raise SystemExit("error: manifest wheel URL points to a directory")
        output = path
    else:
        output = Path(source)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    if digest != expected_sha256:
        raise SystemExit(
            "error: checksum mismatch for manifest wheel\n"
            f"expected: {expected_sha256}\n"
            f"actual:   {digest}"
        )
    print(output)


manifest_bytes, manifest_url = read_ref(manifest_ref)
try:
    manifest = json.loads(manifest_bytes.decode("utf-8"))
except json.JSONDecodeError as error:
    raise SystemExit(f"error: invalid release manifest JSON: {error.msg}") from error

if manifest.get("format") not in {
    "waveforward.release_manifest",
    "waveforward.alpha_manifest",
}:
    raise SystemExit("error: release manifest format is not supported")
if manifest.get("format_version") != 1:
    raise SystemExit("error: release manifest version is not supported")
wheel = manifest.get("wheel")
if not isinstance(wheel, dict):
    raise SystemExit("error: release manifest is missing wheel metadata")
wheel_url = str(wheel.get("url") or "").strip()
wheel_sha256 = str(wheel.get("sha256") or "").strip().lower()
if not wheel_url or len(wheel_sha256) != 64:
    raise SystemExit("error: release manifest wheel metadata is incomplete")

fetch_release_file(manifest_url, wheel_url, wheel_sha256)
PY
}

mkdir -p "$INSTALL_ROOT" "$BIN_DIR"
if [ -z "${UV_CACHE_DIR:-}" ]; then
  UV_CACHE_DIR="$INSTALL_ROOT/uv-cache"
  export UV_CACHE_DIR
fi
if "$PYTHON" -m venv "$VENV"; then
  "$VENV/bin/python" -m pip install --upgrade pip
elif command -v uv >/dev/null 2>&1; then
  echo "python venv is unavailable; using uv to create the environment." >&2
  rm -rf "$VENV"
  uv venv --python "$PYTHON_BIN" "$VENV"
  USE_UV=1
else
  cat >&2 <<'ERROR'
error: could not create a Python virtual environment.

Install python3-venv or uv, then rerun this installer.
On Debian/Ubuntu:
  sudo apt install python3.12-venv
ERROR
  exit 1
fi

install_package() {
  if [ "$USE_UV" -eq 1 ]; then
    uv pip install --python "$VENV/bin/python" --upgrade "$1"
  else
    "$VENV/bin/python" -m pip install --upgrade "$1"
  fi
}

if [ -n "$REPO" ]; then
  SPEC="git+$REPO"
  if [ -n "$REF" ]; then
    SPEC="$SPEC@$REF"
  fi
  install_package "$SPEC"
elif [ -n "$ARCHIVE" ]; then
  verify_archive_checksum "$ARCHIVE"
  install_package "$ARCHIVE"
elif [ -n "$MANIFEST" ]; then
  ARCHIVE="$(resolve_manifest_archive "$MANIFEST")"
  echo "Verified manifest wheel: $ARCHIVE"
  install_package "$ARCHIVE"
else
  install_package "$SOURCE"
fi

cat > "$WRAPPER" <<EOF
#!/bin/sh
exec "$VENV/bin/waveforward" "\$@"
EOF
chmod 755 "$WRAPPER"

case "${WAVEFORWARD_INSTALL_WF_ALIAS:-auto}" in
  0|false|no)
    ;;
  force)
    cat > "$WF_WRAPPER" <<EOF
#!/bin/sh
exec "$VENV/bin/waveforward" "\$@"
EOF
    chmod 755 "$WF_WRAPPER"
    echo "WaveForward shortcut installed: $WF_WRAPPER"
    ;;
  *)
    if command -v wf >/dev/null 2>&1 && [ "$(command -v wf)" != "$WF_WRAPPER" ]; then
      echo "Skipped wf shortcut because another wf command already exists." >&2
      echo "Set WAVEFORWARD_INSTALL_WF_ALIAS=force to replace $WF_WRAPPER." >&2
    else
      cat > "$WF_WRAPPER" <<EOF
#!/bin/sh
exec "$VENV/bin/waveforward" "\$@"
EOF
      chmod 755 "$WF_WRAPPER"
      echo "WaveForward shortcut installed: $WF_WRAPPER"
    fi
    ;;
esac

echo "WaveForward installed: $WRAPPER"
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *)
    echo "Add this to PATH if needed: $BIN_DIR"
    ;;
esac
