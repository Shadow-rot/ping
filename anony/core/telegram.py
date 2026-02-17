# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
import os
import re
import time
import urllib.parse
from pathlib import Path
from typing import Optional

import aiohttp
import yt_dlp
from pyrogram import types

from anony import config
from anony.helpers import Media, buttons, utils

# ─────────────────────────────────────────────────────────────────────────────
#  Platform Detection Helpers
# ─────────────────────────────────────────────────────────────────────────────

PLATFORM_PATTERNS: dict[str, list[str]] = {
    "youtube": [
        r"(?:https?://)?(?:www\.|m\.)?youtube\.com/watch\?v=[\w-]+",
        r"(?:https?://)?youtu\.be/[\w-]+",
        r"(?:https?://)?(?:www\.)?youtube\.com/shorts/[\w-]+",
        r"(?:https?://)?music\.youtube\.com/watch\?v=[\w-]+",
    ],
    "spotify": [
        r"(?:https?://)?open\.spotify\.com/(track|album|playlist|episode)/[\w]+",
    ],
    "soundcloud": [
        r"(?:https?://)?(?:www\.)?soundcloud\.com/[\w-]+/[\w-]+",
        r"(?:https?://)?on\.soundcloud\.com/[\w-]+",
    ],
    "apple_music": [
        r"(?:https?://)?music\.apple\.com/[\w-]+/(?:album|playlist|song)/[\w\-/]+",
    ],
    "instagram": [
        r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|tv)/[\w-]+",
    ],
    "facebook": [
        r"(?:https?://)?(?:www\.)?facebook\.com/(?:watch\?v=|[\w.]+/videos/)[\w-]+",
        r"(?:https?://)?fb\.watch/[\w-]+",
    ],
    "twitter": [
        r"(?:https?://)?(?:www\.)?(?:twitter|x)\.com/[\w-]+/status/\d+",
    ],
    "tiktok": [
        r"(?:https?://)?(?:www\.)?tiktok\.com/@[\w.-]+/video/\d+",
        r"(?:https?://)?vm\.tiktok\.com/[\w-]+",
        r"(?:https?://)?vt\.tiktok\.com/[\w-]+",
    ],
    "twitch": [
        r"(?:https?://)?(?:www\.)?twitch\.tv/videos/\d+",
        r"(?:https?://)?clips\.twitch\.tv/[\w-]+",
        r"(?:https?://)?(?:www\.)?twitch\.tv/[\w-]+/clip/[\w-]+",
    ],
    "deezer": [
        r"(?:https?://)?(?:www\.)?deezer\.com/(?:[\w-]+/)?(?:track|album|playlist)/\d+",
    ],
    "jiosaavn": [
        r"(?:https?://)?(?:www\.)?jiosaavn\.com/song/[\w-]+",
        r"(?:https?://)?(?:www\.)?jiosaavn\.com/album/[\w-]+",
    ],
    "gaana": [
        r"(?:https?://)?(?:www\.)?gaana\.com/song/[\w-]+",
    ],
    "vimeo": [
        r"(?:https?://)?(?:www\.)?vimeo\.com/\d+",
        r"(?:https?://)?player\.vimeo\.com/video/\d+",
    ],
    "dailymotion": [
        r"(?:https?://)?(?:www\.)?dailymotion\.com/video/[\w-]+",
        r"(?:https?://)?dai\.ly/[\w-]+",
    ],
    "m3u8": [
        r"https?://\S+\.m3u8(?:\?\S*)?",
    ],
    "telegram": [],  # Handled separately via Pyrogram
}


def detect_platform(url: str) -> str:
    """Detect the streaming/download platform from a URL."""
    for platform, patterns in PLATFORM_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, url, re.IGNORECASE):
                return platform
    return "unknown"


def is_url(text: str) -> bool:
    try:
        result = urllib.parse.urlparse(text)
        return all([result.scheme in ("http", "https"), result.netloc])
    except ValueError:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  YT-DLP wrapper (covers YouTube, SoundCloud, Vimeo, TikTok, etc.)
# ─────────────────────────────────────────────────────────────────────────────

YTDLP_SUPPORTED: set[str] = {
    "youtube", "soundcloud", "vimeo", "dailymotion",
    "facebook", "twitter", "tiktok", "twitch",
    "instagram", "gaana",
}

