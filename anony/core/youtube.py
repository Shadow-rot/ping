# Copyright (c) 2025 AnonymousX1025
# Licensed under the MIT License.
# This file is part of AnonXMusic

import asyncio
import os
import random
import re
import time
from pathlib import Path
from typing import Optional

import aiohttp
import yt_dlp
from py_yt import Playlist, VideosSearch

from anony import config, logger
from anony.helpers import Track, utils


# ─────────────────────────────────────────────────────────────────────────────
#  Shared constants  (must match telegram.py)
# ─────────────────────────────────────────────────────────────────────────────

DOWNLOAD_DIR = Path("downloads")       # same root used in telegram.py
COOKIE_DIR   = Path("anony/cookies")

# yt-dlp outtmpl — plain string so yt-dlp can interpolate %(id)s
OUTTMPL = str(DOWNLOAD_DIR / "%(id)s.%(ext)s")

# Video quality tiers
VIDEO_FORMAT_HIGH   = "(bestvideo[height<=?1080][width<=?1920][ext=mp4])+(bestaudio[ext=m4a])/best[ext=mp4]/best"
VIDEO_FORMAT_MEDIUM = "(bestvideo[height<=?720][width<=?1280][ext=mp4])+(bestaudio[ext=m4a])/best[ext=mp4]/best"
VIDEO_FORMAT_LOW    = "(bestvideo[height<=?480][width<=?854][ext=mp4])+(bestaudio[ext=m4a])/best[ext=mp4]/best"

# Audio quality tiers (mirrors telegram.py's webm/opus preference)
AUDIO_FORMAT_HIGH   = "bestaudio[ext=webm][acodec=opus]/bestaudio[ext=m4a]/bestaudio"
AUDIO_FORMAT_MEDIUM = "bestaudio[ext=webm][asr<=?192000]/bestaudio"

MAX_RETRIES    = 3   # download attempts before giving up
RETRY_DELAY    = 2   # base seconds between retries (multiplied by attempt no.)
THUMBNAIL_SIZE = "maxresdefault"


# ─────────────────────────────────────────────────────────────────────────────
#  Module-level helpers  (mirrors telegram.py style)
# ─────────────────────────────────────────────────────────────────────────────

def _thumbnail(video_id: str) -> str:
    """Construct the best-quality YouTube thumbnail URL for a video ID."""
    return f"https://i.ytimg.com/vi/{video_id}/{THUMBNAIL_SIZE}.jpg"


def _clean_thumbnail(raw_url: str) -> str:
    """Strip tracking query-string params from a thumbnail URL."""
    return raw_url.split("?")[0] if raw_url else ""


def _safe_title(title: Optional[str], limit: int = 25) -> str:
    """Return a trimmed, non-empty title; fall back to 'Unknown'."""
    return (title or "Unknown").strip()[:limit]


def _safe_duration_sec(duration_str: Optional[str]) -> int:
    """Convert a 'MM:SS' / 'H:MM:SS' string to seconds; return 0 on failure."""
    try:
        return utils.to_seconds(duration_str) or 0
    except Exception:
        return 0


def _duration_fmt(seconds: int) -> str:
    """
    Convert seconds → 'MM:SS' string.
    Matches the format telegram.py's _build_media() produces via time.strftime.
    """
    return time.strftime("%M:%S", time.gmtime(seconds))


# ─────────────────────────────────────────────────────────────────────────────
#  YouTube class
# ─────────────────────────────────────────────────────────────────────────────

