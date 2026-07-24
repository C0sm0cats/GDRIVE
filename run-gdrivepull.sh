#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -x "$script_dir/.venv/bin/python" ]; then
    python -m venv "$script_dir/.venv"
fi

if ! "$script_dir/.venv/bin/python" -c 'import googleapiclient, google_auth_oauthlib, textual' >/dev/null 2>&1; then
    "$script_dir/.venv/bin/python" -m pip install \
        google-api-python-client \
        google-auth \
        google-auth-oauthlib \
        textual
fi

unset NO_COLOR

exec "$script_dir/.venv/bin/python" "$script_dir/gdrivepull.py" "$@"
