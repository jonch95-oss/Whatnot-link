"""
Whatnot User Tagger
-------------------
Joins any Whatnot show as a viewer (or mod), collects usernames from chat,
then @tags them in your own show. Mod access is optional — it only unlocks
the viewer list panel for extra names.

Usage:
    cp .env.example .env        # fill in your credentials and show URLs
    pip install -r requirements.txt
    playwright install chromium
    python bot.py               # collect users, then tag them
    python bot.py --collect-only
    python bot.py --tag-only    # reuse collected_users.txt
"""

import argparse
import asyncio
import json
import os
import re
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
SKIP_OWN_USERNAME = os.getenv("SKIP_OWN_USERNAME", "true").lower() in ("1", "true", "yes")
TRY_VIEWER_LIST = os.getenv("TRY_VIEWER_LIST", "true").lower() in ("1", "true", "yes")

USERS_FILE = Path("collected_users.txt")
SESSION_FILE = Path("whatnot_session.json")

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

CHAT_MESSAGE_SELECTORS = [
    '[data-testid="chat-message"]',
    '[class*="ChatMessage"]',
    '[class*="chat-message"]',
    '[class*="Comment"]',
    '[class*="comment"]',
]

CHAT_CONTAINER_SELECTORS = [
    '[data-testid="chat-messages"]',
    '[data-testid="chat-scroll"]',
    '[class*="ChatMessages"]',
    '[class*="chat-messages"]',
    '[class*="messageList"]',
    '[class*="MessageList"]',
]

CHAT_USER_LINK_SELECTORS = [
    'a[href*="/user/"]',
    'a[href*="/profile/"]',
]

VIEWER_LIST_TOGGLE_SELECTORS = [
    'button:has-text("Watching")',
    '[data-testid="viewer-list"]',
    '[class*="ViewerList"]',
    '[class*="viewerList"]',
    '[class*="viewer-count"]',
    'button[aria-label*="viewer"]',
    'button[aria-label*="Viewer"]',
]

VIEWER_LIST_ITEM_SELECTORS = [
    '[data-testid="viewer-list-item"] [class*="username"]',
    '[data-testid="viewer-list-item"] [class*="displayName"]',
    '[class*="ViewerList"] [class*="username"]',
    '[class*="ViewerList"] [class*="displayName"]',
    '[class*="viewerList"] li',
    '[class*="WatchingTab"] [class*="username"]',
    '[class*="WatchingTab"] button',
]

SKIP_USERNAMES = {
    "whatnot", "system", "moderator", "mod", "host", "seller", "admin",
    "support", "bot", "anonymous", "guest",
}

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.]{2,30}$")
MENTION_RE = re.compile(r"@([a-zA-Z0-9_.]{2,30})\b")
USER_HREF_RE = re.compile(r"/(?:user|profile)/([a-zA-Z0-9_.]{2,30})")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_env(*, need_target: bool, need_own: bool):
    checks = {
        "WHATNOT_USERNAME": USERNAME,
        "WHATNOT_PASSWORD": PASSWORD,
    }
    if need_target:
        checks["TARGET_SHOW_URL"] = TARGET_SHOW_URL
    if need_own:
        checks["OWN_SHOW_URL"] = OWN_SHOW_URL

    missing = [k for k, v in checks.items() if not v]
    if missing:
        print(f"[!] Missing required env vars: {', '.join(missing)}")
        print("    Copy .env.example to .env and fill in your details.")
        sys.exit(1)


def normalize_username(raw: str) -> str | None:
    text = raw.strip().lstrip("@").strip()
    if not text or len(text) > 50:
        return None
    if text.lower() in SKIP_USERNAMES:
        return None
    if not USERNAME_RE.match(text):
        return None
    return text


def extract_usernames_from_text(text: str, found: set):
    """Pull @mentions and /user/ links out of free-form chat text."""
    for match in MENTION_RE.finditer(text):
        normalized = normalize_username(match.group(1))
        if normalized:
            found.add(normalized)
    for match in USER_HREF_RE.finditer(text):
        normalized = normalize_username(match.group(1))
        if normalized:
            found.add(normalized)


def extract_usernames_from_json(data: object, found: set):
    """Recursively walk a JSON blob and harvest any username-shaped values."""
    if isinstance(data, str):
        extract_usernames_from_text(data, found)
    elif isinstance(data, dict):
        for key in ("username", "user_name", "handle", "displayName", "display_name",
                    "userName", "screenName", "screen_name", "body", "message",
                    "text", "content", "comment"):
            val = data.get(key)
            if isinstance(val, str):
                normalized = normalize_username(val)
                if normalized and key in ("username", "user_name", "handle", "displayName",
                                          "display_name", "userName", "screenName", "screen_name"):
                    found.add(normalized)
                else:
                    extract_usernames_from_text(val, found)
        for v in data.values():
            extract_usernames_from_json(v, found)
    elif isinstance(data, list):
        for item in data:
            extract_usernames_from_json(item, found)


