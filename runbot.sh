#!/data/data/com.termux/files/usr/bin/sh

set -eu

cd /data/data/com.termux/files/home/discord-bot || exit 1

CURRENT_BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo detached)"

git fetch origin
if ! git checkout main; then
  if [ "$CURRENT_BRANCH" != "main" ]; then
    echo "Refusing to reset non-main branch after checkout failure: $CURRENT_BRANCH" >&2
    exit 1
  fi
fi
git reset --hard origin/main

. /data/data/com.termux/files/home/discord-bot/setenv.sh
exec python /data/data/com.termux/files/home/discord-bot/bot.py
