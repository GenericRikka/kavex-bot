import aiosqlite
from pathlib import Path

DB_PATH = Path("bot.sqlite3")

INIT_SQL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS guild_settings(
  guild_id INTEGER PRIMARY KEY,
  welcome_channel_id INTEGER,
  welcome_message TEXT DEFAULT 'Welcome to server_name, new_user!',
  command_channel_id INTEGER
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
  message_id INTEGER,
  emoji TEXT,
  role_id INTEGER,
  PRIMARY KEY (guild_id, message_id, emoji)
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
        # MIGRATION: ensure command_channel_id exists on old DBs
        await self._ensure_column("guild_settings", "command_channel_id", "INTEGER")
        await self._conn.commit()
        return self

    async def _ensure_column(self, table: str, column: str, coltype: str):
        cur = await self._conn.execute(f"PRAGMA table_info({table})")
        cols = [r["name"] for r in await cur.fetchall()]
        if column not in cols:
            await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

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

