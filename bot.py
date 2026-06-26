"""
Whatnot Mod User Tagger
-----------------------
Joins a Whatnot show as a moderator, collects active chatters,
then @tags them in your own show.

Usage:
    cp .env.example .env        # fill in your credentials and show URLs
    pip install -r requirements.txt
    python bot.py
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, WebSocket

load_dotenv()

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

USERNAME = os.getenv("WHATNOT_USERNAME", "")
PASSWORD = os.getenv("WHATNOT_PASSWORD", "")
TARGET_SHOW_URL = os.getenv("TARGET_SHOW_URL", "")
OWN_SHOW_URL = os.getenv("OWN_SHOW_URL", "")
COLLECT_DURATION = int(os.getenv("COLLECT_DURATION_SECONDS", "120"))
TAG_DELAY = float(os.getenv("TAG_DELAY_SECONDS", "2.5"))
TAG_MESSAGE = os.getenv("TAG_MESSAGE", "Come check out this show!")

USERS_FILE = Path("collected_users.txt")

# Selectors — Whatnot uses dynamic class names; we try multiple candidates.
CHAT_INPUT_SELECTORS = [
    'textarea[placeholder*="Say something"]',
    'textarea[placeholder*="chat"]',
    'input[placeholder*="Say something"]',
    'input[placeholder*="chat"]',
    '[data-testid="chat-input"]',
    '[class*="ChatInput"] textarea',
    '[class*="chat-input"] textarea',
    '[class*="commentInput"]',
    '[class*="CommentInput"]',
]

USERNAME_IN_CHAT_SELECTORS = [
    '[data-testid="chat-username"]',
    '[class*="Username"]',
    '[class*="username"]',
    '[class*="displayName"]',
    '[class*="authorName"]',
]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_env():
    missing = [k for k, v in {
        "WHATNOT_USERNAME": USERNAME,
        "WHATNOT_PASSWORD": PASSWORD,
        "TARGET_SHOW_URL": TARGET_SHOW_URL,
        "OWN_SHOW_URL": OWN_SHOW_URL,
    }.items() if not v]
    if missing:
        print(f"[!] Missing required env vars: {', '.join(missing)}")
        print("    Copy .env.example to .env and fill in your details.")
        sys.exit(1)


def extract_usernames_from_json(data: object, found: set):
    """Recursively walk a JSON blob and harvest any username-shaped values."""
    if isinstance(data, dict):
        for key in ("username", "user_name", "handle", "displayName", "display_name",
                    "userName", "screenName", "screen_name"):
            val = data.get(key)
            if isinstance(val, str) and 2 <= len(val) <= 50:
                found.add(val.strip().lstrip("@"))
        for v in data.values():
            extract_usernames_from_json(v, found)
    elif isinstance(data, list):
        for item in data:
            extract_usernames_from_json(item, found)


# ---------------------------------------------------------------------------
# Core bot
# ---------------------------------------------------------------------------

class WhatnotBot:
    def __init__(self):
        self.collected_users: set[str] = set()
        self._ws_users: set[str] = set()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def login(self, page: Page):
        print("[*] Opening Whatnot login page...")
        await page.goto("https://www.whatnot.com/login", wait_until="domcontentloaded")

        # Accept cookies banner if present
        try:
            accept_btn = page.locator('button:has-text("Accept"), button:has-text("Got it")')
            await accept_btn.first.click(timeout=3000)
        except Exception:
            pass

        # Try email/password form
        try:
            await page.locator('input[type="email"], input[name="email"]').fill(USERNAME, timeout=8000)
            await page.locator('input[type="password"], input[name="password"]').fill(PASSWORD)
            await page.locator('button[type="submit"]').click()
        except Exception:
            # Some flows start with phone or a different layout
            print("[!] Could not find standard login form — trying phone/alt flow")
            await page.locator('input[type="tel"], input[placeholder*="phone"]').fill(USERNAME, timeout=8000)
            await page.locator('button[type="submit"]').click()

        # Wait for redirect away from /login
        try:
            await page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
        except Exception:
            # May need manual 2FA — give user time
            print("[!] Login did not redirect automatically.")
            print("    If 2FA is required, complete it in the browser window.")
            print("    Waiting up to 60 seconds for you to finish...")
            await page.wait_for_url(lambda url: "/login" not in url, timeout=60000)

        print("[+] Logged in successfully")

    # ------------------------------------------------------------------
    # Collect users from target show
    # ------------------------------------------------------------------

    async def collect_users(self, page: Page):
        print(f"[*] Navigating to target show...")

        # Intercept HTTP responses that may contain user data
        async def on_response(response):
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    body = await response.json()
                    before = len(self._ws_users)
                    extract_usernames_from_json(body, self._ws_users)
                    new_count = len(self._ws_users) - before
                    if new_count:
                        print(f"  [+] API response yielded {new_count} new user(s)")
                except Exception:
                    pass

        # Intercept WebSocket frames for real-time chat
        def on_websocket(ws: WebSocket):
            def on_frame(payload: str | bytes):
                text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="ignore")
                try:
                    data = json.loads(text)
                    before = len(self._ws_users)
                    extract_usernames_from_json(data, self._ws_users)
                    new_count = len(self._ws_users) - before
                    if new_count:
                        for u in list(self._ws_users)[-new_count:]:
                            print(f"  [+] Chat user: {u}")
                except Exception:
                    pass
            ws.on("framereceived", on_frame)

        page.on("response", on_response)
        page.on("websocket", on_websocket)

        await page.goto(TARGET_SHOW_URL, wait_until="domcontentloaded")

        print(f"[*] Collecting users for {COLLECT_DURATION} seconds — watching chat & API...")
        deadline = time.monotonic() + COLLECT_DURATION

        while time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())
            # Also scrape visible chat DOM as a fallback
            for selector in USERNAME_IN_CHAT_SELECTORS:
                try:
                    elements = await page.query_selector_all(selector)
                    for el in elements:
                        text = (await el.inner_text()).strip().lstrip("@")
                        if text and 2 <= len(text) <= 50:
                            if text not in self._ws_users:
                                print(f"  [+] DOM chat user: {text}")
                            self._ws_users.add(text)
                except Exception:
                    pass

            sys.stdout.write(f"\r  ... {remaining}s remaining, {len(self._ws_users)} users collected")
            sys.stdout.flush()
            await asyncio.sleep(3)

        print()  # newline after progress

        self.collected_users = set(self._ws_users)
        print(f"\n[+] Collection done — {len(self.collected_users)} unique user(s) found")

        # Persist to file so you can review / reuse without re-running collection
        USERS_FILE.write_text("\n".join(sorted(self.collected_users)))
        print(f"[+] Saved to {USERS_FILE}")

    # ------------------------------------------------------------------
    # Tag users in own show
    # ------------------------------------------------------------------

    async def tag_users(self, page: Page):
        if not self.collected_users:
            print("[!] No users to tag — exiting")
            return

        print(f"\n[*] Navigating to YOUR show to tag {len(self.collected_users)} user(s)...")
        await page.goto(OWN_SHOW_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)  # let the live stream settle

        # Locate the chat input box
        chat_input = None
        for selector in CHAT_INPUT_SELECTORS:
            try:
                chat_input = await page.wait_for_selector(selector, timeout=5000)
                if chat_input:
                    print(f"[+] Found chat input via: {selector}")
                    break
            except Exception:
                continue

        if chat_input is None:
            print("[!] Could not locate the chat input box.")
            print("    The browser window is still open — you can tag users manually.")
            print("    Users list saved to:", USERS_FILE)
            return

        tagged = 0
        failed = 0

        for username in sorted(self.collected_users):
            try:
                await chat_input.click()
                await chat_input.fill(f"@{username} {TAG_MESSAGE}")
                await page.keyboard.press("Enter")
                print(f"  [+] Tagged @{username}")
                tagged += 1
                await asyncio.sleep(TAG_DELAY)
            except Exception as e:
                print(f"  [!] Failed to tag @{username}: {e}")
                failed += 1

        print(f"\n[+] Done! Tagged {tagged} user(s). {failed} failed.")

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    async def run(self):
        require_env()

        async with async_playwright() as p:
            # Use the pre-installed Chromium in the remote environment
            launch_kwargs = dict(
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            chromium_path = "/opt/pw-browsers/chromium"
            if Path(chromium_path).exists():
                launch_kwargs["executable_path"] = chromium_path

            browser = await p.chromium.launch(**launch_kwargs)
            context: BrowserContext = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
            )
            page = await context.new_page()

            try:
                await self.login(page)
                await self.collect_users(page)
                await self.tag_users(page)
            except KeyboardInterrupt:
                print("\n[!] Interrupted by user")
                if self.collected_users:
                    print(f"[+] Partial results saved to {USERS_FILE}")
            finally:
                await browser.close()


if __name__ == "__main__":
    asyncio.run(WhatnotBot().run())
