"""Interactive local setup. Run only on the operator's trusted machine.

The resulting StringSession is a login credential. Store it only in a secret
manager/environment variable; never commit it or paste it into a chat.
"""

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

api_id = int(input("Telegram API ID: ").strip())
api_hash = input("Telegram API hash: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as client:
    print("Authorized account ID:", client.get_me().id)
    print(
        "\nCopy the session string directly into your trusted host's secret store. Do not share it.\n"
    )
    print(client.session.save())
