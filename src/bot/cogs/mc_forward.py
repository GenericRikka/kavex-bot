import discord
from discord.ext import commands
from ..db import db


class MCForward(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _discord_prefix_and_color(self, member: discord.Member) -> tuple[str | None, str | None]:
        """
        Compute a prefix and color for a Discord member:

        - color: highest-priority role with a non-default color
        - prefix: highest-priority hoisted role (sidebar); fallback to color-role; fallback to top role
        """
        roles = [r for r in member.roles if not r.is_default()]
        if not roles:
            return None, None

        # Color role = highest colored role
        colored_roles = [r for r in roles if r.colour.value != 0]
        color_role = max(colored_roles, key=lambda r: r.position) if colored_roles else None
        color_hex: str | None = None
        if color_role is not None:
            color_hex = f"#{color_role.colour.value:06X}"

        # Prefix role = hoisted role on sidebar, else color role, else top role
        hoisted_roles = [r for r in roles if r.hoist]
        if hoisted_roles:
            prefix_role = max(hoisted_roles, key=lambda r: r.position)
        elif color_role is not None:
            prefix_role = color_role
        else:
            prefix_role = max(roles, key=lambda r: r.position)

        prefix: str | None = f"[{prefix_role.name}]" if prefix_role is not None else None
        return prefix, color_hex

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

        member = message.author
        prefix: str | None = None
        color_hex: str | None = None
        if isinstance(member, discord.Member):
            prefix, color_hex = self._discord_prefix_and_color(member)

        await mc_ws.send_dc_chat(
            guild_id=message.guild.id,
            channel_id=message.channel.id,
            user=message.author.display_name,
            guild_name=message.guild.name,
            text=content,
            prefix=prefix,
            color=color_hex,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MCForward(bot))

