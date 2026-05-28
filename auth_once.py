"""One-time auth script. Usage: python auth_once.py <code>"""
import asyncio
import sys

from app.config import settings


async def main():
    code = sys.argv[1] if len(sys.argv) > 1 else None
    if not code:
        print("Usage: python auth_once.py <code_from_telegram>")
        print("Step 1: run without code to trigger SMS/message")
        print("Step 2: run with the code you received")
        sys.exit(1)

    from telethon import TelegramClient

    client = TelegramClient(
        str(settings.tg_session),
        settings.tg_api_id,
        settings.tg_api_hash,
    )
    await client.connect()

    result = await client.sign_in(settings.tg_phone, code)
    me = await client.get_me()
    print(f"Authorized as {me.first_name} (id={me.id})")
    await client.disconnect()


asyncio.run(main())
