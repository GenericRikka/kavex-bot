import re
import discord
from discord.ext import commands
from discord import app_commands
from ..db import db
from ..utils import apply_placeholders

MSG_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)

async def _extract_message(interaction: discord.Interaction, raw: str) -> discord.Message:
    m = MSG_LINK_RE.match(raw)
    if m:
        channel_id = int(m.group("channel"))
        message_id = int(m.group("message"))
        channel = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
        return await channel.fetch_message(message_id)
    message_id = int(raw)
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel):
        raise app_commands.AppCommandError("Provide a valid message link or ID from a text channel.")
    return await channel.fetch_message(message_id)

class Welcome(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.guild_only()
    @app_commands.command(name="welcome_use_message", description="Use a message's content as the welcome template")
    @app_commands.describe(message_link_or_id="Paste a message link or the message ID")
    @app_commands.default_permissions(administrator=True)
    async def welcome_use_message(self, interaction: discord.Interaction, message_link_or_id: str):
        await db.ensure_connected()
        try:
            msg = await _extract_message(interaction, message_link_or_id)
        except Exception as e:
            await interaction.response.send_message(f"Could not fetch that message: {e}", ephemeral=False)
            return

        content = msg.content.strip()
        if not content:
            await interaction.response.send_message("That message has no text content.", ephemeral=False)
            return

        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)", (interaction.guild_id,)
        )
        await db.conn.execute(
            "UPDATE guild_settings SET welcome_message=? WHERE guild_id=?",
            (content, interaction.guild_id),
        )
        await db.conn.commit()
        await interaction.response.send_message("Welcome template updated from that message.", ephemeral=False)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT welcome_channel_id, welcome_message, default_role_id "
            "FROM guild_settings WHERE guild_id=?",
            (member.guild.id,),
        )
        row = await cur.fetchone()
        if not row:
            return

        # Send welcome (if channel configured)
        channel_id = row["welcome_channel_id"]
        template = row["welcome_message"]
        if channel_id:
            channel = member.guild.get_channel(channel_id)
            if channel and isinstance(channel, discord.TextChannel):
                text = apply_placeholders(
                    template or "Welcome to server_name, new_user!",
                    member.guild.name,
                    member.mention
                )
                try:
                    await channel.send(text)
                except discord.HTTPException:
                    pass

        # Assign default role (if configured)
        role_id = row["default_role_id"]
        if role_id:
            role = member.guild.get_role(role_id)
            if role:
                try:
                    await member.add_roles(role, reason="Auto-assign default member role")
                except discord.Forbidden:
                    # Missing permissions or role hierarchy issue
                    pass
                except discord.HTTPException:
                    pass

    @app_commands.guild_only()
    @app_commands.command(name="welcome_set_channel", description="Set the welcome channel")
    @app_commands.default_permissions(administrator=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.ensure_connected()
        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)",
            (interaction.guild_id,),
        )
        await db.conn.execute(
            "UPDATE guild_settings SET welcome_channel_id=? WHERE guild_id=?",
            (channel.id, interaction.guild_id),
        )
        await db.conn.commit()
        await interaction.response.send_message(
            f"Welcome channel set to {channel.mention}.", ephemeral=False
        )

async def setup(bot):
    await bot.add_cog(Welcome(bot))

