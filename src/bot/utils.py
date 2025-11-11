import time
import math

def needed_xp_for_level(level: int) -> int:
    # simple curve: 100, 200, 300, ...
    return (level + 1) * 100

def now() -> float:
    return time.time()

def apply_placeholders(text: str, guild_name: str, user_mention: str) -> str:
    # Exact tokens requested:
    out = text.replace("server_name", guild_name).replace("new_user", user_mention)
    # Also accept {server_name}/{new_user} for convenience
    out = out.replace("{server_name}", guild_name).replace("{new_user}", user_mention)
    return out

def format_welcome(template: str, guild_name: str, user_mention: str) -> str:
    return (template
            .replace("{guild}", guild_name)
            .replace("{user}", user_mention))

