# Kavex Bot — Secure Discord ↔ Minecraft Bridge  
### A Free, Open-Source, TLS-Encrypted WebSocket Chat Link for Minecraft Servers

[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-3.11+-blue)
![Status](https://img.shields.io/badge/status-active-success)

Kavex Bot is the **Discord half** of the KavexLink project — a **bi-directional, real-time, TLS-encrypted chat bridge** between **Discord** and **Minecraft Paper/Spigot servers**.

Unlike traditional message bridges, Kavex Bot uses:
- 🔐 **Encrypted WebSockets (TLS)**  
- 🧩 **Structured JSON protocol**  
- 🎨 **Minecraft player avatars on Discord**  
- 🔄 **Full multi-guild, multi-server support**  
- 💬 **Rich chat formatting (embeds, italics, colors)**  
- ⚡ **Asynchronous Python backend (aiohttp + discord.py)**  

This bot can serve **multiple guilds** and **multiple Minecraft servers** at the same time, with each having its own channel bindings.

A public demo instance is available:

### 👉 **Invite Demo Bot:**  
**https://discordapp.com/oauth2/authorize?client_id=1437652702489346069**

*(Runs 24/7 on a FreeBSD server.)*

---

## ✨ Features

### 🔄 Real Discord ↔ Minecraft Chat Mirroring
- Minecraft → Discord messages appear as the **player** (avatar = skin head render)
- Discord → Minecraft messages include **formatted usernames**, colors, and role badges
- Supports Unicode, emojis, embeds, markdown

### 🔐 Secure by Design (TLS Everywhere)
- Fully encrypted TLS websocket (wss://)
- Works safely over WAN — host your MC server *anywhere*
- Protection against spoofing, replay, and man-in-the-middle

### 🧭 Multi-Guild & Multi-MC Support
- Every guild can link multiple MC servers
- Every MC server can have its own channel
- Webhooks are auto-managed per channel

### 🧱 Structured JSON Protocol
Reliable communication between Minecraft plugin and bot:

```json
{
"type": "chat",
"player": "KonKavex",
"uuid": "14d212c2-5a35...",
"message": "Hello!"
}
```


### 📸 Minecraft Skin → Discord Avatar
Player messages in Discord show a generated dynamic avatar:

![example avatar](https://minotar.net/avatar/Notch/64)  
*(Real avatar generated live per player.)*

### 📑 Built-In SQLite Database
Stored automatically:
- Linked channels
- Webhooks
- Server identities
- Status tracking

### 🔧 Simple Setup
The bot manages:
- Webhook creation
- Guild commands
- Channel prep
- Cached connections

You only configure:
- Database path  
- WebSocket bind address  
- Logging options  

---

## 📥 Installation

```bash
git clone https://github.com/yourname/kavex-bot.git
cd kavex-bot
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```



Create configuration file:
```bash
cp config.example.json config.json
nano config.json
```

Start the bot:
```bash
python -m bot
```

---

## 🔌 WebSocket Endpoint

The bot exposes a WebSocket endpoint (not yet TLS secured):
```bash
ws://127.0.0.1:8765/mcws
```

For securing the websocket it is advised to proxy it behind a reverse proxy like apache or nginx and configure them to serve the websocket for a certain vhost/domain.
Example config for apache:
```bash
<VirtualHost *:443>
  ServerName bot.example.org
  SSLEngine on
  SSLCertificateFile /usr/local/etc/letsencrypt/live/example.org/fullchain.pem
  SSLCertificateKeyFile /usr/local/etc/letsencrypt/live/example.org/privkey.pem

  ProxyTimeout 300
  ProxyPreserveHost On
  RequestHeader set X-Forwarded-Proto "https"
  RequestHeader set X-Forwarded-Port "443"
  RequestHeader set X-Real-IP %{REMOTE_ADDR}s

  # --- WebSocket tunnel to the bot (aiohttp) ---
  # If the bot runs on the same host, use 127.0.0.1; otherwise internal IP.
  ProxyPass        "/mcws"  "ws://127.0.0.1:8765/mcws" retry=0
  ProxyPassReverse "/mcws"  "ws://127.0.0.1:8765/mcws"

  # Optional: HSTS (only if you’re sure HTTPS works)
  Header always set Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"
</VirtualHost>
```

This exposes a secure websocket endpoint
```bash
wss://bot.example.org/mcws
```
This is consumed by the Paper plugin using an authenticated session token.

---

## 📚 Documentation

Full protocol documentation will be published after the inter-moderation system is implemented.

Planned docs include:
 - WebSocket message types
 - Authentication model
 - Event handling
 - Inter-moderation API (Discord admins → MC ops, MC ops → Discord moderation)


### Discord Commands

For documentation of discord bot commands see [COMMANDS.md](COMMANDS.md)

---

## 🧭 Roadmap

### v1.0.0 — First Stable Release
 [x] TLS encrypted WebSocket
 [x] Avatar sync
 [x] Chat mirroring (both directions)
 [x] Server event integration (join, quit, death logs)
 [ ] Inter-moderation system (next)
 [ ] Command execution
 [ ] User authentication sync
 [ ] Release packaging (Docker, FreeBSD service, systemd)

---

## 🧩 Related Project: Minecraft Plugin

This bot works together with the Paper plugin:
➡️[KavexLink GitHub](https://github.com/GenericRikka/kavexlink)

---

## ❤️ Contributing

Contributions, issues, and feature requests are welcome!
 - Open issues
 - Submit PRs
 - Join the discussion

---
## 📜 License
BSD 3-Clause License — free to use in any project.
