FROM debian:trixie-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates clang cmake curl git libclang-dev \
    ninja-build python3 ripgrep sudo xz-utils
RUN curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain none
ENV PATH=/root/.cargo/bin:$PATH \
    CARGO_NET_GIT_FETCH_WITH_CLI=true \
    CARGO_PROFILE_RELEASE_DEBUG=0 \
    CARGO_PROFILE_RELEASE_LTO=false
RUN git clone --depth 1 --branch rust-v0.172.0 https://github.com/openai/codex.git /src
WORKDIR /src/codex-rs
COPY resume.patch /tmp/resume.patch
RUN git -C /src apply --check /tmp/resume.patch && git -C /src apply /tmp/resume.patch
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
RUN arch="$(uname -m)"; \
    export TARGET="$arch-unknown-linux-musl" RUNNER_TEMP=/tmp GITHUB_ENV=/tmp/build.env; \
    rustup target add "$TARGET"; \
    mkdir /opt/zig; \
    curl -fsSL "https://ziglang.org/download/0.14.0/zig-linux-$arch-0.14.0.tar.xz" \
      | tar -xJ --strip-components=1 -C /opt/zig; \
    export PATH="/opt/zig:$PATH"; \
    bash ../.github/scripts/install-musl-build-tools.sh; \
    while IFS= read -r setting; do export "$setting"; done < "$GITHUB_ENV"; \
    v8="$(python3 ../.github/scripts/rusty_v8_bazel.py resolved-v8-crate-version)"; \
    stem="rusty_v8_ptrcomp_sandbox_release_$TARGET"; \
    base="https://github.com/openai/codex/releases/download/rusty-v8-v$v8"; \
    manifest="$stem.sha256"; \
    mkdir /tmp/v8; \
    curl -fsSL "$base/$manifest" -o "/tmp/v8/$manifest"; \
    expected="$(awk -v name="$manifest" '$2 == name {print $1}' "../third_party/v8/rusty_v8_${v8//./_}_release_manifests.sha256")"; \
    printf '%s  %s\n' "$expected" "/tmp/v8/$manifest" | sha256sum -c -; \
    binding="src_binding_ptrcomp_sandbox_release_$TARGET.rs"; \
    curl -fsSL "$base/lib$stem.a.gz" -o "/tmp/v8/lib$stem.a.gz"; \
    curl -fsSL "$base/$binding" -o "/tmp/v8/$binding"; \
    test "$(wc -l < "/tmp/v8/$manifest")" -eq 2; \
    (cd /tmp/v8 && tr -d '\r' < "$manifest" | sha256sum -c -); \
    export RUSTY_V8_ARCHIVE="/tmp/v8/lib$stem.a.gz" RUSTY_V8_SRC_BINDING_PATH="/tmp/v8/$binding"; \
    export AWS_LC_SYS_NO_JITTER_ENTROPY=1 STABLE_GIT_COMMIT="$(git rev-parse HEAD)"; \
    cargo build --target "$TARGET" --release --bin bwrap; \
    out="target/$TARGET/release"; \
    export CODEX_BWRAP_SHA256="$(sha256sum "$out/bwrap" | cut -d' ' -f1)"; \
    cargo build --target "$TARGET" --release \
      --bin codex --bin codex-code-mode-host --bin codex-exec-server; \
    python3 ../scripts/build_codex_package.py --target "$TARGET" --strip none \
      --entrypoint-bin "$out/codex" --exec-server-bin "$out/codex-exec-server" \
      --code-mode-host-bin "$out/codex-code-mode-host" --bwrap-bin "$out/bwrap" \
      --rg-bin /usr/bin/rg --package-dir /opt/codex

FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates git libgcc-s1 util-linux \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /opt/codex /opt/codex
COPY --chmod=755 container-entrypoint.sh /usr/local/bin/multicodex-entrypoint
ENV PATH=/opt/codex/bin:/opt/codex/codex-path:$PATH
ENTRYPOINT ["/usr/local/bin/multicodex-entrypoint"]
