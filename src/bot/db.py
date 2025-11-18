import aiosqlite
from pathlib import Path
import logging

DB_PATH = Path("bot.sqlite3")

INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS guild_settings(
  guild_id INTEGER PRIMARY KEY,
  welcome_channel_id INTEGER,
  welcome_message TEXT DEFAULT 'Welcome to server_name, new_user!',
  command_channel_id INTEGER,
  default_role_id INTEGER,
  crossmod_enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS xp(
  guild_id INTEGER,
  user_id INTEGER,
  xp INTEGER DEFAULT 0,
  level INTEGER DEFAULT 0,
  last_msg_ts REAL DEFAULT 0,
  PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS reaction_roles(
  guild_id INTEGER,
  channel_id INTEGER,
  message_id INTEGER,
  emoji TEXT,
  role_id INTEGER,
  PRIMARY KEY (guild_id, message_id, emoji)
);

CREATE TABLE IF NOT EXISTS mc_links(
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  token_hash TEXT NOT NULL,      -- sha256 of secret token
  server_name TEXT,
  last_seen REAL DEFAULT 0,
  status TEXT DEFAULT 'pending', -- pending|connected|disconnected
  PRIMARY KEY (guild_id, channel_id)
);

/*
Legacy schema kept for first run; a migration below may rebuild this table
to relax NOT NULL constraints and add webhook_url/thread_id.
*/
CREATE TABLE IF NOT EXISTS mc_webhooks(
  guild_id INTEGER NOT NULL,
  channel_id INTEGER NOT NULL,
  webhook_id TEXT NOT NULL,
  webhook_token TEXT NOT NULL,
  PRIMARY KEY (guild_id, channel_id)
);

-- Discord <-> Minecraft account links
CREATE TABLE IF NOT EXISTS user_links(
  guild_id    INTEGER NOT NULL,
  discord_id  INTEGER NOT NULL,
  mc_uuid     TEXT,
  mc_name     TEXT,
  linked_at   REAL DEFAULT (strftime('%s','now')),
  notify_ping INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (guild_id, discord_id)
);

-- short-lived link tokens (code from /linkdiscord in-game)
CREATE TABLE IF NOT EXISTS link_tokens(
  token      TEXT PRIMARY KEY,
  mc_uuid    TEXT,
  mc_name    TEXT,
  created_at REAL,
  used       INTEGER DEFAULT 0
);

-- per-role permission mapping for MC moderation + cosmetics
CREATE TABLE IF NOT EXISTS mc_perms(
  guild_id    INTEGER NOT NULL,
  role_id     INTEGER NOT NULL,
  can_kick    INTEGER DEFAULT 0,
  can_ban     INTEGER DEFAULT 0,
  can_timeout INTEGER DEFAULT 0,
  is_staff    INTEGER DEFAULT 0,
  prefix      TEXT,
  color_hex   TEXT,
  PRIMARY KEY (guild_id, role_id)
);

-- cached effective perms per MC account (for outage fallback)
CREATE TABLE IF NOT EXISTS mc_perm_cache(
  guild_id    INTEGER NOT NULL,
  mc_uuid     TEXT,
  mc_name     TEXT,
  can_kick    INTEGER DEFAULT 0,
  can_ban     INTEGER DEFAULT 0,
  can_timeout INTEGER DEFAULT 0,
  is_staff    INTEGER DEFAULT 0,
  prefix      TEXT,
  color_hex   TEXT,
  last_sync   REAL,
  PRIMARY KEY (guild_id, mc_uuid)
);
"""

class DB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        if self._conn is not None:
            return self
        self._conn = await aiosqlite.connect(self.path.as_posix())
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(INIT_SQL)

        # Migrations for existing DBs
        await self._ensure_column("guild_settings", "command_channel_id", "INTEGER")
        await self._ensure_column("guild_settings", "default_role_id", "INTEGER")
        await self._ensure_column("guild_settings", "crossmod_enabled", "INTEGER NOT NULL DEFAULT 1")
        await self._ensure_column("reaction_roles", "channel_id", "INTEGER")


        # Ensure mc_links has the newer metadata columns (no-op if present)
        await self._ensure_column("mc_links", "server_name", "TEXT")
        await self._ensure_column("mc_links", "last_seen", "REAL")
        await self._ensure_column("mc_links", "status", "TEXT")

        # Ensure user_links has notify_ping for mention opt-in
        await self._ensure_column("user_links", "notify_ping", "INTEGER NOT NULL DEFAULT 0")

        # Ensure mc_webhooks has webhook_url + thread_id, and relax legacy NOT NULLs
        await self._migrate_mc_webhooks_if_needed()

        await self._conn.commit()
        logging.info("DB schema ensured/migrated at %s", self.path)
        return self

    async def _ensure_column(self, table: str, column: str, coltype: str):
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        cols = [r["name"] for r in await cur.fetchall()]
        if column not in cols:
            await self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {coltype}"
            )

    async def _migrate_mc_webhooks_if_needed(self):
        """
        Make sure mc_webhooks has:
          - webhook_url TEXT (new)
          - thread_id INTEGER (new)
        and that webhook_id/webhook_token are *nullable* (old schema had NOT NULL).
        If the table lacks webhook_url OR webhook_id/webhook_token are NOT NULL,
        rebuild the table with the relaxed schema and copy data.
        """
        cur = await self._conn.execute("PRAGMA table_info(mc_webhooks)")
        rows = await cur.fetchall()
        if not rows:
            # Table truly missing (unlikely). Create with the new relaxed schema.
            await self._create_mc_webhooks_relaxed()
            return

        cols = {row["name"]: row for row in rows}
        needs_rebuild = False

        if "webhook_url" not in cols:
            needs_rebuild = True
        if "thread_id" not in cols:
            needs_rebuild = True

        for legacy in ("webhook_id", "webhook_token"):
            if legacy in cols and cols[legacy]["notnull"] == 1:
                needs_rebuild = True

        if needs_rebuild:
            await self._rebuild_mc_webhooks_relaxed()
        else:
            # If no rebuild needed, at least ensure columns (idempotent safety)
            await self._ensure_column("mc_webhooks", "webhook_url", "TEXT")
            await self._ensure_column("mc_webhooks", "thread_id", "INTEGER")

    async def _create_mc_webhooks_relaxed(self):
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS mc_webhooks(
              guild_id      INTEGER NOT NULL,
              channel_id    INTEGER NOT NULL,   -- parent TextChannel id
              webhook_id    TEXT,               -- legacy (optional now)
              webhook_token TEXT,               -- legacy (optional now)
              webhook_url   TEXT,               -- preferred new field
              thread_id     INTEGER,            -- if link target is a thread
              PRIMARY KEY (guild_id, channel_id)
            )
        """)

    async def _rebuild_mc_webhooks_relaxed(self):
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS mc_webhooks_new(
              guild_id      INTEGER NOT NULL,
              channel_id    INTEGER NOT NULL,
              webhook_id    TEXT,
              webhook_token TEXT,
              webhook_url   TEXT,
              thread_id     INTEGER,
              PRIMARY KEY (guild_id, channel_id)
            )
        """)

        cur = await self._conn.execute("PRAGMA table_info(mc_webhooks)")
        cols = [r["name"] for r in await cur.fetchall()]

        select_list = ", ".join(
            [f"{c}" for c in ["guild_id", "channel_id"]]
            + [("webhook_id" if "webhook_id" in cols else "NULL AS webhook_id")]
            + [("webhook_token" if "webhook_token" in cols else "NULL AS webhook_token")]
        )

        await self._conn.execute(f"""
            INSERT OR REPLACE INTO mc_webhooks_new(
                guild_id, channel_id, webhook_id, webhook_token, webhook_url, thread_id
            )
            SELECT {select_list}, NULL AS webhook_url, NULL AS thread_id
            FROM mc_webhooks
        """)

        await self._conn.execute("DROP TABLE mc_webhooks")
        await self._conn.execute("ALTER TABLE mc_webhooks_new RENAME TO mc_webhooks")
        await self._conn.commit()
        logging.info(
            "DB migration: rebuilt mc_webhooks with webhook_url/thread_id and relaxed NULLs"
        )

    async def ensure_connected(self):
        if self._conn is None:
            await self.connect()

    @property
    def conn(self) -> aiosqlite.Connection | None:
        return self._conn

    async def close(self):
        if self._conn:
            await self._conn.close()
            self._conn = None


db = DB()

