import discord
from discord.ext import commands
from ..db import db

class MCForward(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener("on_message")
    async def relay_to_mc(self, message: discord.Message):
        # ignore bots and webhooks (avoid echo loops)
        if message.author.bot or message.webhook_id:
            return
        if message.guild is None:
            return

        content = (message.content or "").strip()
        if not content:
            return

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT 1 FROM mc_links WHERE guild_id=? AND channel_id=? AND status='connected' LIMIT 1",
            (message.guild.id, message.channel.id),
        )
        row = await cur.fetchone()
        if not row:
            return

        # LAZY IMPORT HERE (prevents early mc_ws import)
        from .. import mc_ws

        await mc_ws.send_dc_chat(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            user=message.author.display_name,
            guild_name=message.guild.name,
            text=content,
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(MCForward(bot))

