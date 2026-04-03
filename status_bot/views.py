from typing import Optional, TYPE_CHECKING

import discord

from .config import ACTION_CLEAR, ACTION_HOLD, ACTION_LABELS, ACTION_NEXT, DEFAULT_STAGE_COUNT, MAX_STAGE_COUNT
from .formatters import (
    build_setup_home_message,
    build_stage_count_preview_message,
    build_stage_editor_message,
    build_stage_save_preview_message,
    build_stage_save_message,
    build_status_count_save_message,
)
from .models import GuildStatusConfig, StatusStageConfig
from .permissions import has_manage_guild
from .validation import (
    days_to_seconds,
    default_stage_config,
    default_stage_name,
    get_stage,
    normalize_label,
    parse_duration_days,
    parse_stage_count,
    seconds_to_days,
)

if TYPE_CHECKING:
    from .app import StatusBot


class OwnerOnlyView(discord.ui.View):
    def __init__(self, bot: "StatusBot", owner_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        self.owner_id = owner_id
        self.message: Optional[discord.InteractionMessage] = None

    async def bind_message(self, interaction: discord.Interaction) -> None:
        self.message = await interaction.original_response()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return False

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return False

        return True


class StageCountModal(discord.ui.Modal, title="段階数設定"):
    def __init__(self, home_view: "SetupHomeView", guild: discord.Guild) -> None:
        super().__init__()
        self.home_view = home_view
        self.bot = home_view.bot
        self.owner_id = home_view.owner_id
        config = self.bot.store.get_status_config(guild.id)
        self.stage_count_input = discord.ui.TextInput(
            label="段階数",
            default=str(config.stage_count if config is not None else DEFAULT_STAGE_COUNT),
            placeholder=f"1〜{MAX_STAGE_COUNT}",
            min_length=1,
            max_length=2,
        )
        self.add_item(self.stage_count_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return

        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        try:
            stage_count = parse_stage_count(self.stage_count_input.value)
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        await interaction.response.defer()
        try:
            preview = self.bot.service.preview_stage_count_settings(guild, stage_count)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        current = self.bot.store.get_status_config(guild.id)
        new_view = StageCountPreviewView(
            self.bot,
            self.owner_id,
            guild.id,
            current.stage_count if current is not None else None,
            stage_count,
            preview,
        )
        content = new_view.render_content()
        if self.home_view.message is not None:
            await self.home_view.message.edit(content=content, view=new_view)
            new_view.message = self.home_view.message
            return

        await interaction.followup.send(content, view=new_view, ephemeral=True)


class SetupHomeView(OwnerOnlyView):
    @discord.ui.button(label="段階数設定", style=discord.ButtonStyle.primary)
    async def configure_stage_count(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        await interaction.response.send_modal(StageCountModal(self, guild))

    @discord.ui.button(label="段階編集", style=discord.ButtonStyle.secondary)
    async def configure_stages(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        config = self.bot.store.get_status_config(guild.id)
        if config is None:
            await interaction.response.send_message("先に段階数を設定してください。", ephemeral=True)
            return

        stage_view = StageSetupView(self.bot, self.owner_id, guild, config.stage_count)
        await interaction.response.edit_message(content=stage_view.render_content(), view=stage_view)
        await stage_view.bind_message(interaction)

    @discord.ui.button(label="再表示", style=discord.ButtonStyle.secondary)
    async def refresh_home(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = SetupHomeView(self.bot, self.owner_id)
        await interaction.response.edit_message(
            content=build_setup_home_message(guild, self.bot.store.get_status_config(guild.id)),
            view=new_view,
        )
        await new_view.bind_message(interaction)


class StageCountPreviewView(OwnerOnlyView):
    def __init__(
        self,
        bot: "StatusBot",
        owner_id: int,
        guild_id: int,
        current_count: Optional[int],
        next_count: int,
        summary,
    ) -> None:
        super().__init__(bot, owner_id)
        self.guild_id = guild_id
        self.current_count = current_count
        self.next_count = next_count
        self.summary = summary

    def render_content(self) -> str:
        return build_stage_count_preview_message(
            self.current_count,
            self.next_count,
            self.summary,
        )

    @discord.ui.button(label="この内容で保存", style=discord.ButtonStyle.success)
    async def confirm_save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            self.bot.service.preview_stage_count_settings(guild, self.next_count)
            refreshed, failed = await self.bot.service.save_stage_count_settings(guild.id, self.next_count)
        except ValueError as e:
            new_view = SetupHomeView(self.bot, self.owner_id)
            await interaction.edit_original_response(
                content=build_setup_home_message(
                    guild,
                    self.bot.store.get_status_config(guild.id),
                    notice=str(e),
                ),
                view=new_view,
            )
            new_view.message = self.message
            return

        new_view = SetupHomeView(self.bot, self.owner_id)
        await interaction.edit_original_response(
            content=build_setup_home_message(
                guild,
                self.bot.store.get_status_config(guild.id),
                notice=build_status_count_save_message(self.next_count, refreshed, failed),
            ),
            view=new_view,
        )
        new_view.message = self.message

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = SetupHomeView(self.bot, self.owner_id)
        await interaction.response.edit_message(
            content=build_setup_home_message(guild, self.bot.store.get_status_config(guild.id)),
            view=new_view,
        )
        await new_view.bind_message(interaction)


class StageActionSelect(discord.ui.Select):
    def __init__(self, stage_view: "StageSetupView") -> None:
        options = []
        if stage_view.stage_index > 1:
            options.append(
                discord.SelectOption(
                    label=ACTION_LABELS[ACTION_NEXT],
                    value=ACTION_NEXT,
                    default=stage_view.selected_action == ACTION_NEXT,
                )
            )
        options.append(
            discord.SelectOption(
                label=ACTION_LABELS[ACTION_CLEAR],
                value=ACTION_CLEAR,
                default=stage_view.selected_action == ACTION_CLEAR,
            )
        )
        options.append(
            discord.SelectOption(
                label=ACTION_LABELS[ACTION_HOLD],
                value=ACTION_HOLD,
                default=stage_view.selected_action == ACTION_HOLD,
            )
        )
        super().__init__(placeholder="満了時動作を選択", options=options, row=1)
        self.stage_view = stage_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.stage_view.selected_action = self.values[0]
        self.stage_view.notice = None
        await interaction.response.edit_message(
            content=self.stage_view.render_content(),
            view=self.stage_view,
        )
        await self.stage_view.bind_message(interaction)


class StageRoleSelect(discord.ui.RoleSelect):
    def __init__(self, stage_view: "StageSetupView") -> None:
        defaults = [stage_view.resolve_selected_role()] if stage_view.resolve_selected_role() is not None else []
        super().__init__(
            placeholder=f"{default_stage_name(stage_view.stage_index)}のロールを選択",
            min_values=1,
            max_values=1,
            default_values=defaults,
            row=0,
        )
        self.stage_view = stage_view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.stage_view.selected_role_id = self.values[0].id
        self.stage_view.notice = None
        await interaction.response.edit_message(
            content=self.stage_view.render_content(),
            view=self.stage_view,
        )
        await self.stage_view.bind_message(interaction)


class StageDetailsModal(discord.ui.Modal, title="段階詳細編集"):
    def __init__(self, stage_view: "StageSetupView") -> None:
        super().__init__()
        self.stage_view = stage_view
        self.owner_id = stage_view.owner_id

        stage = stage_view.current_stage_config()
        self.label_input = discord.ui.TextInput(
            label="表示名（空なら既定名）",
            default=stage.label,
            required=False,
            max_length=50,
        )
        self.duration_input = discord.ui.TextInput(
            label="期間（日数）",
            default=str(stage_view.duration_days),
            placeholder="1〜3650",
            min_length=1,
            max_length=4,
        )
        self.add_item(self.label_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この setup 画面はコマンド実行者のみ操作できます。",
                ephemeral=True,
            )
            return

        if not has_manage_guild(interaction):
            await interaction.response.send_message(
                "Manage Server 権限か管理者権限が必要です。",
                ephemeral=True,
            )
            return

        try:
            duration_days = parse_duration_days(
                self.duration_input.value,
                default_stage_name(self.stage_view.stage_index),
            )
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        self.stage_view.label_value = normalize_label(self.label_input.value)
        self.stage_view.duration_days = duration_days
        self.stage_view.notice = "詳細を更新しました。保存すると反映されます。"

        await interaction.response.defer()
        if self.stage_view.message is not None:
            await self.stage_view.message.edit(
                content=self.stage_view.render_content(),
                view=self.stage_view,
            )


class StageSetupView(OwnerOnlyView):
    def __init__(
        self,
        bot: "StatusBot",
        owner_id: int,
        guild: discord.Guild,
        stage_index: int,
        *,
        notice: Optional[str] = None,
        draft_stage: Optional[StatusStageConfig] = None,
    ) -> None:
        super().__init__(bot, owner_id)
        self.guild_id = guild.id
        config = self.bot.store.get_status_config(guild.id)
        if config is None:
            config = GuildStatusConfig(guild.id, 1, [default_stage_config(1)])

        self.stage_count = config.stage_count
        self.stage_index = max(1, min(stage_index, self.stage_count))
        self.notice = notice
        self._persisted_config = config
        persisted_stage = get_stage(config, self.stage_index) or default_stage_config(self.stage_index)
        working_stage = draft_stage or persisted_stage

        self.label_value = working_stage.label
        self.duration_days = seconds_to_days(working_stage.duration_seconds)
        self.selected_action = (
            ACTION_CLEAR
            if self.stage_index == 1 and working_stage.on_expire_action == ACTION_NEXT
            else working_stage.on_expire_action
        )
        self.selected_role_id = working_stage.role_id

        self.add_item(StageRoleSelect(self))
        self.add_item(StageActionSelect(self))

    def resolve_selected_role(self) -> Optional[discord.Role]:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None or self.selected_role_id is None:
            return None
        return guild.get_role(self.selected_role_id)

    def current_stage_config(self) -> StatusStageConfig:
        return StatusStageConfig(
            stage_index=self.stage_index,
            label=self.label_value,
            role_id=self.selected_role_id,
            duration_seconds=days_to_seconds(self.duration_days),
            on_expire_action=self.selected_action,
        )

    def render_content(self) -> str:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return "サーバー情報が見つかりません。"
        return build_stage_editor_message(
            guild,
            self._persisted_config,
            self.current_stage_config(),
            selected_role=self.resolve_selected_role(),
            duration_days=self.duration_days,
            selected_action=self.selected_action,
            notice=self.notice,
        )

    @discord.ui.button(label="前の段階", style=discord.ButtonStyle.secondary, row=2)
    async def previous_stage(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        new_view = StageSetupView(self.bot, self.owner_id, guild, max(1, self.stage_index - 1))
        await interaction.response.edit_message(content=new_view.render_content(), view=new_view)
        await new_view.bind_message(interaction)

    @discord.ui.button(label="詳細編集", style=discord.ButtonStyle.secondary, row=2)
    async def edit_details(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(StageDetailsModal(self))

    @discord.ui.button(label="保存", style=discord.ButtonStyle.success, row=2)
    async def save_stage(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        await interaction.response.defer()
        draft_stage = self.current_stage_config()
        try:
            preview = self.bot.service.preview_stage_settings(guild, draft_stage)
        except ValueError as e:
            await interaction.followup.send(str(e), ephemeral=True)
            return

        new_view = StageSavePreviewView(
            self.bot,
            self.owner_id,
            guild,
            draft_stage,
            preview,
        )
        await interaction.edit_original_response(content=new_view.render_content(), view=new_view)
        new_view.message = self.message

    @discord.ui.button(label="戻る", style=discord.ButtonStyle.secondary, row=2)
    async def back_to_home(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        new_view = SetupHomeView(self.bot, self.owner_id)
        await interaction.response.edit_message(
            content=build_setup_home_message(guild, self.bot.store.get_status_config(guild.id)),
            view=new_view,
        )
        await new_view.bind_message(interaction)

    @discord.ui.button(label="次の段階", style=discord.ButtonStyle.secondary, row=2)
    async def next_stage(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        new_view = StageSetupView(
            self.bot,
            self.owner_id,
            guild,
            min(self.stage_count, self.stage_index + 1),
        )
        await interaction.response.edit_message(content=new_view.render_content(), view=new_view)
        await new_view.bind_message(interaction)


class StageSavePreviewView(OwnerOnlyView):
    def __init__(
        self,
        bot: "StatusBot",
        owner_id: int,
        guild: discord.Guild,
        draft_stage: StatusStageConfig,
        summary,
    ) -> None:
        super().__init__(bot, owner_id)
        self.guild_id = guild.id
        self.stage_index = draft_stage.stage_index
        self.draft_stage = draft_stage
        self.summary = summary

    def render_content(self) -> str:
        guild = self.bot.get_guild(self.guild_id)
        if guild is None:
            return "サーバー情報が見つかりません。"

        config = self.bot.store.get_status_config(self.guild_id)
        if config is None:
            return "先に段階数を設定してください。"

        persisted_stage = get_stage(config, self.stage_index) or default_stage_config(self.stage_index)
        return build_stage_save_preview_message(
            guild,
            persisted_stage,
            self.draft_stage,
            config,
            self.summary,
        )

    @discord.ui.button(label="編集に戻る", style=discord.ButtonStyle.secondary)
    async def back_to_editor(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        new_view = StageSetupView(
            self.bot,
            self.owner_id,
            guild,
            self.stage_index,
            draft_stage=self.draft_stage,
        )
        await interaction.response.edit_message(content=new_view.render_content(), view=new_view)
        await new_view.bind_message(interaction)

    @discord.ui.button(label="この内容で保存", style=discord.ButtonStyle.success)
    async def confirm_save(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            self.bot.service.preview_stage_settings(guild, self.draft_stage)
            refreshed, failed = await self.bot.service.save_stage_settings(guild.id, self.draft_stage)
        except ValueError as e:
            new_view = StageSetupView(
                self.bot,
                self.owner_id,
                guild,
                self.stage_index,
                notice=str(e),
                draft_stage=self.draft_stage,
            )
            await interaction.edit_original_response(content=new_view.render_content(), view=new_view)
            new_view.message = self.message
            return

        config = self.bot.store.get_status_config(guild.id)
        current_stage = get_stage(config, self.stage_index) if config is not None else None
        new_view = StageSetupView(
            self.bot,
            self.owner_id,
            guild,
            self.stage_index,
            notice=build_stage_save_message(current_stage or self.draft_stage, refreshed, failed),
        )
        await interaction.edit_original_response(content=new_view.render_content(), view=new_view)
        new_view.message = self.message
