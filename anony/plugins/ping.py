from pyrogram import enums
from pyrogram.types import MessageEntity

emoji_id = 5375125990118793401
emoji = "😊"

caption_text = f"{emoji} " + m.lang["ping_pong"].format(
    latency,
    uptime,
    psutil.cpu_percent(interval=0),
    psutil.virtual_memory().percent,
    psutil.disk_usage("/").percent,
    await anon.ping(),
)

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
        ]
    ),
    reply_markup=buttons.ping_markup(m.lang["support"]),
)
