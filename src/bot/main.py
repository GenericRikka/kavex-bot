# src/bot/main.py
import os, asyncio, logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from .db import db

logging.basicConfig(level=logging.INFO)
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._did_guild_sync = False

    async def setup_hook(self):
        await db.connect()
        # Global gate for commands (from previous step)
        async def only_allowed_channel(interaction: discord.Interaction) -> bool:
            if interaction.guild_id is None:
                return True
            await db.ensure_connected()
            cur = await db.conn.execute(
                "SELECT command_channel_id FROM guild_settings WHERE guild_id=?",
                (interaction.guild_id,),
            )
            row = await cur.fetchone()
            allowed = (row and row["command_channel_id"]) or None
            if allowed is None or interaction.channel_id == allowed:
                return True
            ch = interaction.guild.get_channel(allowed)
            target = ch.mention if ch else f"<#{allowed}>"
            # reply ephemerally if used in the wrong channel
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    f"Use commands in {target}.", ephemeral=True
                )
            return False

        self.tree.interaction_check = only_allowed_channel

        # Load cogs
        await self.load_extension("bot.cogs.welcome")
        await self.load_extension("bot.cogs.leveling")
        await self.load_extension("bot.cogs.reaction_roles")
        await self.load_extension("bot.cogs.server_admin")

        # Global sync (slow to propagate, but fine as a baseline)
        try:
            await self.tree.sync()
            logging.info("Slash commands synced globally.")
        except Exception:
            logging.exception("Global slash command sync failed")

bot = MyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)
    # Do a one-time per-guild sync for instant availability
    if not bot._did_guild_sync:
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                logging.info("Synced commands to guild %s (%s)", guild.name, guild.id)
            except Exception:
                logging.exception("Guild sync failed for %s", guild.id)
        bot._did_guild_sync = True

async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

