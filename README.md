# ◬ BOLTIX EMPIRE — Telegram bot (Python, single file: `bot.py`)

Gated vault bot: join 2 channels → verify → get your sigil (referral) link → open category vaults.
1 relic per vault is free; invite **5** verified people to unseal one vault of your choice.

## How it works

1. `/start` → random sigil image + caption **JOIN BOLTIX CHANNEL** + 3 buttons: *Channel 1*, *Channel 2*, *Verify*.
2. **Verify** checks both channels with `getChatMember`. Not joined → alert. Joined → a **new** message with another random sigil, the user's personal referral link, and one button per category.
3. Tap a category → the same message is **edited** into the file list (free file open, rest 🔒) with a **live animated progress bar** (`▰▰▰▱▱▱▱▱▱▱ 3/5`).
4. Every 5 verified invites = 1 unseal token. In a locked vault tap **🗝 UNSEAL** to spend a token on that category.
5. When someone joins through a link, the referrer gets a live-animated notification with the updated bar.

A referral only counts when the invited person is **new** to the bot *and* passes verification (no self-invites, no old users).

## Uploading files (you, the admin)

Send files to the bot **as documents** from an account listed in `ADMIN_IDS`. The **last word of the name is the category**:

| File name you send | Becomes |
|---|---|
| `index.js-Automation` | `index.js` in vault **Automation** |
| `Index.html automation` | `Index.html` in vault **Automation** |
| `scraper.py-Web Scraping` | `scraper.py` in vault **Web Scraping** |

- Hyphen form `name-Category` is preferred (category may have spaces). Space form `name category` takes one word.
- A caption works too and wins over the file name: send any file with the caption `name.ext-Category`.
- The **first** file uploaded to a category is automatically **FREE**; the rest are locked.
- The user receives the file with the clean name (`index.js`), not `index.js-Automation`. (First delivery re-uploads it once, then it is cached.)
- Names with no category (`plain.txt`) go to **General**. A dotted tail like `my-report.pdf` is *not* treated as a category.

Admin commands: `/files` · `/delfile ID` · `/free ID` (toggle free/locked) · `/stats` · `/broadcast text` · `/id`

## Deploy on Render (Blueprint)

1. Push this folder to a GitHub repo (the three sigil images are already in `assets/`).
2. **Make the bot an admin of both channels** (Hoshigami_anime and Boltix_labs). Telegram only lets a bot check membership where it is an admin.
3. Render dashboard → **New → Blueprint** → select the repo. `render.yaml` creates the Postgres database (`boltix-db`), the web service (`boltix-empire`), and links `DATABASE_URL` for you.
4. Render asks for two secrets: **`BOT_TOKEN`** and **`ADMIN_IDS`** (your numeric Telegram id; if unknown, deploy first, send `/id` to the bot, then set it in *Environment*).
5. Deploy. The bot registers its own webhook using `RENDER_EXTERNAL_URL`. Open the bot and send `/start`.

### Free-plan facts you should know
- **Free Postgres expires 30 days after creation** (14-day grace to upgrade, then deleted). All data — users, referrals, file records — lives there. Upgrade the DB plan in Render before it expires, or recreate it and re-upload the files. File records are Telegram `file_id`s stored in this DB.
- Only **one free Postgres per Render workspace**.
- Free web services sleep after ~15 min idle. Telegram's webhook wakes it (the first reply can take up to a minute). The bot pings its own `/health` every 10 min (`KEEP_ALIVE=true`); for extra reliability add a free uptime monitor on `https://<your-app>.onrender.com/health`. 750 free instance hours/month covers one always-on service.
- The database is created with `ipAllowList: []` (private network only). To connect from your PC, change that in `render.yaml`.

## Environment variables

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | from @BotFather |
| `DATABASE_URL` | — | set by the Blueprint |
| `ADMIN_IDS` | — | comma separated numeric ids |
| `CHANNEL_1_URL` / `CHANNEL_2_URL` | the two channels | change channels here |
| `CHANNEL_1_ID` / `CHANNEL_2_ID` | — | only for private (`t.me/+…`) channels |
| `REFERRALS_REQUIRED` | `5` | invites per unseal |
| `CLEAN_FILENAMES` | `true` | strip `-Category` from delivered file names |
| `KEEP_ALIVE` | `true` | self-ping on Render |

## Run locally

```bash
pip install -r requirements.txt
export BOT_TOKEN=... DATABASE_URL=postgres://user:pass@localhost:5432/boltix ADMIN_IDS=123456789
python bot.py          # long polling when PUBLIC_URL / RENDER_EXTERNAL_URL is not set
```

## Project layout

```
bot.py            the whole bot: config, file-name parser, UI/copy, database, handlers, server
requirements.txt  aiogram 3 + asyncpg
render.yaml       Render Blueprint (web service + Postgres + env vars)
assets/           sigil images (random pick) - add/remove freely
```

Want different wording? Everything the user reads is in the **UI** section of `bot.py`
(`CREED`, `SEALED_LINES`, `UNSEALED_LINES`, `STATUS_FRAMES`, `HEADER`).