class YouTube:
    def __init__(self):
        self.base        = "https://www.youtube.com/watch?v="
        self.cookies:    list[str] = []
        self._checked    = False
        self._warned     = False
        self.cookie_dir  = COOKIE_DIR
        self.regex = re.compile(
            r"(https?://)?(www\.|m\.|music\.)?"
            r"(youtube\.com/(watch\?v=|shorts/|embed/|live/|playlist\?list=)"
            r"|youtu\.be/)"
            r"([A-Za-z0-9_-]{11}|PL[A-Za-z0-9_-]+)([&?][^\s]*)?"
        )

        # Ensure directories exist at startup (same as telegram.py's __init__)
        DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        COOKIE_DIR.mkdir(parents=True, exist_ok=True)

    # ── Cookie management ─────────────────────────────────────────────────────

    def _load_cookies(self) -> None:
        """Scan cookie_dir and populate the pool — called lazily once."""
        if self._checked:
            return
        self.cookies = [
            str(COOKIE_DIR / f)
            for f in os.listdir(self.cookie_dir)
            if f.endswith(".txt")
        ]
        self._checked = True

    def get_cookies(self) -> Optional[str]:
        """Return a random cookie path, or None with a one-time warning."""
        self._load_cookies()
        if not self.cookies:
            if not self._warned:
                self._warned = True
                logger.warning(
                    "No cookies found in %s — downloads may fail or be rate-limited.",
                    self.cookie_dir,
                )
            return None
        return random.choice(self.cookies)

    def invalidate_cookie(self, cookie_path: Optional[str]) -> None:
        """Drop a bad/expired cookie from the pool after a DownloadError."""
        if cookie_path and cookie_path in self.cookies:
            self.cookies.remove(cookie_path)
            logger.warning(
                "Removed bad cookie: %s  (%d remaining)",
                cookie_path, len(self.cookies),
            )

    async def save_cookies(self, urls: list[str]) -> None:
        """
        Fetch Netscape cookie files from batbin.me pastebin slugs and save them.

        ``urls`` may be either full URLs or bare paste slugs; the trailing
        slug is extracted either way. After saving, the pool is reloaded.
        """
        logger.info("Fetching %d cookie file(s)...", len(urls))
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for url in urls:
                slug      = url.rstrip("/").split("/")[-1]
                dest      = COOKIE_DIR / f"{slug}.txt"
                fetch_url = f"https://batbin.me/raw/{slug}"
                try:
                    async with session.get(fetch_url) as resp:
                        resp.raise_for_status()
                        dest.write_bytes(await resp.read())
                        logger.info("Saved cookie -> %s", dest)
                except Exception as exc:
                    logger.error("Failed to fetch cookie '%s': %s", slug, exc)

        # Reload pool so new files are picked up immediately
        self._checked = False
        self._load_cookies()
        logger.info("Cookie pool refreshed: %d file(s) available.", len(self.cookies))

    # ── URL helpers ───────────────────────────────────────────────────────────

    def valid(self, url: str) -> bool:
        """Return True if ``url`` matches any recognised YouTube URL pattern."""
        return bool(re.match(self.regex, url))

    def extract_id(self, url: str) -> Optional[str]:
        """Extract the 11-character video ID from any YouTube URL form."""
        match = re.search(
            r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", url
        )
        return match.group(1) if match else None

    def is_playlist(self, url: str) -> bool:
        """Return True if the URL points to a playlist."""
        return "playlist?list=" in url or "&list=" in url

    # ── yt-dlp option builders ────────────────────────────────────────────────

    def _base_opts(self, cookie: Optional[str]) -> dict:
        """
        Shared yt-dlp options. OUTTMPL matches telegram.py's "downloads/..." path.
        Cookie injection matches telegram.py's _base_opts pattern.
        """
        opts: dict = {
            "outtmpl":            OUTTMPL,
            "quiet":              True,
            "noplaylist":         True,
            "geo_bypass":         True,
            "no_warnings":        True,
            "overwrites":         False,
            "nocheckcertificate": True,
            "socket_timeout":     30,
            "retries":            3,
            "fragment_retries":   3,
        }
        if cookie:
            opts["cookiefile"] = cookie
        return opts

    def _video_opts(self, cookie: Optional[str], quality: str = "medium") -> dict:
        fmt_map = {
            "high":   VIDEO_FORMAT_HIGH,
            "medium": VIDEO_FORMAT_MEDIUM,
            "low":    VIDEO_FORMAT_LOW,
        }
        return {
            **self._base_opts(cookie),
            "format":              fmt_map.get(quality, VIDEO_FORMAT_MEDIUM),
            "merge_output_format": "mp4",
        }

    def _audio_opts(self, cookie: Optional[str], quality: str = "high") -> dict:
        fmt_map = {
            "high":   AUDIO_FORMAT_HIGH,
            "medium": AUDIO_FORMAT_MEDIUM,
        }
        return {
            **self._base_opts(cookie),
            "format": fmt_map.get(quality, AUDIO_FORMAT_HIGH),
        }

    # ── Search ────────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 1,
    ) -> Optional[Track]:
        """
        Search YouTube and return the top result as a Track.

        ``m_id`` is the Pyrogram message ID, consistent with how
        telegram.py's process_url() passes message IDs to Media objects.
        """
        try:
            _search = VideosSearch(query, limit=limit, with_live=False)
            results = await _search.next()
        except Exception as exc:
            logger.warning("YouTube search failed for '%s': %s", query, exc)
            return None

        if not results or not results.get("result"):
            return None

        data       = results["result"][0]
        video_id   = data.get("id", "")
        thumbnails = data.get("thumbnails") or [{}]

        return Track(
            id=video_id,
            channel_name=(data.get("channel") or {}).get("name", ""),
            duration=data.get("duration"),
            duration_sec=_safe_duration_sec(data.get("duration")),
            message_id=m_id,
            title=_safe_title(data.get("title")),
            thumbnail=_clean_thumbnail(thumbnails[-1].get("url", "")) or _thumbnail(video_id),
            url=data.get("link", f"{self.base}{video_id}"),
            view_count=(data.get("viewCount") or {}).get("short", ""),
            video=video,
        )

    async def search_many(
        self,
        query: str,
        m_id: int,
        video: bool = False,
        limit: int = 5,
    ) -> list[Track]:
        """Return up to ``limit`` search results as a list of Track objects."""
        try:
            _search = VideosSearch(query, limit=limit, with_live=False)
            results = await _search.next()
        except Exception as exc:
            logger.warning("YouTube search_many failed for '%s': %s", query, exc)
            return []

        tracks: list[Track] = []
        for data in (results or {}).get("result", []):
            video_id   = data.get("id", "")
            thumbnails = data.get("thumbnails") or [{}]
            tracks.append(Track(
                id=video_id,
                channel_name=(data.get("channel") or {}).get("name", ""),
                duration=data.get("duration"),
                duration_sec=_safe_duration_sec(data.get("duration")),
                message_id=m_id,
                title=_safe_title(data.get("title")),
                thumbnail=_clean_thumbnail(thumbnails[-1].get("url", "")) or _thumbnail(video_id),
                url=data.get("link", f"{self.base}{video_id}"),
                view_count=(data.get("viewCount") or {}).get("short", ""),
                video=video,
            ))
        return tracks

    # ── Playlist ──────────────────────────────────────────────────────────────

    async def playlist(
        self,
        limit: int,
        user: str,
        url: str,
        video: bool,
    ) -> list[Track]:
        """
        Fetch up to ``limit`` tracks from a YouTube playlist.

        Bad individual entries are skipped silently — matches the
        silent-skip pattern in telegram.py's process_urls().
        """
        tracks: list[Track] = []
        try:
            plist = await Playlist.get(url)
            for data in (plist.get("videos") or [])[:limit]:
                try:
                    video_id   = data.get("id", "")
                    thumbnails = data.get("thumbnails") or [{}]
                    # Strip playlist context from individual video URLs
                    clean_url  = (data.get("link") or "").split("&list=")[0]
                    tracks.append(Track(
                        id=video_id,
                        channel_name=(data.get("channel") or {}).get("name", ""),
                        duration=data.get("duration"),
                        duration_sec=_safe_duration_sec(data.get("duration")),
                        title=_safe_title(data.get("title")),
                        thumbnail=_clean_thumbnail(thumbnails[-1].get("url", "")) or _thumbnail(video_id),
                        url=clean_url or f"{self.base}{video_id}",
                        user=user,
                        view_count="",
                        video=video,
                    ))
                except Exception as item_exc:
                    logger.debug("Skipping playlist item: %s", item_exc)
        except Exception as exc:
            logger.warning("Playlist fetch failed for '%s': %s", url, exc)
        return tracks

    # ── Core download ─────────────────────────────────────────────────────────

    async def download(
        self,
        video_id: str,
        video: bool = False,
        quality: str = "medium",
    ) -> Optional[str]:
        """
        Download a YouTube track by its 11-character video ID.

        Aligned with telegram.py in the following ways:
        - Uses asyncio.to_thread (not deprecated run_in_executor).
        - File paths built via DOWNLOAD_DIR / f"{id}.{ext}" (Path objects).
        - Enforces config.DURATION_LIMIT before downloading.
        - Returns a plain str path on success, None on failure.
        - Retries up to MAX_RETRIES times, invalidating cookies on each failure.
        - Exponential back-off between attempts.
        """
        url      = self.base + video_id
        ext      = "mp4" if video else "webm"
        filename = DOWNLOAD_DIR / f"{video_id}.{ext}"

        # Cache hit — skip download entirely (mirrors telegram.py's os.path.exists check)
        if filename.exists():
            return str(filename)

        # Duration guard — mirrors telegram.py's config.DURATION_LIMIT check
        info = await self.get_info(url)
        if info:
            duration = int(info.get("duration") or 0)
            if duration and duration > config.DURATION_LIMIT:
                logger.warning(
                    "Skipping '%s': duration %ds exceeds limit of %ds",
                    video_id, duration, config.DURATION_LIMIT,
                )
                return None

        last_error: Optional[Exception] = None

        for attempt in range(1, MAX_RETRIES + 1):
            cookie = self.get_cookies()
            opts   = self._video_opts(cookie, quality) if video else self._audio_opts(cookie)

            # Capture opts/cookie in closure defaults to avoid late-binding issues
            def _run(o=opts, c=cookie):
                with yt_dlp.YoutubeDL(o) as ydl:
                    ydl.download([url])
                    return str(filename)

            try:
                result = await asyncio.to_thread(_run)
                if result and Path(result).exists():
                    return result
            except (yt_dlp.utils.DownloadError, yt_dlp.utils.ExtractorError) as exc:
                last_error = exc
                logger.warning(
                    "Download attempt %d/%d failed for '%s': %s",
                    attempt, MAX_RETRIES, video_id, exc,
                )
                self.invalidate_cookie(cookie)
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)   # exponential back-off
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Unexpected error on attempt %d for '%s': %s",
                    attempt, video_id, exc,
                )
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY)

        logger.error(
            "All %d download attempts failed for '%s'. Last error: %s",
            MAX_RETRIES, video_id, last_error,
        )
        return None

    async def download_url(
        self,
        url: str,
        video: bool = False,
        quality: str = "medium",
    ) -> Optional[str]:
        """
        Convenience wrapper accepting a full YouTube URL instead of a bare video ID.
        Mirrors telegram.py's process_url() which also accepts full URLs.
        """
        video_id = self.extract_id(url)
        if not video_id:
            logger.warning("Could not extract video ID from: %s", url)
            return None
        return await self.download(video_id, video=video, quality=quality)

    # ── Metadata-only fetch ───────────────────────────────────────────────────

    async def get_info(self, url: str) -> Optional[dict]:
        """
        Extract yt-dlp metadata without downloading anything.

        Uses asyncio.to_thread consistently with download() and
        telegram.py's ytdlp_extract_info() pattern.
        Returns the raw info dict, or None on failure.
        """
        cookie = self.get_cookies()
        opts   = {**self._base_opts(cookie), "skip_download": True}

        def _run():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            return await asyncio.to_thread(_run)
        except Exception as exc:
            logger.warning("get_info failed for '%s': %s", url, exc)
            return None

    async def get_track(self, url: str, m_id: int, video: bool = False) -> Optional[Track]:
        """
        Build a Track directly from a YouTube URL without a search step.

        Duration uses _duration_fmt() which produces the same 'MM:SS' string
        that telegram.py's _build_media() stores via time.strftime.
        """
        info = await self.get_info(url)
        if not info:
            return None

        video_id     = info.get("id") or self.extract_id(url) or ""
        duration_sec = int(info.get("duration") or 0)

        return Track(
            id=video_id,
            channel_name=info.get("uploader") or info.get("channel") or "",
            duration=_duration_fmt(duration_sec),   # "MM:SS" — matches telegram.py
            duration_sec=duration_sec,
            message_id=m_id,
            title=_safe_title(info.get("title")),
            thumbnail=info.get("thumbnail") or _thumbnail(video_id),
            url=info.get("webpage_url") or url,
            view_count=str(info.get("view_count") or ""),
            video=video,
        )

    # ── Cache helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def is_cached(video_id: str, video: bool = False) -> bool:
        """Return True if the file already exists in DOWNLOAD_DIR."""
        ext = "mp4" if video else "webm"
        return (DOWNLOAD_DIR / f"{video_id}.{ext}").exists()

    @staticmethod
    def cached_path(video_id: str, video: bool = False) -> Optional[str]:
        """Return the cached file path string if it exists, else None."""
        ext  = "mp4" if video else "webm"
        path = DOWNLOAD_DIR / f"{video_id}.{ext}"
        return str(path) if path.exists() else None

    @staticmethod
    def purge_cache(video_id: str) -> None:
        """
        Delete both .webm and .mp4 cached files for a video ID.
        Mirrors telegram.py's finally-block active-list cleanup pattern.
        """
        for ext in ("webm", "mp4"):
            p = DOWNLOAD_DIR / f"{video_id}.{ext}"
            if p.exists():
                p.unlink()
                logger.debug("Purged cached file: %s", p)
