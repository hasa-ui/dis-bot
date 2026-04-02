import logging
import os

TOKEN = os.environ.get("DISCORD_TOKEN")
DB_PATH = os.environ.get("DB_PATH", "violations.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("status-bot")

MAX_STAGE_COUNT = 10
DEFAULT_STAGE_COUNT = 3
DEFAULT_STAGE_DURATION_DAYS = 7
SETUP_GUIDANCE = "/setup を実行してください。"

ACTION_NEXT = "next"
ACTION_CLEAR = "clear"
ACTION_HOLD = "hold"
VALID_EXPIRE_ACTIONS = {ACTION_NEXT, ACTION_CLEAR, ACTION_HOLD}
ACTION_LABELS = {
    ACTION_NEXT: "次の弱い段階へ移行",
    ACTION_CLEAR: "解除",
    ACTION_HOLD: "同じ段階を維持",
}

LEGACY_LEVEL_TO_STAGE = {
    "light": 1,
    "medium": 2,
    "heavy": 3,
}
