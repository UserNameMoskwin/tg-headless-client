"""Interactive Telegram login: creates/refreshes a session file.

Run in a terminal (on the server: over SSH, as the service user):
    python -m app.login            # the service session (data/telegram.session)
    python -m app.login --query    # the query session (data/query.session)
    python -m app.login --qr       # scan a QR code instead of typing a code

Telegram usually sends the code to your Telegram app (the "Telegram" service
chat); this prints where it actually went. Type the code here. The 2FA password
is taken from TG_2FA_PASSWORD or asked for. Never paste the code into a
Telegram chat: Telegram expires login codes that are sent in messages.
"""
from __future__ import annotations

import argparse
import asyncio
import io
import sys
from datetime import datetime, timezone
from getpass import getpass

from telethon.errors import SessionPasswordNeededError

from app.config import settings
from app.telegram_client import create_client

_CODE_CHANNELS = {
    "SentCodeTypeApp": "the Telegram app, chat \"Telegram\" (service notifications)",
    "SentCodeTypeSms": "SMS",
    "SentCodeTypeCall": "a phone call",
    "SentCodeTypeFlashCall": "a flash call",
    "SentCodeTypeMissedCall": "a missed call (last digits of the caller)",
    "SentCodeTypeEmailCode": "the login email",
    "SentCodeTypeFragmentSms": "Fragment (anonymous number)",
}


def code_channel(sent) -> str:
    """Human-readable place a login code was sent to (auth.SentCode)."""
    name = type(sent.type).__name__
    return _CODE_CHANNELS.get(name, name)


async def _code_login(client) -> None:
    sent = await client.send_code_request(settings.tg_phone)
    print(f"Code sent via {code_channel(sent)}.")
    code = input("Enter the code: ").strip()
    try:
        await client.sign_in(settings.tg_phone, code)
    except SessionPasswordNeededError:
        await _password_login(client)


async def _password_login(client) -> None:
    await client.sign_in(password=settings.tg_2fa_password or getpass("2FA password: "))


def qr_text(url: str) -> str:
    """The login URL as a QR code drawn with half-block characters."""
    import qrcode

    code = qrcode.QRCode(border=2)
    code.add_data(url)
    out = io.StringIO()
    code.print_ascii(out=out, invert=True)
    return out.getvalue()


async def _qr_login(client, attempts: int = 6) -> None:
    """Log in by scanning a QR code: no login code needed.

    In the Telegram app: Settings -> Devices -> Link Desktop Device.
    """
    login = await client.qr_login()
    for _ in range(attempts):
        print(qr_text(login.url))
        print("Scan in Telegram: Settings -> Devices -> Link Desktop Device.")
        try:
            await login.wait(timeout=max(5, (login.expires - datetime.now(timezone.utc)).total_seconds()))
            return
        except asyncio.TimeoutError:
            await login.recreate()
        except SessionPasswordNeededError:
            await _password_login(client)
            return
    raise SystemExit("QR code was not scanned in time.")


async def _main(query: bool, qr: bool) -> None:
    session = settings.tg_query_session if query else settings.tg_session
    session.parent.mkdir(parents=True, exist_ok=True)
    client = create_client(session)
    await client.connect()
    try:
        if not await client.is_user_authorized():
            if qr:
                await _qr_login(client)
            else:
                await _code_login(client)
        me = await client.get_me()
        print(f"Authorized as {me.first_name} (id={me.id}). Session: {session}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Interactive Telegram login")
    parser.add_argument("--query", action="store_true",
                        help="log in the separate session used by app.query")
    parser.add_argument("--qr", action="store_true",
                        help="log in by scanning a QR code instead of typing a code")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    asyncio.run(_main(args.query, args.qr))
