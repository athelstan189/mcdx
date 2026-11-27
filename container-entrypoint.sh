#!/bin/bash
set -euo pipefail
umask 077

action=$1
shift
config=(
    -c 'cli_auth_credentials_store="file"'
    -c 'mcp_oauth_credentials_store="file"'
    -c 'sqlite_home="/var/lib/multicodex/sqlite"'
    -c 'log_dir="/home/codex/.codex/log"'
    -c 'check_for_update_on_startup=false'
)

case "$action" in
    login)  exec codex "${config[@]}" login --device-auth ;;
    logout) exec codex "${config[@]}" logout ;;
    status) exec codex "${config[@]}" login status ;;
    open|resume)
        if [[ ${MULTICODEX_CONTAINER_SANDBOX:-0} == 1 ]]; then
            config+=(-s danger-full-access -a untrusted)
        fi
        if [[ $action == resume ]]; then
            if [[ $# == 0 ]]; then
                exec codex "${config[@]}" resume
            fi
            exec codex "${config[@]}" resume -- "$1"
        fi
        exec codex "${config[@]}"
        ;;
    *) printf 'Unknown action: %s\n' "$action" >&2; exit 2 ;;
esac
