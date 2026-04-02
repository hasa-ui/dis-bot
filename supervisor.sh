#!/data/data/com.termux/files/usr/bin/sh

set -eu

REPO="/data/data/com.termux/files/home/discord-bot"
LOG_DIR="$REPO/logs"
PID_FILE="$REPO/bot.pid"
BRANCH="main"
REMOTE_REF="origin/main"
CHECK_INTERVAL=60

mkdir -p "$LOG_DIR"

cd "$REPO" || exit 1

start_bot() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    return
  fi

  "$REPO/runbot.sh" >> "$LOG_DIR/bot.log" 2>&1 &
  echo $! > "$PID_FILE"
  echo "[supervisor] started bot pid=$(cat "$PID_FILE")" >> "$LOG_DIR/supervisor.log"
}

stop_bot() {
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    kill "$(cat "$PID_FILE")" 2>/dev/null || true
    sleep 2
    if kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
      kill -9 "$(cat "$PID_FILE")" 2>/dev/null || true
    fi
  fi
  rm -f "$PID_FILE"
  echo "[supervisor] stopped bot" >> "$LOG_DIR/supervisor.log"
}

deploy_main() {
  git checkout "$BRANCH" >> "$LOG_DIR/supervisor.log" 2>&1 || :
  git reset --hard "$REMOTE_REF" >> "$LOG_DIR/supervisor.log" 2>&1
}

current_remote_rev() {
  git rev-parse "$REMOTE_REF"
}

current_local_rev() {
  git rev-parse HEAD
}

# 初回起動
git fetch origin >> "$LOG_DIR/supervisor.log" 2>&1 || true
if [ "$(current_local_rev 2>/dev/null || echo none)" != "$(current_remote_rev 2>/dev/null || echo none)" ]; then
  deploy_main
fi
start_bot

LAST_SEEN="$(current_remote_rev 2>/dev/null || echo none)"

while true; do
  sleep "$CHECK_INTERVAL"

  git fetch origin >> "$LOG_DIR/supervisor.log" 2>&1 || continue
  NEW_REMOTE="$(current_remote_rev 2>/dev/null || echo none)"

  if [ "$NEW_REMOTE" != "$LAST_SEEN" ]; then
    echo "[supervisor] detected update: $LAST_SEEN -> $NEW_REMOTE" >> "$LOG_DIR/supervisor.log"
    if deploy_main; then
      stop_bot
      start_bot
      LAST_SEEN="$NEW_REMOTE"
    else
      echo "[supervisor] deploy failed; keeping current bot running" >> "$LOG_DIR/supervisor.log"
    fi
  else
    # Bot が落ちていたら再起動
    start_bot
  fi
done
