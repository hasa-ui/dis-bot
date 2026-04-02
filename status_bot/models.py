from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class StatusStageConfig:
    stage_index: int
    label: str
    role_id: Optional[int]
    duration_seconds: int
    on_expire_action: str


@dataclass(frozen=True)
class GuildStatusConfig:
    guild_id: int
    stage_count: int
    stages: list[StatusStageConfig]