YTDLP_AUDIO_OPTS: dict = {
    "format": "bestaudio/best",
    "outtmpl": "downloads/%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "postprocessors": [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ],
}

YTDLP_VIDEO_OPTS: dict = {
    "format": "bestvideo[height<=1080]+bestaudio/best[height<=1080]",
    "outtmpl": "downloads/%(id)s.%(ext)s",
    "quiet": True,
    "no_warnings": True,
    "noplaylist": True,
    "merge_output_format": "mp4",
}


async def ytdlp_download(url: str, video: bool = False) -> dict:
    """Download via yt-dlp in a thread pool to avoid blocking the event loop."""
    opts = dict(YTDLP_VIDEO_OPTS if video else YTDLP_AUDIO_OPTS)

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            return ydl.sanitize_info(info)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


async def ytdlp_extract_info(url: str) -> dict:
    """Extract metadata only (no download)."""
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}

    def _run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _run)


# ─────────────────────────────────────────────────────────────────────────────
#  Spotify → YouTube resolver
# ─────────────────────────────────────────────────────────────────────────────

async def spotify_to_youtube(url: str) -> Optional[str]:
    """
    Resolve a Spotify track URL to a YouTube search URL.
    Requires spotdl or similar; here we use yt-dlp's spotify support if available,
    falling back to a title-based YouTube search.
    """
    try:
        info = await ytdlp_extract_info(url)
        title = info.get("title", "")
        artist = info.get("artist", "") or info.get("uploader", "")
        query = f"{artist} - {title}" if artist else title
        return f"ytsearch1:{query}"
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Apple Music / JioSaavn → YouTube resolver
# ─────────────────────────────────────────────────────────────────────────────

async def resolve_to_youtube(title: str, artist: str = "") -> str:
    query = f"{artist} - {title}" if artist else title
    return f"ytsearch1:{query}"


# ─────────────────────────────────────────────────────────────────────────────
#  Deezer handler
# ─────────────────────────────────────────────────────────────────────────────

async def deezer_resolve(url: str) -> Optional[str]:
    """Resolve Deezer track to yt-dlp searchable query."""
    try:
        info = await ytdlp_extract_info(url)
        title = info.get("title", "")
        artist = info.get("artist", "") or info.get("uploader", "")
        return f"ytsearch1:{artist} - {title}" if artist else f"ytsearch1:{title}"
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
#  Main Telegram Download Class
# ─────────────────────────────────────────────────────────────────────────────

