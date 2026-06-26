"""
Whatnot Mod User Tagger
-----------------------
Joins a Whatnot show as a moderator, collects active chatters,
then @tags them in your own show.

Usage:
    python bot.py                          # full run: collect then tag
    python bot.py --collect-only           # collect users, save to file, stop
    python bot.py --tag-only               # skip collection, load saved file and tag
    python bot.py --dry-run                # preview who would be tagged, send nothing
    python bot.py --headless               # no browser window (server/background mode)
    python bot.py --exclude bots.txt       # skip usernames listed in a file
    python bot.py --min-messages 3         # only tag people who chatted 3+ times
    python bot.py --no-history             # ignore previous tag history, re-tag everyone
"""

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, BrowserContext, WebSocket
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeRemainingColumn
from rich.table import Table

load_dotenv()

console = Console()

# ---------------------------------------------------------------------------
# Configuration from .env
# ---------------------------------------------------------------------------

USERNAME = os.getenv("WHATNOT_USERNAME", "")
PASSWORD = os.getenv("WHATNOT_PASSWORD", "")
TARGET_SHOW_URLS = [u.strip() for u in os.getenv("TARGET_SHOW_URL", "").split(",") if u.strip()]
OWN_SHOW_URL = os.getenv("OWN_SHOW_URL", "")
COLLECT_DURATION = int(os.getenv("COLLECT_DURATION_SECONDS", "120"))
TAG_DELAY = float(os.getenv("TAG_DELAY_SECONDS", "2.5"))
TAG_MESSAGE = os.getenv("TAG_MESSAGE", "Come check out this show!")
MIN_MESSAGES = int(os.getenv("MIN_MESSAGES", "1"))

USERS_FILE = Path("collected_users.txt")
HISTORY_FILE = Path("tagged_history.txt")
SESSION_FILE = Path("session.json")

# Selectors — Whatnot uses hashed class names; we try multiple candidates.
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
# CLI arguments
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Whatnot mod user tagger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--collect-only", action="store_true",
                   help="Collect users and save to file, then stop without tagging")
    p.add_argument("--tag-only", action="store_true",
                   help="Skip collection — load users from saved file and tag them")
    p.add_argument("--dry-run", action="store_true",
                   help="Print who would be tagged without sending any messages")
    p.add_argument("--headless", action="store_true",
                   help="Run browser without a visible window")
    p.add_argument("--exclude", type=Path, metavar="FILE",
                   help="Path to a file with usernames to always skip (one per line)")
    p.add_argument("--users-file", type=Path, default=USERS_FILE,
                   help=f"File to save/load collected users (default: {USERS_FILE})")
    p.add_argument("--min-messages", type=int, default=MIN_MESSAGES,
                   help="Minimum number of chat messages a user must have sent to be tagged")
    p.add_argument("--no-history", action="store_true",
                   help="Ignore tag history — re-tag users even if tagged before")
    return p.parse_args()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def require_env(args: argparse.Namespace):
    needed: dict[str, str] = {
        "WHATNOT_USERNAME": USERNAME,
        "WHATNOT_PASSWORD": PASSWORD,
    }
    if not args.tag_only and not TARGET_SHOW_URLS:
        needed["TARGET_SHOW_URL"] = ""
    if not args.collect_only:
        needed["OWN_SHOW_URL"] = OWN_SHOW_URL

    missing = [k for k, v in needed.items() if not v]
    if missing:
        console.print(f"[bold red][!] Missing required env vars: {', '.join(missing)}[/]")
        console.print("    Copy [cyan].env.example[/] → [cyan].env[/] and fill in your details.")
        sys.exit(1)


def load_exclude_list(path: Path | None) -> set[str]:
    if path and path.exists():
        return {ln.strip().lstrip("@").lower() for ln in path.read_text().splitlines() if ln.strip()}
    return set()


def load_history() -> set[str]:
    if HISTORY_FILE.exists():
        return {ln.strip().lower() for ln in HISTORY_FILE.read_text().splitlines() if ln.strip()}
    return set()


def save_history(newly_tagged: set[str]):
    existing = load_history()
    merged = existing | {u.lower() for u in newly_tagged}
    HISTORY_FILE.write_text("\n".join(sorted(merged)))


def extract_usernames_from_json(data: object, counter: dict[str, int]):
    """Recursively walk a JSON blob and tally appearances of username-like values."""
    if isinstance(data, dict):
        for key in ("username", "user_name", "handle", "displayName", "display_name",
                    "userName", "screenName", "screen_name"):
            val = data.get(key)
            if isinstance(val, str) and 2 <= len(val) <= 50 and not val.startswith("http"):
                counter[val.strip().lstrip("@")] += 1
        for v in data.values():
            extract_usernames_from_json(v, counter)
    elif isinstance(data, list):
        for item in data:
            extract_usernames_from_json(item, counter)

# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------

async def save_session(context: BrowserContext):
    storage = await context.storage_state()
    SESSION_FILE.write_text(json.dumps(storage))
    console.print(f"[dim]Session saved → {SESSION_FILE}[/]")


