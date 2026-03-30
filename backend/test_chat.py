"""
KenBot Chat E2E Test
Tests that the WebSocket agent handles different user messages correctly.

Run with:
    python test_chat.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "kenbot.settings.development")

import django
django.setup()

import websockets
import httpx

BASE_HTTP = "http://127.0.0.1:8000"
WS_BASE   = "ws://127.0.0.1:8000/ws/pilot"

# ── colours ──────────────────────────────────────────────────────────────────
GREEN  = "\033[32m"
RED    = "\033[31m"
YELLOW = "\033[33m"
CYAN   = "\033[36m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

def ok(msg):  print(f"  {GREEN}[OK]{RESET}  {msg}")
def fail(msg): print(f"  {RED}[FAIL]{RESET}  {msg}")
def info(msg): print(f"  {CYAN}-->{RESET}  {msg}")
def hdr(msg):  print(f"\n{BOLD}{msg}{RESET}")

# ── helpers ───────────────────────────────────────────────────────────────────

async def get_jwt(username: str, password: str) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE_HTTP}/api/auth/token/",
            json={"username": username, "password": password},
            timeout=10,
        )
    assert r.status_code == 200, f"Login failed {r.status_code}: {r.text}"
    token = r.json()["access"]
    ok(f"Logged in as '{username}', got JWT")
    return token


async def create_session(token: str) -> str:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{BASE_HTTP}/api/pilot/sessions/",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    assert r.status_code == 201, f"Session create failed {r.status_code}: {r.text}"
    sid = r.json()["session_id"]
    ok(f"Session created: {sid}")
    return sid


async def collect_agent_reply(ws, timeout: float = 45.0) -> dict | None:
    """Wait for the next agent_message frame, ignoring state_update frames."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        remaining = deadline - time.time()
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            continue
        msg = json.loads(raw)
        if msg["type"] == "agent_message":
            return msg
        # state_update / execute_step / etc. — show briefly and keep waiting
        if msg["type"] not in ("state_update",):
            info(f"  [side-effect frame: {msg['type']}]")
    return None


async def chat_test(ws, label: str, user_text: str, expect_keywords: list[str]) -> bool:
    """Send a user_message and assert the reply contains at least one keyword."""
    info(f"Sending: \"{user_text}\"")
    await ws.send(json.dumps({"type": "user_message", "content": user_text}))
    reply = await collect_agent_reply(ws)
    if reply is None:
        fail(f"{label}: no response within 45 s")
        return False

    en = reply.get("content_en", "")
    sw = reply.get("content_sw", "")
    full = (en + " " + sw).lower()
    for kw in expect_keywords:
        if kw.lower() in full:
            ok(f"{label}")
            print(f"       EN: {en[:200]}")
            if sw and sw != en:
                print(f"       SW: {sw[:120]}")
            return True

    fail(f"{label}: expected one of {expect_keywords!r} in reply")
    print(f"       EN: {en[:300]}")
    return False


# ── test cases ────────────────────────────────────────────────────────────────

TEST_CASES = [
    # (label, user_text, expected_keywords_any)
    (
        "Greeting",
        "Hello",
        ["hello", "hi", "kenbot", "help", "assist"],
    ),
    (
        "Services list",
        "What services can you help me with?",
        ["ecitizen", "ntsa", "kra", "nhif", "driving", "good conduct", "pin"],
    ),
    (
        "Good conduct certificate",
        "I want to apply for a certificate of good conduct",
        ["good conduct", "ecitizen", "steps", "sure", "help", "service"],
    ),
    (
        "Driving licence renewal",
        "Help me renew my driving licence",
        ["driving", "licence", "ntsa", "renew", "steps", "help"],
    ),
    (
        "KRA PIN registration",
        "I need to register for a KRA PIN",
        ["kra", "pin", "register", "tax", "steps", "help"],
    ),
    (
        "Language switch — Swahili",
        "Niambie unavyoweza kunisaidia",           # "Tell me how you can help me"
        ["ninaweza", "msaada", "huduma", "kra", "ecitizen", "help", "assist"],
    ),
    (
        "Missing vault key awareness",
        "What information do I need to provide to complete a task?",
        ["national id", "national_id", "pin", "vault", "credentials", "details", "information", "id"],
    ),
    (
        "Out-of-scope question handled gracefully",
        "What is the weather today?",
        ["weather", "sorry", "help", "government", "portal", "services", "can't", "cannot"],
    ),
]


# ── main ──────────────────────────────────────────────────────────────────────

async def main() -> int:
    hdr("=== KenBot Chat Capability Test ===")

    # Auth + session
    try:
        token = await get_jwt("chiqo", "8844")
        session_id = await create_session(token)
    except Exception as e:
        fail(f"Setup failed: {e}")
        return 1

    ws_url = f"{WS_BASE}/{session_id}/?token={token}"
    info(f"Connecting to {ws_url[:60]}...")

    passed = 0
    failed = 0

    try:
        async with websockets.connect(ws_url, ping_interval=20) as ws:
            # Wait for initial state_update frame
            raw = await asyncio.wait_for(ws.recv(), timeout=10)
            init_msg = json.loads(raw)
            if init_msg.get("type") == "state_update":
                ok(f"WS connected — initial state: {init_msg.get('state', {}).get('status', '?')}")
            else:
                info(f"First frame: {init_msg['type']}")

            hdr("--- Running chat tests ---")
            for label, text, keywords in TEST_CASES:
                result = await chat_test(ws, label, text, keywords)
                if result:
                    passed += 1
                else:
                    failed += 1
                # Brief pause between messages so the agent isn't flooded
                await asyncio.sleep(1)

    except Exception as e:
        fail(f"WebSocket error: {e}")
        return 1

    hdr("=== Results ===")
    total = passed + failed
    pct   = int(100 * passed / total) if total else 0
    if failed == 0:
        print(f"  {GREEN}{BOLD}All {total} tests passed ({pct}%) PASS{RESET}")
    else:
        print(f"  {YELLOW}{passed}/{total} passed ({pct}%){RESET}  —  {RED}{failed} failed{RESET}")

    return 0 if failed == 0 else 1


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
