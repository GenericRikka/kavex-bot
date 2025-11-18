import asyncio
import discord
import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, List, Tuple, Optional

from aiohttp import web, ClientSession
from discord.ext import commands
from .db import db

logging.info("[BOOT] mc_ws module imported")

MC_WS_PORT = int(os.getenv("MC_WS_PORT", "8765"))
MC_TOKEN_PEPPER = os.getenv("MC_TOKEN_PEPPER", "")

MC_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{2,32})")
log = logging.getLogger(__name__)

# token_hash -> {"ws": WebSocketResponse, "server": str, "ts": float}
_connections: Dict[str, Dict] = {}

# Discord readiness (set by main.on_ready())
_ready_evt = asyncio.Event()


def mark_discord_ready():
    if not _ready_evt.is_set():
        _ready_evt.set()
        logging.info("[BOOT] mc_ws received discord-ready signal")


async def _wait_ready():
    await _ready_evt.wait()


# Give Plugin access to Bot
_bot: commands.Bot | None = None


def set_bot(bot: commands.Bot) -> None:
    global _bot
    _bot = bot
    logging.info("[BOOT] mc_ws stored bot reference for perm refresh")


# Buffers for early events (delivered after ready)
_pending_notifies: List[Tuple[str, str]] = []
_pending_mc_msgs: List[Tuple[str, dict]] = []
_pending_flush_started = False

# local HTTP session for webhook posts (avoid discord.py HTTP client entirely)
_http_session: Optional[ClientSession] = None


def _http() -> ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        _http_session = ClientSession()
    return _http_session

import logging
log = logging.getLogger(__name__)

MC_MENTION_RE = re.compile(r"@([A-Za-z0-9_]{2,32})")
log = logging.getLogger(__name__)

async def _apply_mc_mentions_to_discord(token_hash: str | None, text: str) -> str:
    """
    Scan MC text for @Name patterns and, if Name matches a *Discord* user in the
    linked guild (and that user has a linked MC account), convert to a real
    Discord mention <@discord_id>.
    """
    if not token_hash or "@" not in text:
        return text

    matches = list(MC_MENTION_RE.finditer(text))
    if not matches:
        return text

    await db.ensure_connected()

    # 1) Find guild for this token_hash
    cur = await db.conn.execute(
        "SELECT guild_id FROM mc_links WHERE token_hash=? LIMIT 1",
        (token_hash,),
    )
    row = await cur.fetchone()
    if not row:
        return text

    guild_id = int(row["guild_id"])

    if _bot is None:
        log.warning("MC mention: _bot is None, cannot resolve Discord members")
        return text

    guild: discord.Guild | None = _bot.get_guild(guild_id)
    if guild is None:
        log.warning("MC mention: bot not in guild_id=%s", guild_id)
        return text

    # 2) Build a lookup of Discord members by name/display name/global name (lowercased)
    name_to_member: dict[str, discord.Member] = {}
    for m in guild.members:
        candidates = set()
        candidates.add(m.name.lower())
        if m.display_name:
            candidates.add(m.display_name.lower())
        gn = getattr(m, "global_name", None)
        if gn:
            candidates.add(gn.lower())

        for n in candidates:
            # don't overwrite if already mapped; first seen wins
            name_to_member.setdefault(n, m)

    # 3) Get list of linked discord_ids in this guild (for safety: only ping linked users)
    cur = await db.conn.execute(
        "SELECT discord_id FROM user_links WHERE guild_id=?",
        (guild_id,),
    )
    linked_ids = {int(r["discord_id"]) for r in await cur.fetchall()}

    if not linked_ids:
        # no linked accounts => no special mapping
        return text

    # 4) Build text -> discord_id mapping for any @Name in the message
    names_in_text = {m.group(1) for m in matches}
    log.info("MC mention: candidate tokens from text=%r in guild=%s", names_in_text, guild_id)

    token_to_id: dict[str, int] = {}
    for raw_name in names_in_text:
        member = name_to_member.get(raw_name.lower())
        if not member:
            log.info("MC mention: no Discord member matching %r in guild=%s", raw_name, guild_id)
            continue

        if member.id not in linked_ids:
            log.info(
                "MC mention: member %s (%s) matched %r but has no user_links row; skipping",
                member, member.id, raw_name,
            )
            continue

        token_to_id[raw_name.lower()] = member.id
        log.info(
            "MC mention mapping: text-name=%r -> discord_id=%s",
            raw_name, member.id,
        )

    if not token_to_id:
        log.info("MC mention: no names in text resolved to linked Discord users.")
        return text

    # 5) Replace @Name with <@id>
    def repl(m: re.Match) -> str:
        original = m.group(0)  # "@Name"
        name = m.group(1)
        did = token_to_id.get(name.lower())
        if not did:
            return original
        return f"<@{did}>"

    new_text = re.sub(MC_MENTION_RE, repl, text)
    log.info("MC mention: final text=%r (original=%r)", new_text, text)
    return new_text

