#!/usr/bin/env sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -x "$script_dir/.venv/bin/python" ]; then
    if command -v python3 >/dev/null 2>&1; then
        python=python3
    elif command -v python >/dev/null 2>&1; then
        python=python
    else
        echo "SaveGDrive needs Python 3.11 or newer, and neither python3 nor python was found." >&2
        exit 1
    fi
    if ! "$python" -c 'import sys; sys.exit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
        echo "SaveGDrive needs Python 3.11 or newer; $python is $("$python" -V 2>&1)." >&2
        exit 1
    fi
    "$python" -m venv "$script_dir/.venv"
fi

if ! "$script_dir/.venv/bin/python" -c 'import googleapiclient, google_auth_oauthlib, send2trash, textual' >/dev/null 2>&1; then
    "$script_dir/.venv/bin/python" -m pip install \
        google-api-python-client \
        google-auth \
        google-auth-oauthlib \
        send2trash \
        textual
fi

unset NO_COLOR

exec "$script_dir/.venv/bin/python" "$script_dir/savegdrive.py" "$@"
