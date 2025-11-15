import os
import asyncio
import logging
from dotenv import load_dotenv
import discord
from discord.ext import commands
from .db import db

logging.basicConfig(level=logging.INFO)
load_dotenv()
logging.info("[BOOT] main module imported")

TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

class MyBot(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._did_guild_sync = False
        self._ws_task = None

    async def setup_hook(self):
        await db.connect()

        async def gate(interaction: discord.Interaction) -> bool:
            if interaction.guild_id is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Commands must be used inside a server.", ephemeral=False
                    )
                return False

            # admin-only by default
            member = interaction.user
            is_admin = False
            if isinstance(member, discord.Member):
                perms = member.guild_permissions
                owner_id = interaction.guild.owner_id if interaction.guild else None
                is_admin = perms.administrator or (owner_id == member.id)

            NONADMIN_ALLOW = {"linkdiscord"}
            cmd_name = interaction.command.qualified_name if interaction.command else ""
            if not is_admin and cmd_name not in NONADMIN_ALLOW:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Only **administrators** can use this bot.", ephemeral=False
                    )
                return False

            # command-channel gate (optional)
            await db.ensure_connected()

            # 1) Get the primary command channel (if configured)
            cur = await db.conn.execute(
                "SELECT command_channel_id FROM guild_settings WHERE guild_id=?",
                (interaction.guild_id,),
            )
            row = await cur.fetchone()
            command_channel_id = row["command_channel_id"] if row and row["command_channel_id"] else None

            # 2) Collect all MC-linked channels for this guild
            mc_cur = await db.conn.execute(
                "SELECT channel_id FROM mc_links WHERE guild_id=?",
                (interaction.guild_id,),
            )
            mc_rows = await mc_cur.fetchall()
            mc_channels = {int(r["channel_id"]) for r in mc_rows} if mc_rows else set()

            ch_id = interaction.channel_id

            # If a command channel is configured:
            #   - allow that channel
            #   - allow all MC-linked channels
            if command_channel_id is not None:
                if ch_id != command_channel_id and ch_id not in mc_channels:
                    # Still reject, but mention where they *should* go
                    ch = interaction.guild.get_channel(command_channel_id)
                    target = ch.mention if ch else f"<#{command_channel_id}>"
                    if not interaction.response.is_done():
                        await interaction.response.send_message(
                            f"Use commands in {target} or in the linked Minecraft channel(s).",
                            ephemeral=False,
                        )
                    return False

            return True

        self.tree.interaction_check = gate

        # cogs
        await self.load_extension("src.bot.cogs.welcome")
        await self.load_extension("src.bot.cogs.leveling")
        await self.load_extension("src.bot.cogs.reaction_roles")
        await self.load_extension("src.bot.cogs.server_admin")
        await self.load_extension("src.bot.cogs.minecraft")
        await self.load_extension("src.bot.cogs.mc_forward")
        await self.load_extension("src.bot.cogs.mc_mod_bridge")
        await self.load_extension("src.bot.cogs.mc_identity")

        try:
            await self.tree.sync()
            logging.info("Slash commands synced globally.")
        except Exception:
            logging.exception("Global slash command sync failed")

bot = MyBot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    logging.info("[BOOT] on_ready() entered")
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)

    if not bot._did_guild_sync:
        for guild in bot.guilds:
            try:
                bot.tree.copy_global_to(guild=guild)
                await bot.tree.sync(guild=guild)
                logging.info("Synced commands to guild %s (%s)", guild.name, guild.id)
            except Exception:
                logging.exception("Guild sync failed for %s", guild.id)
        bot._did_guild_sync = True

    # warm cache (threads not included; that’s fine)
    for guild in bot.guilds:
        try:
            chans = await guild.fetch_channels()
            logging.info("Preloaded %d channels for guild %s (%s)", len(chans), guild.name, guild.id)
        except Exception as e:
            logging.warning("Channel preload failed for guild %s: %s", guild.id, e)

    # Import mc_ws ONLY now to avoid early side-effects
    logging.info("[BOOT] importing mc_ws inside on_ready()")
    from . import mc_ws

    # Signal readiness BEFORE starting WS
    logging.info("[BOOT] calling mc_ws.mark_discord_ready()")
    mc_ws.mark_discord_ready()

    # Start WS once
    if bot._ws_task is None or bot._ws_task.done():
        async def _runner():
            try:
                await mc_ws.run_ws_app()
            except Exception:
                logging.exception("WebSocket server task exited with an error")
        logging.info("[BOOT] scheduling run_ws_app() now that discord is ready")
        bot._ws_task = asyncio.create_task(_runner())
        logging.info("Scheduled MC WebSocket server task")

async def main():
    async with bot:
        await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())

