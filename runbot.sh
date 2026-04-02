#!/data/data/com.termux/files/usr/bin/sh

set -eu

cd /data/data/com.termux/files/home/discord-bot || exit 1

git fetch origin
git checkout main
git reset --hard origin/main

. /data/data/com.termux/files/home/discord-bot/setenv.sh
exec python /data/data/com.termux/files/home/discord-bot/bot.py
