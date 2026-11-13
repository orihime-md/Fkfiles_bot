"""
◬ BOLTIX EMPIRE — Telegram vault bot (single file)

Flow:  /start -> sigil image + "JOIN BOLTIX CHANNEL" + [Channel 1][Channel 2][Verify]
       Verify -> checks both channels -> new sigil message with the user's referral link
                 + one button per file category
       Category -> message is edited into the file list. 1 file is FREE, the rest are
                   locked until the user has invited REFERRALS_REQUIRED (5) verified people,
                   then they can UNSEAL that category. Live animated progress bar.

Admin uploads files (as documents) named  "name.ext-Category"  or  "name.ext category".
"""
import asyncio
import hashlib
import html
import io
import logging
import os
import re
import secrets
import signal
import ssl
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote, urlparse

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ErrorEvent,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Message,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("boltix")

# ════════════════════════════════════════════════════════════════
#  CONFIG
# ════════════════════════════════════════════════════════════════

DEFAULT_CHANNELS = {1: "https://t.me/Hoshigami_anime", 2: "https://t.me/Boltix_labs"}
ASSETS = Path(__file__).parent / "assets"
MAX_REUPLOAD = 19 * 1024 * 1024  # bot API download limit is 20 MB
PAGE_SIZE = 6

CFG = None  # filled in main()
BOT = None
DB = None


def chat_from_url(url: str):
    m = re.fullmatch(r"https?://t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?", (url or "").strip())
    return "@" + m.group(1) if m else None


def load_config(env=os.environ):
    def fail(msg):
        log.error("CONFIG ERROR: %s", msg)
        sys.exit(1)

    token = env.get("BOT_TOKEN", "").strip()
    dsn = env.get("DATABASE_URL", "").strip()
    if not token:
        fail("BOT_TOKEN is not set")
    if not dsn:
        fail("DATABASE_URL is not set")

    channels = []
    for n in (1, 2):
        url = (env.get(f"CHANNEL_{n}_URL") or DEFAULT_CHANNELS[n]).strip()
        raw_id = (env.get(f"CHANNEL_{n}_ID") or "").strip()
        if raw_id:
            chat = int(raw_id) if re.fullmatch(r"-?\d+", raw_id) else raw_id
        else:
            chat = chat_from_url(url)
        if not chat:
            fail(f"CHANNEL_{n}_URL ({url}) is a private link. Set CHANNEL_{n}_ID to the numeric channel id.")
        channels.append(SimpleNamespace(n=n, url=url, chat=chat))

    admins = []
    for x in env.get("ADMIN_IDS", "").split(","):
        x = x.strip()
        if x.isdigit():
            admins.append(int(x))

    try:
        required = max(1, int(env.get("REFERRALS_REQUIRED", "5")))
    except ValueError:
        required = 5

    return SimpleNamespace(
        token=token,
        dsn=dsn,
        admins=admins,
        channels=channels,
        required=required,
        clean_names=env.get("CLEAN_FILENAMES", "true").lower() != "false",
        public_url=(env.get("PUBLIC_URL") or env.get("RENDER_EXTERNAL_URL") or "").rstrip("/"),
        keep_alive=env.get("KEEP_ALIVE", "true").lower() != "false",
        port=int(env.get("PORT", "10000")),
    )


def is_admin(user_id: int) -> bool:
    return user_id in CFG.admins


# ════════════════════════════════════════════════════════════════
#  FILE-NAME PARSER   "index.js-Automation" -> (index.js, Automation)
# ════════════════════════════════════════════════════════════════

_CAT_HYPHEN = re.compile(r"[\w ]{2,32}")  # letters/digits/_/space, never a dot
_CAT_SPACE = re.compile(r"\w{2,32}")


def title_case(s: str) -> str:
    return " ".join(w[:1].upper() + w[1:] for w in s.strip().split())


def parse_file_label(raw):
    s = (raw or "").strip()
    if not s:
        return None
    h = s.rfind("-")
    if 0 < h < len(s) - 1:
        name, cat = s[:h].strip(), s[h + 1:].strip()
        if name and _CAT_HYPHEN.fullmatch(cat):
            return name, title_case(cat)
    sp = s.rfind(" ")
    if 0 < sp < len(s) - 1:
        name, cat = s[:sp].strip(), s[sp + 1:].strip()
        if name and _CAT_SPACE.fullmatch(cat):
            return name, title_case(cat)
    return None


# ════════════════════════════════════════════════════════════════
#  UI  (every caption, bar, button and phrase — edit the cult copy here)
# ════════════════════════════════════════════════════════════════

def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=False)


def pick(seq):
    return seq[secrets.randbelow(len(seq))]


W = 32


def box(text: str) -> str:
    pad = max(0, W - len(text))
    l = pad // 2
    r = pad - l
    return f"<code>╔{'═' * W}╗\n║{' ' * l}{text}{' ' * r}║\n╚{'═' * W}╝</code>"


HEADER = box("◬  B O L T I X  E M P I R E  ◬")
THIN = "<code>" + "─" * 32 + "</code>"

CAT_ICONS = ["⌬", "⛓", "🕸", "📡", "🧬", "🗝", "🧿", "☠️", "🔮", "⚙️"]

