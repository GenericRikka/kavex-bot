import re
import discord
from discord.ext import commands
from discord import app_commands
from ..db import db

MSG_LINK_RE = re.compile(
    r"https?://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/(?P<guild>\d+)/(?P<channel>\d+)/(?P<message>\d+)"
)

async def resolve_message_and_channel(
    interaction: discord.Interaction, message_link_or_id: str
) -> tuple[discord.TextChannel, int]:
    """
    Returns (channel, message_id).
    Accepts full message link or a raw message ID from the current channel.
    """
    m = MSG_LINK_RE.match(message_link_or_id)
    if m:
        channel_id = int(m.group("channel"))
        message_id = int(m.group("message"))
        ch = interaction.client.get_channel(channel_id) or await interaction.client.fetch_channel(channel_id)
        if not isinstance(ch, discord.TextChannel):
            raise app_commands.AppCommandError("Target channel is not a text channel.")
        return ch, message_id
    # Fallback: raw ID assumed to be in the current channel
    if not isinstance(interaction.channel, discord.TextChannel):
        raise app_commands.AppCommandError("Provide a valid message link or use the command in a text channel.")
    return interaction.channel, int(message_link_or_id)


class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="rr", description="Reaction roles")

    @group.command(name="add", description="Bind an emoji to a role on a message")
    @app_commands.describe(
        message="Paste a message link or message ID (ID assumed from this channel)",
        emoji="Emoji to bind (use a real emoji like ✅ or a custom one like <:name:id>)",
        role="Role to grant when users react"
    )
    @app_commands.default_permissions(administrator=True)
    async def add(
        self,
        interaction: discord.Interaction,
        message: str,
        emoji: str,
        role: discord.Role
    ):
        await db.ensure_connected()
        try:
            channel, message_id = await resolve_message_and_channel(interaction, message)
        except Exception as e:
            await interaction.response.send_message(f"Could not resolve message: {e}", ephemeral=False)
            return

        # Save binding (store channel_id to auto-react later)
        await db.conn.execute(
            "INSERT OR REPLACE INTO reaction_roles(guild_id, channel_id, message_id, emoji, role_id) "
            "VALUES(?,?,?,?,?)",
            (interaction.guild_id, channel.id, message_id, emoji, role.id),
        )
        await db.conn.commit()

        # Try to add the reaction on the message to help users
        try:
            msg = await channel.fetch_message(message_id)
            await msg.add_reaction(emoji)
        except discord.HTTPException:
            # Missing perms (Add Reactions / Read History) or bad emoji; still keep the binding
            pass

        await interaction.response.send_message(
            f"Bound {emoji} → {role.mention} on message `{message_id}` in {channel.mention}.",
            ephemeral=False
        )

    @group.command(name="remove", description="Remove a reaction role binding")
    @app_commands.describe(
        message="Paste a message link or message ID (ID assumed from this channel)",
        emoji="Emoji to unbind"
    )
    @app_commands.default_permissions(administrator=True)
    async def remove(self, interaction: discord.Interaction, message: str, emoji: str):
        await db.ensure_connected()
        try:
            channel, message_id = await resolve_message_and_channel(interaction, message)
        except Exception as e:
            await interaction.response.send_message(f"Could not resolve message: {e}", ephemeral=False)
            return

        await db.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (interaction.guild_id, message_id, emoji),
        )
        await db.conn.commit()

        # Optionally remove our own reaction as a visual cue
        try:
            msg = await channel.fetch_message(message_id)
            # remove only the bot's own reaction
            for r in msg.reactions:
                # r.emoji can be str or PartialEmoji; compare stringified
                if str(r.emoji) == emoji:
                    async for user in r.users():
                        if user.id == interaction.client.user.id:
                            await msg.remove_reaction(r.emoji, user)
                    break
        except discord.HTTPException:
            pass

        await interaction.response.send_message("Binding removed.", ephemeral=False)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        if payload.user_id == self.bot.user.id:
            return  # ignore our own reaction

        await db.ensure_connected()
        # Look up the role for this (guild, message, emoji)
        cur = await db.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (payload.guild_id, payload.message_id, str(payload.emoji)),
        )
        row = await cur.fetchone()
        if not row:
            return

        role_id = row["role_id"]
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(role_id)
        if role is None:
            return

        # Ensure we have a Member object (payload.member may be None)
        member = payload.member
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return

        if member.bot:
            return

        try:
            await member.add_roles(role, reason="Reaction role add")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (payload.guild_id, payload.message_id, str(payload.emoji)),
        )
        row = await cur.fetchone()
        if not row:
            return

        role_id = row["role_id"]
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(role_id)
        if role is None:
            return

        # Member is not included on remove events; fetch it
        try:
            member = await guild.fetch_member(payload.user_id)
        except discord.HTTPException:
            return

        try:
            await member.remove_roles(role, reason="Reaction role remove")
        except discord.Forbidden:
            pass
        except discord.HTTPException:
            pass


async def setup(bot):
    cog = ReactionRoles(bot)
    await bot.add_cog(cog)
    if bot.tree.get_command("rr") is None:
        bot.tree.add_command(cog.group)

