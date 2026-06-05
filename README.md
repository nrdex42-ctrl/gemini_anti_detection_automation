# Anti Detection FB Automation

Authorized Facebook page posting automation with a Telegram control bot, Render deployment config, and Supabase/Postgres storage.

This repository is intended for accounts and pages you own or are explicitly authorized to manage. Do not commit cookies, bot tokens, database URLs, or other credentials.

## What Is Included

- Playwright posting engine for text, image, and video posts.
- Telegram webhook bot in `telegram_bot.py`.
- Supabase/Postgres schema in `supabase/schema.sql`.
- Render Blueprint in `render.yaml`.
- Account isolation and cookie-use cooldown stored in Supabase.
- Local live-test scripts for controlled manual validation.

## Local Setup

```bash
cd "/home/shabana/Public/anti-detection FB automation"
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

Generate an encryption key for `.env`:

```bash
python scripts/generate_fernet_key.py
```

Initialize the database schema:

```bash
export DATABASE_URL="postgresql://..."
python scripts/init_supabase.py
```

Run the bot locally:

```bash
python telegram_bot.py
```

## Telegram Bot Commands

- `/dashboard` opens the persistent typing-area dashboard panel.
- `/start` or `/help` shows usage.
- `/add_account <account_id> <raw_cookie>` stores or updates an account cookie.
- `/add_account auto <raw_cookie>` derives the account id from `c_user`.
- `/accounts` lists stored accounts.
- `/remove_account <account_id>` deactivates an account.
- `/pages <account_id>` discovers and stores managed pages.
- `/list_pages <account_id>` lists stored pages.
- `/post <account_id> <page_id_or_url> <text|image|video> <caption>` queues a post.

For image/video posts, attach the media to the Telegram message or reply to a media message with the `/post` command.

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
- My Accounts
- Check All Cookies
- Post History
- Discover Pages
- Stored Pages
- Bot Status

Cookie ingestion accepts:

- Raw cookie strings.
- JSON cookie arrays.
- `{ "cookies": [...] }` browser-export payloads.
- Uploaded JSON files.
- Long JSON pasted across multiple messages, finished with `/done`.

The quick-post buttons use guided steps:

1. Use the active account, or choose/switch an account first.
2. Choose a stored page or type a page id/full URL.
3. Send text, image, or video depending on the selected post type.

`Post to All Pages` queues one batch for all stored pages of the active account and uses one isolated account session for that batch.

Slash commands remain available for direct automation and testing.

## Deployment

See `DEPLOYMENT.md` for the Render, Supabase, Telegram webhook, and GitHub push checklist.

## Security Notes

- `.gitignore` excludes `.env`, cookies, session state, diagnostics, and artifacts.
- Set `BOT_ADMIN_IDS` in production so only approved Telegram users can control the bot.
- Set `ENCRYPTION_KEY` before storing cookies. If this key changes, previously encrypted cookies cannot be decrypted.
- Rotate any account cookie that was pasted into logs, chat history, screenshots, or committed files.
- `BOT_ACCOUNT_COOKIE_COOLDOWN_SECONDS` defaults to `360` seconds to avoid overlapping account sessions.
