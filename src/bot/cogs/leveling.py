import discord
from discord.ext import commands
from discord import app_commands
from ..db import db
from ..utils import needed_xp_for_level, now

COOLDOWN_SECONDS = 60

class Leveling(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        await db.ensure_connected()
        ts = now()
        await db.conn.execute(
            "INSERT OR IGNORE INTO xp(guild_id, user_id) VALUES(?,?)",
            (message.guild.id, message.author.id)
        )
        cur = await db.conn.execute(
            "SELECT xp, level, last_msg_ts FROM xp WHERE guild_id=? AND user_id=?",
            (message.guild.id, message.author.id)
        )
        row = await cur.fetchone()
        xp, level, last_ts = row if row else (0, 0, 0)
        if ts - (last_ts or 0) < COOLDOWN_SECONDS:
            return
        xp += 15
        while xp >= needed_xp_for_level(level):
            xp -= needed_xp_for_level(level)
            level += 1
            try:
                await message.channel.send(f"🎉 {message.author.mention} reached **level {level}**!")
            except discord.HTTPException:
                pass
        await db.conn.execute(
            "UPDATE xp SET xp=?, level=?, last_msg_ts=? WHERE guild_id=? AND user_id=?",
            (xp, level, ts, message.guild.id, message.author.id)
        )
        await db.conn.commit()

    @app_commands.guild_only()
    @app_commands.command(name="level", description="Show your level")
    async def show_level(self, interaction: discord.Interaction, member: discord.Member | None = None):
        await db.ensure_connected()
        member = member or interaction.user
        cur = await db.conn.execute(
            "SELECT xp, level FROM xp WHERE guild_id=? AND user_id=?",
            (interaction.guild_id, member.id)
        )
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(f"{member.mention} is level 0 (no xp yet).")
            return
        xp, level = row
        await interaction.response.send_message(f"{member.mention} is level **{level}** with {xp} XP.")

async def setup(bot):
    await bot.add_cog(Leveling(bot))

