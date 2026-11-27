#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'mcdx install: %s\n' "$*" >&2
    exit 1
}

fetch() {
    curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 15 --retry 2 "$@"
}

add_path_to() {
    local profile="$1"
    if [[ -f "$profile" ]] && grep -Fxq '# mcdx installer' "$profile"; then
        return
    fi
    cat >> "$profile" <<'EOF'

# mcdx installer
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac
EOF
}

configure_path() {
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) return ;;
    esac
    local shell="${SHELL:-/bin/bash}"
    case "${shell##*/}" in
        bash)
            add_path_to "$HOME/.bashrc" || return
            local profile="$HOME/.profile"
            if [[ -f "$HOME/.bash_profile" ]]; then
                profile="$HOME/.bash_profile"
            elif [[ -f "$HOME/.bash_login" ]]; then
                profile="$HOME/.bash_login"
            fi
            add_path_to "$profile"
            ;;
        zsh) add_path_to "${ZDOTDIR:-$HOME}/.zshrc" ;;
        sh|dash|ksh) add_path_to "$HOME/.profile" ;;
        *) return 1 ;;
    esac
}

finish_install() {
    if ! configure_path; then
        printf 'Add %s to PATH or run %s directly.\n' "$HOME/.local/bin" "$HOME/.local/bin/mcdx" >&2
    fi
}

main() {
    local package='multicodex-linux-x86_64'
    local archive="$package.tar.gz"
    local local_dir='' download_url='' script_dir=''
    if [[ $# -eq 2 && "$1" == '--from-dir' ]]; then
        local_dir="$(cd -- "$2" && pwd -P)" || fail 'Could not find the archive directory.'
    elif [[ $# -ne 0 ]]; then
        fail 'Usage: bash install.sh [--from-dir ARCHIVE_DIRECTORY]'
    elif [[ -f "${BASH_SOURCE[0]:-}" ]]; then
        script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
        if [[ -f "$script_dir/$archive" ]]; then
            local_dir="$script_dir"
        elif [[ -f "$script_dir/dist/$archive" ]]; then
            local_dir="$script_dir/dist"
        elif [[ -x "$script_dir/mcdx" && -x "$script_dir/codex-runtime/bin/codex" && ! -d "$script_dir/mcdx" ]]; then
            "$script_dir/mcdx" install
            finish_install
            return
        elif [[ -f "$script_dir/multicodex.py" && -d "$script_dir/mcdx" ]]; then
            command -v python3 >/dev/null && python3 -c $'import curses, sys\nsys.exit(sys.version_info < (3, 9))' \
                || fail 'Source installation requires Python 3.9 or newer with curses.'
            [[ -x "$script_dir/codex-runtime/bin/codex" ]] || command -v codex >/dev/null \
                || fail 'Install Codex first or download the bundled Linux x86_64 release.'
            python3 "$script_dir/multicodex.py" install
            finish_install
            return
        fi
    fi

    [[ "$(uname -s)/$(uname -m)" == 'Linux/x86_64' ]] || fail 'The available package requires Linux x86_64.'
    local command_name
    for command_name in tar sha256sum flock; do
        command -v "$command_name" >/dev/null || fail "Required command missing: $command_name"
    done

    local repository='https://github.com/athelstan189/mcdx'
    if [[ -z "$local_dir" ]]; then
        command -v curl >/dev/null || fail 'Required command missing: curl'
        local latest_url
        latest_url="$(fetch --output /dev/null --write-out '%{url_effective}' "$repository/releases/latest")" \
            || fail "Could not find a published release. See $repository/releases."
        local tag="${latest_url#"$repository/releases/tag/"}"
        [[ "$tag" =~ ^v?[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "No stable release is available at $repository/releases."
        download_url="$repository/releases/download/$tag"
    fi

    local bin="$HOME/.local/bin"
    local install_dir="$HOME/.local/opt/mcdx"
    local target="$install_dir/current/mcdx"
    local launcher="$bin/mcdx"
    umask 077
    mkdir -p -- "$install_dir/versions" "$bin"
    local lock_fd
    exec {lock_fd}>"$install_dir/.install.lock"
    flock "$lock_fd"
    if [[ -e "$launcher" || -L "$launcher" ]]; then
        [[ -L "$launcher" && "$(readlink -- "$launcher")" == "$target" ]] \
            || fail "An existing launcher occupies $launcher. Move it before running this installer."
    fi

    scratch="$(mktemp -d "$install_dir/.install-XXXXXXXX")"
    trap 'rm -rf -- "$scratch"' EXIT
    if [[ -n "$local_dir" ]]; then
        cp -- "$local_dir/$archive.sha256" "$scratch/$archive.sha256"
    else
        fetch --output "$scratch/$archive.sha256" "$download_url/$archive.sha256" \
            || fail 'The release checksum could not be downloaded.'
    fi
    local digest asset_name
    read -r digest asset_name < "$scratch/$archive.sha256" || fail 'The release checksum is empty.'
    [[ "$digest" =~ ^[a-f0-9]{64}$ && "$asset_name" == "$archive" ]] || fail 'Invalid release checksum.'

    local release_dir="$install_dir/versions/$digest" version
    if [[ -x "$release_dir/mcdx" ]]; then
        version="$("$release_dir/mcdx" --version)" || fail 'The installed release could not run.'
    else
        [[ ! -e "$release_dir" && ! -L "$release_dir" ]] || fail "A partial install occupies $release_dir."
        if [[ -n "$local_dir" ]]; then
            cp -- "$local_dir/$archive" "$scratch/$archive"
        else
            printf 'Downloading %s…\n' "$archive"
            fetch --output "$scratch/$archive" "$download_url/$archive" || fail 'The release archive could not be downloaded.'
        fi
        (cd -- "$scratch" && printf '%s  %s\n' "$digest" "$archive" | sha256sum --check --status -) \
            || fail 'SHA-256 verification failed.'
        tar -xzf "$scratch/$archive" -C "$scratch" -- "$package"
        [[ -x "$scratch/$package/mcdx" && -x "$scratch/$package/codex-runtime/bin/codex" ]] \
            || fail 'The release archive is incomplete.'
        version="$("$scratch/$package/mcdx" --version)" || fail 'The release could not run on this system.'
        mv -T -- "$scratch/$package" "$release_dir"
    fi

    ln -s -- "$release_dir" "$scratch/current"
    mv -Tf -- "$scratch/current" "$install_dir/current"
    if [[ ! -L "$launcher" ]]; then
        ln -s -- "$target" "$launcher"
    fi
    printf 'Installed %s at %s\n' "$version" "$launcher"
    finish_install
}

main "$@"
