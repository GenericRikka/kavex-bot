import hashlib, time, os, re, typing, discord
from discord.ext import commands
from discord import app_commands
from ..db import db
from .. import mc_ws

PEPPER = os.getenv("MC_TOKEN_PEPPER", "")


def token_hash(tok: str) -> str:
    return hashlib.sha256((tok + PEPPER).encode("utf-8")).hexdigest()


HEX_RE = re.compile(r"^[0-9a-fA-F]+$")


class MinecraftLink(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Top-level slash group: /minecraft
    group = app_commands.Group(name="minecraft", description="Minecraft linking & moderation")

    async def _ensure_webhook_schema(self):
        await db.ensure_connected()
        # add webhook_url and thread_id if missing
        cur = await db.conn.execute("PRAGMA table_info(mc_webhooks)")
        cols = [row["name"] for row in await cur.fetchall()]
        q = []
        if "webhook_url" not in cols:
            q.append("ALTER TABLE mc_webhooks ADD COLUMN webhook_url TEXT")
        if "thread_id" not in cols:
            q.append("ALTER TABLE mc_webhooks ADD COLUMN thread_id INTEGER")
        for stmt in q:
            await db.conn.execute(stmt)
        if q:
            await db.conn.commit()

    # ---------- Linking / status ----------

    @group.command(
        name="connect",
        description="Link a channel or thread to a Minecraft server using its token",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        token="Paste token from plugins/KavexLink/secret.txt",
        channel="Target text channel or thread",
        crossmod="Use Discord roles for MC moderation & in-game styling (default: enabled)",
    )
    async def connect(
        self,
        interaction: discord.Interaction,
        token: str,
        channel: typing.Union[discord.TextChannel, discord.Thread],
        crossmod: bool = True,
    ):
        tok = token.strip()
        if not HEX_RE.match(tok):
            await interaction.response.send_message(
                "Token looks malformed. Paste exactly the contents of `plugins/KavexLink/secret.txt`.",
                ephemeral=True,
            )
            return

        await self._ensure_webhook_schema()

        await db.ensure_connected()
        # Ensure guild_settings row exists
        await db.conn.execute(
            "INSERT OR IGNORE INTO guild_settings(guild_id) VALUES(?)",
            (interaction.guild_id,),
        )
        # Store crossmod toggle (guild-wide)
        await db.conn.execute(
            "UPDATE guild_settings SET crossmod_enabled=? WHERE guild_id=?",
            (1 if crossmod else 0, interaction.guild_id),
        )
        await db.conn.commit()

        # Determine where the webhook must live:
        parent_channel: discord.TextChannel
        thread_id: int | None = None
        if isinstance(channel, discord.TextChannel):
            parent_channel = channel
        elif isinstance(channel, discord.Thread):
            if isinstance(channel.parent, discord.TextChannel):
                parent_channel = channel.parent
                thread_id = channel.id
            else:
                await interaction.response.send_message(
                    "Unsupported thread parent type for webhook posting.",
                    ephemeral=True,
                )
                return
        else:
            await interaction.response.send_message(
                "Unsupported channel type. Use a text channel or a thread.",
                ephemeral=True,
            )
            return

        # Create or reuse webhook on the parent text channel
        webhook_url: str | None = None
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT webhook_url FROM mc_webhooks WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, parent_channel.id),
        )
        row = await cur.fetchone()
        if row and row["webhook_url"]:
            webhook_url = row["webhook_url"]
        else:
            try:
                wh = await parent_channel.create_webhook(name="Kavex MC Link")
                webhook_url = wh.url
                await db.conn.execute(
                    "INSERT OR REPLACE INTO mc_webhooks(guild_id, channel_id, webhook_url, thread_id) "
                    "VALUES (?,?,?,NULL)",
                    (interaction.guild_id, parent_channel.id, webhook_url),
                )
                await db.conn.commit()
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"❌ I need **Manage Webhooks** in {parent_channel.mention}.",
                    ephemeral=True,
                )
                return
            except Exception as e:
                await interaction.response.send_message(
                    f"❌ Failed creating webhook in {parent_channel.mention}: `{e}`",
                    ephemeral=True,
                )
                return

        # Store the link (the linked target may be a thread or a text channel)
        th = token_hash(tok)
        await db.conn.execute(
            "INSERT OR REPLACE INTO mc_links(guild_id, channel_id, token_hash, last_seen, status) "
            "VALUES (?,?,?,?,?)",
            (interaction.guild_id, channel.id, th, time.time(), "pending"),
        )
        # Also store thread_id mapping (if the link target is a thread)
        if thread_id:
            await db.conn.execute(
                "UPDATE mc_webhooks SET thread_id=? WHERE guild_id=? AND channel_id=?",
                (thread_id, interaction.guild_id, parent_channel.id),
            )
            await db.conn.commit()

        await db.conn.commit()
        mode = "with cross-moderation enabled" if crossmod else "in chat-only mode (no Discord perms/prefixes)"
        await interaction.response.send_message(
            f"Linked to {channel.mention} ({mode}). Waiting for server auth… (hash `{th[:12]}`)",
            ephemeral=False,
        )

    # ---------- Moderation from Discord -> MC ----------

    @group.command(
        name="kick",
        description="Kick an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Exact Minecraft player name",
        reason="Optional reason to show in-game",
    )
    async def kick(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str | None = None,
    ):
        reason_str = reason or "Kicked by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="kick",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **kick** for `{player}` to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    @group.command(
        name="ban",
        description="Ban an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Exact Minecraft player name",
        reason="Optional reason to show in-game",
    )
    async def ban(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str | None = None,
    ):
        reason_str = reason or "Banned by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="ban",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **ban** for `{player}` to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    @group.command(
        name="tempban",
        description="Temporarily ban an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Exact Minecraft player name",
        minutes="Duration of the ban in minutes (>=1)",
        reason="Optional reason to show in-game",
    )
    async def tempban(
        self,
        interaction: discord.Interaction,
        player: str,
        minutes: app_commands.Range[int, 1, 60_000],
        reason: str | None = None,
    ):
        reason_str = reason or "Temporarily banned by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="tempban",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
            minutes=minutes,
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **tempban** for `{player}` ({minutes} minute(s)) "
                f"to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    @group.command(
        name="mute",
        description="Mute an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Exact Minecraft player name",
        minutes="Duration of the mute in minutes (>=1)",
        reason="Optional reason to show in-game",
    )
    async def mute(
        self,
        interaction: discord.Interaction,
        player: str,
        minutes: app_commands.Range[int, 1, 60_000],
        reason: str | None = None,
    ):
        reason_str = reason or "Muted by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="mute",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
            minutes=minutes,
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **mute** for `{player}` ({minutes} minute(s)) "
                f"to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    @group.command(
        name="pardon",
        description="Pardon (unban) an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Minecraft name of the player to unban",
        reason="Optional unban reason to log",
    )
    async def pardon(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str | None = None,
    ):
        reason_str = reason or "Unbanned by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="pardon",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
            minutes=0,
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **pardon** for `{player}` to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    @group.command(
        name="unmute",
        description="Unmute an in-game player via the Minecraft server",
    )
    @app_commands.default_permissions(administrator=True)
    @app_commands.describe(
        player="Minecraft name of the player to unmute",
        reason="Optional unmute reason to log",
    )
    async def unmute(
        self,
        interaction: discord.Interaction,
        player: str,
        reason: str | None = None,
    ):
        reason_str = reason or "Unmuted by Discord admin"
        delivered = await mc_ws.send_dc_admin(
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            action="unmute",
            player=player,
            reason=reason_str,
            issued_by=str(interaction.user),
            minutes=0,
        )

        if delivered > 0:
            await interaction.response.send_message(
                f"✅ Sent **unmute** for `{player}` to {delivered} linked server(s).",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ No active linked Minecraft server for this channel.",
                ephemeral=True,
            )

    # ---------- Link management / debug ----------

    @group.command(
        name="disconnect",
        description="Unlink the Minecraft server from this channel/thread",
    )
    @app_commands.default_permissions(administrator=True)
    async def disconnect(
        self,
        interaction: discord.Interaction,
        channel: typing.Union[discord.TextChannel, discord.Thread],
    ):
        await db.ensure_connected()
        await db.conn.execute(
            "DELETE FROM mc_links WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, channel.id),
        )
        # Do not delete webhook (could be reused); but clear thread_id if it pointed to this thread
        await db.conn.execute(
            "UPDATE mc_webhooks SET thread_id=NULL WHERE guild_id=? AND thread_id=?",
            (interaction.guild_id, getattr(channel, "id", None)),
        )
        await db.conn.commit()
        await interaction.response.send_message(
            f"Unlinked {channel.mention}.",
            ephemeral=False,
        )

    @group.command(
        name="status",
        description="Show link status for a channel/thread",
    )
    @app_commands.default_permissions(administrator=True)
    async def status(
        self,
        interaction: discord.Interaction,
        channel: typing.Union[discord.TextChannel, discord.Thread],
    ):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT server_name, status, last_seen "
            "FROM mc_links WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, channel.id),
        )
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message(
                "No link configured.",
                ephemeral=False,
            )
            return
        # crossmod flag (guild-wide)
        ccur = await db.conn.execute(
            "SELECT crossmod_enabled FROM guild_settings WHERE guild_id=?",
            (interaction.guild_id,),
        )
        grow = await ccur.fetchone()
        crossmod = (not grow) or (grow["crossmod_enabled"] != 0)

        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["last_seen"])) if row["last_seen"] else "never"
        mode = "cross-moderation **ON**" if crossmod else "cross-moderation **OFF** (chat-only)"
        await interaction.response.send_message(
            f"{channel.mention} ↔ **{row['server_name'] or 'Minecraft'}**: **{row['status']}**, "
            f"last seen {last} — {mode}.",
            ephemeral=False,
        )

    @group.command(
        name="debug_hash",
        description="(Admin) Show the computed token hash for troubleshooting",
    )
    @app_commands.default_permissions(administrator=True)
    async def debug_hash(self, interaction: discord.Interaction, token: str):
        tok = token.strip()
        th = token_hash(tok)
        await interaction.response.send_message(
            f"sha256(token+pepper) = `{th}`",
            ephemeral=True,
        )

    @group.command(
        name="debug_links",
        description="(Admin) Show stored links for this guild",
    )
    @app_commands.default_permissions(administrator=True)
    async def debug_links(self, interaction: discord.Interaction):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT channel_id, server_name, status "
            "FROM mc_links WHERE guild_id=?",
            (interaction.guild_id,),
        )
        rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message(
                "No mc_links rows for this guild.",
                ephemeral=True,
            )
            return
        lines = []
        for r in rows:
            cid, sname, st = int(r["channel_id"]), r["server_name"], r["status"]
            ch = interaction.guild.get_channel(cid)
            chname = ch.mention if ch else f"<#{cid}> (not cached)"
            lines.append(
                f"- {chname}: status **{st}**, server **{sname or 'Minecraft'}**"
            )
        await interaction.response.send_message(
            "\n".join(lines),
            ephemeral=True,
        )

    @group.command(
        name="test_send",
        description="(Admin) Try sending a test line to a channel/thread (via stored webhook)",
    )
    @app_commands.default_permissions(administrator=True)
    async def test_send(
        self,
        interaction: discord.Interaction,
        channel: typing.Union[discord.TextChannel, discord.Thread],
    ):
        # resolve parent webhook + thread_id like mc_ws does
        await self._ensure_webhook_schema()
        parent_id = (
            channel.id
            if isinstance(channel, discord.TextChannel)
            else channel.parent.id  # type: ignore
        )

        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT webhook_url, thread_id "
            "FROM mc_webhooks WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, parent_id),
        )
        row = await cur.fetchone()
        if not row or not row["webhook_url"]:
            await interaction.response.send_message(
                "No webhook stored for this channel parent. Re-run /minecraft connect.",
                ephemeral=True,
            )
            return

        try:
            await channel.send("KavexLink: test message")
            await interaction.response.send_message(
                f"Sent test to {channel.mention}.",
                ephemeral=True,
            )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Send failed: `{e}`",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    cog = MinecraftLink(bot)
    await bot.add_cog(cog)
    # Register /minecraft group once
    if bot.tree.get_command("minecraft") is None:
        bot.tree.add_command(cog.group)

