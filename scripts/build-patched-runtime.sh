#!/bin/bash
set -euo pipefail

if [[ $# != 3 ]]; then
    echo 'Usage: bash scripts/build-patched-runtime.sh CODEX_SOURCE_DIR STOCK_RUNTIME_DIR V8_ASSETS_DIR' >&2
    exit 2
fi

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="$(realpath "$1")"
stock="$(realpath "$2")"
v8="$(realpath "$3")"
target="$(rustc -vV | sed -n 's/^host: //p')"
test "$(git -C "$source_dir" rev-parse HEAD)" = "$(git -C "$source_dir" rev-parse 'rust-v0.172.0^{commit}')"
test "$("$stock/bin/codex" --version)" = 'codex-cli 0.172.0'

if git -C "$source_dir" apply --check "$root/patches/resume.patch" 2>/dev/null; then
    git -C "$source_dir" apply "$root/patches/resume.patch"
else
    git -C "$source_dir" apply --reverse --check "$root/patches/resume.patch"
fi

version="$(python3 "$source_dir/.github/scripts/rusty_v8_bazel.py" resolved-v8-crate-version)"
stem="rusty_v8_ptrcomp_sandbox_release_$target"
expected="$(awk -v file="$stem.sha256" '$2 == file {print $1}' "$source_dir/third_party/v8/rusty_v8_${version//./_}_release_manifests.sha256")"
printf '%s  %s\n' "$expected" "$v8/$stem.sha256" | sha256sum -c -
(cd "$v8" && tr -d '\r' < "$stem.sha256" | sha256sum -c -)

export RUSTY_V8_ARCHIVE="$v8/lib$stem.a.gz"
export RUSTY_V8_SRC_BINDING_PATH="$v8/src_binding_ptrcomp_sandbox_release_$target.rs"
export CODEX_BWRAP_SHA256="$(sha256sum "$stock/codex-resources/bwrap" | cut -d' ' -f1)"
export STABLE_GIT_COMMIT="$(git -C "$source_dir" rev-parse HEAD)"
export CARGO_PROFILE_RELEASE_DEBUG=0 CARGO_PROFILE_RELEASE_LTO=false
export CARGO_TARGET_DIR="$(realpath -m "${CARGO_TARGET_DIR:-$source_dir/codex-rs/target}")"
cd "$source_dir/codex-rs"
cargo build --release --target "$target" --package codex-cli --bin codex

stage="$(mktemp -d "$root/.codex-runtime.XXXXXX")"
trap 'rm -rf "$stage"' EXIT
cp -a "$stock" "$stage/runtime"
install -m 755 "$CARGO_TARGET_DIR/$target/release/codex" "$stage/runtime/bin/codex"
python3 - "$stage/runtime/codex-package.json" "$target" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
metadata = json.loads(path.read_text())
metadata["target"] = sys.argv[2]
path.write_text(json.dumps(metadata, indent=2) + "\n")
PY
test "$("$stage/runtime/bin/codex" --version)" = 'codex-cli 0.172.0'
if [[ -e "$root/codex-runtime" || -L "$root/codex-runtime" ]]; then
    echo "An existing runtime occupies $root/codex-runtime." >&2
    exit 2
fi
mv -T "$stage/runtime" "$root/codex-runtime"
printf 'Patched runtime: %s\n' "$root/codex-runtime/bin/codex"
