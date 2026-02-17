# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
from typing import Optional

from ntgcalls import (
    ConnectionNotFound,
    RTMPStreamingUnsupported,
    TelegramServerError,
)
from pyrogram.errors import (
    ChatSendMediaForbidden,
    ChatSendPhotosForbidden,
    MessageIdInvalid,
    MessageNotModified,
)
from pyrogram.types import InputMediaPhoto, Message
from pytgcalls import PyTgCalls, exceptions, types
from pytgcalls.pytgcalls_session import PyTgCallsSession

from anony import app, config, db, lang, logger, queue, userbot, yt
from anony.helpers import Media, Track, buttons, thumb


class TgCall:
    """
    High-level voice/video-chat manager for AnonXMusic.

    Wraps a pool of PyTgCalls userbot clients and provides a clean API for
    play, pause, resume, stop, next, replay, ping, and volume control.

    Design notes
    ─────────────
    • One PyTgCalls instance per userbot session; each chat is pinned to
      exactly one instance via ``db.get_assistant``.
    • ``play_media`` is the single source-of-truth for actually streaming.
      All other helpers (``play_next``, ``replay``) funnel through it.
    • Every method that touches the call state guards against concurrent
      mutations with an ``asyncio.Lock`` keyed by chat_id so rapid
      skip/stop commands can't create race conditions.
    """

    def __init__(self) -> None:
        self.clients: list[PyTgCalls] = []
        # Per-chat mutex to prevent concurrent play/stop races.
        self._locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------ #
    #  Internal helpers                                                     #
    # ------------------------------------------------------------------ #

    def _lock(self, chat_id: int) -> asyncio.Lock:
        """Return (creating if needed) a per-chat asyncio.Lock."""
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    @staticmethod
    def _build_stream(media: "Media | Track", seek_time: int = 0) -> types.MediaStream:
        """
        Build a ``MediaStream`` from a Media/Track object.

        Audio is always REQUIRED.  Video is AUTO_DETECT when the track has
        a video stream, and IGNORE otherwise — this prevents PyTgCalls from
        crashing on audio-only files.
        """
        return types.MediaStream(
            media_path=media.file_path,
            audio_parameters=types.AudioQuality.HIGH,
            video_parameters=types.VideoQuality.HD_720p,
            audio_flags=types.MediaStream.Flags.REQUIRED,
            video_flags=(
                types.MediaStream.Flags.AUTO_DETECT
                if media.video
                else types.MediaStream.Flags.IGNORE
            ),
            # Seek is applied via an ffmpeg -ss pre-input filter; skip if ≤ 1 s
            # to avoid a tiny but noisy seek on fresh plays.
            ffmpeg_parameters=f"-ss {seek_time}" if seek_time > 1 else None,
        )

    @staticmethod
    async def _safe_delete(chat_id: int, message_id: int) -> None:
        """Delete a message silently, ignoring all errors."""
        if not message_id:
            return
        try:
            await app.delete_messages(
                chat_id=chat_id,
                message_ids=message_id,
                revoke=True,
            )
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    #  Playback controls                                                    #
    # ------------------------------------------------------------------ #

    async def pause(self, chat_id: int) -> bool:
        """Pause the active stream. Returns True on success."""
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=True)
        return await client.pause(chat_id)

    async def resume(self, chat_id: int) -> bool:
        """Resume a paused stream. Returns True on success."""
        client = await db.get_assistant(chat_id)
        await db.playing(chat_id, paused=False)
        return await client.resume(chat_id)

    async def mute(self, chat_id: int) -> bool:
        """Mute the bot in the voice chat."""
        client = await db.get_assistant(chat_id)
        return await client.mute_stream(chat_id)

    async def unmute(self, chat_id: int) -> bool:
        """Unmute the bot in the voice chat."""
        client = await db.get_assistant(chat_id)
        return await client.unmute_stream(chat_id)

    async def set_volume(self, chat_id: int, volume: int) -> bool:
        """
        Set playback volume (0–200).

        PyTgCalls accepts 0–200 where 100 is the default level.
        Values are clamped to that range before passing to the library.
        """
        volume = max(0, min(200, volume))
        client = await db.get_assistant(chat_id)
        return await client.change_volume_call(chat_id, volume)

    async def stop(self, chat_id: int) -> None:
        """
        Fully stop the call: clear the queue, remove DB state, leave the
        voice chat (without closing the underlying connection so the
        assistant stays alive for future calls).
        """
        async with self._lock(chat_id):
            client = await db.get_assistant(chat_id)
            queue.clear(chat_id)
            await db.remove_call(chat_id)
            # Clean up the per-chat lock since no active call remains.
            self._locks.pop(chat_id, None)

            try:
                await client.leave_call(chat_id, close=False)
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    #  Core playback                                                        #
    # ------------------------------------------------------------------ #

    async def play_media(
        self,
        chat_id: int,
        message: Message,
        media: "Media | Track",
        seek_time: int = 0,
    ) -> None:
        """
        Stream *media* into the voice chat identified by *chat_id*.

        Parameters
        ----------
        chat_id:    Target group/channel ID.
        message:    The Pyrogram message to edit with now-playing info.
        media:      A ``Media`` or ``Track`` object describing the content.
        seek_time:  Optional start offset in seconds (for seek/resume).
        """
        async with self._lock(chat_id):
            client = await db.get_assistant(chat_id)
            _lang = await lang.get_lang(chat_id)

            # ── Thumbnail ──────────────────────────────────────────────
            _thumb: Optional[str] = None
            if config.THUMB_GEN:
                _thumb = (
                    await thumb.generate(media)
                    if isinstance(media, Track)
                    else config.DEFAULT_THUMB
                )

            # ── Guard: file must exist ──────────────────────────────────
            if not media.file_path:
                await _safe_edit(message, _lang["error_no_file"].format(config.SUPPORT_CHAT))
                return await self.play_next(chat_id)

            stream = self._build_stream(media, seek_time)

            try:
                await client.play(
                    chat_id=chat_id,
                    stream=stream,
                    config=types.GroupCallConfig(auto_start=False),
                )
            # ── Hard errors: stop the call ──────────────────────────────
            except exceptions.NoActiveGroupCall:
                await self.stop(chat_id)
                return await _safe_edit(message, _lang["error_no_call"])
            except (ConnectionNotFound, TelegramServerError):
                await self.stop(chat_id)
                return await _safe_edit(message, _lang["error_tg_server"])
            except RTMPStreamingUnsupported:
                await self.stop(chat_id)
                return await _safe_edit(message, _lang["error_rtmp"])
            # ── Soft errors: skip to next ───────────────────────────────
            except FileNotFoundError:
                await _safe_edit(message, _lang["error_no_file"].format(config.SUPPORT_CHAT))
                return await self.play_next(chat_id)
            except exceptions.NoAudioSourceFound:
                await _safe_edit(message, _lang["error_no_audio"])
                return await self.play_next(chat_id)

            # ── Seek path: nothing more to do ───────────────────────────
            if seek_time:
                return

            # ── Fresh play: update state & send now-playing card ────────
            media.time = 1
            await db.add_call(chat_id)

            text = _lang["play_media"].format(
                media.url,
                media.title,
                media.duration,
                media.user,
            )
            keyboard = buttons.controls(chat_id)

            sent = await _send_now_playing(chat_id, message, text, keyboard, _thumb)
            if sent:
                media.message_id = sent.id

    # ------------------------------------------------------------------ #
    #  Queue navigation                                                     #
    # ------------------------------------------------------------------ #

    async def replay(self, chat_id: int) -> None:
        """Restart the currently playing track from the beginning."""
        if not await db.get_call(chat_id):
            return
        media = queue.get_current(chat_id)
        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_again"])
        await self.play_media(chat_id, msg, media)

    async def play_next(self, chat_id: int) -> None:
        """
        Advance to the next track in the queue.

        Steps:
        1. Ask the queue for the next item (this also removes the current one).
        2. Delete the current now-playing card (best-effort).
        3. If the queue is empty, stop the call.
        4. Otherwise, ensure the file is downloaded, then stream it.
        """
        current = queue.get_current(chat_id)
        # Delete the now-playing message for the track we're moving past.
        if current and current.message_id:
            await self._safe_delete(chat_id, current.message_id)
            current.message_id = 0

        media = queue.get_next(chat_id)
        if not media:
            return await self.stop(chat_id)

        _lang = await lang.get_lang(chat_id)
        msg = await app.send_message(chat_id=chat_id, text=_lang["play_next"])

        # Download if the cached file path is missing (e.g., was evicted).
        if not media.file_path:
            media.file_path = await yt.download(media.id, video=media.video)
            if not media.file_path:
                await self.stop(chat_id)
                return await msg.edit_text(
                    _lang["error_no_file"].format(config.SUPPORT_CHAT)
                )

        media.message_id = msg.id
        await self.play_media(chat_id, msg, media)

    # ------------------------------------------------------------------ #
    #  Diagnostics                                                          #
    # ------------------------------------------------------------------ #

    async def ping(self) -> float:
        """
        Return the mean ping across all active PyTgCalls clients.

        Falls back to 0.0 if no clients are registered yet.
        """
        if not self.clients:
            return 0.0
        pings = [client.ping for client in self.clients]
        return round(sum(pings) / len(pings), 2)

    async def active_calls(self) -> int:
        """Return the total number of active group calls across all clients."""
        total = 0
        for client in self.clients:
            try:
                total += len(await client.get_active_calls())
            except Exception:
                pass
        return total

    # ------------------------------------------------------------------ #
    #  Event decorators                                                     #
    # ------------------------------------------------------------------ #

    async def decorators(self, client: PyTgCalls) -> None:
        """
        Register event handlers on a single PyTgCalls client instance.

        pytgcalls v2.x type changes applied here:
        ──────────────────────────────────────────
        OLD (broken)               NEW (v2.x)
        ─────────────────────────────────────────
        types.MuteStream       →   types.MutedStream
        types.UnMuteStream     →   types.UnMutedStream
        types.StreamAudioEnded →   types.StreamEnded  (with .stream_type check)
        types.StreamVideoEnded →   types.StreamEnded  (with .stream_type check)

        Handles:
        • StreamEnded  (AUDIO or VIDEO) → advance the queue.
        • ChatUpdate   (kicked / left / VC closed) → clean up state.
        • MutedStream  (server-side mute) → log for awareness.
        • UnMutedStream (server-side unmute) → log for awareness.
        """

        @client.on_update()
        async def update_handler(_, update: types.Update) -> None:

            # ── Stream finished ─────────────────────────────────────────
            # pytgcalls v2.x: StreamAudioEnded + StreamVideoEnded were
            # merged into a single StreamEnded type with a stream_type field.
            if isinstance(update, types.StreamEnded):
                if update.stream_type in (
                    types.StreamEnded.Type.AUDIO,
                    types.StreamEnded.Type.VIDEO,
                ):
                    asyncio.create_task(
                        self.play_next(update.chat_id),
                        name=f"play_next:{update.chat_id}",
                    )

            # ── Chat / call state changes ───────────────────────────────
            # ChatUpdate.Status values are unchanged in v2.x.
            elif isinstance(update, types.ChatUpdate):
                terminal_statuses = {
                    types.ChatUpdate.Status.KICKED,
                    types.ChatUpdate.Status.LEFT_GROUP,
                    types.ChatUpdate.Status.CLOSED_VOICE_CHAT,
                }
                if update.status in terminal_statuses:
                    asyncio.create_task(
                        self.stop(update.chat_id),
                        name=f"stop:{update.chat_id}",
                    )

            # ── Server-side mute ────────────────────────────────────────
            # FIX: types.MuteStream → types.MutedStream  (v2.x rename)
            elif isinstance(update, types.MutedStream):
                logger.debug(
                    "MutedStream update for chat %s",
                    update.chat_id,
                )

            # ── Server-side unmute ──────────────────────────────────────
            # FIX: types.UnMuteStream → types.UnMutedStream  (v2.x rename)
            elif isinstance(update, types.UnMutedStream):
                logger.debug(
                    "UnMutedStream update for chat %s",
                    update.chat_id,
                )

    # ------------------------------------------------------------------ #
    #  Boot                                                                 #
    # ------------------------------------------------------------------ #

    async def boot(self) -> None:
        """
        Start all PyTgCalls userbot clients.

        Suppresses the library's startup notice (it's redundant in production)
        and registers event decorators on each client before returning.
        """
        PyTgCallsSession.notice_displayed = True

        for ub in userbot.clients:
            client = PyTgCalls(ub, cache_duration=100)
            await client.start()
            self.clients.append(client)
            await self.decorators(client)
            logger.info(
                "PyTgCalls client started for userbot %s.",
                ub.me.id if ub.me else "?",
            )

        logger.info(
            "TgCall pool ready: %d client(s) active.", len(self.clients)
        )


