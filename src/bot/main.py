# src/bot/main.py
import os
import asyncio
import logging
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

        # ------------------------------------------------------------------
        #  Global gate: admin-only + command-channel restriction
        # ------------------------------------------------------------------
        async def gate(interaction: discord.Interaction) -> bool:
            # Reject DM use
            if interaction.guild_id is None:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Commands must be used inside a server.", ephemeral=True
                    )
                return False

            # ---------- Administrator requirement ----------
            member = interaction.user
            is_admin = False
            if isinstance(member, discord.Member):
                perms = member.guild_permissions
                owner_id = interaction.guild.owner_id if interaction.guild else None
                is_admin = perms.administrator or (owner_id == member.id)

            # Allow-list for optional public commands
            NONADMIN_ALLOW = {
                # "level",  # uncomment if you want /level usable by everyone
            }

            cmd_name = (
                interaction.command.qualified_name
                if interaction.command is not None
                else ""
            )

            if not is_admin and cmd_name not in NONADMIN_ALLOW:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "Only **administrators** can use this bot.", ephemeral=True
                    )
                return False

            # ---------- Channel restriction ----------
            await db.ensure_connected()
            cur = await db.conn.execute(
                "SELECT command_channel_id FROM guild_settings WHERE guild_id=?",
                (interaction.guild_id,),
            )
            row = await cur.fetchone()
            allowed_channel_id = (
                row["command_channel_id"] if row and row["command_channel_id"] else None
            )
            if allowed_channel_id and interaction.channel_id != allowed_channel_id:
                ch = interaction.guild.get_channel(allowed_channel_id)
                target = ch.mention if ch else f"<#{allowed_channel_id}>"
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"Use commands in {target}.", ephemeral=True
                    )
                return False

            return True

        self.tree.interaction_check = gate

        # ------------------------------------------------------------------
        #  Load all cogs
        # ------------------------------------------------------------------
        await self.load_extension("bot.cogs.welcome")
        await self.load_extension("bot.cogs.leveling")
        await self.load_extension("bot.cogs.reaction_roles")
        await self.load_extension("bot.cogs.server_admin")

        # ------------------------------------------------------------------
        #  Global sync (may take minutes to appear globally)
        # ------------------------------------------------------------------
        try:
            await self.tree.sync()
            logging.info("Slash commands synced globally.")
        except Exception:
            logging.exception("Global slash command sync failed")


bot = MyBot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    logging.info("Logged in as %s (%s)", bot.user, bot.user.id)
    # One-time per-guild sync for instant availability
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

