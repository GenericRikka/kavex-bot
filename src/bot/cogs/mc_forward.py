import json, time, discord
from discord.ext import commands
from ..db import db
from ..mc_ws import _connections  # token_hash -> state

class MCForward(commands.Cog):
    def __init__(self, bot): self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT token_hash FROM mc_links WHERE guild_id=? AND channel_id=? AND status='connected'",
            (message.guild.id, message.channel.id)
        )
        rows = await cur.fetchall()
        if not rows:
            return
        payload = json.dumps({
            "op":"dc_chat",
            "user": message.author.display_name,
            "guild": message.guild.name,
            "text": message.content
        })
        for r in rows:
            st = _connections.get(r["token_hash"])
            ws = st["ws"] if st else None
            if ws:
                try:
                    await ws.send_str(payload)
                except Exception:
                    pass

async def setup(bot):
    await bot.add_cog(MCForward(bot))

