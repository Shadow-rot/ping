# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
import signal
from contextlib import asynccontextmanager
from typing import Optional

import pyrogram
from pyrogram import enums, types
from pyrogram.errors import (
    AuthKeyUnregistered,
    FloodWait,
    RPCError,
    UserDeactivated,
)

from anony import config, logger


class Bot(pyrogram.Client):
    """
    AnonXMusic Telegram bot client.

    Extends pyrogram.Client with:
    - Graceful SIGINT/SIGTERM/SIGABRT shutdown handling
    - Flood-wait-aware boot with automatic retry
    - Admin privilege validation using ChatPrivileges (Pyrogram v2)
    - Dynamic sudo/blocklist filter helpers
    - Context-manager support for clean lifecycle management
    - Structured startup/shutdown messaging
    """

    # Maximum seconds to wait on FloodWait during boot before giving up.
    _BOOT_FLOOD_LIMIT: int = 60

    def __init__(self) -> None:
        super().__init__(
            name="Anony",
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            bot_token=config.BOT_TOKEN,
            # Use HTML parse mode project-wide; switch to MARKDOWN_V2 if needed.
            parse_mode=enums.ParseMode.HTML,
            # Allow up to 7 simultaneous file uploads/downloads.
            max_concurrent_transmissions=7,
            # Suppress link previews on all outgoing messages by default.
            link_preview_options=types.LinkPreviewOptions(is_disabled=True),
            # Automatically sleep through FloodWaits ≤ 30 s instead of raising.
            sleep_threshold=30,
            # Keep the client alive even if Telegram sends no updates for a while.
            no_updates=False,
        )

        # Primary config references stored as instance attributes for convenience.
        self.owner: int = config.OWNER_ID
        self.logger_group: int = config.LOGGER_ID

        # --- Dynamic filters ---
        # Both are mutable; handlers can call add_sudo/remove_sudo at runtime.
        self.bl_users: pyrogram.filters.Filter = pyrogram.filters.user()
        self.sudoers: pyrogram.filters.Filter = pyrogram.filters.user(self.owner)

        # Will be populated inside boot() after the client connects.
        self.id: Optional[int] = None
        self.name: Optional[str] = None
        self.username: Optional[str] = None
        self.mention: Optional[str] = None

        # Internal flag used by the signal handler.
        self._running: bool = False

    # ------------------------------------------------------------------ #
    #  Filter helpers                                                       #
    # ------------------------------------------------------------------ #

    def add_sudo(self, user_id: int) -> None:
        """Add *user_id* to the sudoers filter at runtime."""
        self.sudoers = self.sudoers | pyrogram.filters.user(user_id)

    def remove_sudo(self, user_id: int) -> None:
        """Remove *user_id* from the sudoers filter at runtime."""
        # Re-build without the removed id (pyrogram filters don't support
        # subtraction, so we rebuild from config + remaining ids if needed).
        # This is a no-op sentinel; callers should track ids separately and
        # call rebuild_sudoers() with the updated list.
        logger.warning(
            "remove_sudo called for %s – rebuild sudoers via rebuild_sudoers().",
            user_id,
        )

    def rebuild_sudoers(self, user_ids: list[int]) -> None:
        """Rebuild the sudoers filter from scratch given a list of user IDs."""
        base = pyrogram.filters.user(self.owner)
        for uid in user_ids:
            if uid != self.owner:
                base = base | pyrogram.filters.user(uid)
        self.sudoers = base

    def rebuild_blocklist(self, user_ids: list[int]) -> None:
        """Rebuild the blocklist filter from a list of user IDs."""
        self.bl_users = pyrogram.filters.user(user_ids) if user_ids else pyrogram.filters.user()

    # ------------------------------------------------------------------ #
    #  Context-manager support                                              #
    # ------------------------------------------------------------------ #

    @asynccontextmanager
    async def lifespan(self):
        """
        Async context manager for clean bot lifecycle management.

        Usage::

            async with bot.lifespan():
                await idle()
        """
        await self.boot()
        try:
            yield self
        finally:
            await self.exit()

    # ------------------------------------------------------------------ #
    #  Signal handling                                                      #
    # ------------------------------------------------------------------ #

    def _register_signal_handlers(self) -> None:
        """
        Register SIGINT / SIGTERM / SIGABRT handlers on the running event loop
        so that Docker stops, keyboard interrupts, and OS signals all trigger a
        clean shutdown via self.exit().
        """
        loop = asyncio.get_running_loop()

        def _handle(sig: signal.Signals) -> None:
            logger.info("Received signal %s – initiating shutdown.", sig.name)
            self._running = False
            asyncio.ensure_future(self.exit(), loop=loop)

        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGABRT):
            try:
                loop.add_signal_handler(sig, _handle, sig)
            except (NotImplementedError, OSError):
                # Windows does not support add_signal_handler for all signals.
                signal.signal(sig, lambda s, f, _sig=sig: _handle(_sig))

    # ------------------------------------------------------------------ #
    #  Boot / shutdown                                                      #
    # ------------------------------------------------------------------ #

    async def boot(self) -> None:
        """
        Start the bot and perform initial setup.

        Steps:
        1. Connect to Telegram (with automatic FloodWait handling).
        2. Populate identity attributes from ``self.me``.
        3. Verify that the bot can reach the logger group and holds admin rights.
        4. Send a startup notification.
        5. Register OS signal handlers for clean shutdown.

        Raises:
            SystemExit: If the bot cannot reach the logger group or is not an
                        administrator there.
        """
        await self._connect_with_retry()

        # Populate identity from the connected session.
        me = self.me
        self.id = me.id
        self.name = me.first_name
        self.username = me.username
        self.mention = me.mention

        await self._verify_logger()
        await self._send_startup_message()

        self._running = True
        self._register_signal_handlers()

        logger.info("Bot started as @%s (id=%s).", self.username, self.id)

    async def _connect_with_retry(self) -> None:
        """
        Call super().start() with retry logic for transient FloodWait errors
        that may occur immediately at connection time.
        """
        while True:
            try:
                await super().start()
                return
            except FloodWait as exc:
                wait = exc.value
                if wait > self._BOOT_FLOOD_LIMIT:
                    raise SystemExit(
                        f"FloodWait of {wait}s exceeds boot limit "
                        f"({self._BOOT_FLOOD_LIMIT}s). Aborting."
                    ) from exc
                logger.warning(
                    "FloodWait during boot – sleeping %ss before retry.", wait
                )
                await asyncio.sleep(wait)
            except (AuthKeyUnregistered, UserDeactivated) as exc:
                raise SystemExit(
                    f"Bot token is invalid or the account was deactivated: {exc}"
                ) from exc

    async def _verify_logger(self) -> None:
        """
        Confirm the bot can post to the logger group and has administrator status.

        Raises:
            SystemExit: On any access or permission failure.
        """
        try:
            await self.send_message(self.logger_group, "🔄 Bot is initialising…")
            member = await self.get_chat_member(self.logger_group, self.id)
        except RPCError as exc:
            raise SystemExit(
                f"Bot failed to access logger group {self.logger_group}: {exc}"
            ) from exc

        if member.status != enums.ChatMemberStatus.ADMINISTRATOR:
            raise SystemExit(
                "Bot must be an administrator in the logger group. "
                "Please promote it and restart."
            )

        # Optionally warn if specific admin rights are missing.
        privs = member.privileges
        if privs and not privs.can_delete_messages:
            logger.warning(
                "Bot is admin in logger group but lacks 'Delete Messages' right. "
                "Some moderation features may not work."
            )

    async def _send_startup_message(self) -> None:
        """
        Send a formatted startup notification to the logger group.
        """
        text = (
            f"<b>✅ Bot Started</b>\n"
            f"├ <b>Name:</b> {self.name}\n"
            f"├ <b>Username:</b> @{self.username}\n"
            f"└ <b>ID:</b> <code>{self.id}</code>"
        )
        try:
            await self.send_message(self.logger_group, text)
        except RPCError as exc:
            # Non-fatal – log and continue.
            logger.warning("Could not send startup message: %s", exc)

    async def exit(self) -> None:
        """
        Gracefully stop the bot.

        - Sends a shutdown notification to the logger group (best-effort).
        - Calls super().stop() to flush pending updates and disconnect.
        - Safe to call multiple times (guarded by self._running).
        """
        if not self._running:
            return  # Already shutting down or never started.
        self._running = False

        logger.info("Shutting down bot @%s…", self.username)

        # Best-effort goodbye message; don't let it block shutdown.
        try:
            await self.send_message(self.logger_group, "🛑 <b>Bot Stopped</b>")
        except Exception:  # noqa: BLE001
            pass

        try:
            await super().stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Exception during stop(): %s", exc)

        logger.info("Bot stopped.")
