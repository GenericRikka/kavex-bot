import time
import typing

import discord
from discord.ext import commands
from discord import app_commands

from ..db import db


class MCIdentity(commands.Cog):
    """
    - /linkdiscord <code>: link your MC account (from /linkdiscord in-game) to this Discord user.
    - /mcperms_set: map a Discord role to MC moderation perms + prefix/color.
    - /mcperms_list: show current mappings.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- Account linking ----------

    @app_commands.command(
        name="linkdiscord",
        description="Link your Minecraft account using a code from /linkdiscord in-game."
    )
    @app_commands.describe(
        code="Code shown in Minecraft after running /linkdiscord"
    )
    async def linkdiscord(self, interaction: discord.Interaction, code: str):
        if interaction.guild is None:
            await interaction.response.send_message(
                "You must run this inside a server, not in DMs.",
                ephemeral=True,
            )
            return

        token = code.strip()
        if not token:
            await interaction.response.send_message(
                "Please provide a valid link code.",
                ephemeral=True,
            )
            return

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT mc_uuid, mc_name, created_at, used FROM link_tokens WHERE token=?",
            (token,),
        )
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "❌ Unknown or expired code. Run `/linkdiscord` in Minecraft again.",
                ephemeral=True,
            )
            return

        if row["used"]:
            await interaction.response.send_message(
                "❌ This code has already been used. Run `/linkdiscord` in Minecraft again.",
                ephemeral=True,
            )
            return

        mc_uuid = row["mc_uuid"]
        mc_name = row["mc_name"] or "(unknown)"

        # (Optional) enforce max age
        created_at = row["created_at"] or 0
        if created_at and time.time() - created_at > 3600 * 24:
            await interaction.response.send_message(
                "❌ This code is more than 24h old. Please generate a new one in-game.",
                ephemeral=True,
            )
            return

        # Store / update link
        await db.conn.execute(
            "INSERT OR REPLACE INTO user_links(guild_id, discord_id, mc_uuid, mc_name, linked_at) "
            "VALUES (?,?,?,?,strftime('%s','now'))",
            (interaction.guild_id, interaction.user.id, mc_uuid, mc_name),
        )
        await db.conn.execute(
            "UPDATE link_tokens SET used=1 WHERE token=?",
            (token,),
        )
        await db.conn.commit()

        await interaction.response.send_message(
            f"✅ Linked **{mc_name}** to {interaction.user.mention}.",
            ephemeral=True,
        )

    @app_commands.guild_only()
    @app_commands.command(
        name="mention_notify",
        description="Enable or disable Minecraft ping sounds when someone @mentions your MC name in Discord."
    )
    async def mention_notify(
        self,
        interaction: discord.Interaction,
        enabled: bool
    ):
        await db.ensure_connected()

        # Find this user's linked MC account for this guild
        cur = await db.conn.execute(
            "SELECT mc_name FROM user_links WHERE guild_id=? AND discord_id=?",
            (interaction.guild_id, interaction.user.id),
        )
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "You don’t have a linked Minecraft account on this server. "
                "Use `/linkdiscord` in-game first.",
                ephemeral=True,
            )
            return

        mc_name = row["mc_name"]

        await db.conn.execute(
            "UPDATE user_links SET notify_ping=? "
            "WHERE guild_id=? AND discord_id=?",
            (1 if enabled else 0, interaction.guild_id, interaction.user.id),
        )
        await db.conn.commit()

        await interaction.response.send_message(
            f"Minecraft mention ping has been **{'enabled' if enabled else 'disabled'}** "
            f"for **{mc_name}**.",
            ephemeral=True,
        )

    # ---------- Role → perm mapping ----------

    @app_commands.command(
        name="mcperms_set",
        description="Set Minecraft moderation perms + prefix/color for a Discord role."
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        role="Discord role to configure",
        can_kick="Allow this role to request kicks from Minecraft",
        can_ban="Allow this role to request bans from Minecraft",
        can_timeout="Allow this role to request timeouts from Minecraft",
        is_staff="Mark as staff (for prefixes / grouping)",
        prefix="Optional chat prefix (e.g. [Mod])",
        color_hex="Optional hex color (e.g. #FF5555)"
    )
    async def mcperms_set(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        can_kick: bool = False,
        can_ban: bool = False,
        can_timeout: bool = False,
        is_staff: bool = False,
        prefix: typing.Optional[str] = None,
        color_hex: typing.Optional[str] = None,
    ):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this inside a server.",
                ephemeral=True,
            )
            return

        await db.ensure_connected()
        await db.conn.execute(
            "INSERT OR REPLACE INTO mc_perms("
            "guild_id, role_id, can_kick, can_ban, can_timeout, is_staff, prefix, color_hex"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                interaction.guild_id,
                role.id,
                int(can_kick),
                int(can_ban),
                int(can_timeout),
                int(is_staff),
                prefix,
                color_hex,
            ),
        )
        await db.conn.commit()

        await interaction.response.send_message(
            f"✅ Updated MC perms for role {role.mention}:\n"
            f"- kick={can_kick}, ban={can_ban}, timeout={can_timeout}, staff={is_staff}\n"
            f"- prefix={prefix!r}, color={color_hex!r}",
            ephemeral=True,
        )

    @app_commands.command(
        name="mcperms_list",
        description="List configured Minecraft permission mappings for this guild."
    )
    @app_commands.default_permissions(administrator=True)
    async def mcperms_list(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "Run this inside a server.",
                ephemeral=True,
            )
            return

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT role_id, can_kick, can_ban, can_timeout, is_staff, prefix, color_hex "
            "FROM mc_perms WHERE guild_id=?",
            (interaction.guild_id,),
        )
        rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No MC permission mappings configured yet.",
                ephemeral=True,
            )
            return

        lines: list[str] = []
        for r in rows:
            rid = int(r["role_id"])
            role = interaction.guild.get_role(rid)
            label = role.mention if role else f"<@&{rid}> (missing)"
            lines.append(
                f"{label}: kick={bool(r['can_kick'])}, "
                f"ban={bool(r['can_ban'])}, timeout={bool(r['can_timeout'])}, "
                f"staff={bool(r['is_staff'])}, "
                f"prefix={r['prefix']!r}, color={r['color_hex']!r}"
            )

        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(MCIdentity(bot))

