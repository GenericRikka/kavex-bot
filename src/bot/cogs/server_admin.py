import discord
from discord.ext import commands
from discord import app_commands
from ..db import db

class ServerAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.guild_only()
    @app_commands.command(name="set_channel", description="Restrict bot commands to this channel")
    @app_commands.default_permissions(manage_guild=True)
    async def set_command_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.ensure_connected()
        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)", (interaction.guild_id,)
        )
        await db.conn.execute(
            "UPDATE guild_settings SET command_channel_id=? WHERE guild_id=?",
            (channel.id, interaction.guild_id),
        )
        await db.conn.commit()
        await interaction.response.send_message(
            f"Commands are now restricted to {channel.mention}.", ephemeral=True
        )

    @app_commands.guild_only()
    @app_commands.command(name="command_channel", description="Show the current command channel")
    async def show_command_channel(self, interaction: discord.Interaction):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT command_channel_id FROM guild_settings WHERE guild_id=?", (interaction.guild_id,)
        )
        row = await cur.fetchone()
        if not row or not row["command_channel_id"]:
            await interaction.response.send_message("No command channel set.", ephemeral=True)
            return
        ch = interaction.guild.get_channel(row["command_channel_id"])
        mention = ch.mention if ch else f"<#{row['command_channel_id']}>"
        await interaction.response.send_message(f"Commands restricted to {mention}.", ephemeral=True)

async def setup(bot):
    await bot.add_cog(ServerAdmin(bot))

