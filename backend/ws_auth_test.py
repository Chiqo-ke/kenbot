"""
End-to-end WS auth test.
1. Gets a fresh JWT from the login API
2. Connects via WebSocket with that token
3. Reports what close code is received
Run from kenbot/backend/: python ws_auth_test.py
"""
import asyncio, json, sys, os, urllib.request, urllib.parse

sys.path.insert(0, '.')
os.environ['DJANGO_SETTINGS_MODULE'] = 'kenbot.settings.development'
from dotenv import load_dotenv
load_dotenv('.env', override=False)

BACKEND = 'http://127.0.0.1:8000'
WS_BASE = 'ws://127.0.0.1:8000/ws/pilot/'


async def main():
    # ── 1. Login ────────────────────────────────────────────────────────────
    import django; django.setup()
    from django.contrib.auth import get_user_model
    from rest_framework_simplejwt.tokens import AccessToken

    User = get_user_model()
    user = User.objects.first()
    if not user:
        print("ERROR: No user in DB"); return

    token = str(AccessToken.for_user(user))
    print(f"Token issued for {user.username} (first 20 chars): {token[:20]}…")

    # ── 2. WS connect ────────────────────────────────────────────────────────
    import websockets
    import uuid

    session_id = str(uuid.uuid4())
    url = f"ws://127.0.0.1:8000/ws/pilot/{session_id}/?token={token}&vault_key=test-key"
    print(f"Connecting to {url[:80]}…")

    try:
        async with websockets.connect(url) as ws:
            print("✓ WS connected — waiting for server message…")
            try:
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                print("Server sent:", json.loads(msg)["type"])
                print("✓ AUTH SUCCESS — connection is authenticated")
            except asyncio.TimeoutError:
                print("No message within 5s — connection open but idle")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"✗ Connection closed — code={e.code} reason={e.reason!r}")
        if e.code == 4001:
            print("  → Auth rejected: token was not accepted by the server")
    except OSError as e:
        print(f"✗ Could not connect: {e}")
        print("  → Is the server running?  (cd kenbot/backend && .\\start.ps1)")


asyncio.run(main())
