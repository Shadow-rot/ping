import asyncio
from pyrogram import filters
from pyrogram.errors import FloodWait
from anony import app
from anony.core.userbot import userbot


@app.on_message(filters.command("lv", prefixes=["."]))
async def leave_all(_, message):
    left = 0

    for assistant in userbot.clients:
        async for dialog in assistant.get_dialogs():
            try:
                await assistant.leave_chat(dialog.chat.id)
                left += 1
                await asyncio.sleep(0.2)
            except FloodWait as e:
                await asyncio.sleep(e.value)
            except:
                pass

    await message.reply(f"Left from `{left}` chats.")