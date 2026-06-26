# Whatnot Mod User Tagger

A Python bot that logs into Whatnot, joins a show you're moderating, collects usernames from chat and the mod viewer list, then `@` tags those users in your own live show.

## How it works

1. **Login** — Opens a Chromium browser and signs into your Whatnot account (supports saved sessions).
2. **Collect** — Visits the target show and gathers usernames from:
   - Live chat (WebSocket + DOM)
   - API responses
   - Mod **viewer list** panel (if you're a moderator on that show)
3. **Tag** — Goes to your show and sends `@username` messages in chat to invite viewers over.

## Prerequisites

- Python 3.10+
- A Whatnot account that is a **moderator** on the target show
- Your own live show URL (you must be live or have chat open)

## Setup

```bash
cp .env.example .env
# Edit .env with your credentials and show URLs

pip install -r requirements.txt
playwright install chromium
```

## Configuration (.env)

| Variable | Description |
|---|---|
| `WHATNOT_USERNAME` | Your Whatnot email or phone |
| `WHATNOT_PASSWORD` | Your Whatnot password |
| `TARGET_SHOW_URL` | Show you're modding (to collect users from) |
| `OWN_SHOW_URL` | Your show (to @tag users in) |
| `COLLECT_DURATION_SECONDS` | How long to watch the target show (default: 120) |
| `TAG_DELAY_SECONDS` | Pause between each @tag (default: 2.5 — keep ≥2 to avoid rate limits) |
| `TAG_MESSAGE` | Message appended after each @tag |
| `SKIP_OWN_USERNAME` | Skip your own username when collecting (default: true) |

## Usage

```bash
# Full run: collect users, then tag them in your show
python bot.py

# Only collect users (saves to collected_users.txt)
python bot.py --collect-only

# Only tag users from a previous collection
python bot.py --tag-only

# Use a custom user list file
python bot.py --tag-only --users-file my_users.txt
```

## Important notes

- **Moderator access**: The full viewer list is only visible if the seller has added you as a mod. Chat collection still works without mod access, but you'll get fewer names.
- **2FA**: If your account uses two-factor authentication, the browser window stays open so you can complete it manually.
- **Rate limits**: Whatnot enforces chat rate limits. Use `TAG_DELAY_SECONDS` of at least 2–3 seconds between messages.
- **Terms of service**: Mass @tagging may violate Whatnot's community guidelines. Use responsibly and only invite users who would genuinely be interested in your show.
- **Session file**: After the first login, credentials are stored in `whatnot_session.json` so you don't need to log in every run.

## Output

Collected usernames are saved to `collected_users.txt` (one per line) so you can review, edit, or reuse them before tagging.
