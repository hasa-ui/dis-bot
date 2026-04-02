import time
from typing import Optional

from .config import (
    ACTION_CLEAR,
    ACTION_HOLD,
    ACTION_NEXT,
    DEFAULT_STAGE_DURATION_DAYS,
    MAX_STAGE_COUNT,
    VALID_EXPIRE_ACTIONS,
)
from .models import GuildStatusConfig, StatusStageConfig


def now_ts() -> int:
    return int(time.time())


def days_to_seconds(days: int) -> int:
    return days * 24 * 60 * 60


def seconds_to_days(seconds: int) -> int:
    if seconds <= 0:
        return DEFAULT_STAGE_DURATION_DAYS
    return max(1, seconds // 86400)


def default_stage_name(stage_index: int) -> str:
    return f"段階{stage_index}"


def normalize_label(value: str) -> str:
    return value.strip()


def default_stage_action(stage_index: int) -> str:
    return ACTION_CLEAR if stage_index == 1 else ACTION_NEXT


def default_stage_config(stage_index: int) -> StatusStageConfig:
    return StatusStageConfig(
        stage_index=stage_index,
        label="",
        role_id=None,
        duration_seconds=days_to_seconds(DEFAULT_STAGE_DURATION_DAYS),
        on_expire_action=default_stage_action(stage_index),
    )


def build_stage_map(config: GuildStatusConfig) -> dict[int, StatusStageConfig]:
    return {stage.stage_index: stage for stage in config.stages}


def get_stage(config: GuildStatusConfig, stage_index: int) -> Optional[StatusStageConfig]:
    return build_stage_map(config).get(stage_index)


def configured_role_ids(config: Optional[GuildStatusConfig]) -> set[int]:
    if config is None:
        return set()
    return {stage.role_id for stage in config.stages if stage.role_id is not None}


def is_stage_ready(stage: Optional[StatusStageConfig]) -> bool:
    if stage is None:
        return False
    if stage.role_id is None:
        return False
    if stage.duration_seconds <= 0:
        return False
    if stage.on_expire_action not in VALID_EXPIRE_ACTIONS:
        return False
    if stage.stage_index == 1 and stage.on_expire_action == ACTION_NEXT:
        return False
    return True


def config_complete(config: Optional[GuildStatusConfig]) -> bool:
    if config is None:
        return False
    if not 1 <= config.stage_count <= MAX_STAGE_COUNT:
        return False
    if len(config.stages) != config.stage_count:
        return False
    return all(is_stage_ready(stage) for stage in config.stages)


def stage_path_is_ready(config: Optional[GuildStatusConfig], stage_index: int) -> bool:
    if config is None:
        return False
    if not 1 <= stage_index <= config.stage_count:
        return False

    stage_map = build_stage_map(config)
    current_index = stage_index
    visited: set[int] = set()

    while True:
        if current_index in visited:
            return False
        visited.add(current_index)

        stage = stage_map.get(current_index)
        if not is_stage_ready(stage):
            return False

        if stage.on_expire_action in {ACTION_CLEAR, ACTION_HOLD}:
            return True

        current_index -= 1
        if current_index < 1:
            return False


def parse_duration_days(value: str, label: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError(f"{label}の日数を入力してください。")
    if not stripped.isdigit():
        raise ValueError(f"{label}の日数は整数で入力してください。")

    days = int(stripped)
    if not 1 <= days <= 3650:
        raise ValueError(f"{label}の日数は 1〜3650 の範囲で入力してください。")
    return days


def parse_stage_count(value: str) -> int:
    stripped = value.strip()
    if not stripped:
        raise ValueError("段階数を入力してください。")
    if not stripped.isdigit():
        raise ValueError("段階数は整数で入力してください。")

    count = int(stripped)
    if not 1 <= count <= MAX_STAGE_COUNT:
        raise ValueError(f"段階数は 1〜{MAX_STAGE_COUNT} の範囲で入力してください。")
    return count


def validate_stage_configuration(config: GuildStatusConfig, replacement: StatusStageConfig) -> None:
    if replacement.role_id is None:
        raise ValueError(f"{default_stage_name(replacement.stage_index)}のロールを選択してください。")
    if replacement.duration_seconds <= 0:
        raise ValueError(f"{default_stage_name(replacement.stage_index)}の期間を設定してください。")
    if replacement.on_expire_action not in VALID_EXPIRE_ACTIONS:
        raise ValueError("満了時動作が不正です。")
    if replacement.stage_index == 1 and replacement.on_expire_action == ACTION_NEXT:
        raise ValueError("段階1は次の弱い段階へ移行できません。解除か維持を選択してください。")

    seen: dict[int, int] = {}
    for stage in config.stages:
        current = replacement if stage.stage_index == replacement.stage_index else stage
        if current.role_id is None:
            continue
        if current.role_id in seen:
            raise ValueError(
                f"{default_stage_name(seen[current.role_id])} と "
                f"{default_stage_name(current.stage_index)} で同じロールは使えません。"
            )
        seen[current.role_id] = current.stage_index
