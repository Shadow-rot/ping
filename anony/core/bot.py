import asyncio
import signal
from contextlib import asynccontextmanager
from typing import Optional

import pyrogram
from pyrogram import enums, types
from pyrogram.errors import AuthKeyUnregistered, FloodWait, RPCError, UserDeactivated

from anony import config, logger


class Bot(pyrogram.Client):
    _BOOT_FLOOD_LIMIT: int = 60

    def __init__(self) -> None:
        super().__init__(
            name="Anony",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            parse_mode=enums.ParseMode.HTML,
            max_concurrent_transmissions=7,
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
            sleep_threshold=30,
            no_updates=False,
        )
        self.owner: int = config.OWNER_ID
        self.logger_group: int = config.LOGGER_ID
        self.bl_users: pyrogram.filters.Filter = pyrogram.filters.user()
        self.sudoers: pyrogram.filters.Filter = pyrogram.filters.user(self.owner)
        self.id: Optional[int] = None
        self.name: Optional[str] = None
        self.username: Optional[str] = None
        self.mention: Optional[str] = None
        self._running: bool = False

    def add_sudo(self, user_id: int) -> None:
        self.sudoers = self.sudoers | pyrogram.filters.user(user_id)

    def rebuild_sudoers(self, user_ids: list[int]) -> None:
        base = pyrogram.filters.user(self.owner)
        for uid in user_ids:
            if uid != self.owner:
                base = base | pyrogram.filters.user(uid)
        self.sudoers = base

    def rebuild_blocklist(self, user_ids: list[int]) -> None:
        self.bl_users = pyrogram.filters.user(user_ids) if user_ids else pyrogram.filters.user()

    @asynccontextmanager
    async def lifespan(self):
        await self.boot()
        try:
            yield self
        finally:
            await self.exit()

    def _register_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        def _handle(sig: signal.Signals) -> None:
            self._running = False
            asyncio.ensure_future(self.exit(), loop=loop)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
            try:
                loop.add_signal_handler(sig, _handle, sig)
            except (NotImplementedError, OSError):
                signal.signal(sig, lambda s, f, _sig=sig: _handle(_sig))

    async def boot(self) -> None:
        await self._connect_with_retry()
        me = self.me
        self.id = me.id
        self.name = me.first_name
        self.username = me.username
        self.mention = me.mention
        await self._verify_logger()
        await self._send_startup_message()
        self._running = True
        self._register_signal_handlers()

    async def _connect_with_retry(self) -> None:
        while True:
            try:
                await super().start()
                return
            except FloodWait as exc:
                if exc.value > self._BOOT_FLOOD_LIMIT:
                    raise SystemExit(f"FloodWait {exc.value}s exceeds limit. Aborting.")
                await asyncio.sleep(exc.value)
            except (AuthKeyUnregistered, UserDeactivated) as exc:
                raise SystemExit(f"Invalid token or deactivated account: {exc}")

    async def _resolve_peer(self) -> bool:
        """Try every possible method to resolve and join the logger group."""
        # Step 1: try get_chat directly
        try:
            await self.get_chat(self.logger_group)
            logger.info("Logger group resolved via get_chat.")
            return True
        except RPCError:
            pass

        # Step 2: try join_chat with numeric id
        try:
            await self.join_chat(self.logger_group)
            await asyncio.sleep(2)
            logger.info("Joined logger group via numeric id.")
            return True
        except RPCError:
            pass

        # Step 3: try resolving via invite link if LOGGER_INVITE is set
        invite = getattr(config, "LOGGER_INVITE", None)
        if invite:
            try:
                await self.join_chat(invite)
                await asyncio.sleep(2)
                logger.info("Joined logger group via invite link.")
                return True
            except RPCError:
                pass

        # Step 4: try get_chat after all join attempts
        try:
            await self.get_chat(self.logger_group)
            return True
        except RPCError:
            pass

        return False

    async def _verify_logger(self) -> None:
        resolved = await self._resolve_peer()
        if not resolved:
            logger.warning("Could not resolve logger group — bot will retry sending anyway.")

        await asyncio.sleep(1)

        for attempt in range(5):
            try:
                await self.send_message(self.logger_group, "🔄 Initialising…")
                break
            except RPCError as exc:
                if attempt == 4:
                    raise SystemExit(f"Can't access logger group after 5 attempts: {exc}")
                logger.warning("Send attempt %s failed: %s — retrying in 3s…", attempt + 1, exc)
                # Try joining again between retries
                try:
                    await self.join_chat(self.logger_group)
                except RPCError:
                    pass
                await asyncio.sleep(3)

        try:
            member = await self.get_chat_member(self.logger_group, self.id)
            if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
                logger.warning("Bot is not admin in logger group — some features may not work.")
        except RPCError:
            logger.warning("Could not verify admin status in logger group.")

    async def _send_startup_message(self) -> None:
        text = (
            f"<b>✅ Started</b>\n"
            f"├ <b>Name:</b> {self.name}\n"
            f"├ <b>Username:</b> @{self.username}\n"
            f"└ <b>ID:</b> <code>{self.id}</code>"
        )
        for attempt in range(3):
            try:
                await self.send_message(self.logger_group, text)
                return
            except FloodWait as exc:
                await asyncio.sleep(exc.value)
            except RPCError:
                if attempt == 2:
                    break
                await asyncio.sleep(2)

    async def exit(self) -> None:
        if not self._running:
            return
        self._running = False
        try:
            await self.send_message(self.logger_group, "🛑 <b>Stopped</b>")
        except Exception:
            pass
        try:
            await super().stop()
        except Exception as exc:
            logger.warning("Exception during stop(): %s", exc)
