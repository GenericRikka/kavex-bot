# src/bot/mc_ws.py
import asyncio
import hashlib
import json
import logging
import os
import time
from aiohttp import web
from .db import db
import discord

MC_WS_HOST = os.getenv("MC_WS_HOST", "0.0.0.0")
MC_WS_PORT = int(os.getenv("MC_WS_PORT", "8765"))
MC_TOKEN_PEPPER = os.getenv("MC_TOKEN_PEPPER", "")  # keep this the same across restarts

# token_hash -> {"ws": WebSocketResponse, "server": str, "ts": float}
_connections: dict[str, dict] = {}

def _token_hash(token: str) -> str:
    h = hashlib.sha256()
    h.update((token.strip() + MC_TOKEN_PEPPER).encode("utf-8"))  # normalize here as well
    return h.hexdigest()

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

                    # Mark connected
                    await db.ensure_connected()
                    cur = await db.conn.execute(
                        "UPDATE mc_links SET status='connected', server_name=?, last_seen=? WHERE token_hash=?",
                        (server_name, time.time(), token_hash),
                    )
                    await db.conn.commit()
                    updated = cur.rowcount if hasattr(cur, "rowcount") else None

                    # Notify channels bound to this hash
                    bound = await _notify_bound_channels(token_hash, f"🟢 **{server_name}** connected.")
                    logging.info(
                        "MC auth ok: server=%s short_hash=%s row_update=%s bound_channels=%d",
                        server_name, token_hash[:12], updated, bound
                    )
                    if bound == 0:
                        logging.warning("Auth ok but no bound channels matched this hash. Did you /minecraft connect with the same token + pepper?")

                elif op == "mc_chat":
                    await _relay_mc_to_discord(token_hash, data)

                else:
                    # ignore unknown ops
                    pass

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
            await _notify_bound_channels(token_hash, f"🔴 **{server_name}** disconnected. Waiting for reconnect…")
            st = _connections.get(token_hash)
            if st and st.get("ws") is ws:
                _connections.pop(token_hash, None)

    return ws

async def _notify_bound_channels(token_hash: str, text: str) -> int:
    from .main import bot
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT channel_id FROM mc_links WHERE token_hash=?", (token_hash,)
    )
    rows = await cur.fetchall()
    if not rows:
        logging.warning("notify: no rows for hash=%s", token_hash[:12])
        return 0

    sent = 0
    for r in rows:
        cid = int(r["channel_id"])
        ch = bot.get_channel(cid)  # relies on preload

        if ch is None:
            logging.warning("notify: channel %s not in cache after preload (wrong ID? deleted? perms?)", cid)
            continue

        try:
            await ch.send(text)  # type: ignore
            sent += 1
        except discord.Forbidden:
            logging.warning("notify: Forbidden sending to channel %s (missing perms)", cid)
        except Exception as e:
            logging.warning("notify: send failed (channel_id=%s): %s", cid, e)

    return sent


async def _get_or_create_webhook(guild: discord.Guild, channel: discord.TextChannel) -> discord.Webhook | None:
    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT webhook_id, webhook_token FROM mc_webhooks WHERE guild_id=? AND channel_id=?",
        (guild.id, channel.id),
    )
    row = await cur.fetchone()
    if row:
        try:
            wh = discord.Webhook.partial(
                int(row["webhook_id"]),
                row["webhook_token"],
                adapter=discord.AsyncWebhookAdapter(guild._state.http._HTTPClient__session),
            )
            return wh
        except Exception:
            pass

    try:
        wh = await channel.create_webhook(name="Kavex MC Link")
        await db.conn.execute(
            "INSERT OR REPLACE INTO mc_webhooks(guild_id, channel_id, webhook_id, webhook_token) VALUES (?,?,?,?)",
            (guild.id, channel.id, str(wh.id), wh.token),
        )
        await db.conn.commit()
        return wh
    except Exception as e:
        logging.warning("create_webhook failed: %s", e)
        return None

async def _relay_mc_to_discord(token_hash: str, data: dict):
    from .main import bot
    player = data.get("player", "Player")
    uuid = data.get("uuid", "")
    text = data.get("text", "")

    await db.ensure_connected()
    cur = await db.conn.execute(
        "SELECT guild_id, channel_id FROM mc_links WHERE token_hash=? AND status='connected'",
        (token_hash,),
    )
    rows = await cur.fetchall()
    if not rows:
        return

    avatar = f"https://crafatar.com/avatars/{uuid}?size=64&overlay" if uuid else None

    for r in rows:
        cid = int(r["channel_id"])
        ch = bot.get_channel(cid)
        if ch is None:
            try:
                ch = await bot.fetch_channel(cid)
                logging.info("relay: fetched channel via API (channel_id=%s)", cid)
            except Exception as e:
                logging.warning("relay: fetch_channel failed (channel_id=%s): %s", cid, e)
                continue

        # Prefer webhook if it's a text-ish channel
        if isinstance(ch, discord.TextChannel):
            guild = ch.guild
            wh = await _get_or_create_webhook(guild, ch)
            if wh:
                try:
                    await wh.send(text, username=player, avatar_url=avatar, wait=False)
                    continue
                except Exception as e:
                    logging.warning("webhook send failed: %s", e)
        # Fallback for any messageable channel/thread
        try:
            await ch.send(f"**{player}**: {text}")  # type: ignore
        except Exception as e:
            logging.warning("relay fallback send failed (channel_id=%s): %s", cid, e)

async def run_ws_app():
    app = web.Application()
    app.add_routes([web.get("/mcws", ws_handler)])
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        # IPv4
        try:
            site4 = web.TCPSite(runner, "0.0.0.0", MC_WS_PORT)
            await site4.start()
            logging.info("MC WebSocket (IPv4) listening on 0.0.0.0:%s", MC_WS_PORT)
        except Exception as e:
            logging.exception("Failed to start IPv4 WS listener: %s", e)
        # IPv6
        try:
            site6 = web.TCPSite(runner, "::", MC_WS_PORT)
            await site6.start()
            logging.info("MC WebSocket (IPv6) listening on [::]:%s", MC_WS_PORT)
        except Exception as e:
            logging.warning("IPv6 WS listener not started: %s", e)

        # Keep running
        while True:
            await asyncio.sleep(3600)

    except Exception as e:
        logging.exception("MC WS server crashed during startup: %s", e)
        raise

