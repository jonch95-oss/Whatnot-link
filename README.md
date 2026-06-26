# Whatnot User Tagger

A Python bot that logs into Whatnot, joins any live show as a **regular viewer**, collects usernames from chat, then `@` tags those users in your own live show.

**You do not need to be a moderator.** Mod access is optional and only adds the viewer list panel as an extra source.

## How it works

1. **Login** — Opens a Chromium browser and signs into your Whatnot account (supports saved sessions).
2. **Collect** — Visits the target show and gathers usernames from:
   - Live chat messages (WebSocket + DOM)
   - **Sold list** — buyers shown on sold items
   - **Activity tab** — purchases and auction wins
   - `@mentions` and profile links
   - API / WebSocket sale events
   - Mod **viewer list** panel (bonus — only if you're a moderator)
3. **Tag** — Goes to your show and sends `@username` messages in chat to invite viewers over.

## Prerequisites

- Python 3.10+
- Any Whatnot account (viewer access is enough)
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
| `TARGET_SHOW_URL` | Show to watch and collect users from |
| `OWN_SHOW_URL` | Your show (to @tag users in) |
| `COLLECT_DURATION_SECONDS` | How long to watch the target show (default: 120; use 180–300 for busier shows) |
| `TAG_DELAY_SECONDS` | Pause between each @tag (default: 2.5 — keep ≥2 to avoid rate limits) |
| `TAG_MESSAGE` | Message appended after each @tag |
| `SKIP_OWN_USERNAME` | Skip your own username when collecting (default: true) |
| `TRY_VIEWER_LIST` | Attempt mod viewer list if available (default: true) |
| `COLLECT_SOLD_LIST` | Scrape sold list and activity tab for buyers (default: true) |

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

## Viewer vs moderator

| Source | Regular viewer | Moderator |
|---|---|---|
| Live chat usernames | Yes | Yes |
| Sold list / buyers | Yes | Yes |
| Activity (purchases, wins) | Yes | Yes |
| @mentions in chat | Yes | Yes |
| WebSocket / API data | Yes | Yes |
| Full viewer list panel | No | Yes |

As a viewer you'll collect people who **chat** and **buy** during your watch window. Longer `COLLECT_DURATION_SECONDS` (e.g. 3–5 minutes) on an active show will gather more names.

## Important notes

- **2FA**: If your account uses two-factor authentication, the browser window stays open so you can complete it manually.
- **Rate limits**: Whatnot enforces chat rate limits. Use `TAG_DELAY_SECONDS` of at least 2–3 seconds between messages.
- **Terms of service**: Mass @tagging may violate Whatnot's community guidelines. Use responsibly and only invite users who would genuinely be interested in your show.
- **Session file**: After the first login, credentials are stored in `whatnot_session.json` so you don't need to log in every run.

## Output

Collected usernames are saved to `collected_users.txt` (one per line) so you can review, edit, or reuse them before tagging.
