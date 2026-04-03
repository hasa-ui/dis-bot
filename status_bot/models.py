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


@dataclass(frozen=True)
class SetupPreviewSummary:
    reapply_count: int
    clamp_count: int
    missing_role_count: int


@dataclass(frozen=True)
class StatusListEntry:
    user_id: int
    member_display: str
    stage_index: int
    stage_name: str
    next_change_text: str
    reason: str
    expires_at: Optional[int]