def load_users_from_file(path: Path) -> set[str]:
    if not path.exists():
        print(f"[!] Users file not found: {path}")
        sys.exit(1)
    users = set()
    for line in path.read_text().splitlines():
        normalized = normalize_username(line)
        if normalized:
            users.add(normalized)
    return users


def save_users(users: set[str], path: Path = USERS_FILE):
    path.write_text("\n".join(sorted(users)))
    print(f"[+] Saved {len(users)} user(s) to {path}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Collect users from a Whatnot show and @tag them in your own show."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--collect-only",
        action="store_true",
        help="Only collect users from the target show; do not tag.",
    )
    mode.add_argument(
        "--tag-only",
        action="store_true",
        help="Skip collection; tag users from collected_users.txt.",
    )
    parser.add_argument(
        "--users-file",
        type=Path,
        default=USERS_FILE,
        help=f"Path to user list file (default: {USERS_FILE})",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Core bot
# ---------------------------------------------------------------------------

class WhatnotBot:
    def __init__(self, *, collect_only: bool = False, tag_only: bool = False,
                 users_file: Path = USERS_FILE):
        self.collect_only = collect_only
        self.tag_only = tag_only
        self.users_file = users_file
        self.collected_users: set[str] = set()
        self._ws_users: set[str] = set()
        self._own_username: str | None = None
        self._viewer_list_available: bool | None = None

    def _add_user(self, raw: str, source: str = ""):
        normalized = normalize_username(raw)
        if not normalized:
            return False
        if SKIP_OWN_USERNAME and self._own_username and normalized.lower() == self._own_username.lower():
            return False
        if normalized not in self._ws_users:
            label = f" ({source})" if source else ""
            print(f"  [+] User: {normalized}{label}")
        self._ws_users.add(normalized)
        return True

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def login(self, page: Page, context: BrowserContext):
        if SESSION_FILE.exists():
            print("[*] Restoring saved session...")
            try:
                await context.add_cookies(json.loads(SESSION_FILE.read_text()))
                await page.goto("https://www.whatnot.com/", wait_until="domcontentloaded")
                if "/login" not in page.url:
                    print("[+] Restored session — already logged in")
                    return
            except Exception:
                print("[!] Saved session expired — logging in again")

        print("[*] Opening Whatnot login page...")
        await page.goto("https://www.whatnot.com/login", wait_until="domcontentloaded")

        try:
            accept_btn = page.locator('button:has-text("Accept"), button:has-text("Got it")')
            await accept_btn.first.click(timeout=3000)
        except Exception:
            pass

        try:
            await page.locator('input[type="email"], input[name="email"]').fill(USERNAME, timeout=8000)
            await page.locator('input[type="password"], input[name="password"]').fill(PASSWORD)
            await page.locator('button[type="submit"]').click()
        except Exception:
            print("[!] Could not find standard login form — trying phone/alt flow")
            await page.locator('input[type="tel"], input[placeholder*="phone"]').fill(USERNAME, timeout=8000)
            await page.locator('button[type="submit"]').click()

        try:
            await page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
        except Exception:
            print("[!] Login did not redirect automatically.")
            print("    If 2FA is required, complete it in the browser window.")
            print("    Waiting up to 60 seconds for you to finish...")
            await page.wait_for_url(lambda url: "/login" not in url, timeout=60000)

        cookies = await context.cookies()
        SESSION_FILE.write_text(json.dumps(cookies))
        print("[+] Logged in successfully (session saved)")

    async def _detect_own_username(self, page: Page):
        """Best-effort: skip tagging yourself when collecting."""
        for selector in (
            '[data-testid="user-menu"]',
            '[class*="UserMenu"]',
            '[class*="profileMenu"]',
            'button[aria-label*="profile"]',
            'button[aria-label*="Profile"]',
        ):
            try:
                el = await page.query_selector(selector)
                if el:
                    text = (await el.inner_text()).strip().lstrip("@")
                    normalized = normalize_username(text)
                    if normalized:
                        self._own_username = normalized
                        print(f"[*] Detected your username: @{normalized}")
                        return
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Collect users from target show
    # ------------------------------------------------------------------

    async def _open_viewer_list(self, page: Page):
        for selector in VIEWER_LIST_TOGGLE_SELECTORS:
            try:
                el = await page.query_selector(selector)
                if el and await el.is_visible():
                    await el.click()
                    await asyncio.sleep(1)
                    print(f"  [+] Opened viewer list via: {selector}")
                    return True
            except Exception:
                continue
        return False

    async def _scrape_viewer_list(self, page: Page):
        if not TRY_VIEWER_LIST:
            return

        opened = await self._open_viewer_list(page)
        if not opened:
            if self._viewer_list_available is None:
                self._viewer_list_available = False
                print("  [*] Viewer list not available (normal for regular viewers)")
            return

        self._viewer_list_available = True
        for selector in VIEWER_LIST_ITEM_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = (await el.inner_text()).strip()
                    for part in re.split(r"[\s\n]+", text):
                        self._add_user(part, source="viewer list")
            except Exception:
                pass

    async def _scrape_chat(self, page: Page):
        """Collect usernames from visible chat — works for any viewer."""
        for selector in USERNAME_IN_CHAT_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = (await el.inner_text()).strip()
                    self._add_user(text, source="chat")
            except Exception:
                pass

        for selector in CHAT_USER_LINK_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    href = await el.get_attribute("href") or ""
                    for match in USER_HREF_RE.finditer(href):
                        self._add_user(match.group(1), source="chat link")
                    text = (await el.inner_text()).strip()
                    self._add_user(text, source="chat link")
            except Exception:
                pass

        for selector in CHAT_MESSAGE_SELECTORS:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements:
                    text = await el.inner_text()
                    for match in MENTION_RE.finditer(text):
                        self._add_user(match.group(1), source="chat mention")
            except Exception:
                pass

    async def _scroll_chat_history(self, page: Page):
        """Scroll the chat panel to surface older messages."""
        for selector in CHAT_CONTAINER_SELECTORS:
            try:
                container = await page.query_selector(selector)
                if container:
                    await container.evaluate("el => el.scrollTop = 0")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                pass

        try:
            await page.mouse.wheel(0, -400)
        except Exception:
            pass

    async def collect_users(self, page: Page):
        print("[*] Navigating to target show...")
        print("    Joining as a regular viewer — collecting from live chat.")
        if TRY_VIEWER_LIST:
            print("    (Will also try the viewer list if you happen to be a mod.)")

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

        def on_websocket(ws: WebSocket):
            def on_frame(payload: str | bytes):
                text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="ignore")
                try:
                    data = json.loads(text)
                    before = len(self._ws_users)
                    extract_usernames_from_json(data, self._ws_users)
                    for u in list(self._ws_users)[before:]:
                        print(f"  [+] Chat user: {u}")
                except Exception:
                    pass
            ws.on("framereceived", on_frame)

        page.on("response", on_response)
        page.on("websocket", on_websocket)

        await page.goto(TARGET_SHOW_URL, wait_until="domcontentloaded")
        await self._detect_own_username(page)
        await asyncio.sleep(3)
        await self._scroll_chat_history(page)
        await self._scrape_chat(page)
        await self._scrape_viewer_list(page)

        sources = "chat, API"
        if TRY_VIEWER_LIST:
            sources += ", viewer list (if mod)"
        print(f"[*] Collecting users for {COLLECT_DURATION} seconds — {sources}...")
        deadline = time.monotonic() + COLLECT_DURATION
        viewer_scrape_interval = 0
        scroll_interval = 0

        while time.monotonic() < deadline:
            remaining = int(deadline - time.monotonic())

            await self._scrape_chat(page)

            scroll_interval += 3
            if scroll_interval >= 12:
                await self._scroll_chat_history(page)
                scroll_interval = 0

            if TRY_VIEWER_LIST:
                viewer_scrape_interval += 3
                if viewer_scrape_interval >= 15:
                    await self._scrape_viewer_list(page)
                    viewer_scrape_interval = 0

            sys.stdout.write(f"\r  ... {remaining}s remaining, {len(self._ws_users)} users collected")
            sys.stdout.flush()
            await asyncio.sleep(3)

        print()
        self.collected_users = set(self._ws_users)
        print(f"\n[+] Collection done — {len(self.collected_users)} unique user(s) found")
        save_users(self.collected_users, self.users_file)

    # ------------------------------------------------------------------
    # Tag users in own show
    # ------------------------------------------------------------------

    async def tag_users(self, page: Page):
        if not self.collected_users:
            print("[!] No users to tag — exiting")
            return

        print(f"\n[*] Navigating to YOUR show to tag {len(self.collected_users)} user(s)...")
        await page.goto(OWN_SHOW_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

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
            print("    Users list saved to:", self.users_file)
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
        if self.tag_only:
            require_env(need_target=False, need_own=True)
            self.collected_users = load_users_from_file(self.users_file)
            print(f"[*] Loaded {len(self.collected_users)} user(s) from {self.users_file}")
        else:
            require_env(need_target=True, need_own=not self.collect_only)

        async with async_playwright() as p:
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
                await self.login(page, context)

                if not self.tag_only:
                    await self.collect_users(page)

                if not self.collect_only:
                    await self.tag_users(page)
            except KeyboardInterrupt:
                print("\n[!] Interrupted by user")
                if self.collected_users:
                    save_users(self.collected_users, self.users_file)
            finally:
                await browser.close()


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(WhatnotBot(
        collect_only=args.collect_only,
        tag_only=args.tag_only,
        users_file=args.users_file,
    ).run())
