import discord
from discord.ext import commands, tasks

from .commands import register_commands
from .config import DB_PATH, logger
from .service import StatusService
from .store import StatusStore


class StatusBot(commands.Bot):
    def __init__(self, db_path: str = DB_PATH) -> None:
        intents = discord.Intents.default()
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)
        self.store = StatusStore(db_path)
        self.service = StatusService(self, self.store)

    async def setup_hook(self) -> None:
        register_commands(self)
        await self.tree.sync()
        self.expiry_loop.start()

    async def close(self) -> None:
        if self.expiry_loop.is_running():
            self.expiry_loop.cancel()
        self.store.close()
        await super().close()

    @tasks.loop(minutes=1)
    async def expiry_loop(self) -> None:
        await self.service.process_due_records()

    @expiry_loop.before_loop
    async def before_expiry_loop(self) -> None:
        await self.wait_until_ready()

    async def on_ready(self) -> None:
        logger.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "unknown")

    async def on_member_join(self, member: discord.Member) -> None:
        await self.service.handle_member_join(member)


def create_bot(db_path: str = DB_PATH) -> StatusBot:
    return StatusBot(db_path=db_path)