# ──────────────────────────────────────────────────────────────────────────── #
#  Module-level helpers (private)                                               #
# ──────────────────────────────────────────────────────────────────────────── #


async def _safe_edit(message: Message, text: str) -> None:
    """Edit *message* text, silently swallowing Telegram errors."""
    try:
        await message.edit_text(text)
    except (MessageNotModified, MessageIdInvalid):
        pass
    except Exception as exc:
        logger.debug("_safe_edit failed: %s", exc)


async def _send_now_playing(
    chat_id: int,
    message: Message,
    text: str,
    keyboard,
    thumbnail: Optional[str],
) -> Optional[Message]:
    """
    Try to update the now-playing card in-place (edit the existing message).

    Falls back to sending a brand-new photo/text message when the edit is
    forbidden or the message no longer exists.  Returns the *final* message
    object so the caller can store its ID, or None on total failure.
    """
    # ── Attempt 1: edit in-place ────────────────────────────────────────
    try:
        if thumbnail:
            await message.edit_media(
                media=InputMediaPhoto(media=thumbnail, caption=text),
                reply_markup=keyboard,
            )
        else:
            await message.edit_text(text, reply_markup=keyboard)
        return message
    except (ChatSendMediaForbidden, ChatSendPhotosForbidden, MessageIdInvalid, MessageNotModified):
        pass  # Fall through to the send-new path.
    except Exception as exc:
        logger.debug("edit_media/edit_text failed: %s", exc)

    # ── Attempt 2: send a fresh message ─────────────────────────────────
    try:
        if thumbnail:
            return await app.send_photo(
                chat_id=chat_id,
                photo=thumbnail,
                caption=text,
                reply_markup=keyboard,
            )
        return await app.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
        )
    except Exception as exc:
        logger.warning("_send_now_playing: fallback send failed: %s", exc)
        return None