async def _http_shutdown():
    global _http_session
    if _http_session and not _http_session.closed:
        try:
            await _http_session.close()
        except Exception:
            pass
        _http_session = None


async def _flush_pending():
    global _pending_notifies, _pending_mc_msgs
    await _wait_ready()
    if _pending_notifies:
        for th, text in _pending_notifies:
            try:
                await _notify_bound_channels(th, text)
            except Exception as e:
                logging.warning("pending notify failed: %s", e)
        _pending_notifies = []
    if _pending_mc_msgs:
        for th, data in _pending_mc_msgs:
            try:
                # route based on op
                op = data.get("op")
                if op == "mc_chat":
                    await _relay_mc_to_discord(th, data)
                elif op == "mc_event":
                    await _relay_mc_event_to_discord(th, data)
                elif op == "mc_mod":
                    await _relay_mc_mod_to_discord(th, data)
            except Exception as e:
                logging.warning("pending relay failed: %s", e)
        _pending_mc_msgs = []


def _token_hash(token: str) -> str:
    h = hashlib.sha256()
    h.update((token.strip() + MC_TOKEN_PEPPER).encode("utf-8"))
    return h.hexdigest()


# ---------- Webhook resolution helpers ----------

async def _lookup_webhook_for_link(
    guild_id: int, linked_channel_id: int
) -> Tuple[Optional[str], Optional[int]]:
    """
    Return (webhook_url, thread_id).
    We store webhook per **parent text channel**; for thread links we store thread_id in mc_webhooks.
    First try exact parent match (channel_id == linked_channel_id). If none, try thread_id == linked_channel_id.
    """
    await db.ensure_connected()
    # exact channel match
    cur = await db.conn.execute(
        "SELECT webhook_url, thread_id FROM mc_webhooks WHERE guild_id=? AND channel_id=?",
        (guild_id, linked_channel_id),
    )
    row = await cur.fetchone()
    if row and row["webhook_url"]:
        return (row["webhook_url"], row["thread_id"])
    # thread mapping (linked channel is a thread; find parent webhook)
    cur = await db.conn.execute(
        "SELECT webhook_url, thread_id FROM mc_webhooks WHERE guild_id=? AND thread_id=?",
        (guild_id, linked_channel_id),
    )
    row = await cur.fetchone()
    if row and row["webhook_url"]:
        return (row["webhook_url"], row["thread_id"])
    return (None, None)


async def _post_webhook(
    wh_url: str,
    content: Optional[str] = None,
    username: Optional[str] = None,
    avatar_url: Optional[str] = None,
    thread_id: Optional[int] = None,
) -> None:
    """
    Post to webhook using our own aiohttp session.
    """
    url = wh_url
    if thread_id:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}thread_id={thread_id}"
    payload: Dict[str, object] = {}
    if content is not None:
        payload["content"] = content
    if username is not None:
        payload["username"] = username
    if avatar_url is not None:
        payload["avatar_url"] = avatar_url
    async with _http().post(url, json=payload) as resp:
        if resp.status >= 300:
            txt = await resp.text()
            raise RuntimeError(f"webhook post failed: HTTP {resp.status} {txt}")


