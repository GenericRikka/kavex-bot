import hashlib, time, os, discord, re
from discord.ext import commands
from discord import app_commands
from ..db import db

PEPPER = os.getenv("MC_TOKEN_PEPPER", "")

def token_hash(tok: str) -> str:
    return hashlib.sha256((tok + PEPPER).encode("utf-8")).hexdigest()

HEX_RE = re.compile(r"^[0-9a-fA-F]+$")

class MinecraftLink(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    group = app_commands.Group(name="minecraft", description="Minecraft linking")

    @group.command(name="connect", description="Link this channel to a Minecraft server using its token")
    @app_commands.default_permissions(administrator=True)
    async def connect(self, interaction: discord.Interaction, token: str, channel: discord.TextChannel):
        # Normalize: trim whitespace/newlines
        tok = token.strip()
        # Optional: validate (your generator is hex-only)
        if not HEX_RE.match(tok):
            await interaction.response.send_message(
                "Token looks malformed. Please paste exactly the contents of `plugins/KavexLink/secret.txt`.", ephemeral=True
            )
            return

        await db.ensure_connected()
        th = token_hash(tok)
        await db.conn.execute(
            "INSERT OR REPLACE INTO mc_links(guild_id, channel_id, token_hash, last_seen, status) VALUES (?,?,?,?,?)",
            (interaction.guild_id, channel.id, th, time.time(), "pending"),
        )
        await db.conn.commit()
        # Short hash for logs
        short = th[:12]
        await interaction.response.send_message(
            f"Linked to {channel.mention}. Waiting for server auth… (hash `{short}`)", ephemeral=False
        )

    @group.command(name="disconnect", description="Unlink the Minecraft server from this channel")
    @app_commands.default_permissions(administrator=True)
    async def disconnect(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.ensure_connected()
        await db.conn.execute(
            "DELETE FROM mc_links WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, channel.id),
        )
        await db.conn.execute(
            "DELETE FROM mc_webhooks WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, channel.id),
        )
        await db.conn.commit()
        await interaction.response.send_message(f"Unlinked {channel.mention}.", ephemeral=False)

    @group.command(name="debug_links", description="(Admin) Show stored links for this guild")
    @app_commands.default_permissions(administrator=True)
    async def debug_links(self, interaction: discord.Interaction):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT guild_id, channel_id, server_name, status FROM mc_links WHERE guild_id=?",
            (interaction.guild_id,),
        )
        rows = await cur.fetchall()
        if not rows:
            await interaction.response.send_message("No mc_links rows for this guild.", ephemeral=True)
            return
        lines = []
        for r in rows:
            gid, cid, sname, st = r["guild_id"], r["channel_id"], r["server_name"], r["status"]
            ch = interaction.guild.get_channel(cid)
            chname = f"#{ch.name}" if ch else f"<#{cid}> (not cached)"
            lines.append(f"- {chname}: status **{st}**, server **{sname or 'Minecraft'}**")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


    @group.command(name="status", description="Show link status for this channel")
    @app_commands.default_permissions(administrator=True)
    async def status(self, interaction: discord.Interaction, channel: discord.TextChannel):
        await db.ensure_connected()
        cur = await db.conn.execute(
            "SELECT server_name, status, last_seen FROM mc_links WHERE guild_id=? AND channel_id=?",
            (interaction.guild_id, channel.id)
        )
        row = await cur.fetchone()
        if not row:
            await interaction.response.send_message("No link configured.", ephemeral=False)
            return
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(row["last_seen"])) if row["last_seen"] else "never"
        await interaction.response.send_message(
            f"{channel.mention} ↔ **{row['server_name'] or 'Minecraft'}**: **{row['status']}**, last seen {last}.",
            ephemeral=False
        )

    # Optional: debugging aid to compare hashes safely (admins only, ephemeral)
    @group.command(name="debug_hash", description="(Admin) Show the computed token hash for troubleshooting")
    @app_commands.default_permissions(administrator=True)
    async def debug_hash(self, interaction: discord.Interaction, token: str):
        tok = token.strip()
        th = token_hash(tok)
        await interaction.response.send_message(f"sha256(token+pepper) = `{th}`", ephemeral=True)

async def setup(bot):
    cog = MinecraftLink(bot)
    await bot.add_cog(cog)
    if bot.tree.get_command("minecraft") is None:
        bot.tree.add_command(cog.group)

