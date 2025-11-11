import discord
from discord.ext import commands
from discord import app_commands
from ..db import db

class ServerAdmin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.guild_only()
    @app_commands.command(name="set_channel", description="Restrict bot commands to this channel")
    @app_commands.default_permissions(administrator=True)
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
            f"Commands are now restricted to {channel.mention}.", ephemeral=False
        )

    @app_commands.guild_only()
    @app_commands.command(name="command_channel", description="Show the current command channel")
    @app_commands.default_permissions(administrator=True)
    async def show_command_channel(self, interaction: discord.Interaction):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT command_channel_id FROM guild_settings WHERE guild_id=?", (interaction.guild_id,)
        )
        row = await cur.fetchone()
        if not row or not row["command_channel_id"]:
            await interaction.response.send_message("No command channel set.", ephemeral=False)
            return
        ch = interaction.guild.get_channel(row["command_channel_id"])
        mention = ch.mention if ch else f"<#{row['command_channel_id']}>"
        await interaction.response.send_message(f"Commands restricted to {mention}.", ephemeral=False)

    # NEW: set default member role for auto-assignment on join
    @app_commands.guild_only()
    @app_commands.command(name="set_member_role", description="Give every new member this role when they join")
    @app_commands.default_permissions(administrator=True)
    async def set_member_role(self, interaction: discord.Interaction, role: discord.Role):
        await db.ensure_connected()
        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)", (interaction.guild_id,)
        )
        await db.conn.execute(
            "UPDATE guild_settings SET default_role_id=? WHERE guild_id=?",
            (role.id, interaction.guild_id),
        )
        await db.conn.commit()
        await interaction.response.send_message(
            f"New members will now receive role {role.mention}.", ephemeral=False
        )

    # Optional helper to view current setting
    @app_commands.guild_only()
    @app_commands.command(name="member_role", description="Show the default role given to new members")
    @app_commands.default_permissions(administrator=True)
    async def show_member_role(self, interaction: discord.Interaction):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT default_role_id FROM guild_settings WHERE guild_id=?", (interaction.guild_id,)
        )
        row = await cur.fetchone()
        if not row or not row["default_role_id"]:
            await interaction.response.send_message("No default member role set.", ephemeral=False)
            return
        role = interaction.guild.get_role(row["default_role_id"])
        if role is None:
            await interaction.response.send_message(
                "A default role ID is set, but I can't find that role. Maybe it was deleted or renamed.",
                ephemeral=False,
            )
            return
        await interaction.response.send_message(
            f"Default member role is {role.mention}.", ephemeral=False
        )

async def setup(bot):
    await bot.add_cog(ServerAdmin(bot))

