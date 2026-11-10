"""Read-only MTProto roster scan with the existing Telegram bot account."""

import asyncio


def scan_members(api_id, api_hash, bot_token, groups, session="", expected_bot_id=None):
    try:
        from telethon import TelegramClient, types
        from telethon.sessions import StringSession
    except ImportError:
        raise ValueError("Для полного сканирования нужен Telethon. На ZimaOS он устанавливается в фоне после обновления; проверьте журнал запуска и повторите.") from None

    async def run():
        client = TelegramClient(StringSession(session), int(api_id), api_hash, request_retries=1, connection_retries=1, timeout=12)
        people, errors = [], []
        try:
            await client.connect()
            if not await client.is_user_authorized():
                await client.sign_in(bot_token=bot_token)
            me = await client.get_me()
            if not me or not me.bot or (expected_bot_id and int(me.id) != int(expected_bot_id)):
                raise ValueError("MTProto подключён не к текущему Telegram-боту. Сохраните API ID и Hash заново.")
            for group in groups:
                try:
                    chat_id = int(group["id"])
                    if group["type"] == "group":
                        peer = types.InputPeerChat(abs(chat_id))
                    else:
                        raw_id = abs(chat_id) - 1000000000000
                        if raw_id <= 0:
                            raise ValueError("Неизвестный ID супергруппы")
                        peer = types.InputPeerChannel(raw_id, 0)
                        if group.get("username"):
                            try:
                                peer = await client.get_input_entity("@" + group["username"].lstrip("@"))
                            except Exception:
                                pass
                    members = await asyncio.wait_for(client.get_participants(peer, limit=5000), timeout=25)
                    for member in members:
                        if getattr(member, "bot", False) or getattr(member, "deleted", False):
                            continue
                        name = " ".join(filter(None, (getattr(member, "first_name", ""), getattr(member, "last_name", ""))))
                        people.append({"chat_id": str(chat_id), "id": str(member.id), "name": name or member.username or str(member.id), "username": member.username or ""})
                except Exception as exc:
                    errors.append({"chat": group["title"], "reason": str(exc)[:160]})
            return {"people": people, "errors": errors, "session": client.session.save()}
        finally:
            await client.disconnect()

    return asyncio.run(run())
