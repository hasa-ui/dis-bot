#!/data/data/com.termux/files/usr/bin/sh

cd /data/data/com.termux/files/home/discord-bot || exit 1

git fetch origin || exit 1
git checkout main || exit 1
git reset --hard origin/main || exit 1

. /data/data/com.termux/files/home/discord-bot/setenv.sh
exec python /data/data/com.termux/files/home/discord-bot/bot.py
