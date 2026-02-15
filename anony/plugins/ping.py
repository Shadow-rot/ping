import time
import psutil

from pyrogram import filters, types
from anony import app, anon, boot, config, lang
from anony.helpers import buttons


@app.on_message(filters.command(["alive", "ping"]) & ~app.bl_users)
@lang.language()
async def _ping(_, m: types.Message):

    start = time.time()
    sent = await m.reply_text(m.lang["pinging"])

    uptime = int(time.time() - boot)
    latency = round((time.time() - start) * 1000, 2)
    assistant_ping = await anon.ping()

    caption = m.lang["ping_pong"].format(
        latency,
        uptime,
        psutil.cpu_percent(interval=0),
        psutil.virtual_memory().percent,
        psutil.disk_usage("/").percent,
        assistant_ping,
    )

    await sent.edit_caption(
        caption,
        reply_markup=buttons.ping_markup(m.lang["support"])
    )
