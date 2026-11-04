#!/bin/sh
# What launchd runs: t2c slack from the current release, with the secrets in ~/.t2c/env (T26).
# Never prints a secret's value. Sourced from a file, so none of them sits in the plist.
set -eu

home="$HOME/.t2c"
env_file="$home/env"

if [ ! -f "$env_file" ]; then
    echo "run-slack: $env_file is missing; run deploy/t2c-service secrets" >&2
    exit 78
fi
if [ -n "$(find "$env_file" ! -perm 600)" ]; then
    echo "run-slack: $env_file can be read by others; run chmod 600 on it" >&2
    exit 78
fi

. "$env_file"

for name in ANTHROPIC_API_KEY SLACK_BOT_TOKEN SLACK_APP_TOKEN T2C_MARKETERS; do
    eval "value=\${$name:-}"
    if [ -z "$value" ]; then
        echo "run-slack: $name isn't set in $env_file" >&2
        exit 78
    fi
done
unset value

cd "$home"
caffeinate -i -w $$ >/dev/null 2>&1 &  # keeps the Mac awake for as long as this process lives
exec "$home/current/.venv/bin/t2c" slack
