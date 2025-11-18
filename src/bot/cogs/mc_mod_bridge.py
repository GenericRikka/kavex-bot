import re
import time
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands
from ..db import db


class MCModBridge(commands.Cog):
    """
    Parses moderation 'commands' coming from Minecraft chat via the bridge.

    Example in-game:
      !dcban @User spamming slurs
      !dckick @User being annoying
      !dctimeout @User 15 caps lock warrior
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Target resolution ----------

    async def _resolve_target_member(
        self,
        guild: discord.Guild,
        raw: str,
        content: str,
        mentions: list[discord.Member],
    ) -> discord.Member | None:
        raw = (raw or "").strip()

        # 1) If Discord already gave us a proper mention object, use it.
        if mentions:
            return mentions[0]

        # 2) <@123> or <@!123>
        m = re.match(r"^<@!?(\d+)>$", raw)
        if m:
            try:
                return await guild.fetch_member(int(m.group(1)))
            except Exception:
                return None

        # 3) Numeric ID
        if raw.isdigit():
            try:
                return await guild.fetch_member(int(raw))
            except Exception:
                return None

        # 4) Strip leading '@' from @Name / @DisplayName
        if raw.startswith("@"):
            raw = raw[1:]
        needle = raw.lower()

        # Make sure members are actually cached; you already have intents.members = True
        # If you want to be extra sure, you can call await guild.chunk() somewhere on startup.

        # Try matching by username, display_name, or global_name
        for m in guild.members:
            if m.name.lower() == needle:
                return m
            if m.display_name and m.display_name.lower() == needle:
                return m
            gn = getattr(m, "global_name", None)
            if gn and gn.lower() == needle:
                return m

        # 5) As a last resort, scan the whole content for a real mention and use that
        if mentions:
            return mentions[0]

        return None

    async def _check_mc_permission(
        self,
        guild: discord.Guild,
        mc_name: str,
        action: str,
    ) -> tuple[bool, discord.Member | None, str | None]:
        """
        Resolve the MC player name to a linked Discord user, then decide permission
        based on *native Discord guild permissions*:

          - kick:    member.guild_permissions.kick_members or administrator
          - ban:     member.guild_permissions.ban_members or administrator
          - timeout: member.guild_permissions.moderate_members or administrator

        Also computes prefix & color from roles and caches them in mc_perm_cache.

        Returns (allowed, member_or_None, reason_if_denied).
        """
        await db.ensure_connected()

        # Cross-moderation toggle: if disabled, MC → Discord moderation is off
        cur = await db.conn.execute(
            "SELECT crossmod_enabled FROM guild_settings WHERE guild_id=?",
            (guild.id,),
        )
        row = await cur.fetchone()
        if row and row["crossmod_enabled"] == 0:
            return False, None, "Cross moderation is disabled for this server."


        # Find linked account by mc_name (case-sensitive for now)
        cur = await db.conn.execute(
            "SELECT discord_id, mc_uuid FROM user_links WHERE guild_id=? AND mc_name=?",
            (guild.id, mc_name),
        )
        row = await cur.fetchone()
        if not row:
            return False, None, "Your Minecraft account is not linked to Discord. Use /linkdiscord."

        discord_id = int(row["discord_id"])
        mc_uuid = row["mc_uuid"]

        member: discord.Member | None = guild.get_member(discord_id)
        if member is None:
            try:
                member = await guild.fetch_member(discord_id)
            except Exception:
                member = None

        can_kick = can_ban = can_timeout = False
        prefix: str | None = None
        color_hex: str | None = None

        if member is not None:
            perms = member.guild_permissions

            can_kick = bool(perms.kick_members or perms.administrator)
            can_ban = bool(perms.ban_members or perms.administrator)
            # discord.py 2.x exposes moderate_members for timeout perms
            can_timeout = bool(getattr(perms, "moderate_members", False) or perms.administrator)

            # consider "staff" broadly: anyone who can do at least one of these or manage_guild/roles/messages
            is_staff = any(
                [
                    can_kick,
                    can_ban,
                    can_timeout,
                    perms.manage_guild,
                    perms.manage_roles,
                    getattr(perms, "moderate_members", False),
                    perms.manage_messages,
                    perms.administrator,
                ]
            )

            # ---- Color: highest-priority colored role ----
            roles = [r for r in member.roles if not r.is_default()]
            colored_roles = [r for r in roles if r.colour.value != 0]
            color_role = max(colored_roles, key=lambda r: r.position) if colored_roles else None
            if color_role is not None:
                color_hex = f"#{color_role.colour.value:06X}"

            # ---- Prefix role: hoisted role on sidebar, else color role, else top role ----
            if roles:
                hoisted_roles = [r for r in roles if r.hoist]
                if hoisted_roles:
                    prefix_role = max(hoisted_roles, key=lambda r: r.position)
                elif color_role is not None:
                    prefix_role = color_role
                else:
                    prefix_role = max(roles, key=lambda r: r.position)
                prefix = f"[{prefix_role.name}]"
            else:
                is_staff = False  # no roles, probably not staff

            # ---- Update cache with both perms and cosmetics ----
            await db.conn.execute(
                "INSERT OR REPLACE INTO mc_perm_cache("
                "guild_id, mc_uuid, mc_name, can_kick, can_ban, can_timeout, is_staff, prefix, color_hex, last_sync"
                ") VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    guild.id,
                    mc_uuid,
                    mc_name,
                    int(can_kick),
                    int(can_ban),
                    int(can_timeout),
                    int(is_staff),
                    prefix,
                    color_hex,
                    time.time(),
                ),
            )
            await db.conn.commit()
        else:
            # Fallback to cached perms & cosmetics if we can't resolve member (Discord outage etc.)
            cur = await db.conn.execute(
                "SELECT can_kick, can_ban, can_timeout, is_staff, prefix, color_hex "
                "FROM mc_perm_cache WHERE guild_id=? AND mc_uuid=?",
                (guild.id, mc_uuid),
            )
            rowc = await cur.fetchone()
            if rowc:
                can_kick = bool(rowc["can_kick"])
                can_ban = bool(rowc["can_ban"])
                can_timeout = bool(rowc["can_timeout"])
                # is_staff = bool(rowc["is_staff"])  # not used directly here
                prefix = rowc["prefix"]
                color_hex = rowc["color_hex"]
            else:
                return False, None, "Cannot resolve your Discord account right now."

        allowed = {
            "kick": can_kick,
            "ban": can_ban,
            "timeout": can_timeout,
        }.get(action, False)

        if not allowed:
            return False, member, "You are not allowed to perform that action from Minecraft."

        # prefix/color are now cached and will also be visible to the MC plugin via mc_perm_query.
        return True, member, None

    # ---------- Main listener ----------

    @commands.Cog.listener("on_message")
    async def handle_mc_mod_request(self, message: discord.Message):
        # Only process in-guild messages
        if message.guild is None:
            return

        # Only process messages coming from webhooks (MC side),
        # NOT from real users or the bot itself.
        if message.webhook_id is None:
            return

        content = (message.content or "").strip()
        if not content.startswith("!dc"):
            return

        parts = content.split()
        if len(parts) < 2:
            return

        cmd = parts[0].lower()       # !dcban, !dckick, !dctimeout, ...
        target_raw = parts[1]
        requester = message.author.name  # MC player name (webhook username)

        await db.ensure_connected()
        # (Optionally check guild_settings here if you want to toggle this feature.)

        target_member = await self._resolve_target_member(
            guild=message.guild,
            raw=target_raw,
            content=content,
            mentions=list(message.mentions),
        )

        if target_member is None:
            await message.channel.send(
                f"⚠️ MC requested moderation for `{target_raw}`, "
                f"but I couldn't resolve that user.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if cmd == "!dcban":
            ok, linked_member, deny_reason = await self._check_mc_permission(
                message.guild,
                requester,
                "ban",
            )
            if not ok:
                await message.channel.send(
                    f"❌ {deny_reason}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            reason = " ".join(parts[2:]) if len(parts) > 2 else f"Requested from MC by {requester}"
            await self._ban_user(message, target_member, requester, reason)

        elif cmd == "!dckick":
            ok, linked_member, deny_reason = await self._check_mc_permission(
                message.guild,
                requester,
                "kick",
            )
            if not ok:
                await message.channel.send(
                    f"❌ {deny_reason}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            reason = " ".join(parts[2:]) if len(parts) > 2 else f"Requested from MC by {requester}"
            await self._kick_user(message, target_member, requester, reason)

        elif cmd == "!dctimeout":
            # Syntax: !dctimeout <user> <minutes> [reason...]
            if len(parts) < 3:
                await message.channel.send(
                    "Usage: `!dctimeout <user> <minutes> [reason...]`",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            ok, linked_member, deny_reason = await self._check_mc_permission(
                message.guild,
                requester,
                "timeout",
            )
            if not ok:
                await message.channel.send(
                    f"❌ {deny_reason}",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            raw_minutes = parts[2]
            try:
                minutes = int(raw_minutes)
            except ValueError:
                await message.channel.send(
                    f"❌ `{raw_minutes}` is not a valid number of minutes.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            if minutes <= 0:
                await message.channel.send(
                    "❌ Minutes must be greater than 0.",
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            reason = (
                " ".join(parts[3:])
                if len(parts) > 3
                else f"Requested from MC by {requester}"
            )

            await self._timeout_user(message, target_member, requester, minutes, reason)



    # ---------- Actions ----------

    async def _ban_user(
        self,
        message: discord.Message,
        target: discord.Member,
        requester: str,
        reason: str,
    ):
        try:
            await message.guild.ban(target, reason=f"[MC] {reason}")
            await message.channel.send(
                f"🔨 Banned {target.mention} (requested by `{requester}` in Minecraft).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await message.channel.send("❌ I don't have permission to ban that user.")
        except Exception as e:
            await message.channel.send(f"❌ Ban failed: `{e}`")

    async def _kick_user(
        self,
        message: discord.Message,
        target: discord.Member,
        requester: str,
        reason: str,
    ):
        try:
            await target.kick(reason=f"[MC] {reason}")
            await message.channel.send(
                f"👢 Kicked {target.mention} (requested by `{requester}` in Minecraft).",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.Forbidden:
            await message.channel.send("❌ I don't have permission to kick that user.")
        except Exception as e:
            await message.channel.send(f"❌ Kick failed: `{e}`")

    async def _timeout_user(
        self,
        message: discord.Message,
        target: discord.Member,
        requester: str,
        minutes: int,
        reason: str,
    ):
        try:
            # discord.py 2.x: Member.timeout(duration: timedelta, reason=...)
            duration = timedelta(minutes=minutes)
            await target.timeout(duration, reason=f"[MC] {reason}")

            await message.channel.send(
                f"⏱️ Timed out {target.mention} for {minutes} minutes "
                f"(requested by `{requester}` in Minecraft).",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except AttributeError:
            # This means your installed discord.py does NOT have Member.timeout
            await message.channel.send(
                "❌ This bot library version doesn’t support native timeouts "
                "(no `Member.timeout`). Please upgrade discord.py to 2.x.",
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.Forbidden:
            await message.channel.send("❌ I don't have permission to timeout that user.")

        except Exception as e:
            await message.channel.send(f"❌ Timeout failed: `{e}`")

async def setup(bot: commands.Bot):
    await bot.add_cog(MCModBridge(bot))

