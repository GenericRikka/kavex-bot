# src/bot/cogs/mc_forward.py
import re
import logging

import discord
from discord.ext import commands

from ..db import db
from .. import mc_ws

MC_UNDERLINE = "§n"
MC_RESET = "§r"

log = logging.getLogger(__name__)


class MCForward(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def _discord_prefix_and_color(
        self, member: discord.Member
    ) -> tuple[str | None, str | None]:
        roles = [r for r in member.roles if not r.is_default()]
        if not roles:
            return None, None

        colored_roles = [r for r in roles if r.colour.value != 0]
        color_role = max(colored_roles, key=lambda r: r.position) if colored_roles else None
        color_hex: str | None = None
        if color_role is not None:
            color_hex = f"#{color_role.colour.value:06X}"

        hoisted_roles = [r for r in roles if r.hoist]
        if hoisted_roles:
            prefix_role = max(hoisted_roles, key=lambda r: r.position)
        elif color_role is not None:
            prefix_role = color_role
        else:
            prefix_role = max(roles, key=lambda r: r.position)

        prefix: str | None = f"[{prefix_role.name}]" if prefix_role is not None else None
        return prefix, color_hex

    def _apply_discord_markdown_to_mc(self, text: str) -> str:
        """
        Translate a subset of Discord markdown to Minecraft formatting codes:

          **bold**         -> §lbold§r
          *italic* / _i_   -> §oitalic§r
          __underline__    -> §nunderline§r
          ~~strike~~       -> §mstrike§r
          `code`           -> §0§a code §r   (approx. black/green highlight)
          ```block```      -> same as inline code, but multi-line
        """

        # --- Code blocks (``` ... ```), including language hint (```java, ```py, etc.)
        def repl_block(m: re.Match) -> str:
            inner = m.group(1)
            # Keep line breaks, color the whole block
            return f"§0§a{inner}§r"

        text = re.sub(
            r"```(?:[a-zA-Z0-9_+\-]+)?\n?([\s\S]*?)```",
            repl_block,
            text,
            flags=re.MULTILINE,
        )

        # --- Inline code `code`
        def repl_inline(m: re.Match) -> str:
            inner = m.group(1)
            return f"§0§a{inner}§r"

        text = re.sub(r"`([^`\n]+)`", repl_inline, text)

        # --- Bold **text**
        text = re.sub(r"\*\*(.+?)\*\*", r"§l\1§r", text)

        # --- Underline __text__
        text = re.sub(r"__(.+?)__", r"§n\1§r", text)

        # --- Strikethrough ~~text~~
        text = re.sub(r"~~(.+?)~~", r"§m\1§r", text)

        # --- Italics *text* (single-asterisk, not bold)
        text = re.sub(
            r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)",
            r"§o\1§r",
            text,
        )

        # --- Italics _text_ (single underscore, not __underline__)
        text = re.sub(
            r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)",
            r"§o\1§r",
            text,
        )

        return text

    def _discord_to_mc_text(self, message: discord.Message) -> str:
        text = message.clean_content

        # underline user mentions
        for user in message.mentions:
            if user.display_name:
                needle = f"@{user.display_name}"
            else:
                needle = f"@{user.name}"
            replacement = f"{MC_UNDERLINE}{needle}{MC_RESET}"
            text = text.replace(needle, replacement)

        # underline role mentions
        for role in message.role_mentions:
            needle = f"@{role.name}"
            replacement = f"{MC_UNDERLINE}{needle}{MC_RESET}"
            text = text.replace(needle, replacement)

        # Apply Discord markdown → MC formatting
        text = self._apply_discord_markdown_to_mc(text)

        return text

    @commands.Cog.listener("on_message")
    async def relay_to_mc(self, message: discord.Message):
        # ignore bots and webhooks (avoid echo loops)
        if message.webhook_id:
            return
        if message.guild is None:
            return

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT 1 FROM mc_links "
            "WHERE guild_id=? AND channel_id=? AND status='connected' LIMIT 1",
            (message.guild.id, message.channel.id),
        )
        row = await cur.fetchone()
        if not row:
            return

        # Format text for MC (mentions, markdown, etc.)
        content = (self._discord_to_mc_text(message) or "").strip()
        if not content:
            return

        # ---------- Ping detection ----------
        raw_content = message.content or ""
        triggered: set[str] = set()

        if "@" in raw_content:
            cur = await db.conn.execute(
                "SELECT mc_name FROM user_links "
                "WHERE guild_id=? AND notify_ping=1",
                (message.guild.id,),
            )
            rows = await cur.fetchall()

            if rows:
                lower = raw_content.lower()

                for r in rows:
                    mc_name = r["mc_name"]
                    if not mc_name:
                        continue

                    # Allow both `@Name` and plain @Name
                    pattern = re.compile(
                        rf"(?<!\w)`?@{re.escape(mc_name)}`?(?!\w)",
                        re.IGNORECASE,
                    )
                    if pattern.search(lower):
                        triggered.add(mc_name)

        if triggered:
            log.info(
                "Discord->MC mention_notify: guild=%s channel=%s triggered=%r",
                message.guild.id,
                message.channel.id,
                triggered,
            )

            # Visually underline the recognized mentions in the MC text
            for mc_name in triggered:
                for needle in (f"@{mc_name}", f"`@{mc_name}`"):
                    if needle in content:
                        content = content.replace(
                            needle,
                            f"{MC_UNDERLINE}{needle}{MC_RESET}",
                        )

            # Send ping notifications to MC (sound)
            for mc_name in triggered:
                await mc_ws.send_dc_notify(
                    guild_id=message.guild.id,
                    channel_id=message.channel.id,
                    mc_name=mc_name,
                )

        # ---------- Normal relay ----------
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

