import asyncio
import hashlib
import json
import logging
import os
import time
from typing import Dict, List, Tuple, Optional

from aiohttp import web, ClientSession
from .db import db

logging.info("[BOOT] mc_ws module imported")

MC_WS_PORT = int(os.getenv("MC_WS_PORT", "8765"))
MC_TOKEN_PEPPER = os.getenv("MC_TOKEN_PEPPER", "")

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
            except Exception as e:
                logging.warning("pending relay failed: %s", e)
        _pending_mc_msgs = []

def _token_hash(token: str) -> str:
    h = hashlib.sha256()
    h.update((token.strip() + MC_TOKEN_PEPPER).encode("utf-8"))
    return h.hexdigest()

# ---------- Webhook resolution helpers ----------

async def _lookup_webhook_for_link(guild_id: int, linked_channel_id: int) -> Tuple[Optional[str], Optional[int]]:
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

async def _post_webhook(wh_url: str, content: Optional[str] = None,
                        username: Optional[str] = None, avatar_url: Optional[str] = None,
                        thread_id: Optional[int] = None) -> None:
    """
    Post to webhook using our own aiohttp session.
    """
    url = wh_url
    if thread_id:
        sep = '&' if '?' in url else '?'
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

    token_hash = None
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
                    _connections[token_hash] = {"ws": ws, "server": server_name, "ts": time.time()}
                    await ws.send_json({"op": "auth", "ok": True})

                    await db.ensure_connected()
                    cur = await db.conn.execute(
                        "UPDATE mc_links SET status='connected', server_name=?, last_seen=? WHERE token_hash=?",
                        (server_name, time.time(), token_hash),
                    )
                    await db.conn.commit()

                    _pending_notifies.append((token_hash, f"🟢 **{server_name}** connected."))
                    logging.info(
                        "MC auth ok: server=%s short_hash=%s row_update=%s (notify buffered until ready)",
                        server_name, token_hash[:12], getattr(cur, "rowcount", None),
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
                elif op == "mc_link_request":
                    # Player requested a Discord link in-game
                    code = (data.get("code") or "").strip()
                    mc_uuid = (data.get("uuid") or "").strip()
                    mc_name = (data.get("name") or "").strip()

                    if not code or not mc_name:
                        await ws.send_json({
                            "op": "mc_link_ack",
                            "ok": False,
                            "err": "missing code or name"
                        })
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
                        code, mc_name, mc_uuid
                    )


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
            _pending_notifies.append((token_hash, f"🔴 **{server_name}** disconnected. Waiting for reconnect…"))
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
        logging.info("dc_chat: no active links for guild=%s channel=%s", guild_id, channel_id)
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
                logging.warning("dc_chat: send failed for hash=%s: %s", th[:12], e)
    logging.info("dc_chat: delivered=%d", delivered)
    return delivered

async def send_dc_admin(
    guild_id: int,
    channel_id: int,
    action: str,
    player: str,
    reason: str,
    issued_by: str,
) -> int:
    """
    Discord -> MC admin command (kick/ban/etc.).
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
        "action": action,            # "kick", "ban", "pardon", "command", etc.
        "player": player,
        "reason": reason,
        "issued_by": issued_by,      # "Name#1234" or just display_name
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
                logging.warning("dc_admin: send failed for hash=%s: %s", th[:12], e)

    logging.info(
        "dc_admin: action=%s player=%s delivered=%d",
        action,
        player,
        delivered,
    )
    return delivered

async def _notify_bound_channels(token_hash: str, text: str) -> int:
    """
    Send connect/disconnect notices via webhook (parent webhook + optional thread_id).
    """
    await _wait_ready()
    await db.ensure_connected()
    cur = await db.conn.execute("SELECT guild_id, channel_id FROM mc_links WHERE token_hash=?", (token_hash,))
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
            logging.warning("notify: webhook post failed for channel %s: %s", cid, e)
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
            logging.info("relay: using avatar %s for player %s", avatar, player)
            await _post_webhook(wh_url, content=text, username=player, avatar_url=avatar, thread_id=thread_id)
            logging.info("relay: webhook posted to channel %s (thread_id=%s)", cid, thread_id)
        except Exception as e:
            logging.warning("relay: webhook post failed for %s: %s", cid, e)

async def _relay_mc_event_to_discord(token_hash: str, data: dict):
    """
    MC events (join/quit/death) -> Discord via webhook.
    Formats the event text in *italics* as requested.
    """
    await _wait_ready()

    etype = (data.get("etype") or "").lower()     # "join" | "quit" | "death"
    player = data.get("player", "Player")
    uuid = data.get("uuid", "")
    raw_text = data.get("text", "")               # e.g., "connected", "disconnected", "drowned", etc.

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
            await _post_webhook(wh_url, content=formatted, username=player, avatar_url=avatar, thread_id=thread_id)
            logging.info("event: posted %s for %s to channel %s", etype, player, cid)
        except Exception as e:
            logging.warning("event: webhook post failed for %s: %s", cid, e)

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