CREED = [
    "We are the signal inside the noise.",
    "Knowledge is the only sacrament.",
    "Root access to the unseen.",
    "The code remembers what men forget.",
    "In silence we compile. In shadow we deploy.",
    "No gods. No masters. Only source.",
    "Every system has a door. We are the key.",
    "The empire grows in the dark.",
]
SEALED_LINES = [
    "The seal holds. Bring souls to the circle.",
    "ACCESS_DENIED // the empire demands tribute.",
    "Power is earned, initiate. Not given.",
    "The vault hungers for new blood.",
    "Recruit. Expand. Ascend.",
]
UNSEALED_LINES = [
    "The seal is broken. The vault bows to you.",
    "ROOT GRANTED // take what is yours.",
    "The circle recognises its own.",
]
STATUS_FRAMES = [
    "INITIALIZING SIGIL ░",
    "READING THE LEDGER ▒",
    "COUNTING THE SWORN ▓",
    "DECRYPTING SEALS  ▓",
    "LEDGER SYNCED     █",
]


def cat_icon(key: str) -> str:
    h = 0
    for ch in str(key):
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return CAT_ICONS[h % len(CAT_ICONS)]


def plural(n, one, many):
    return one if n == 1 else many


def progress_bar(cur, total, width=None):
    width = width or total * 2
    c = max(0, min(cur, total))
    f = round(c / total * width)
    return "▰" * f + "▱" * (width - f)


def progress_frames(cur, total):
    """5 animation frames: the bar fills from empty up to `cur`."""
    width = total * 2
    c = max(0, min(cur, total))
    target = round(c / total * width)
    last = len(STATUS_FRAMES) - 1
    frames = []
    for i, status in enumerate(STATUS_FRAMES):
        filled = round(target * i / last)
        frames.append({
            "bar": "▰" * filled + "▱" * (width - filled),
            "status": status,
            "count": round(c * i / last),
        })
    return frames


def bar_block(cur, total, frame=None):
    c = max(0, min(cur, total))
    if frame:
        return f"<code>[ {frame['status']} ]\n{frame['bar']} {frame['count']}/{total}</code>"
    pct = round(c / total * 100)
    return f"<code>{progress_bar(c, total)} {c}/{total} · {pct}%</code>"


def truncate(s, n):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def share_url(link: str) -> str:
    text = "𓂀 Boltix Empire — the vault opens for the sworn. Take the oath:"
    return f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(text, safe='')}"


def ui_gate(name, channels):
    caption = "\n".join([
        HEADER,
        "",
        "<b>JOIN BOLTIX CHANNEL</b> 𓂀",
        "",
        f"<i>The circle is closed, {esc(name or 'stranger')}.</i>",
        "<i>Only the sworn may enter.</i>",
        "",
        THIN,
        "<code>⟦01⟧</code> Join <b>Channel 1</b>",
        "<code>⟦02⟧</code> Join <b>Channel 2</b>",
        "<code>⟦03⟧</code> Tap <b>Verify</b> — the sigil reads your soul",
        THIN,
        "",
        "<code>root@boltix:~# ./oath --await</code>",
    ])
    rows = [
        [{"text": f"📡 Channel {c.n}", "url": c.url} for c in channels],
        [{"text": "✅ Verify", "callback_data": "verify"}],
    ]
    return caption, rows


def ui_not_joined(missing):
    names = " + ".join(f"Channel {c.n}" for c in missing)
    return f"✖ OATH REJECTED\nYou have not joined: {names}.\nJoin, then tap Verify again."


def ui_vault(name, link, st, required, cats, admin):
    lines = [
        HEADER,
        "",
        "<b>◢ THE OATH IS SEALED ◣</b>",
        f"<i>Welcome to the inner circle, {esc(name or 'initiate')}.</i>",
        "",
        "<b>🔗 YOUR SIGIL LINK</b>",
        f"<code>{esc(link)}</code>",
        "",
        THIN,
        f"<b>⛓ SOULS BOUND:</b> <code>{st['refs']}</code>   <b>🗝 TOKENS:</b> <code>{st['credits']}</code>",
        bar_block(st["progress"], required),
        f"<i>{required} souls = 1 seal broken (your choice of vault).</i>",
        THIN,
        "",
        "<b>▼ CHOOSE A VAULT</b>" if cats else
        "<i>The vaults are empty. The Architect has not sealed any relics yet.</i>",
        f"<i>{esc(pick(CREED))}</i>",
    ]
    if admin:
        lines += ["", "<code>[ADMIN] send me a file named  name.ext-Category</code>"]
    rows = []
    for i in range(0, len(cats), 2):
        rows.append([
            {"text": f"{cat_icon(c['key'])} {c['name']} · {c['n']}", "callback_data": f"cat:{c['id']}:0"}
            for c in cats[i:i + 2]
        ])
    rows.append([
        {"text": "📤 Share Sigil Link", "url": share_url(link)},
        {"text": "🔄 Refresh", "callback_data": "vault"},
    ])
    return "\n".join(lines), rows


