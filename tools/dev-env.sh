#!/usr/bin/env bash
# The environment the test suite runs in, for CI and for development alike: MAST_common's CI
# pins and this repo's pyproject.toml with its dev group, in ONE uv resolve -- the joint
# install mast-clone performs on the fleet. `uv sync` cannot build it: this repo does not
# declare MAST_common's dependencies, so a synced venv cannot import `common`, and a sync run
# later strips them from a venv this script built.
#
#   tools/dev-env.sh            install into <repo>/.venv, creating it if missing
#   tools/dev-env.sh --system   install into the current interpreter (CI)
#
# Expects MAST_common checked out as the sibling <top>/common.
set -euo pipefail

PYTHON_VERSION=3.12

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
top="$(dirname "$repo")"
name="$(basename "$repo")"

if [[ ! -f "$top/common/requirements-ci.txt" ]]; then
    echo "dev-env: $top/common/requirements-ci.txt not found; check out MAST_common as $top/common" >&2
    exit 1
fi

# Relative paths from <top>: on Windows CI, Git Bash would rewrite an absolute
# `<path>/pyproject.toml:dev` as a path list.
cd "$top"
install=(uv pip install -r common/requirements-ci.txt -r "$name/pyproject.toml" --group "$name/pyproject.toml:dev")

case "${1:-}" in
    --system)
        "${install[@]}" --system
        ;;
    "")
        if [[ ! -d "$name/.venv" ]]; then
            uv venv --python "$PYTHON_VERSION" "$name/.venv"
        fi
        VIRTUAL_ENV="$top/$name/.venv" "${install[@]}"
        ;;
    *)
        echo "usage: tools/dev-env.sh [--system]" >&2
        exit 2
        ;;
esac
