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
class GuildStatusNotificationConfig:
    guild_id: int
    channel_id: Optional[int]
    notify_manual_set: bool
    notify_manual_clear: bool
    notify_auto_transition: bool
    notify_auto_hold: bool
    notify_config_change: bool


@dataclass(frozen=True)
class StatusConfigExportStage:
    stage_index: int
    label: str
    role_id: Optional[int]
    duration_seconds: int
    on_expire_action: str


@dataclass(frozen=True)
class StatusConfigExportPayload:
    schema_version: int
    source_guild_id: int
    exported_at: int
    stage_count: int
    stages: list[StatusConfigExportStage]


@dataclass(frozen=True)
class StatusConfigImportPreview:
    source_guild_id: Optional[int]
    exported_at: int
    current_stage_count: Optional[int]
    imported_config: GuildStatusConfig
    reapply_count: int
    clamp_count: int
    missing_role_count: int
    diff_lines: list[str]
    warning_lines: list[str]


@dataclass(frozen=True)
class StatusTemplateApplyPreview:
    template_key: str
    template_name: str
    current_stage_count: Optional[int]
    projected_config: GuildStatusConfig
    reapply_count: int
    clamp_count: int
    missing_role_count: int
    diff_lines: list[str]
    warning_lines: list[str]


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


@dataclass(frozen=True)
class StatusHistoryEntry:
    created_at: int
    event_type: str
    actor_display: str
    from_stage_name: Optional[str]
    to_stage_name: Optional[str]
    reason: str
    detail: str


@dataclass(frozen=True)
class BulkOperationResult:
    processed_count: int
    success_count: int
    failure_count: int
    detail_lines: list[str]