async def try_load_session(p, launch_kwargs: dict) -> tuple[object, object, bool]:
    """Attempt to reuse a saved browser session. Returns (browser, context, valid)."""
    if not SESSION_FILE.exists():
        return None, None, False
    try:
        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            storage_state=json.loads(SESSION_FILE.read_text()),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto("https://www.whatnot.com", wait_until="domcontentloaded", timeout=15000)
        logged_in = "/login" not in page.url and bool(
            await page.query_selector(
                '[data-testid="user-avatar"], [class*="UserAvatar"], [class*="Avatar"]'
            )
        )
        await page.close()
        if logged_in:
            console.print("[green][+] Reusing saved session — no login needed[/]")
            return browser, context, True
        await browser.close()
        return None, None, False
    except Exception:
        return None, None, False

# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class WhatnotBot:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._message_counts: dict[str, int] = defaultdict(int)
        self.collected_users: set[str] = set()

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    async def login(self, page: Page):
        console.print("[yellow][*] Opening Whatnot login page...[/]")
        await page.goto("https://www.whatnot.com/login", wait_until="domcontentloaded")

        # Dismiss cookie banner if present
        try:
            btn = page.locator('button:has-text("Accept"), button:has-text("Got it")')
            await btn.first.click(timeout=3000)
        except Exception:
            pass

        # Standard email/password form
        try:
            await page.locator('input[type="email"], input[name="email"]').fill(USERNAME, timeout=8000)
            await page.locator('input[type="password"], input[name="password"]').fill(PASSWORD)
            await page.locator('button[type="submit"]').click()
        except Exception:
            console.print("[yellow][!] Standard form not found — trying phone/alt layout[/]")
            await page.locator('input[type="tel"], input[placeholder*="phone"]').fill(USERNAME, timeout=8000)
            await page.locator('button[type="submit"]').click()

        try:
            await page.wait_for_url(lambda url: "/login" not in url, timeout=20000)
        except Exception:
            console.print("[bold yellow][!] Login stalled — 2FA or CAPTCHA required.[/]")
            console.print("    Complete it in the browser window. Waiting up to 90 seconds...")
            await page.wait_for_url(lambda url: "/login" not in url, timeout=90000)

        console.print("[green][+] Logged in successfully[/]")

    # ------------------------------------------------------------------
    # Collect users from a single show
    # ------------------------------------------------------------------

    async def _collect_from_show(self, page: Page, show_url: str):
        show_id = show_url.rstrip("/").split("/")[-1]
        console.print(f"\n[bold cyan][*] Watching show: {show_id}[/]")

        async def on_response(response):
            if "json" in response.headers.get("content-type", ""):
                try:
                    body = await response.json()
                    extract_usernames_from_json(body, self._message_counts)
                except Exception:
                    pass

        def on_websocket(ws: WebSocket):
            def on_frame(payload):
                text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="ignore")
                try:
                    data = json.loads(text)
                    extract_usernames_from_json(data, self._message_counts)
                except Exception:
                    pass
            ws.on("framereceived", on_frame)

        page.on("response", on_response)
        page.on("websocket", on_websocket)
        await page.goto(show_url, wait_until="domcontentloaded")

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TimeRemainingColumn(),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("", total=COLLECT_DURATION)
            deadline = time.monotonic() + COLLECT_DURATION

            while time.monotonic() < deadline:
                elapsed = COLLECT_DURATION - int(deadline - time.monotonic())
                progress.update(
                    task,
                    completed=elapsed,
                    description=f"[cyan]Collecting... {len(self._message_counts)} users found[/]",
                )

                # DOM scrape as fallback
                for selector in USERNAME_IN_CHAT_SELECTORS:
                    try:
                        for el in await page.query_selector_all(selector):
                            text = (await el.inner_text()).strip().lstrip("@")
                            if text and 2 <= len(text) <= 50 and not text.startswith("http"):
                                self._message_counts[text] += 1
                    except Exception:
                        pass

                await asyncio.sleep(3)

            progress.update(task, completed=COLLECT_DURATION)

    # ------------------------------------------------------------------
    # Collect across all configured shows
    # ------------------------------------------------------------------

    async def collect_users(self, page: Page):
        for show_url in TARGET_SHOW_URLS:
            await self._collect_from_show(page, show_url)

        min_msgs = self.args.min_messages
        self.collected_users = {
            u for u, count in self._message_counts.items() if count >= min_msgs
        }

        # Print results table (top 40)
        table = Table(title="Collected Users", show_lines=False, highlight=True)
        table.add_column("Username", style="cyan")
        table.add_column("Messages seen", justify="right")
        table.add_column("Status", justify="center")

        for user, count in sorted(self._message_counts.items(), key=lambda x: -x[1])[:40]:
            passed = user in self.collected_users
            table.add_row(
                user,
                str(count),
                "[green]included[/]" if passed else f"[dim]below {min_msgs}[/]",
            )
        console.print(table)
        console.print(
            f"[green][+] {len(self.collected_users)} user(s) collected "
            f"(min {min_msgs} message{'s' if min_msgs != 1 else ''})[/]"
        )

        self.args.users_file.write_text("\n".join(sorted(self.collected_users)))
        console.print(f"[dim]Saved → {self.args.users_file}[/]")

    # ------------------------------------------------------------------
    # Tag users in own show
    # ------------------------------------------------------------------

    async def tag_users(self, page: Page):
        users = self.collected_users
        if not users:
            console.print("[yellow][!] No users to tag[/]")
            return

        exclude = load_exclude_list(self.args.exclude)
        history = set() if self.args.no_history else load_history()

        skipped_blocked = {u for u in users if u.lower() in exclude}
        skipped_history = {u for u in users if u.lower() in history} - skipped_blocked
        to_tag = users - skipped_blocked - skipped_history

        console.print(Panel(
            f"Total collected : [white]{len(users)}[/]\n"
            f"Blocklisted     : [red]{len(skipped_blocked)}[/]\n"
            f"Already tagged  : [yellow]{len(skipped_history)}[/]\n"
            f"[bold green]Will tag        : {len(to_tag)}[/]",
            title="Tagging Plan",
            expand=False,
        ))

        if self.args.dry_run:
            console.print("\n[bold yellow][DRY RUN] Would send these messages:[/]")
            for u in sorted(to_tag):
                console.print(f"  [cyan]@{u}[/] {TAG_MESSAGE}")
            return

        if not to_tag:
            console.print("[yellow][!] Nothing left to tag after filters[/]")
            return

        console.print(f"\n[*] Navigating to your show...")
        await page.goto(OWN_SHOW_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        tagged_ok: set[str] = set()
        failed: list[str] = []

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
        ) as progress:
            task = progress.add_task("[cyan]Tagging...[/]", total=len(to_tag))

            for username in sorted(to_tag):
                sent = False

                for attempt in range(3):
                    try:
                        # Re-locate input every time — chat DOM can change
                        chat_input = None
                        for selector in CHAT_INPUT_SELECTORS:
                            try:
                                chat_input = await page.wait_for_selector(selector, timeout=3000)
                                if chat_input:
                                    break
                            except Exception:
                                continue

                        if chat_input is None:
                            raise RuntimeError("Chat input not found")

                        await chat_input.click()
                        await chat_input.fill(f"@{username} {TAG_MESSAGE}")
                        await page.keyboard.press("Enter")
                        tagged_ok.add(username)
                        sent = True
                        break
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff
                        else:
                            failed.append(username)
                            console.print(f"[red]  [!] Failed @{username} after 3 attempts: {e}[/]")

                if sent:
                    progress.update(
                        task,
                        advance=1,
                        description=f"[cyan]Tagged {len(tagged_ok)}/{len(to_tag)} — last: @{username}[/]",
                    )
                else:
                    progress.advance(task)

                await asyncio.sleep(TAG_DELAY)

        save_history(tagged_ok)

        console.print(Panel(
            f"[green]Tagged successfully : {len(tagged_ok)}[/]\n"
            f"[red]Failed             : {len(failed)}[/]"
            + (f"\n  {', '.join(failed)}" if failed else ""),
            title="[bold]Results[/]",
            expand=False,
        ))

    # ------------------------------------------------------------------
    # Main
    # ------------------------------------------------------------------

    async def run(self):
        args = self.args
        require_env(args)

        if args.tag_only:
            if not args.users_file.exists():
                console.print(f"[red][!] {args.users_file} not found — run without --tag-only first.[/]")
                sys.exit(1)
            self.collected_users = {
                ln.strip() for ln in args.users_file.read_text().splitlines() if ln.strip()
            }
            console.print(f"[green][+] Loaded {len(self.collected_users)} users from {args.users_file}[/]")

        async with async_playwright() as p:
            launch_kwargs: dict = dict(
                headless=args.headless,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                ],
            )
            chromium_path = "/opt/pw-browsers/chromium"
            if Path(chromium_path).exists():
                launch_kwargs["executable_path"] = chromium_path

            browser, context, session_valid = await try_load_session(p, launch_kwargs)

            if not session_valid:
                browser = await p.chromium.launch(**launch_kwargs)
                context = await browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                )

            page = await context.new_page()

            try:
                if not session_valid:
                    await self.login(page)
                    await save_session(context)

                if not args.tag_only:
                    await self.collect_users(page)

                if not args.collect_only:
                    await self.tag_users(page)

            except KeyboardInterrupt:
                console.print("\n[yellow][!] Interrupted[/]")
                if self.collected_users:
                    args.users_file.write_text("\n".join(sorted(self.collected_users)))
                    console.print(f"[dim]Partial results saved → {args.users_file}[/]")
            finally:
                await browser.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    console.rule("[bold cyan]Whatnot Mod User Tagger[/]")
    asyncio.run(WhatnotBot(parse_args()).run())