# ---------- WS Handlers ----------

async def ws_handler(request: web.Request):
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(request)

    token_hash: Optional[str] = None
    server_name = "Minecraft"

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    await ws.send_json({"op": "error", "err": "bad_json"})
                    continue

                op = data.get("op")

                if op == "auth":
                    token = (data.get("token") or "").strip()
                    server_name = data.get("server") or server_name
                    if not token:
                        await ws.send_json({"op": "auth", "ok": False, "err": "missing token"})
                        continue

                    token_hash = _token_hash(token)
                    _connections[token_hash] = {
                        "ws": ws,
                        "server": server_name,
                        "ts": time.time(),
                    }
                    await ws.send_json({"op": "auth", "ok": True})

                    await db.ensure_connected()
                    cur = await db.conn.execute(
                        "UPDATE mc_links SET status='connected', server_name=?, last_seen=? WHERE token_hash=?",
                        (server_name, time.time(), token_hash),
                    )
                    await db.conn.commit()

                    _pending_notifies.append(
                        (token_hash, f"🟢 **{server_name}** connected.")
                    )
                    logging.info(
                        "MC auth ok: server=%s short_hash=%s row_update=%s (notify buffered until ready)",
                        server_name,
                        token_hash[:12],
                        getattr(cur, "rowcount", None),
                    )

                    global _pending_flush_started
                    if not _pending_flush_started:
                        _pending_flush_started = True
                        asyncio.create_task(_flush_pending())

                elif op == "mc_chat":
                    if _ready_evt.is_set():
                        await _relay_mc_to_discord(token_hash, data)
                    else:
                        _pending_mc_msgs.append((token_hash, data))

                elif op == "mc_event":
                    if _ready_evt.is_set():
                        await _relay_mc_event_to_discord(token_hash, data)
                    else:
                        _pending_mc_msgs.append((token_hash, data))

                elif op == "mc_perm_query":
                    mc_uuid = (data.get("uuid") or "").strip().lower()
                    if not mc_uuid:
                        await ws.send_json({"op": "mc_permset", "uuid": mc_uuid})
                        continue

                    # First, try to refresh from live Discord data (if linked + bot has access)
                    profile = await _recompute_perm_profile_for_uuid(mc_uuid)

                    # If refresh failed (no link / no bot / etc.), fall back to cache
                    if profile is None:
                        await db.ensure_connected()
                        cur = await db.conn.execute(
                            "SELECT prefix, color_hex, can_kick, can_ban, can_timeout, is_staff "
                            "FROM mc_perm_cache "
                            "WHERE mc_uuid=? ORDER BY last_sync DESC LIMIT 1",
                            (mc_uuid,),
                        )
                        row = await cur.fetchone()
                        if row:
                            profile = {
                                "prefix": row["prefix"],
                                "color_hex": row["color_hex"],
                                "can_kick": int(row["can_kick"]),
                                "can_ban": int(row["can_ban"]),
                                "can_timeout": int(row["can_timeout"]),
                                "is_staff": int(row["is_staff"]),
                            }

                    resp: dict[str, object] = {"op": "mc_permset", "uuid": mc_uuid}
                    if profile:
                        if profile.get("prefix") is not None:
                            resp["prefix"] = profile["prefix"]
                        if profile.get("color_hex") is not None:
                            resp["color"] = profile["color_hex"]
                        resp["can_kick"] = profile.get("can_kick", 0)
                        resp["can_ban"] = profile.get("can_ban", 0)
                        resp["can_timeout"] = profile.get("can_timeout", 0)
                        resp["is_staff"] = profile.get("is_staff", 0)

                    try:
                        await ws.send_json(resp)
                    except Exception as e:
                        logging.warning(
                            "perm_query: failed sending response for uuid=%s: %s",
                            mc_uuid,
                            e,
                        )

                elif op == "mc_link_request":
                    # Player requested a Discord link in-game
                    code = (data.get("code") or "").strip()
                    mc_uuid = (data.get("uuid") or "").strip()
                    mc_name = (data.get("name") or "").strip()

                    if not code or not mc_name:
                        await ws.send_json(
                            {
                                "op": "mc_link_ack",
                                "ok": False,
                                "err": "missing code or name",
                            }
                        )
                        continue

                    await db.ensure_connected()
                    now = time.time()
                    await db.conn.execute(
                        "INSERT OR REPLACE INTO link_tokens(token, mc_uuid, mc_name, created_at, used) "
                        "VALUES (?,?,?,?,0)",
                        (code, mc_uuid, mc_name, now),
                    )
                    await db.conn.commit()

                    await ws.send_json({"op": "mc_link_ack", "ok": True})
                    logging.info(
                        "Stored link token %s for player %s uuid=%s",
                        code,
                        mc_name,
                        mc_uuid,
                    )

                elif op == "mc_mod":
                    # MC-side moderation action (kick/ban/tempban/mute)
                    if _ready_evt.is_set():
                        await _relay_mc_mod_to_discord(token_hash, data)
                    else:
                        _pending_mc_msgs.append((token_hash, data))

            elif msg.type == web.WSMsgType.ERROR:
                logging.warning("mc ws error: %s", ws.exception())

    finally:
        if token_hash:
            await db.ensure_connected()
            await db.conn.execute(
                "UPDATE mc_links SET status='disconnected', last_seen=? WHERE token_hash=?",
                (time.time(), token_hash),
            )
            await db.conn.commit()
            _pending_notifies.append(
                (token_hash, f"🔴 **{server_name}** disconnected. Waiting for reconnect…")
            )
            st = _connections.get(token_hash)
            if st and st.get("ws") is ws:
                _connections.pop(token_hash, None)

    return ws


