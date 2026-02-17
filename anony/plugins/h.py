import asyncio
from pyrogram import filters
from pyrogram.errors import FloodWait
from anony import app
from anony.core.userbot import ub


@app.on_message(filters.command("lv", prefixes=["."]))
async def leave_all(_, message):
    total_left = 0
print(ub.clients)
    for assistant in ub.clients:
        async for dialog in assistant.get_dialogs():
            try:
                chat_id = dialog.chat.id

                
                if chat_id == assistant.me.id:
                    continue

                await assistant.leave_chat(chat_id)
                total_left += 1
                await asyncio.sleep(0.3)

            except FloodWait as e:
                await asyncio.sleep(e.value)
            except Exception:
                pass

    await message.reply(f"Userbots left `{total_left}` chats.")
