#!/bin/bash
set -euo pipefail

if [[ $# != 2 ]]; then
    echo 'Usage: bash scripts/package-release.sh CODEX_PACKAGE_DIR CODEX_SOURCE_DIR' >&2
    exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
codex_package="$(realpath "$1")"
codex_source="$(realpath "$2")"
python="${PYTHON:-python3}"

if [[ ! -x "$codex_package/bin/codex" || ! -f "$codex_package/codex-package.json" ]]; then
    echo 'Expected a Codex runtime package with bin/codex and codex-package.json.' >&2
    exit 2
fi
test -f "$codex_source/LICENSE"
test -f "$codex_source/NOTICE"
test "$("$codex_package/bin/codex" --version)" = 'codex-cli 0.172.0'

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
cd "$root"
"$python" -m PyInstaller --noconfirm --onedir --name mcdx \
    --specpath "$scratch" --workpath "$scratch/build" --distpath "$scratch/dist" multicodex.py

package="multicodex-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)"
release="$scratch/$package"
mv "$scratch/dist/mcdx" "$release"
cp -a "$codex_package" "$release/codex-runtime"
mkdir "$release/licenses"
cp "$codex_source/LICENSE" "$release/licenses/codex-LICENSE"
cp "$codex_source/NOTICE" "$release/licenses/codex-NOTICE"
cp README.md .dockerignore "$release/"
install -m 755 install.sh "$release/install.sh"
cp -a docker patches "$release/"

mkdir -p dist
tar -C "$scratch" -cf - "$package" | gzip -1 > "$scratch/$package.tar.gz"
mv "$scratch/$package.tar.gz" "dist/$package.tar.gz"
(cd dist && sha256sum "$package.tar.gz" > "$package.tar.gz.sha256")
printf 'Release: %s\n' "$root/dist/$package.tar.gz"