async def send_dc_chat(
    guild_id: int,
    channel_id: int,
    user: str,
    guild_name: str,
    text: str,
    prefix: str | None = None,
    color: str | None = None,
) -> int:
    """
    Discord -> MC (over WS).
    Optionally includes Discord-derived prefix and color.
    """
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT token_hash FROM mc_links WHERE guild_id=? AND channel_id=? AND status='connected'",
        (guild_id, channel_id),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info(
            "dc_chat: no active links for guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return 0

    delivered = 0
    payload_dict: dict[str, object] = {
        "op": "dc_chat",
        "user": user,
        "guild": guild_name,
        "text": text,
    }
    if prefix is not None:
        payload_dict["prefix"] = prefix
    if color is not None:
        payload_dict["color"] = color

    payload = json.dumps(payload_dict)

    for r in rows:
        th = r["token_hash"]
        st = _connections.get(th)
        ws = st.get("ws") if st else None
        if ws:
            try:
                await ws.send_str(payload)
                delivered += 1
            except Exception as e:
                logging.warning(
                    "dc_chat: send failed for hash=%s: %s",
                    th[:12],
                    e,
                )
    logging.info("dc_chat: delivered=%d", delivered)
    return delivered


async def send_dc_admin(
    guild_id: int,
    channel_id: int,
    action: str,
    player: str,
    reason: str,
    issued_by: str,
    minutes: int = 0,
) -> int:
    """
    Discord -> MC admin command (kick/ban/tempban/mute/pardon/unmute/etc.).
    Sent as op='dc_admin' over the existing WebSocket link.
    """
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT token_hash FROM mc_links WHERE guild_id=? AND channel_id=? AND status='connected'",
        (guild_id, channel_id),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info(
            "dc_admin: no active links for guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return 0

    payload = json.dumps({
        "op": "dc_admin",
        "action": action,            # "kick", "ban", "tempban", "mute", "pardon", "unmute", "command", etc.
        "player": player,
        "reason": reason,
        "issued_by": issued_by,      # "Name#1234" or display_name
        "minutes": minutes,
    })

    delivered = 0
    for r in rows:
        th = r["token_hash"]
        st = _connections.get(th)
        ws = st.get("ws") if st else None
        if ws:
            try:
                await ws.send_str(payload)
                delivered += 1
            except Exception as e:
                logging.warning(
                    "dc_admin: send failed for hash=%s: %s",
                    th[:12],
                    e,
                )

    logging.info(
        "dc_admin: action=%s player=%s delivered=%d",
        action,
        player,
        delivered,
    )
    return delivered

async def send_dc_notify(
    guild_id: int,
    channel_id: int,
    mc_name: str,
) -> int:
    """
    Discord -> MC ping notification.

    Sent as op='dc_notify' over the existing WebSocket link. The MC plugin
    then decides whether the target is online and plays the sound.
    """
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT token_hash FROM mc_links "
        "WHERE guild_id=? AND channel_id=? AND status='connected'",
        (guild_id, channel_id),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info(
            "dc_notify: no active links for guild=%s channel=%s",
            guild_id,
            channel_id,
        )
        return 0

    payload = json.dumps(
        {
            "op": "dc_notify",
            "mc_name": mc_name,
        }
    )

    delivered = 0
    for r in rows:
        th = r["token_hash"]
        st = _connections.get(th)
        ws = st.get("ws") if st else None
        if ws:
            try:
                await ws.send_str(payload)
                delivered += 1
            except Exception as e:
                logging.warning(
                    "dc_notify: send failed for hash=%s: %s",
                    th[:12],
                    e,
                )

    logging.info(
        "dc_notify: mc_name=%s delivered=%d",
        mc_name,
        delivered,
    )
    return delivered

async def _recompute_perm_profile_for_uuid(mc_uuid: str) -> dict | None:
    """
    Look up the linked Discord account(s) for this mc_uuid, compute
    Discord-based perms + cosmetics, write them to mc_perm_cache, and
    return a dict with prefix/color/perms for one of them.

    If multiple guilds are linked, we just use the first row.
    """
    await db.ensure_connected()
    if _bot is None:
        logging.warning("perm_refresh: bot reference not set; cannot refresh perms")
        return None

    # Find link by mc_uuid
    cur = await db.conn.execute(
        "SELECT guild_id, discord_id, mc_name FROM user_links WHERE mc_uuid=? LIMIT 1",
        (mc_uuid,),
    )
    row = await cur.fetchone()
    if not row:
        logging.info("perm_refresh: no user_links row for mc_uuid=%s", mc_uuid)
        return None

    guild_id = int(row["guild_id"])
    discord_id = int(row["discord_id"])
    mc_name = row["mc_name"]

    # Check cross-moderation toggle for this guild
    cur2 = await db.conn.execute(
        "SELECT crossmod_enabled FROM guild_settings WHERE guild_id=?",
        (guild_id,),
    )
    row2 = await cur2.fetchone()
    if row2 and row2["crossmod_enabled"] == 0:
        logging.info(
            "perm_refresh: cross moderation disabled for guild_id=%s; skipping Discord perms/prefix",
            guild_id,
        )
        return None

    guild: discord.Guild | None = _bot.get_guild(guild_id)
    if guild is None:
        logging.warning("perm_refresh: bot not in guild_id=%s", guild_id)
        return None

    member: discord.Member | None = guild.get_member(discord_id)
    if member is None:
        try:
            member = await guild.fetch_member(discord_id)
        except Exception as e:
            logging.warning(
                "perm_refresh: fetch_member failed for %s in guild %s: %s",
                discord_id,
                guild_id,
                e,
            )
            return None

    perms = member.guild_permissions

    can_kick = bool(perms.kick_members or perms.administrator)
    can_ban = bool(perms.ban_members or perms.administrator)
    can_timeout = bool(
        getattr(perms, "moderate_members", False) or perms.administrator
    )

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

    prefix: str | None = None
    color_hex: str | None = None

    roles = [r for r in member.roles if not r.is_default()]
    colored_roles = [r for r in roles if r.colour.value != 0]
    color_role = (
        max(colored_roles, key=lambda r: r.position) if colored_roles else None
    )
    if color_role is not None:
        color_hex = f"#{color_role.colour.value:06X}"

    if roles:
        hoisted_roles = [r for r in roles if r.hoist]
        if hoisted_roles:
            prefix_role = max(hoisted_roles, key=lambda r: r.position)
        elif color_role is not None:
            prefix_role = color_role
        else:
            prefix_role = max(roles, key=lambda r: r.position)
        prefix = f"[{prefix_role.name}]"

    # Upsert into mc_perm_cache
    await db.conn.execute(
        "INSERT OR REPLACE INTO mc_perm_cache("
        "guild_id, mc_uuid, mc_name, can_kick, can_ban, can_timeout, is_staff, "
        "prefix, color_hex, last_sync"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            guild_id,
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

    return {
        "prefix": prefix,
        "color_hex": color_hex,
        "can_kick": int(can_kick),
        "can_ban": int(can_ban),
        "can_timeout": int(can_timeout),
        "is_staff": int(is_staff),
    }


async def _notify_bound_channels(token_hash: str, text: str) -> int:
    """
    Send connect/disconnect notices via webhook (parent webhook + optional thread_id).
    """
    await _wait_ready()
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT guild_id, channel_id FROM mc_links WHERE token_hash=?",
        (token_hash,),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.warning("notify: no rows for hash=%s", token_hash[:12])
        return 0

    sent = 0
    for r in rows:
        gid, cid = int(r["guild_id"]), int(r["channel_id"])
        wh_url, thread_id = await _lookup_webhook_for_link(gid, cid)
        if not wh_url:
            logging.warning("notify: no webhook available for channel %s", cid)
            continue
        try:
            await _post_webhook(wh_url, content=text, thread_id=thread_id)
            sent += 1
        except Exception as e:
            logging.warning(
                "notify: webhook post failed for channel %s: %s",
                cid,
                e,
            )
    return sent


def _skin_avatar(player: str, uuid: str) -> str:
    # Reliable avatar: Minotar returns a PNG every time (Discord-friendly).
    safe_player = (player or "Player").replace("/", "_").replace("\\", "_")
    return f"https://minotar.net/helm/{safe_player}/128.png"
    # If you prefer online-mode UUIDs:
    # if uuid:
    #     return f"https://crafatar.com/avatars/{uuid}?size=128&overlay&default=MHF_Steve"
    # return f"https://minotar.net/helm/{safe_player}/128.png"

async def _relay_mc_to_discord(token_hash: str, data: dict):
    """
    MC chat -> Discord via webhook (with player name + avatar).
    """
    await _wait_ready()

    player = data.get("player", "Player")
    uuid = data.get("uuid", "")
    text = data.get("text", "")

    # Convert @DiscordUser into real mentions <@id>
    text = await _apply_mc_mentions_to_discord(token_hash, text)

    avatar = _skin_avatar(player, uuid)

    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT guild_id, channel_id FROM mc_links WHERE token_hash=? AND status='connected'",
        (token_hash,),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info("relay: no channels bound for hash=%s", token_hash[:12])
        return

    for r in rows:
        gid, cid = int(r["guild_id"]), int(r["channel_id"])
        wh_url, thread_id = await _lookup_webhook_for_link(gid, cid)
        if not wh_url:
            logging.warning("relay: no webhook available for channel %s", cid)
            continue
        try:
            logging.info(
                "relay: using avatar %s for player %s",
                avatar,
                player,
            )
            await _post_webhook(
                wh_url,
                content=text,
                username=player,
                avatar_url=avatar,
                thread_id=thread_id,
            )
            logging.info(
                "relay: webhook posted to channel %s (thread_id=%s)",
                cid,
                thread_id,
            )
        except Exception as e:
            logging.warning("relay: webhook post failed for %s: %s", cid, e)


async def _relay_mc_event_to_discord(token_hash: str, data: dict):
    """
    MC events (join/quit/death) -> Discord via webhook.
    Formats the event text in *italics* as requested.
    """
    await _wait_ready()

    etype = (data.get("etype") or "").lower()  # "join" | "quit" | "death"
    player = data.get("player", "Player")
    uuid = data.get("uuid", "")
    raw_text = data.get("text", "")  # e.g., "connected", "disconnected", "drowned", etc.

    # Italic formatting; switch to inline code by wrapping with backticks if you prefer.
    formatted = f"*{raw_text}*"

    avatar = _skin_avatar(player, uuid)

    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT guild_id, channel_id FROM mc_links WHERE token_hash=? AND status='connected'",
        (token_hash,),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info("event: no channels bound for hash=%s", token_hash[:12])
        return

    for r in rows:
        gid, cid = int(r["guild_id"]), int(r["channel_id"])
        wh_url, thread_id = await _lookup_webhook_for_link(gid, cid)
        if not wh_url:
            logging.warning("event: no webhook available for channel %s", cid)
            continue
        try:
            await _post_webhook(
                wh_url,
                content=formatted,
                username=player,
                avatar_url=avatar,
                thread_id=thread_id,
            )
            logging.info(
                "event: posted %s for %s to channel %s",
                etype,
                player,
                cid,
            )
        except Exception as e:
            logging.warning("event: webhook post failed for %s: %s", cid, e)


async def _relay_mc_mod_to_discord(token_hash: str, data: dict):
    """
    MC moderation actions -> Discord via webhook.
    Uses the same bound channels as chat/events.
    """
    await _wait_ready()

    action = (data.get("action") or "").lower()
    target = data.get("target") or "Player"
    issued_by = data.get("issued_by") or "System"
    reason = data.get("reason") or ""
    try:
        minutes = int(data.get("minutes") or 0)
    except Exception:
        minutes = 0

    if action == "kick":
        text = f"🔨 **{target}** was kicked by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    elif action == "ban":
        text = f"⛔ **{target}** was **banned** by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    elif action == "tempban":
        text = f"⏱️ **{target}** was **temp-banned** for {minutes} minute(s) by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    elif action == "mute":
        text = f"🔇 **{target}** was **muted** for {minutes} minute(s) by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    elif action == "pardon":
        text = f"✅ **{target}** was **unbanned** by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    elif action == "unmute":
        text = f"🔊 **{target}** was **unmuted** by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"
    else:
        text = f"⚙️ Moderation action `{action}` on **{target}** by **{issued_by}**."
        if reason:
            text += f" Reason: *{reason}*"

    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT guild_id, channel_id FROM mc_links WHERE token_hash=? AND status='connected'",
        (token_hash,),
    )
    rows = await cur.fetchall()
    if not rows:
        logging.info("mc_mod: no channels bound for hash=%s", token_hash[:12])
        return

    for r in rows:
        gid, cid = int(r["guild_id"]), int(r["channel_id"])
        wh_url, thread_id = await _lookup_webhook_for_link(gid, cid)
        if not wh_url:
            logging.warning("mc_mod: no webhook available for channel %s", cid)
            continue
        try:
            await _post_webhook(wh_url, content=text, thread_id=thread_id)
            logging.info("mc_mod: posted %s for %s to channel %s", action, target, cid)
        except Exception as e:
            logging.warning("mc_mod: webhook post failed for %s: %s", cid, e)

async def run_ws_app():
    logging.info("[BOOT] run_ws_app() starting")
    app = web.Application()
    app.add_routes([web.get("/mcws", ws_handler)])
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site4 = web.TCPSite(runner, "0.0.0.0", MC_WS_PORT)
        await site4.start()
        logging.info("MC WebSocket (IPv4) listening on 0.0.0.0:%s", MC_WS_PORT)
        try:
            site6 = web.TCPSite(runner, "::", MC_WS_PORT)
            await site6.start()
            logging.info("MC WebSocket (IPv6) listening on [::]:%s", MC_WS_PORT)
        except Exception as e:
            logging.warning("IPv6 WS listener not started: %s", e)
        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        logging.exception("MC WS server crashed during startup: %s", e)
        raise
    finally:
        await _http_shutdown()