class Telegram:
    def __init__(self):
        self.active: list[str] = []
        self.events: dict[int, asyncio.Event] = {}
        self.last_edit: dict[int, float] = {}
        self.active_tasks: dict[int, asyncio.Task] = {}
        self.sleep: int = 5

        os.makedirs("downloads", exist_ok=True)

    # ── Utility ──────────────────────────────────────────────────────────────

    def get_media(self, msg: types.Message) -> bool:
        """Return True if the message contains a downloadable media file."""
        return any([msg.video, msg.audio, msg.document, msg.voice])

    def _build_media(
        self,
        file_id: str,
        file_path: str,
        title: str,
        duration: int,
        video: bool,
        msg_id: int,
        url: str = "",
    ) -> Media:
        return Media(
            id=file_id,
            duration=time.strftime("%M:%S", time.gmtime(duration)),
            duration_sec=duration,
            file_path=file_path,
            message_id=msg_id,
            url=url,
            title=title[:25],
            video=video,
        )

    # ── Cancel ───────────────────────────────────────────────────────────────

    async def cancel(self, query: types.CallbackQuery):
        event = self.events.get(query.message.id)
        task = self.active_tasks.pop(query.message.id, None)
        if event:
            event.set()
        if task and not task.done():
            task.cancel()
        if event or task:
            await query.edit_message_text(
                query.lang["dl_cancel"].format(query.from_user.mention)
            )
        else:
            await query.answer(query.lang["dl_not_found"], show_alert=True)

    # ── Progress callback ─────────────────────────────────────────────────────

    def _make_progress(self, msg_id: int, sent: types.Message, start_time: float):
        event = self.events[msg_id]

        async def progress(current: int, total: int):
            if event.is_set():
                return
            now = time.time()
            if now - self.last_edit.get(msg_id, 0) < self.sleep:
                return
            self.last_edit[msg_id] = now
            percent = current * 100 / (total or 1)
            speed = current / (now - start_time or 1e-6)
            eta = utils.format_eta(int((total - current) / (speed or 1)))
            text = sent.lang["dl_progress"].format(
                utils.format_size(current),
                utils.format_size(total),
                percent,
                utils.format_size(speed),
                eta,
            )
            await sent.edit_text(
                text, reply_markup=buttons.cancel_dl(sent.lang["cancel"])
            )

        return progress

    # ── Telegram file download ────────────────────────────────────────────────

    async def download(
        self, msg: types.Message, sent: types.Message
    ) -> Optional[Media]:
        msg_id = sent.id
        event = asyncio.Event()
        self.events[msg_id] = event
        self.last_edit[msg_id] = 0
        start_time = time.time()

        media = msg.audio or msg.voice or msg.video or msg.document
        file_id = getattr(media, "file_unique_id", None)
        file_name = getattr(media, "file_name", "") or ""
        file_ext = Path(file_name).suffix.lstrip(".") or "bin"
        file_size = getattr(media, "file_size", 0)
        file_title = getattr(media, "title", None) or "Telegram File"
        duration = getattr(media, "duration", 0) or 0
        mime = getattr(media, "mime_type", "") or ""
        video = mime.startswith("video/")

        # Duration guard
        if duration and duration > config.DURATION_LIMIT:
            await sent.edit_text(
                sent.lang["play_duration_limit"].format(config.DURATION_LIMIT // 60)
            )
            return await sent.stop_propagation()

        # Size guard (200 MB)
        if file_size > 200 * 1024 * 1024:
            await sent.edit_text(sent.lang["dl_limit"])
            return await sent.stop_propagation()

        progress = self._make_progress(msg_id, sent, start_time)

        try:
            file_path = f"downloads/{file_id}.{file_ext}"
            if not os.path.exists(file_path):
                if file_id in self.active:
                    await sent.edit_text(sent.lang["dl_active"])
                    return await sent.stop_propagation()

                self.active.append(file_id)
                task = asyncio.create_task(
                    msg.download(file_name=file_path, progress=progress)
                )
                self.active_tasks[msg_id] = task
                await task
                self.active.remove(file_id)
                self.active_tasks.pop(msg_id, None)
                await sent.edit_text(
                    sent.lang["dl_complete"].format(round(time.time() - start_time, 2))
                )

            return self._build_media(
                file_id=file_id,
                file_path=file_path,
                title=file_title,
                duration=duration,
                video=video,
                msg_id=sent.id,
                url=msg.link,
            )

        except asyncio.CancelledError:
            return await sent.stop_propagation()
        finally:
            self.events.pop(msg_id, None)
            self.last_edit.pop(msg_id, None)
            self.active = [f for f in self.active if f != file_id]

    # ── M3U8 / HLS stream ────────────────────────────────────────────────────

    async def process_m3u8(
        self, url: str, msg_id: int, video: bool
    ) -> Media:
        return Media(
            id=str(msg_id),
            file_path=url,
            message_id=msg_id,
            url=url,
            title="M3U8 Stream",
            video=video,
        )

    # ── Universal URL handler ─────────────────────────────────────────────────

    async def process_url(
        self,
        url: str,
        sent: types.Message,
        video: bool = False,
    ) -> Optional[Media]:
        """
        Detect platform from URL and download/resolve appropriately.

        Supported platforms:
            Telegram, YouTube, SoundCloud, Vimeo, Dailymotion,
            Facebook, Twitter/X, TikTok, Twitch, Instagram,
            Gaana, Deezer, Spotify, Apple Music, JioSaavn, M3U8/HLS
        """
        if not is_url(url):
            return None

        platform = detect_platform(url)
        msg_id = sent.id

        # ── M3U8 / HLS ─────────────────────────────────────────────────
        if platform == "m3u8":
            return await self.process_m3u8(url, msg_id, video)

        # ── Spotify → resolve to YouTube search ────────────────────────
        if platform == "spotify":
            await sent.edit_text(sent.lang.get("dl_resolving", "🔍 Resolving Spotify track…"))
            resolved = await spotify_to_youtube(url)
            if not resolved:
                await sent.edit_text(sent.lang.get("dl_error", "❌ Could not resolve Spotify track."))
                return None
            url = resolved
            platform = "youtube"

        # ── Deezer → resolve to YouTube search ─────────────────────────
        if platform == "deezer":
            await sent.edit_text(sent.lang.get("dl_resolving", "🔍 Resolving Deezer track…"))
            resolved = await deezer_resolve(url)
            if not resolved:
                await sent.edit_text(sent.lang.get("dl_error", "❌ Could not resolve Deezer track."))
                return None
            url = resolved
            platform = "youtube"

        # ── Apple Music → resolve to YouTube search ─────────────────────
        if platform == "apple_music":
            await sent.edit_text(sent.lang.get("dl_resolving", "🔍 Resolving Apple Music track…"))
            try:
                info = await ytdlp_extract_info(url)
                resolved = await resolve_to_youtube(
                    info.get("title", ""), info.get("artist", "")
                )
                url = resolved
                platform = "youtube"
            except Exception:
                await sent.edit_text(sent.lang.get("dl_error", "❌ Could not resolve Apple Music track."))
                return None

        # ── JioSaavn → resolve to YouTube search ────────────────────────
        if platform == "jiosaavn":
            await sent.edit_text(sent.lang.get("dl_resolving", "🔍 Resolving JioSaavn track…"))
            try:
                info = await ytdlp_extract_info(url)
                resolved = await resolve_to_youtube(
                    info.get("title", ""), info.get("artist", "")
                )
                url = resolved
                platform = "youtube"
            except Exception:
                await sent.edit_text(sent.lang.get("dl_error", "❌ Could not resolve JioSaavn track."))
                return None

        # ── yt-dlp supported platforms ──────────────────────────────────
        if platform in YTDLP_SUPPORTED or platform == "youtube":
            await sent.edit_text(
                sent.lang.get("dl_ytdlp_start", "⬇️ Downloading via {platform}…").format(
                    platform=platform.replace("_", " ").title()
                )
            )
            try:
                start_time = time.time()
                info = await ytdlp_download(url, video=video)

                # yt-dlp writes the file; locate it
                file_id = info.get("id", str(msg_id))
                ext = "mp4" if video else "mp3"
                file_path = f"downloads/{file_id}.{ext}"

                # Fallback: check requested_downloads list
                if not os.path.exists(file_path):
                    downloads = info.get("requested_downloads", [])
                    if downloads:
                        file_path = downloads[0].get("filepath", file_path)

                duration = int(info.get("duration", 0) or 0)

                # Duration guard
                if duration > config.DURATION_LIMIT:
                    await sent.edit_text(
                        sent.lang["play_duration_limit"].format(config.DURATION_LIMIT // 60)
                    )
                    return None

                title = (
                    info.get("title")
                    or info.get("track")
                    or info.get("fulltitle")
                    or "Unknown"
                )

                await sent.edit_text(
                    sent.lang["dl_complete"].format(round(time.time() - start_time, 2))
                )

                return self._build_media(
                    file_id=file_id,
                    file_path=file_path,
                    title=title,
                    duration=duration,
                    video=video,
                    msg_id=msg_id,
                    url=url,
                )

            except yt_dlp.utils.DownloadError as e:
                await sent.edit_text(
                    sent.lang.get("dl_error", "❌ Download failed: {err}").format(err=str(e)[:200])
                )
                return None

        # ── Unknown / unsupported ───────────────────────────────────────
        await sent.edit_text(
            sent.lang.get("dl_unsupported", "❌ Platform not supported: {url}").format(url=url)
        )
        return None

    # ── Batch URL processor ───────────────────────────────────────────────────

    async def process_urls(
        self,
        urls: list[str],
        sent: types.Message,
        video: bool = False,
    ) -> list[Media]:
        """Process multiple URLs sequentially, collecting successful results."""
        results: list[Media] = []
        for url in urls:
            media = await self.process_url(url, sent, video=video)
            if media:
                results.append(media)
        return results

    # ── Platform info (for help commands) ────────────────────────────────────

    @staticmethod
    def supported_platforms() -> list[str]:
        return [
            "Telegram (audio, video, voice, document)",
            "YouTube & YouTube Music",
            "YouTube Shorts",
            "Spotify (track, album, playlist) → via YouTube",
            "Apple Music → via YouTube",
            "SoundCloud",
            "Deezer → via YouTube",
            "JioSaavn → via YouTube",
            "Gaana",
            "Instagram Reels / Posts",
            "Facebook Videos",
            "Twitter / X Videos",
            "TikTok",
            "Twitch VODs & Clips",
            "Vimeo",
            "Dailymotion",
            "M3U8 / HLS Streams",
        ]
