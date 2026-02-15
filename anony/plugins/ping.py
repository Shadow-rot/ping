import time
import psutil

from pyrogram import filters, types, enums
from pyrogram.types import MessageEntity

from anony import app, anon, boot, config, lang
from anony.helpers import buttons


@app.on_message(filters.command(["alive", "ping"]) & ~app.bl_users)
@lang.language()
async def _ping(_, m: types.Message):

    start = time.time()
    sent = await m.reply_text(m.lang["pinging"])

    uptime = int(time.time() - boot)
    latency = round((time.time() - start) * 1000, 2)

    assistant_ping = await anon.ping()  # ✅ await inside function

    emoji = "😊"
    emoji_id = 5375125990118793401

    caption_text = f"{emoji} " + m.lang["ping_pong"].format(
        latency,
        uptime,
        psutil.cpu_percent(interval=0),
        psutil.virtual_memory().percent,
        psutil.disk_usage("/").percent,
        assistant_ping,
    )

    from pyrogram import enums
    from pyrogram.types import MessageEntity

    offset = 0
    length = len(emoji.encode("utf-16-le")) // 2

    await sent.edit_media(
        media=types.InputMediaPhoto(
            media=config.PING_IMG,
            caption=caption_text,
            caption_entities=[
                MessageEntity(
                    type=enums.MessageEntityType.CUSTOM_EMOJI,
                    offset=offset,
                    length=length,
                    custom_emoji_id=emoji_id,
                )
            ],
        ),
        reply_markup=buttons.ping_markup(m.lang["support"]),
    )
print(repr(m.lang["ping_pong"]))
