# Anti Detection FB Automation

Authorized Facebook page posting automation with a Telegram control bot, Render deployment config, and Supabase/Postgres storage.

This repository is intended for accounts and pages you own or are explicitly authorized to manage. Do not commit cookies, bot tokens, database URLs, or other credentials.

## What Is Included

- Playwright posting engine for text, image, and video posts.
- Telegram webhook bot in `telegram_bot.py`.
- Supabase/Postgres schema in `supabase/schema.sql`.
- Render Blueprint in `render.yaml`.
- Account isolation locks stored in Supabase.
- Local live-test scripts for controlled manual validation.

## Local Setup

```bash
cd "/home/shabana/Public/anti-detection FB automation"
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env
```

Generate an encryption key for `.env`:

```bash
python3 scripts/generate_fernet_key.py
```

Initialize the database schema:

```bash
export DATABASE_URL="postgresql://..."
python3 scripts/init_supabase.py
```

Run the bot locally:

```bash
python3 telegram_bot.py
```

## Telegram Bot Commands

- `/start` opens the persistent typing-area dashboard panel.

All account, page, posting, and admin actions are handled through dashboard buttons.

## Dashboard Panel

The bot sends a persistent Telegram reply keyboard in the typing area. The dashboard mirrors the older bot flow with an active account model:

- Add Facebook Account
- Post With Active Account
- Quick Text Post
- Quick Image Post
- Quick Video Post
- Post to All Pages
- Switch Active Account
- Select Account & Post
- Refresh Pages
- My Accounts
- Check All Cookies
- Post History
- Discover Pages
- Stored Pages
- Bot Status
- Admin Dashboard for users listed in `BOT_ADMIN_IDS`

Cookie ingestion accepts:

- Raw cookie strings.
- JSON cookie arrays.
- `{ "cookies": [...] }` browser-export payloads.
- Uploaded JSON files.
- Long JSON pasted across multiple messages, finished with the `✅ Done` button.

The quick-post buttons use guided steps:

1. Use the active account, or choose/switch an account first.
2. Choose a stored page or type a page id/full URL.
3. Send text, image, or video depending on the selected post type.

Stored page discovery is cached in Supabase. Posting uses the saved page list and does not rediscover pages each time. Use `Refresh Pages` when an account gains/loses page access or you want to update the cache.

`Post to All Pages` queues one batch for all stored pages of the active account and uses one isolated account session for that batch.

Admin dashboard buttons expose system stats, users, accounts, post stats, runtime locks, and key runtime config values.

On each new Render deploy, the bot sends known users a refreshed dashboard message once per deploy revision.

## Deployment

See `DEPLOYMENT.md` for the Render, Supabase, Telegram webhook, and GitHub push checklist.

## Security Notes

- `.gitignore` excludes `.env`, cookies, session state, diagnostics, and artifacts.
- Set `BOT_ADMIN_IDS` in production to restrict the Admin Dashboard. With `BOT_REQUIRE_USER_APPROVAL=true`, new Telegram users are pending until an admin approves them from the Users card.
- Set `ENCRYPTION_KEY` before storing cookies. If this key changes, previously encrypted cookies cannot be decrypted.
- Rotate any account cookie that was pasted into logs, chat history, screenshots, or committed files.
- Normal posting has no cookie cooldown by default; account overlap is controlled by the runtime lock.
