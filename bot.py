from status_bot import create_bot
from status_bot.config import TOKEN

bot = create_bot()


if __name__ == "__main__":
    if TOKEN is None:
        raise KeyError("DISCORD_TOKEN")
    bot.run(TOKEN)