def ui_category_caption(cat, total, st, required, unlocked, frame, admin):
    lines = [
        HEADER,
        "",
        f"<b>{cat_icon(cat['key'])} VAULT // {esc(cat['name'].upper())}</b>",
        f"<i>{total} {plural(total, 'relic', 'relics')} sealed inside</i>",
        "",
    ]
    if unlocked:
        lines += [
            "<b>STATUS:</b> 👑 <code>ARCHITECT ACCESS</code>" if admin
            else "<b>STATUS:</b> 🔓 <code>UNSEALED</code>",
            f"<i>{esc(pick(UNSEALED_LINES))}</i>",
            "",
            "<b>▼ TAP A RELIC TO RECEIVE IT</b>",
        ]
    else:
        need = max(0, required - st["progress"])
        lines += [
            "<b>STATUS:</b> 🔒 <code>SEALED</code> — 1 free relic, the rest are locked",
            "",
            bar_block(st["progress"], required, frame),
        ]
        if not frame:
            if st["credits"] > 0:
                c = st["credits"]
                lines += [
                    "",
                    f"🗝 <b>You hold {c} unseal {plural(c, 'token', 'tokens')}.</b>",
                    "Tap <b>UNSEAL</b> to open this vault.",
                ]
            else:
                lines += [
                    "",
                    f"<b>{need}</b> more {plural(need, 'soul', 'souls')} must take the oath through your sigil link.",
                    f"<i>{esc(pick(SEALED_LINES))}</i>",
                ]
    return "\n".join(lines)


def ui_category_keyboard(cat, files, total, page, st, unlocked, link, admin):
    rows = []
    for f in files:
        open_ = f["is_free"] or unlocked
        tag = " · FREE" if f["is_free"] else ""
        rows.append([{
            "text": f"{'📜' if open_ else '🔒'} {truncate(f['display_name'], 34)}{tag}",
            "callback_data": f"get:{f['id']}",
        }])
    pages = max(1, -(-total // PAGE_SIZE))
    if pages > 1:
        rows.append([
            {"text": "◀", "callback_data": f"cat:{cat['id']}:{max(0, page - 1)}"},
            {"text": f"{page + 1}/{pages}", "callback_data": "noop"},
            {"text": "▶", "callback_data": f"cat:{cat['id']}:{min(pages - 1, page + 1)}"},
        ])
    if not unlocked and not admin:
        if st["credits"] > 0:
            rows.append([{"text": "🗝 UNSEAL THIS VAULT", "callback_data": f"unlock:{cat['id']}"}])
        rows.append([
            {"text": "📤 Recruit", "url": share_url(link)},
            {"text": "🔄 Refresh", "callback_data": f"cat:{cat['id']}:{page}"},
        ])
    rows.append([{"text": "⟵ Back to the Vault", "callback_data": "vault"}])
    return rows


def ui_locked_alert(need, credits):
    if credits > 0:
        return "🗝 You hold an unseal token!\nOpen this vault and tap UNSEAL."
    return f"🔒 SEALED\nBring {need} more {plural(need, 'soul', 'souls')} through your sigil link to break this seal."


def ui_file_caption(f):
    vault = re.sub(r"\s+", "_", str(f["category_name"]).lower())
    return "\n".join([
        f"<b>📜 {esc(f['display_name'])}</b>",
        f"<code>vault://{esc(vault)}</code>",
        "",
        "<i>Guard the key. The empire watches.</i> 𓂀",
    ])


def ui_ref_notice(who, st, required, frame=None, done=False):
    lines = [
        "<b>⚡ A NEW SOUL TOOK YOUR OATH</b>",
        f"<i>{esc(who)} has joined the empire through your sigil.</i>",
        "",
        bar_block(st["progress"], required, frame),
    ]
    if done:
        if st["credits"] > 0:
            c = st["credits"]
            lines += ["", f"🗝 <b>THE SEAL CAN BE BROKEN.</b> You hold {c} unseal {plural(c, 'token', 'tokens')}."]
        else:
            need = max(0, required - st["progress"])
            lines += ["", f"<i>{need} more {plural(need, 'soul', 'souls')} until the next seal breaks.</i>"]
    return "\n".join(lines)


HELP_TEXT = "\n".join([
    HEADER,
    "",
    "<b>COMMANDS</b>",
    "<code>/start</code>  — enter the empire",
    "<code>/vault</code>  — open your vault",
    "<code>/invite</code> — your sigil link & progress",
    "<code>/id</code>     — show your numeric id",
    "",
    "<i>1 relic per vault is free. Recruit souls to unseal the rest.</i>",
])


def markup(rows):
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(**b) for b in row] for row in rows]
    )


NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

# ════════════════════════════════════════════════════════════════
#  DATABASE (PostgreSQL via asyncpg)
# ════════════════════════════════════════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  tg_id        BIGINT PRIMARY KEY,
  username     TEXT,
  first_name   TEXT,
  ref_code     TEXT UNIQUE NOT NULL,
  referred_by  BIGINT,
  verified     BOOLEAN NOT NULL DEFAULT FALSE,
  verified_at  TIMESTAMPTZ,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS users_referred_by_idx ON users (referred_by) WHERE verified;

