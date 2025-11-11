import discord
from discord.ext import commands
from discord import app_commands
from ..db import db

class ReactionRoles(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="rr", description="Reaction roles")

    @group.command(name="add", description="Bind an emoji to a role on a message")
    @app_commands.default_permissions(manage_roles=True)
    async def add(self, interaction: discord.Interaction, message_id: str, emoji: str, role: discord.Role):
        await db.ensure_connected()
        await db.conn.execute(
            "INSERT OR REPLACE INTO reaction_roles(guild_id, message_id, emoji, role_id) VALUES(?,?,?,?)",
            (interaction.guild_id, int(message_id), emoji, role.id)
        )
        await db.conn.commit()
        await interaction.response.send_message(f"Bound {emoji} → {role.mention} on message `{message_id}`.", ephemeral=True)

    @group.command(name="remove", description="Remove a reaction role binding")
    @app_commands.default_permissions(manage_roles=True)
    async def remove(self, interaction: discord.Interaction, message_id: str, emoji: str):
        await db.ensure_connected()
        await db.conn.execute(
            "DELETE FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (interaction.guild_id, int(message_id), emoji)
        )
        await db.conn.commit()
        await interaction.response.send_message("Binding removed.", ephemeral=True)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (payload.guild_id, payload.message_id, str(payload.emoji))
        )
        row = await cur.fetchone()
        if not row:
            return
        role_id = row[0]
        guild = self.bot.get_guild(payload.guild_id)
        role = guild.get_role(role_id) if guild else None
        if role:
            try:
                await payload.member.add_roles(role, reason="Reaction role add")
            except discord.Forbidden:
                pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        if payload.guild_id is None:
            return
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT role_id FROM reaction_roles WHERE guild_id=? AND message_id=? AND emoji=?",
            (payload.guild_id, payload.message_id, str(payload.emoji))
        )
        row = await cur.fetchone()
        if not row:
            return
        role_id = row[0]
        guild = self.bot.get_guild(payload.guild_id)
        if not guild:
            return
        member = guild.get_member(payload.user_id)
        role = guild.get_role(role_id)
        if member and role:
            try:
                await member.remove_roles(role, reason="Reaction role remove")
            except discord.Forbidden:
                pass

async def setup(bot):
    cog = ReactionRoles(bot)
    await bot.add_cog(cog)
    if bot.tree.get_command("rr") is None:
        bot.tree.add_command(cog.group)