CREATE TABLE IF NOT EXISTS categories (
  id    SERIAL PRIMARY KEY,
  key   TEXT UNIQUE NOT NULL,
  name  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
  id             SERIAL PRIMARY KEY,
  category_id    INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  display_name   TEXT NOT NULL,
  orig_name      TEXT,
  tg_file_id     TEXT NOT NULL,
  tg_unique_id   TEXT UNIQUE NOT NULL,
  clean_file_id  TEXT,
  size           BIGINT,
  is_free        BOOLEAN NOT NULL DEFAULT FALSE,
  created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS files_category_idx ON files (category_id);

CREATE TABLE IF NOT EXISTS unlocks (
  user_id      BIGINT NOT NULL,
  category_id  INT NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (user_id, category_id)
);
"""


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool = None

    def _ssl(self):
        mode = os.environ.get("PGSSL", "").lower()
        if mode == "false":
            return False
        host = urlparse(self.dsn).hostname or ""
        # Render internal hostnames have no dots (dpg-xxxx-a). External ones need TLS.
        if mode == "true" or ("." in host and host not in ("localhost", "127.0.0.1")):
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return False

    async def init(self):
        for attempt in range(1, 9):
            try:
                self.pool = await asyncpg.create_pool(self.dsn, ssl=self._ssl(), min_size=1, max_size=8)
                async with self.pool.acquire() as c:
                    await c.execute(SCHEMA)
                return
            except Exception as e:  # DB may still be starting on first deploy
                if attempt >= 8:
                    raise
                log.warning("db not ready (%s) - retry %s/8", e, attempt)
                await asyncio.sleep(2.5)

    async def close(self):
        if self.pool:
            await self.pool.close()

    # ── users ──
    async def upsert_user(self, tg_user, ref_code):
        for attempt in range(4):
            try:
                return await self.pool.fetchrow(
                    """INSERT INTO users (tg_id, username, first_name, ref_code, referred_by)
                       VALUES ($1, $2, $3, $4,
                         (SELECT tg_id FROM users WHERE ref_code = $5::text AND tg_id <> $1))
                       ON CONFLICT (tg_id) DO UPDATE
                         SET username = EXCLUDED.username, first_name = EXCLUDED.first_name
                       RETURNING *""",
                    tg_user.id, tg_user.username, tg_user.first_name,
                    secrets.token_hex(5), ref_code,
                )
            except asyncpg.UniqueViolationError:
                if attempt == 3:
                    raise  # ref_code collision: retry with a fresh code

    async def get_user(self, user_id):
        return await self.pool.fetchrow("SELECT * FROM users WHERE tg_id = $1", user_id)

    async def mark_verified(self, user_id):
        """returns (changed, referred_by) - changed only the first time"""
        row = await self.pool.fetchrow(
            """UPDATE users SET verified = TRUE, verified_at = now()
               WHERE tg_id = $1 AND verified = FALSE RETURNING referred_by""",
            user_id,
        )
        return (True, row["referred_by"]) if row else (False, None)

    async def ref_count(self, user_id):
        return await self.pool.fetchval(
            "SELECT count(*) FROM users WHERE referred_by = $1 AND verified", user_id)

    async def unlock_count(self, user_id):
        return await self.pool.fetchval("SELECT count(*) FROM unlocks WHERE user_id = $1", user_id)

    async def all_user_ids(self):
        return [r["tg_id"] for r in await self.pool.fetch("SELECT tg_id FROM users")]

    # ── categories & files ──
    async def list_categories(self):
        return await self.pool.fetch(
            """SELECT c.id, c.key, c.name, count(f.id) AS n
               FROM categories c JOIN files f ON f.category_id = c.id
               GROUP BY c.id ORDER BY c.name""")

    async def get_category(self, cat_id):
        return await self.pool.fetchrow("SELECT * FROM categories WHERE id = $1", cat_id)

    async def list_files(self, cat_id, page):
        total = await self.pool.fetchval("SELECT count(*) FROM files WHERE category_id = $1", cat_id)
        rows = await self.pool.fetch(
            """SELECT * FROM files WHERE category_id = $1
               ORDER BY is_free DESC, id ASC LIMIT $2 OFFSET $3""",
            cat_id, PAGE_SIZE, page * PAGE_SIZE)
        return rows, total

    async def get_file(self, file_id):
        return await self.pool.fetchrow(
            """SELECT f.*, c.name AS category_name FROM files f
               JOIN categories c ON c.id = f.category_id WHERE f.id = $1""", file_id)

    async def all_files(self):
        return await self.pool.fetch(
            """SELECT f.id, f.display_name, f.is_free, c.name AS category_name
               FROM files f JOIN categories c ON c.id = f.category_id
               ORDER BY c.name, f.is_free DESC, f.id""")

    async def add_file(self, category, display_name, orig_name, tg_file_id, tg_unique_id, size):
        """the first file of a category is automatically FREE"""
        name = title_case(category)
        cat = await self.pool.fetchrow(
            """INSERT INTO categories (key, name) VALUES ($1, $2)
               ON CONFLICT (key) DO UPDATE SET key = EXCLUDED.key RETURNING id, name""",
            name.lower(), name)
        row = await self.pool.fetchrow(
            """INSERT INTO files (category_id, display_name, orig_name, tg_file_id, tg_unique_id, size, is_free)
               VALUES ($1, $2, $3, $4, $5, $6, NOT EXISTS (SELECT 1 FROM files WHERE category_id = $1))
               ON CONFLICT (tg_unique_id) DO NOTHING RETURNING *""",
            cat["id"], display_name, orig_name, tg_file_id, tg_unique_id, size)
        return row, cat

    async def set_clean_file_id(self, file_id, clean_id):
        await self.pool.execute("UPDATE files SET clean_file_id = $2 WHERE id = $1", file_id, clean_id)

    async def delete_file(self, file_id):
        cat_id = await self.pool.fetchval("DELETE FROM files WHERE id = $1 RETURNING category_id", file_id)
        if cat_id is None:
            return False
        await self.pool.execute(
            """DELETE FROM categories c WHERE c.id = $1
               AND NOT EXISTS (SELECT 1 FROM files f WHERE f.category_id = c.id)""", cat_id)
        return True

    async def toggle_free(self, file_id):
        return await self.pool.fetchval(
            "UPDATE files SET is_free = NOT is_free WHERE id = $1 RETURNING is_free", file_id)

    # ── unlocks ──
    async def is_unlocked(self, user_id, cat_id):
        return bool(await self.pool.fetchval(
            "SELECT 1 FROM unlocks WHERE user_id = $1 AND category_id = $2", user_id, cat_id))

    async def unlock_category(self, user_id, cat_id, required):
        """spends one '5 invites' token on a category -> 'ok' | 'already' | 'no_credit'"""
        async with self.pool.acquire() as c:
            async with c.transaction():
                await c.execute("SELECT pg_advisory_xact_lock($1)", user_id)
                refs = await c.fetchval(
                    "SELECT count(*) FROM users WHERE referred_by = $1 AND verified", user_id)
                used = await c.fetchval("SELECT count(*) FROM unlocks WHERE user_id = $1", user_id)
                if await c.fetchval(
                        "SELECT 1 FROM unlocks WHERE user_id = $1 AND category_id = $2", user_id, cat_id):
                    return "already"
                if refs // required - used < 1:
                    return "no_credit"
                await c.execute("INSERT INTO unlocks (user_id, category_id) VALUES ($1, $2)", user_id, cat_id)
                return "ok"

    async def stats(self):
        q = self.pool.fetchval
        return {
            "users": await q("SELECT count(*) FROM users"),
            "verified": await q("SELECT count(*) FROM users WHERE verified"),
            "referred": await q("SELECT count(*) FROM users WHERE verified AND referred_by IS NOT NULL"),
            "files": await q("SELECT count(*) FROM files"),
            "categories": await q("SELECT count(*) FROM categories"),
            "unlocks": await q("SELECT count(*) FROM unlocks"),
        }


# ════════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════════

_photo_cache = {}  # filename -> telegram file_id (avoid re-uploading)
_bg_tasks = set()
_busy = set()  # users currently watching an animation


def spawn(coro):
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)


async def send_sigil(chat_id, caption, rows):
    """random sigil image + caption + inline keyboard (text-only if /assets is empty)"""
    files = []
    if ASSETS.is_dir():
        files = [p for p in ASSETS.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp")]
    if not files:
        return await BOT.send_message(chat_id, caption, reply_markup=markup(rows),
                                      link_preview_options=NO_PREVIEW)
    f = files[secrets.randbelow(len(files))]
    cached = _photo_cache.get(f.name)
    if cached:
        try:
            return await BOT.send_photo(chat_id, cached, caption=caption, reply_markup=markup(rows))
        except Exception:
            _photo_cache.pop(f.name, None)
    msg = await BOT.send_photo(chat_id, FSInputFile(f), caption=caption, reply_markup=markup(rows))
    if msg.photo:
        _photo_cache[f.name] = msg.photo[-1].file_id
    return msg


async def edit_screen(cb: CallbackQuery, text, rows):
    msg = cb.message
    try:
        if msg.photo:
            await msg.edit_caption(caption=text, reply_markup=markup(rows))
        else:
            await msg.edit_text(text, reply_markup=markup(rows), link_preview_options=NO_PREVIEW)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


async def safe_answer(cb: CallbackQuery, text=None, alert=False):
    try:
        await cb.answer(text, show_alert=alert)
    except Exception:
        pass


def ref_link(code):
    return f"https://t.me/{ME.username}?start=ref_{code}"


async def get_stats(user_id):
    refs = await DB.ref_count(user_id)
    used = await DB.unlock_count(user_id)
    credits = max(0, refs // CFG.required - used)
    progress = max(0, min(CFG.required, refs - CFG.required * used))
    return {"refs": refs, "used": used, "credits": credits, "progress": progress}


async def check_membership(user_id):
    missing, errors = [], []
    for ch in CFG.channels:
        try:
            m = await BOT.get_chat_member(ch.chat, user_id)
            ok = m.status in ("creator", "administrator", "member") or (
                m.status == "restricted" and getattr(m, "is_member", False))
            if not ok:
                missing.append(ch)
        except Exception as e:
            log.error("membership check failed for channel %s (%s): %s", ch.n, ch.chat, e)
            errors.append(ch)
    return missing, errors


async def build_vault(user):
    cats = await DB.list_categories()
    st = await get_stats(user["tg_id"])
    return ui_vault(user["first_name"], ref_link(user["ref_code"]), st, CFG.required,
                    cats, is_admin(user["tg_id"]))


async def send_vault(chat_id, user):
    caption, rows = await build_vault(user)
    return await send_sigil(chat_id, caption, rows)


async def send_gate(chat_id, name):
    caption, rows = ui_gate(name, CFG.channels)
    return await send_sigil(chat_id, caption, rows)


async def require_verified(cb: CallbackQuery):
    user = await DB.get_user(cb.from_user.id) or await DB.upsert_user(cb.from_user, None)
    if user["verified"]:
        return user
    await safe_answer(cb, "🔒 Take the oath first.")
    await send_gate(cb.from_user.id, cb.from_user.first_name)
    return None


async def render_category(cb: CallbackQuery, user, cat_id, page, animate):
    cat = await DB.get_category(cat_id)
    if not cat:
        await safe_answer(cb, "This vault has vanished.", True)
        return
    admin = is_admin(user["tg_id"])
    files, total = await DB.list_files(cat_id, page)
    st = await get_stats(user["tg_id"])
    unlocked = admin or await DB.is_unlocked(user["tg_id"], cat_id)
    link = ref_link(user["ref_code"])

    def caption(frame):
        return ui_category_caption(cat, total, st, CFG.required, unlocked, frame, admin)

    rows = ui_category_keyboard(cat, files, total, page, st, unlocked, link, admin)

    uid = user["tg_id"]
    if not unlocked and animate and uid not in _busy:
        _busy.add(uid)
        try:
            for frame in progress_frames(st["progress"], CFG.required):
                await edit_screen(cb, caption(frame), rows)
                await asyncio.sleep(0.38)
        except Exception as e:  # e.g. flood limit - skip straight to the final frame
            log.warning("animation skipped: %s", e)
        finally:
            _busy.discard(uid)
    await edit_screen(cb, caption(None), rows)


async def deliver(user_id, f):
    """first delivery re-uploads the file under its clean name and caches the new file_id"""
    cap = ui_file_caption(f)
    if f["clean_file_id"]:
        try:
            return await BOT.send_document(user_id, f["clean_file_id"], caption=cap)
        except Exception as e:
            log.warning("clean file_id failed, falling back: %s", e)
    needs_rename = (CFG.clean_names and f["orig_name"] and f["orig_name"] != f["display_name"]
                    and (not f["size"] or f["size"] <= MAX_REUPLOAD))
    if needs_rename:
        try:
            buf = io.BytesIO()
            await BOT.download(f["tg_file_id"], destination=buf)
            sent = await BOT.send_document(
                user_id, BufferedInputFile(buf.getvalue(), filename=f["display_name"]), caption=cap)
            if sent.document:
                await DB.set_clean_file_id(f["id"], sent.document.file_id)
            return sent
        except Exception as e:
            log.warning("rename failed, sending original: %s", e)
    return await BOT.send_document(user_id, f["tg_file_id"], caption=cap)


async def notify_referrer(referrer_id, new_user):
    try:
        st = await get_stats(referrer_id)
        who = f"@{new_user.username}" if new_user.username else (new_user.first_name or "An initiate")
        rows = [[{"text": "🗝 Open the Vault", "callback_data": "vault:new"}]]
        frames = progress_frames(st["progress"], CFG.required)
        msg = await BOT.send_message(
            referrer_id, ui_ref_notice(who, st, CFG.required, frames[0]), reply_markup=markup(rows))
        for frame in frames[1:]:
            await asyncio.sleep(0.38)
            try:
                await msg.edit_text(ui_ref_notice(who, st, CFG.required, frame), reply_markup=markup(rows))
            except Exception:
                pass
        await asyncio.sleep(0.38)
        await msg.edit_text(ui_ref_notice(who, st, CFG.required, None, True), reply_markup=markup(rows))
    except Exception as e:
        log.warning("notify_referrer failed (maybe blocked the bot): %s", e)


# ════════════════════════════════════════════════════════════════
#  HANDLERS
# ════════════════════════════════════════════════════════════════

router = Router()
router.message.filter(F.chat.type == "private")
router.callback_query.filter(F.message.chat.type == "private")
ME = None


@router.error()
async def on_error(event: ErrorEvent):
    log.exception("handler error: %s", event.exception)
    cb = event.update.callback_query
    if cb:
        await safe_answer(cb, "⚠ The sigil flickered. Try again.")
    return True


# ── user commands ──

@router.message(CommandStart())
async def h_start(m: Message, command: CommandObject):
    code = None
    if command.args:
        mm = re.fullmatch(r"ref_([a-f0-9]{10})", command.args.strip())
        if mm:
            code = mm.group(1)
    user = await DB.upsert_user(m.from_user, code)
    if user["verified"]:
        await send_vault(m.chat.id, user)
    else:
        await send_gate(m.chat.id, m.from_user.first_name)


@router.message(Command("vault", "invite"))
async def h_vault_cmd(m: Message):
    user = await DB.get_user(m.from_user.id) or await DB.upsert_user(m.from_user, None)
    if not user["verified"]:
        await send_gate(m.chat.id, m.from_user.first_name)
    else:
        await send_vault(m.chat.id, user)


@router.message(Command("help"))
async def h_help(m: Message):
    await m.answer(HELP_TEXT, link_preview_options=NO_PREVIEW)


@router.message(Command("id"))
async def h_id(m: Message):
    await m.answer(f"<code>{m.from_user.id}</code>")


# ── callbacks ──

@router.callback_query(F.data == "noop")
async def h_noop(cb: CallbackQuery):
    await safe_answer(cb)


@router.callback_query(F.data == "verify")
async def h_verify(cb: CallbackQuery):
    user = await DB.get_user(cb.from_user.id) or await DB.upsert_user(cb.from_user, None)
    missing, errors = await check_membership(cb.from_user.id)
    if errors:
        return await safe_answer(
            cb, "⚠ SIGIL FAULT\nThe channel check is offline. Tell the admin to make the bot "
                "an admin of both channels.", True)
    if missing:
        return await safe_answer(cb, ui_not_joined(missing), True)

    await safe_answer(cb, "✔ OATH ACCEPTED")
    changed, referred_by = await DB.mark_verified(cb.from_user.id)
    fresh = await DB.get_user(cb.from_user.id) or user
    await send_vault(cb.from_user.id, fresh)
    try:
        await cb.message.delete()
    except Exception:
        pass
    if changed and referred_by:
        spawn(notify_referrer(referred_by, cb.from_user))


@router.callback_query(F.data.in_({"vault", "vault:new"}))
async def h_vault(cb: CallbackQuery):
    await safe_answer(cb)
    user = await require_verified(cb)
    if not user:
        return
    if cb.data == "vault:new" or not cb.message.photo:
        await send_vault(cb.from_user.id, user)
        return
    caption, rows = await build_vault(user)
    await edit_screen(cb, caption, rows)


@router.callback_query(F.data.regexp(r"^cat:\d+:\d+$"))
async def h_cat(cb: CallbackQuery):
    await safe_answer(cb)
    user = await require_verified(cb)
    if not user:
        return
    _, cat_id, page = cb.data.split(":")
    await render_category(cb, user, int(cat_id), int(page), True)


@router.callback_query(F.data.regexp(r"^unlock:\d+$"))
async def h_unlock(cb: CallbackQuery):
    user = await require_verified(cb)
    if not user:
        return
    cat_id = int(cb.data.split(":")[1])
    result = await DB.unlock_category(user["tg_id"], cat_id, CFG.required)
    if result == "no_credit":
        st = await get_stats(user["tg_id"])
        return await safe_answer(cb, ui_locked_alert(CFG.required - st["progress"], 0), True)
    await safe_answer(cb, "🔓 THE SEAL IS BROKEN" if result == "ok" else "Already unsealed.")
    await render_category(cb, user, cat_id, 0, False)


@router.callback_query(F.data.regexp(r"^get:\d+$"))
async def h_get(cb: CallbackQuery):
    user = await require_verified(cb)
    if not user:
        return
    f = await DB.get_file(int(cb.data.split(":")[1]))
    if not f:
        return await safe_answer(cb, "This relic has been erased.", True)

    missing, errors = await check_membership(cb.from_user.id)  # still in the circle?
    if missing and not errors:
        await safe_answer(cb, "☠ YOU LEFT THE CIRCLE\nRejoin the channels and verify again.", True)
        return await send_gate(cb.from_user.id, cb.from_user.first_name)

    allowed = f["is_free"] or is_admin(user["tg_id"]) or await DB.is_unlocked(user["tg_id"], f["category_id"])
    if not allowed:
        st = await get_stats(user["tg_id"])
        return await safe_answer(
            cb, ui_locked_alert(CFG.required - st["progress"], st["credits"]), True)

    await safe_answer(cb, "📡 Transmitting relic…")
    try:
        await deliver(cb.from_user.id, f)
    except Exception as e:
        log.error("deliver failed: %s", e)
        await BOT.send_message(cb.from_user.id, "⚠ The transmission failed. Try again in a moment.")


# ── admin ──

def admin_filter(m: Message) -> bool:
    return bool(m.from_user) and is_admin(m.from_user.id)


@router.message(F.document, admin_filter)
async def h_upload(m: Message):
    d = m.document
    parsed = parse_file_label(m.caption) or parse_file_label(d.file_name)
    display = parsed[0] if parsed else (d.file_name or "file")
    category = parsed[1] if parsed else "General"

    row, cat = await DB.add_file(category, display, d.file_name, d.file_id, d.file_unique_id, d.file_size)
    if row is None:
        return await m.answer(f"⚠ Already sealed in <b>{esc(cat['name'])}</b>.")
    await m.answer("\n".join([
        "<b>✔ RELIC SEALED</b>",
        f"<code>#{row['id']}</code> {esc(row['display_name'])}",
        f"Vault: <b>{esc(cat['name'])}</b>"
        + ("" if parsed else " <i>(no category found in the name → General)</i>"),
        "Access: " + ("🆓 <b>FREE</b> (first in this vault)" if row["is_free"] else "🔒 locked"),
    ]))


@router.message(Command("files"), admin_filter)
async def h_files(m: Message):
    rows = await DB.all_files()
    if not rows:
        return await m.answer("No relics sealed yet. Send me a file named  name.ext-Category")
    chunks, out, cat = [], "", None
    for f in rows:
        block = ""
        if f["category_name"] != cat:
            cat = f["category_name"]
            block += f"\n<b>{esc(cat)}</b>\n"
        block += f"<code>#{f['id']}</code> {'🆓' if f['is_free'] else '🔒'} {esc(f['display_name'])}\n"
        if len(out) + len(block) > 3500:
            chunks.append(out)
            out = ""
        out += block
    chunks.append(out)
    for c in chunks:
        await m.answer(c)
    await m.answer("<code>/delfile ID</code> · <code>/free ID</code> (toggle free/locked)")


def _id_arg(command: CommandObject):
    a = (command.args or "").strip()
    return int(a) if a.isdigit() else None


@router.message(Command("delfile"), admin_filter)
async def h_delfile(m: Message, command: CommandObject):
    fid = _id_arg(command)
    if not fid:
        return await m.answer("Usage: /delfile ID")
    await m.answer(f"🗑 Relic #{fid} erased." if await DB.delete_file(fid) else "No such relic.")


@router.message(Command("free"), admin_filter)
async def h_free(m: Message, command: CommandObject):
    fid = _id_arg(command)
    if not fid:
        return await m.answer("Usage: /free ID")
    v = await DB.toggle_free(fid)
    await m.answer("No such relic." if v is None else f"Relic #{fid} is now {'🆓 FREE' if v else '🔒 locked'}.")


@router.message(Command("stats"), admin_filter)
async def h_stats(m: Message):
    s = await DB.stats()
    await m.answer("\n".join([
        "<b>◬ BOLTIX EMPIRE // STATS</b>",
        f"<code>users      {s['users']}</code>",
        f"<code>verified   {s['verified']}</code>",
        f"<code>via refs   {s['referred']}</code>",
        f"<code>vaults     {s['categories']}</code>",
        f"<code>relics     {s['files']}</code>",
        f"<code>unseals    {s['unlocks']}</code>",
    ]))


@router.message(Command("broadcast"), admin_filter)
async def h_broadcast(m: Message, command: CommandObject):
    text = (command.args or "").strip()
    if not text:
        return await m.answer("Usage: /broadcast your message")
    ids = await DB.all_user_ids()
    await m.answer(f"📡 Broadcasting to {len(ids)} souls…")
    ok = fail = 0
    for uid in ids:
        try:
            await BOT.send_message(uid, text, parse_mode=None)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.045)  # stay under Telegram's ~30 msg/s limit
    await m.answer(f"✔ Delivered: {ok}\n✖ Failed: {fail}")


# ════════════════════════════════════════════════════════════════
#  SERVER / ENTRYPOINT
# ════════════════════════════════════════════════════════════════

async def health(_request):
    return web.Response(text="BOLTIX EMPIRE // the signal is alive")


async def keep_alive_loop(url):
    # Free Render web services sleep after ~15 min without inbound traffic.
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(url + "/health", timeout=aiohttp.ClientTimeout(total=20))
            except Exception:
                pass


async def main():
    global CFG, BOT, DB, ME
    CFG = load_config()
    DB = Database(CFG.dsn)
    await DB.init()
    log.info("database ready")

    BOT = Bot(token=CFG.token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    ME = await BOT.get_me()
    log.info("bot @%s online | admins: %d | %d invites per unseal", ME.username, len(CFG.admins), CFG.required)
    if not CFG.admins:
        log.warning("ADMIN_IDS is empty - nobody can upload files. Send /id to the bot to get your id.")

    dp = Dispatcher()
    dp.include_router(router)

    # secrets are derived from the bot token, so no extra env var is needed
    hook_secret = hashlib.sha256(("secret:" + CFG.token).encode()).hexdigest()
    hook_path = "/tg/" + hashlib.sha256(("path:" + CFG.token).encode()).hexdigest()[:32]

    app = web.Application()
    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    webhook_mode = bool(CFG.public_url)
    if webhook_mode:
        SimpleRequestHandler(dispatcher=dp, bot=BOT, secret_token=hook_secret).register(app, path=hook_path)
        setup_application(app, dp, bot=BOT)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", CFG.port).start()
    log.info("http listening on :%d", CFG.port)

    await BOT.set_my_commands([
        BotCommand(command="start", description="Enter the empire"),
        BotCommand(command="vault", description="Open your vault"),
        BotCommand(command="invite", description="Your sigil link & progress"),
        BotCommand(command="help", description="Commands"),
    ])

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass

    try:
        if webhook_mode:
            await BOT.set_webhook(
                CFG.public_url + hook_path,
                secret_token=hook_secret,
                allowed_updates=["message", "callback_query"],
                max_connections=40,
            )
            log.info("webhook set (mode: webhook)")
            if CFG.keep_alive:
                spawn(keep_alive_loop(CFG.public_url))
                log.info("keep-alive ping enabled (every 10 min)")
            await stop.wait()
        else:
            log.info("no PUBLIC_URL / RENDER_EXTERNAL_URL -> long polling mode")
            await BOT.delete_webhook()
            poll = asyncio.create_task(
                dp.start_polling(BOT, allowed_updates=["message", "callback_query"], handle_signals=False))
            waiter = asyncio.create_task(stop.wait())
            await asyncio.wait({poll, waiter}, return_when=asyncio.FIRST_COMPLETED)
            poll.cancel()
    finally:
        log.info("shutting down")
        await runner.cleanup()
        await BOT.session.close()
        await DB.close()


if __name__ == "__main__":
    asyncio.run(main())
