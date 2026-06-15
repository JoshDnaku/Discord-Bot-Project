import discord
import os
import random
import asyncio
import functools
import logging
import time
import math
from urllib.parse import urlparse
from dotenv import load_dotenv
from discord.ext import commands
import yt_dlp
from collections import deque

# Load the bot token from .env file
load_dotenv()

# -------------------------------------------------
# LOGGING SETUP
# -------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

logger = logging.getLogger(__name__)

# -------------------------------------------------
# CONFIG
# -------------------------------------------------

MAX_QUEUE_SIZE = 3

IDLE_LEAVE_SECONDS = 180
ALONE_LEAVE_SECONDS = 30

YTDLP_SOCKET_TIMEOUT_SECONDS = 20
YTDLP_COMMAND_TIMEOUT_SECONDS = 25

PLAY_COOLDOWN_SECONDS = 5
SKIP_COOLDOWN_SECONDS = 5
VOLUME_COOLDOWN_SECONDS = 3

VOTE_TIMEOUT_SECONDS = 60
HISTORY_LIMIT = 50

MIN_PLAYBACK_SPEED = 0.5
MAX_PLAYBACK_SPEED = 2.0

ALLOWED_YOUTUBE_HOSTS = {
    'youtube.com',
    'www.youtube.com',
    'm.youtube.com',
    'music.youtube.com',
    'youtu.be',
}

YDL_OPTIONS = {
    # Use the Android player client — it returns audio URLs without YouTube's
    # session-locking that causes 403 errors when FFmpeg tries to fetch them.
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'cachedir': False,
    'socket_timeout': YTDLP_SOCKET_TIMEOUT_SECONDS,
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],
        }
    },
}

FFMPEG_BEFORE_OPTIONS = (
    '-nostdin '
    '-reconnect 1 '
    '-reconnect_streamed 1 '
    '-reconnect_at_eof 1 '
    '-reconnect_on_network_error 1 '
    '-reconnect_delay_max 10'
)

FFMPEG_BASE_OPTIONS = '-vn'


# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def format_duration(seconds):
    """Converts seconds into a nice mm:ss or hh:mm:ss format."""
    try:
        if seconds is None:
            return 'Unknown'

        seconds = int(max(0, float(seconds)))

        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)

        if hours > 0:
            return f'{hours}:{minutes:02d}:{secs:02d}'

        return f'{minutes}:{secs:02d}'

    except Exception:
        logger.exception('format_duration error')
        return 'Unknown'


def parse_time_value(value):
    """
    Accepts:
    30       -> 30 seconds
    2:03     -> 123 seconds
    1:02:03  -> 3723 seconds
    """
    try:
        text = str(value).strip()

        if not text:
            return None

        if ':' not in text:
            seconds = int(float(text))
            return max(0, seconds)

        parts = text.split(':')

        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return max(0, minutes * 60 + seconds)

        if len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = int(parts[2])
            return max(0, hours * 3600 + minutes * 60 + seconds)

        return None

    except Exception:
        logger.exception('parse_time_value error')
        return None


def looks_like_url(text):
    try:
        parsed = urlparse(text.strip())
        return parsed.scheme in ('http', 'https') and bool(parsed.netloc)
    except Exception:
        logger.exception('looks_like_url error')
        return False


def normalize_hostname(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()

    if '@' in host:
        host = host.split('@', 1)[1]

    if ':' in host:
        host = host.split(':', 1)[0]

    return host


def is_allowed_youtube_url(url):
    """Only allow youtube.com subdomains and youtu.be links from user input."""
    try:
        if not looks_like_url(url):
            return False

        host = normalize_hostname(url)

        if host == 'youtu.be':
            return True

        if host == 'youtube.com':
            return True

        return host.endswith('.youtube.com')

    except Exception:
        logger.exception('is_allowed_youtube_url error')
        return False


def build_atempo_filter(speed):
    """Build an FFmpeg atempo filter chain."""
    try:
        speed = float(speed)
        speed = max(MIN_PLAYBACK_SPEED, min(MAX_PLAYBACK_SPEED, speed))

        # Since we clamp to 0.5 - 2.0, one atempo filter is enough.
        return f'atempo={speed:.3f}'.rstrip('0').rstrip('.')

    except Exception:
        logger.exception('build_atempo_filter error')
        return 'atempo=1'


def build_ffmpeg_options(http_headers=None, seek_seconds=0, speed=1.0):
    """Build FFmpeg options including YouTube headers, seek offset, and playback speed."""
    try:
        before_options = FFMPEG_BEFORE_OPTIONS

        if http_headers:
            # Format headers as "Key: Value\r\n" lines for FFmpeg.
            header_lines = ''.join(f'{k}: {v}\\r\\n' for k, v in http_headers.items())
            before_options = f'{before_options} -headers "{header_lines}"'

        seek_seconds = int(max(0, float(seek_seconds or 0)))

        if seek_seconds > 0:
            before_options = f'{before_options} -ss {seek_seconds}'

        options = FFMPEG_BASE_OPTIONS

        speed = float(speed or 1.0)

        if abs(speed - 1.0) > 0.01:
            options = f'{options} -filter:a "{build_atempo_filter(speed)}"'

        return {
            'before_options': before_options,
            'options': options,
        }

    except Exception:
        logger.exception('build_ffmpeg_options error')
        return {
            'before_options': FFMPEG_BEFORE_OPTIONS,
            'options': FFMPEG_BASE_OPTIONS,
        }


def create_progress_bar(position, duration, length=18):
    try:
        if not duration or duration <= 0:
            return '░' * length

        ratio = max(0.0, min(1.0, position / duration))
        filled = int(round(ratio * length))
        filled = max(0, min(length, filled))

        return '█' * filled + '░' * (length - filled)

    except Exception:
        logger.exception('create_progress_bar error')
        return '░' * length


def search_youtube(query):
    """Searches YouTube and returns song info. Runs in a thread so it does not block the bot."""
    try:
        if not query or not query.strip():
            return None

        query = query.strip()

        if looks_like_url(query):
            if not is_allowed_youtube_url(query):
                logger.warning('Rejected non-YouTube URL: %s', query)
                return None

            extract_target = query
        else:
            # Searches are YouTube-only.
            extract_target = f'ytsearch1:{query}'

        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(extract_target, download=False)

            if not info:
                return None

            if 'entries' in info:
                entries = info.get('entries') or []

                if len(entries) == 0:
                    return None

                info = entries[0]

            url = info.get('url')
            webpage_url = info.get('webpage_url') or info.get('original_url') or query

            # User-facing result must still be a YouTube URL/page.
            if webpage_url and looks_like_url(webpage_url) and not is_allowed_youtube_url(webpage_url):
                logger.warning('Rejected non-YouTube result: %s', webpage_url)
                return None

            if not url:
                logger.error('yt-dlp did not return a playable URL.')
                return None

            return {
                'url': url,
                'webpage_url': webpage_url,
                'title': info.get('title', query),
                'duration': info.get('duration', 0),
                'requester': None,
                'http_headers': info.get('http_headers') or {},
            }

    except Exception:
        logger.exception('yt-dlp search error')
        return None


def get_human_user_ids_in_channel(guild, channel, bot_user_id):
    """
    Safer human detection for voice channels.

    We avoid relying only on len(channel.members), because during voice leave/move
    events Discord cache can briefly look wrong.
    """
    human_user_ids = set()

    try:
        # Primary source: guild.voice_states
        voice_states = getattr(guild, 'voice_states', {}) or {}

        for user_id, voice_state in voice_states.items():
            if user_id == bot_user_id:
                continue

            if not voice_state.channel:
                continue

            if voice_state.channel.id != channel.id:
                continue

            guild_member = guild.get_member(user_id)

            if guild_member and guild_member.bot:
                continue

            human_user_ids.add(user_id)

        # Fallback source: channel.members.
        # This prevents false "alone" detection if voice_states is temporarily incomplete.
        for guild_member in getattr(channel, 'members', []):
            if guild_member.id == bot_user_id:
                continue

            if guild_member.bot:
                continue

            human_user_ids.add(guild_member.id)

    except Exception:
        logger.exception('get_human_user_ids_in_channel error')

    return list(human_user_ids)


def get_vote_threshold(total_humans):
    """Strict majority. 1 human = 1 vote, 2 humans = 2 votes, 3 humans = 2 votes."""
    try:
        total_humans = max(1, int(total_humans))
        return (total_humans // 2) + 1
    except Exception:
        logger.exception('get_vote_threshold error')
        return 1


def make_vote(action, target, payload):
    return {
        'action': action,
        'target': target,
        'payload': payload,
        'voters': set(),
        'created_at': time.time(),
    }


def song_identity(song):
    """Stable-ish identity used to avoid duplicate queue entries when using !previous."""
    try:
        if not song:
            return None

        # Prefer the watch page URL, because the direct stream URL can change.
        if song.get('webpage_url'):
            return f'web:{song.get("webpage_url")}'

        # Fallback for older queued items that may not have webpage_url.
        return f'title:{song.get("title")}|duration:{song.get("duration")}'

    except Exception:
        logger.exception('song_identity error')
        return None


def remove_song_from_queue(queue, song):
    """Remove matching copies of a song from the queue and return how many were removed."""
    try:
        target = song_identity(song)

        if not target:
            return 0

        kept = deque()
        removed = 0

        while queue:
            queued_song = queue.popleft()

            if song_identity(queued_song) == target:
                removed += 1
                continue

            kept.append(queued_song)

        queue.extend(kept)
        return removed

    except Exception:
        logger.exception('remove_song_from_queue error')
        return 0


# -------------------------------------------------
# MUSIC STATE
# -------------------------------------------------

class MusicState:
    """Holds all music-related state for a single Discord server."""

    def __init__(self):
        self.voice_client = None
        self.queue = deque()  # Real future songs users added with !play
        self.history = deque(maxlen=HISTORY_LIMIT)  # Bounded previous-song stack
        self.forward_stack = deque(maxlen=HISTORY_LIMIT)  # Songs to return to after !previous
        self.current_song = None
        self.current_source = None  # direct / queue / history / forward
        self.loop_mode = 'off'
        self.volume = 0.5
        self.playback_speed = 1.0
        self.idle_timer = None
        self.alone_timer = None
        self.text_channel = None

        # Prevents stop/skip/restart from accidentally triggering loop replay or Queue ended spam.
        self.stop_requested = False
        self.skip_requested = False
        self.ignore_next_after = False  # Legacy one-shot suppress flag
        self.ignore_after_count = 0  # Robust suppress counter for intentional FFmpeg restarts

        # Playback clock tracking for progress, seeking, and speed.
        self.started_at = None
        self.seek_offset = 0.0
        self.paused_at = None
        self.total_paused_time = 0.0

        # Pending votes: skip, volume, queue.
        self.votes = {}


class MusicBot(commands.Bot):
    """Bot subclass with per-guild music states."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.music_states = {}
        self.loop_ref = None

    def get_state(self, guild_id):
        """Return the MusicState for a guild, creating one if it does not exist."""
        if guild_id is None:
            raise ValueError('guild_id cannot be None. Music commands must be used inside a server.')

        if guild_id not in self.music_states:
            self.music_states[guild_id] = MusicState()

        return self.music_states[guild_id]

    def run_threadsafe(self, coro):
        """Safely run a coroutine from Discord audio callback threads."""
        try:
            if self.loop_ref and not self.loop_ref.is_closed():
                return asyncio.run_coroutine_threadsafe(coro, self.loop_ref)

            logger.error('Cannot schedule coroutine: bot loop is not ready.')

            if hasattr(coro, 'close'):
                coro.close()

            return None

        except Exception:
            logger.exception('run_threadsafe error')

            if hasattr(coro, 'close'):
                coro.close()

            return None

    def cancel_idle_timer(self, guild_id):
        """Cancels the idle timer for a guild."""
        try:
            state = self.get_state(guild_id)

            if state.idle_timer:
                state.idle_timer.cancel()
                state.idle_timer = None

        except Exception:
            logger.exception('cancel_idle_timer error')

    def cancel_alone_timer(self, guild_id):
        """Cancels the alone timer for a guild."""
        try:
            state = self.get_state(guild_id)

            if state.alone_timer:
                state.alone_timer.cancel()
                state.alone_timer = None

        except Exception:
            logger.exception('cancel_alone_timer error')

    def reset_playback_clock(self, guild_id, offset=0):
        try:
            state = self.get_state(guild_id)

            state.seek_offset = float(max(0, offset or 0))
            state.started_at = time.monotonic()
            state.paused_at = None
            state.total_paused_time = 0.0

        except Exception:
            logger.exception('reset_playback_clock error')

    def get_current_position(self, guild_id):
        try:
            state = self.get_state(guild_id)

            if not state.current_song:
                return 0

            if state.started_at is None:
                return int(state.seek_offset)

            now = state.paused_at if state.paused_at is not None else time.monotonic()
            real_elapsed = max(0.0, now - state.started_at - state.total_paused_time)
            song_position = state.seek_offset + (real_elapsed * state.playback_speed)

            duration = state.current_song.get('duration') or 0

            if duration:
                song_position = min(song_position, duration)

            return int(max(0, song_position))

        except Exception:
            logger.exception('get_current_position error')
            return 0

    def start_idle_timer(self, guild_id):
        """Starts a timer. If no song plays before it expires, bot leaves."""
        try:
            state = self.get_state(guild_id)

            if state.idle_timer:
                state.idle_timer.cancel()
                state.idle_timer = None

            async def idle_disconnect():
                try:
                    await asyncio.sleep(IDLE_LEAVE_SECONDS)

                    current_state = self.get_state(guild_id)
                    voice_client = current_state.voice_client

                    if not voice_client or not voice_client.is_connected():
                        return

                    if voice_client.is_playing() or voice_client.is_paused():
                        return

                    if current_state.text_channel:
                        await current_state.text_channel.send(
                            f'👋 Left the voice channel (idle for {IDLE_LEAVE_SECONDS // 60} minutes).'
                        )

                    await voice_client.disconnect(force=True)

                    current_state.voice_client = None
                    current_state.queue.clear()
                    current_state.forward_stack.clear()
                    current_state.current_song = None
                    current_state.current_source = None
                    current_state.votes.clear()
                    current_state.stop_requested = False
                    current_state.skip_requested = False
                    current_state.ignore_next_after = False
                    current_state.ignore_after_count = 0

                except asyncio.CancelledError:
                    return
                except Exception:
                    logger.exception('idle_disconnect error')

            if self.loop_ref and not self.loop_ref.is_closed():
                state.idle_timer = asyncio.run_coroutine_threadsafe(idle_disconnect(), self.loop_ref)
            else:
                logger.error('Cannot start idle timer: bot loop is not ready.')

        except Exception:
            logger.exception('start_idle_timer error')

    def start_song(self, guild_id, song, offset=0, announce=False, source_kind=None):
        """Start a song immediately. Safe to call from normal code or audio callback code."""
        try:
            state = self.get_state(guild_id)

            if not state.voice_client:
                logger.warning('start_song called but no voice client exists.')
                return False

            if not state.voice_client.is_connected():
                logger.warning('start_song called but voice client is disconnected.')
                return False

            duration = song.get('duration') or 0
            offset = int(max(0, offset or 0))

            if duration and offset >= duration:
                offset = max(0, int(duration) - 1)

            ffmpeg_opts = build_ffmpeg_options(
                song.get('http_headers'),
                seek_seconds=offset,
                speed=state.playback_speed,
            )

            source = discord.FFmpegPCMAudio(song['url'], **ffmpeg_opts)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)

            state.current_song = song

            if source_kind is not None:
                state.current_source = source_kind

            state.stop_requested = False
            state.skip_requested = False
            state.ignore_next_after = False
            self.reset_playback_clock(guild_id, offset=offset)

            state.voice_client.play(
                source,
                after=lambda e: self.play_next(guild_id, e)
            )

            if announce and state.text_channel:
                self.run_threadsafe(
                    state.text_channel.send(
                        f'🎵 Now playing: **{song["title"]}** '
                        f'[{format_duration(song.get("duration", 0))}]'
                    )
                )

            return True

        except Exception:
            logger.exception('start_song error')
            return False

    def play_next(self, guild_id, error=None):
        """Called when a song finishes. Plays the next song in queue if there is one."""
        try:
            state = self.get_state(guild_id)

            if getattr(state, 'ignore_after_count', 0) > 0:
                state.ignore_after_count -= 1
                return

            if state.ignore_next_after:
                state.ignore_next_after = False
                return

            if error:
                logger.error('Player error: %s', error)

                if state.text_channel:
                    self.run_threadsafe(
                        state.text_channel.send('⚠️ Error playing song, checking the queue...')
                    )

            # If !stop triggered this callback, do nothing.
            if state.stop_requested:
                state.stop_requested = False
                state.skip_requested = False
                return

            if not state.voice_client:
                logger.warning('play_next called but no voice client exists.')
                return

            if not state.voice_client.is_connected():
                logger.warning('play_next called but voice client is disconnected.')
                return

            finished_song = state.current_song

            # If !skip / !previous triggered this callback, do not loop/re-add current song.
            if state.skip_requested:
                state.skip_requested = False
                state.current_song = None
                state.current_source = None
            else:
                # LOOP SONG: replay same song from the start.
                if state.loop_mode == 'song' and finished_song:
                    ok = self.start_song(
                        guild_id,
                        finished_song,
                        offset=0,
                        announce=False,
                        source_kind=state.current_source,
                    )

                    if not ok:
                        state.loop_mode = 'off'

                        if state.text_channel:
                            self.run_threadsafe(
                                state.text_channel.send('⚠️ Loop failed, disabling song loop.')
                            )

                    return

                if finished_song:
                    state.history.append(finished_song)

                # LOOP QUEUE: put finished queue/direct/forward songs back at the end.
                # Songs reached via !previous are history navigation, not real queue songs.
                if state.loop_mode == 'queue' and finished_song and state.current_source != 'history':
                    state.queue.append(finished_song)

            # Navigation return has priority over the normal user queue.
            if len(state.forward_stack) > 0:
                next_song = state.forward_stack.popleft()
                ok = self.start_song(
                    guild_id,
                    next_song,
                    offset=0,
                    announce=True,
                    source_kind='forward',
                )

                if not ok:
                    if state.text_channel:
                        self.run_threadsafe(
                            state.text_channel.send(
                                f'⚠️ Error returning to **{next_song["title"]}**, skipping...'
                            )
                        )

                    state.current_song = None
                    state.current_source = None
                    self.play_next(guild_id)

                return

            # Queue is empty.
            if len(state.queue) == 0:
                state.current_song = None
                state.current_source = None
                state.started_at = None
                state.seek_offset = 0.0
                state.paused_at = None
                state.total_paused_time = 0.0
                self.start_idle_timer(guild_id)
                return

            # Play next queued song.
            next_song = state.queue.popleft()
            ok = self.start_song(
                guild_id,
                next_song,
                offset=0,
                announce=True,
                source_kind='queue',
            )

            if not ok:
                if state.text_channel:
                    self.run_threadsafe(
                        state.text_channel.send(
                            f'⚠️ Error playing **{next_song["title"]}**, skipping...'
                        )
                    )

                state.current_song = None
                state.current_source = None
                self.play_next(guild_id)

        except Exception:
            logger.exception('play_next fatal error')


# -------------------------------------------------
# BOT SETUP
# -------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = MusicBot(command_prefix='!', intents=intents, help_command=None)


# -------------------------------------------------
# PERMISSIONS / VOTES
# -------------------------------------------------

def can_override_votes(ctx):
    """Admins / high hierarchy users can override votes."""
    try:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return False

        permissions = ctx.author.guild_permissions

        if permissions.administrator or permissions.manage_guild or permissions.manage_channels:
            return True

        bot_member = ctx.guild.me

        if bot_member and ctx.author.top_role > bot_member.top_role:
            return True

        return False

    except Exception:
        logger.exception('can_override_votes error')
        return False


def user_is_in_bot_voice(ctx, state):
    try:
        if not isinstance(ctx.author, discord.Member):
            return False

        if not ctx.author.voice or not ctx.author.voice.channel:
            return False

        if not state.voice_client or not state.voice_client.channel:
            return True

        return ctx.author.voice.channel.id == state.voice_client.channel.id

    except Exception:
        logger.exception('user_is_in_bot_voice error')
        return False


def expire_old_votes(state):
    try:
        now = time.time()

        for action in list(state.votes.keys()):
            vote = state.votes[action]

            if now - vote.get('created_at', now) > VOTE_TIMEOUT_SECONDS:
                del state.votes[action]

    except Exception:
        logger.exception('expire_old_votes error')


def get_vote_info(ctx, state):
    try:
        if state.voice_client and state.voice_client.channel:
            channel = state.voice_client.channel
        elif isinstance(ctx.author, discord.Member) and ctx.author.voice:
            channel = ctx.author.voice.channel
        else:
            return 1, 1

        human_ids = get_human_user_ids_in_channel(ctx.guild, channel, bot.user.id)
        total = max(1, len(human_ids))
        threshold = get_vote_threshold(total)

        return threshold, total

    except Exception:
        logger.exception('get_vote_info error')
        return 1, 1


async def execute_skip(ctx, state, reason='Skipped'):
    try:
        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await ctx.send('Nothing is playing.')
            return

        if state.current_song:
            state.history.append(state.current_song)

        state.skip_requested = True
        state.current_song = None
        state.current_source = None
        state.voice_client.stop()

        await ctx.send(f'⏭️ {reason}!')

    except Exception:
        logger.exception('execute_skip error')
        await ctx.send('❌ Could not skip.')


async def execute_volume(ctx, state, volume_percent, reason='Volume changed'):
    try:
        volume_percent = int(volume_percent)

        if volume_percent < 0 or volume_percent > 100:
            await ctx.send('Volume must be between 0 and 100.')
            return

        state.volume = volume_percent / 100

        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume

        await ctx.send(f'🔊 {reason}: **{volume_percent}%**')

    except Exception:
        logger.exception('execute_volume error')
        await ctx.send('❌ Could not set volume.')


async def execute_queue_add(ctx, state, song_info, reason='Added to queue'):
    try:
        if len(state.queue) >= MAX_QUEUE_SIZE:
            await ctx.send(f'❌ Queue is full. Max queue size is **{MAX_QUEUE_SIZE}** songs.')
            return

        state.queue.append(song_info)
        position = len(state.queue)

        await ctx.send(
            f'📋 {reason} (#{position}): '
            f'**{song_info["title"]}** [{format_duration(song_info["duration"])}]'
        )

    except Exception:
        logger.exception('execute_queue_add error')
        await ctx.send('❌ Could not add song to queue.')


async def add_vote_or_execute(ctx, state, action, target, payload, execute_func):
    try:
        expire_old_votes(state)

        if not user_is_in_bot_voice(ctx, state) and not can_override_votes(ctx):
            await ctx.send('❌ You need to be in the same voice channel as the bot.')
            return

        threshold, total = get_vote_info(ctx, state)

        if can_override_votes(ctx):
            state.votes.pop(action, None)
            await execute_func()
            return

        if threshold <= 1:
            state.votes.pop(action, None)
            await execute_func()
            return

        vote = state.votes.get(action)

        if not vote or vote.get('target') != target:
            vote = make_vote(action, target, payload)
            state.votes[action] = vote

        vote['voters'].add(ctx.author.id)

        count = len(vote['voters'])

        if count >= threshold:
            state.votes.pop(action, None)
            await execute_func()
            return

        await ctx.send(
            f'🗳️ Vote started for **{action}**: **{count}/{threshold}** votes '
            f'({total} users in voice). Others can use `!vote {action}`.'
        )

    except Exception:
        logger.exception('add_vote_or_execute error')
        await ctx.send('❌ Vote failed.')


async def execute_pending_vote(ctx, state, vote):
    try:
        action = vote.get('action')
        payload = vote.get('payload') or {}

        if action == 'skip':
            await execute_skip(ctx, state, reason='Vote passed, skipped')
        elif action == 'volume':
            await execute_volume(ctx, state, payload.get('volume'), reason='Vote passed, volume changed')
        elif action == 'queue':
            await execute_queue_add(ctx, state, payload.get('song_info'), reason='Vote passed, added to queue')
        else:
            await ctx.send('❌ Unknown vote action.')

    except Exception:
        logger.exception('execute_pending_vote error')
        await ctx.send('❌ Could not execute vote.')


# -------------------------------------------------
# EVENTS
# -------------------------------------------------

@bot.event
async def on_ready():
    """Runs when the bot successfully connects to Discord."""
    bot.loop_ref = asyncio.get_running_loop()
    logger.info(f'Logged on as {bot.user}!')


@bot.event
async def on_voice_state_update(member, before, after):
    """Auto-leave only when there are ZERO real human users left in the bot's voice channel."""
    try:
        if member == bot.user:
            return

        if not member.guild:
            return

        state = bot.get_state(member.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return

        bot_channel = state.voice_client.channel

        if not bot_channel:
            return

        human_user_ids = get_human_user_ids_in_channel(
            member.guild,
            bot_channel,
            bot.user.id
        )

        logger.info(
            'Voice check: channel=%s humans=%s cached_members=%s',
            bot_channel.name,
            human_user_ids,
            [m.id for m in getattr(bot_channel, 'members', [])]
        )

        # If at least one real human is still there, do NOT leave.
        if len(human_user_ids) > 0:
            bot.cancel_alone_timer(member.guild.id)
            return

        # No humans detected. Start delayed leave.
        bot.cancel_alone_timer(member.guild.id)

        async def alone_disconnect():
            try:
                await asyncio.sleep(ALONE_LEAVE_SECONDS)

                current_state = bot.get_state(member.guild.id)

                if not current_state.voice_client or not current_state.voice_client.is_connected():
                    return

                current_channel = current_state.voice_client.channel

                if not current_channel:
                    return

                # Recheck after delay.
                remaining_humans = get_human_user_ids_in_channel(
                    member.guild,
                    current_channel,
                    bot.user.id
                )

                logger.info(
                    'Alone timer recheck: channel=%s remaining_humans=%s cached_members=%s',
                    current_channel.name,
                    remaining_humans,
                    [m.id for m in getattr(current_channel, 'members', [])]
                )

                # Someone is still there, so stay.
                if len(remaining_humans) > 0:
                    return

                if current_state.text_channel:
                    await current_state.text_channel.send(
                        f'👋 Left the voice channel (nobody else here for {ALONE_LEAVE_SECONDS} seconds).'
                    )

                current_state.stop_requested = True
                current_state.queue.clear()
                current_state.forward_stack.clear()
                current_state.votes.clear()
                current_state.current_song = None
                current_state.current_source = None
                current_state.ignore_after_count = 0

                if current_state.voice_client.is_playing() or current_state.voice_client.is_paused():
                    current_state.voice_client.stop()

                await current_state.voice_client.disconnect(force=True)
                current_state.voice_client = None

            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception('alone_disconnect error')

        state.alone_timer = asyncio.create_task(alone_disconnect())

    except Exception:
        logger.exception('on_voice_state_update error')


@bot.event
async def on_command_error(ctx, error):
    """Better command error logging and cooldown messages."""
    try:
        if isinstance(error, commands.CommandNotFound):
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f'⏳ Slow down. Try again in **{error.retry_after:.1f}s**.')
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send('❌ Missing command argument. Use `!help` to see commands.')
            return

        logger.error('Unhandled command error in %s', ctx.command, exc_info=error)
        await ctx.send('❌ Unexpected command error. Check the terminal for details.')

    except Exception:
        logger.exception('on_command_error error')


# -------------------------------------------------
# COMMANDS
# -------------------------------------------------

@bot.command(name='play')
@commands.cooldown(1, PLAY_COOLDOWN_SECONDS, commands.BucketType.user)
async def play(ctx, *, query: str = ''):
    """Search YouTube and play a song. Adds to queue if something is already playing."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        # Finish multi-guild migration safety:
        # If Discord already has a voice client for this guild, keep state synced.
        if state.voice_client is None and ctx.voice_client:
            state.voice_client = ctx.voice_client

        query = query.strip()

        if not query:
            return await ctx.send('❌ You need to tell me what to play! Example: `!play lofi beats`')

        if looks_like_url(query) and not is_allowed_youtube_url(query):
            return await ctx.send('❌ Only YouTube links are allowed. Use `youtube.com`, `music.youtube.com`, or `youtu.be`.')

        state.text_channel = ctx.channel

        if ctx.author.voice is None or ctx.author.voice.channel is None:
            return await ctx.send('❌ You need to be in a voice channel!')

        voice_channel = ctx.author.voice.channel

        bot.cancel_idle_timer(ctx.guild.id)
        bot.cancel_alone_timer(ctx.guild.id)

        try:
            if state.voice_client is None or not state.voice_client.is_connected():
                state.voice_client = await voice_channel.connect(timeout=60.0, reconnect=True)
            elif state.voice_client.channel != voice_channel:
                await state.voice_client.move_to(voice_channel)

        except Exception:
            logger.exception('Voice connection error')
            return await ctx.send("❌ Couldn't connect to the voice channel.")

        await ctx.send(f'🔎 Searching YouTube for: **{query}**')

        try:
            loop = asyncio.get_running_loop()

            song_info = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    functools.partial(search_youtube, query)
                ),
                timeout=YTDLP_COMMAND_TIMEOUT_SECONDS
            )

            if song_info is None:
                return await ctx.send('❌ No YouTube results found for that search/link.')

            song_info['requester'] = ctx.author.display_name

        except asyncio.TimeoutError:
            logger.exception('yt-dlp command timeout')
            return await ctx.send('❌ Search timed out. YouTube might be slow, try again.')
        except Exception:
            logger.exception('yt-dlp command error')
            return await ctx.send("❌ Couldn't find or load that song. Try a different search.")

        # Already playing: queue vote or admin/direct queue.
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            if len(state.queue) >= MAX_QUEUE_SIZE:
                return await ctx.send(f'❌ Queue is full. Max queue size is **{MAX_QUEUE_SIZE}** songs.')

            target = song_info.get('webpage_url') or song_info.get('title')

            async def do_queue_add():
                await execute_queue_add(ctx, state, song_info)

            await add_vote_or_execute(
                ctx,
                state,
                action='queue',
                target=target,
                payload={'song_info': song_info},
                execute_func=do_queue_add,
            )

            return

        # Nothing playing: start now.
        state.current_song = song_info
        state.current_source = 'direct'
        state.forward_stack.clear()
        state.stop_requested = False
        state.skip_requested = False
        state.ignore_next_after = False
        state.ignore_after_count = 0

        ok = bot.start_song(
            ctx.guild.id,
            song_info,
            offset=0,
            announce=False,
            source_kind='direct',
        )

        if not ok:
            state.current_song = None
            state.current_source = None
            return await ctx.send('❌ Error playing that song. Try again or try a different one.')

        await ctx.send(
            f'🎵 Now playing: **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
        )

    except Exception:
        logger.exception('play command error')
        await ctx.send('❌ Unexpected error in play command.')


@bot.command(name='np', aliases=['nowplaying', 'progress', 'time'])
async def now_playing(ctx):
    """Show details and progress for the song currently playing."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.current_song is None:
            return await ctx.send('Nothing is playing right now.')

        song = state.current_song
        position = bot.get_current_position(ctx.guild.id)
        duration = song.get('duration') or 0
        left = max(0, duration - position) if duration else 0
        bar = create_progress_bar(position, duration)

        loop_status = ''

        if state.loop_mode == 'song':
            loop_status = '\n🔂 **Loop:** Song'
        elif state.loop_mode == 'queue':
            loop_status = '\n🔁 **Loop:** Queue'

        await ctx.send(
            f'🎵 **Now Playing**\n'
            f'**Title:** {song["title"]}\n'
            f'**Progress:** `{format_duration(position)}/{format_duration(duration)}`\n'
            f'`{bar}`\n'
            f'**Left:** `{format_duration(left)}`\n'
            f'**Speed:** `{state.playback_speed:.2f}x`\n'
            f'**Requested by:** {song["requester"]}'
            f'{loop_status}'
        )

    except Exception:
        logger.exception('np/progress command error')
        await ctx.send('❌ Could not show now playing.')


@bot.command(name='previous', aliases=['prev'])
async def previous_song(ctx):
    """Go back to the previous song using a separate bounded history stack."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if not state.voice_client or not state.voice_client.is_connected():
            return await ctx.send('I am not connected to a voice channel.')

        if len(state.history) == 0:
            return await ctx.send('No previous song in history.')

        previous = state.history.pop()

        # Clean model:
        # - queue stays only for future songs users added with !play
        # - history stores previous songs
        # - forward_stack stores the song(s) we need to return to after !previous
        if state.current_song:
            state.forward_stack.appendleft(state.current_song)

        state.ignore_next_after = False
        state.ignore_after_count += 1

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.voice_client.stop()
            await asyncio.sleep(0.2)

        ok = bot.start_song(
            ctx.guild.id,
            previous,
            offset=0,
            announce=False,
            source_kind='history',
        )

        if not ok:
            return await ctx.send('❌ Could not go to previous song.')

        await ctx.send(f'⏮️ Going back to: **{previous["title"]}**')

    except Exception:
        logger.exception('previous command error')
        await ctx.send('❌ Could not go to previous song.')


async def restart_current_song_at(ctx, offset):
    """Restart current song at offset. Used by seek/forward/back/speed."""
    try:
        state = bot.get_state(ctx.guild.id)

        if not state.current_song:
            await ctx.send('Nothing is playing right now.')
            return

        duration = state.current_song.get('duration') or 0

        if duration:
            offset = max(0, min(int(offset), int(duration) - 1))
        else:
            offset = max(0, int(offset))

        if not state.voice_client or not state.voice_client.is_connected():
            await ctx.send('I am not connected to a voice channel.')
            return

        state.ignore_next_after = False
        state.ignore_after_count += 1

        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.voice_client.stop()
            await asyncio.sleep(0.2)

        ok = bot.start_song(
            ctx.guild.id,
            state.current_song,
            offset=offset,
            announce=False,
            source_kind=state.current_source,
        )

        if not ok:
            await ctx.send('❌ Could not restart the song at that position.')
            return

        await ctx.send(
            f'⏩ Position: `{format_duration(offset)}/{format_duration(duration)}` '
            f'| Speed: `{state.playback_speed:.2f}x`'
        )

    except Exception:
        logger.exception('restart_current_song_at error')
        await ctx.send('❌ Could not seek/restart song.')


@bot.command(name='forward', aliases=['ff'])
async def forward_cmd(ctx, seconds: str = '10'):
    """Forward inside the current song. Example: !forward 30"""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        amount = parse_time_value(seconds)

        if amount is None:
            return await ctx.send('Use seconds or timestamp. Example: `!forward 30`')

        current = bot.get_current_position(ctx.guild.id)
        await restart_current_song_at(ctx, current + amount)

    except Exception:
        logger.exception('forward command error')
        await ctx.send('❌ Could not forward.')


@bot.command(name='backward', aliases=['back', 'rewind'])
async def backward_cmd(ctx, seconds: str = '10'):
    """Go backward inside the current song. Example: !back 15"""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        amount = parse_time_value(seconds)

        if amount is None:
            return await ctx.send('Use seconds or timestamp. Example: `!back 15`')

        current = bot.get_current_position(ctx.guild.id)
        await restart_current_song_at(ctx, max(0, current - amount))

    except Exception:
        logger.exception('backward command error')
        await ctx.send('❌ Could not rewind.')


@bot.command(name='seek')
async def seek_cmd(ctx, timestamp: str):
    """Seek to a specific time. Example: !seek 2:03"""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        offset = parse_time_value(timestamp)

        if offset is None:
            return await ctx.send('Use seconds or timestamp. Example: `!seek 2:03`')

        await restart_current_song_at(ctx, offset)

    except Exception:
        logger.exception('seek command error')
        await ctx.send('❌ Could not seek.')


@bot.command(name='speed')
async def speed_cmd(ctx, value: str = ''):
    """Set playback speed from 0.5x to 2.0x. Example: !speed 1.25"""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if not value:
            return await ctx.send(f'⏩ Current playback speed: **{state.playback_speed:.2f}x**')

        try:
            speed = float(value)
        except ValueError:
            return await ctx.send('Use a number between 0.5 and 2.0. Example: `!speed 1.25`')

        if speed < MIN_PLAYBACK_SPEED or speed > MAX_PLAYBACK_SPEED:
            return await ctx.send(f'Speed must be between {MIN_PLAYBACK_SPEED} and {MAX_PLAYBACK_SPEED}.')

        current_position = bot.get_current_position(ctx.guild.id)
        state.playback_speed = speed

        if state.current_song and state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            await restart_current_song_at(ctx, current_position)
        else:
            await ctx.send(f'⏩ Playback speed set to **{speed:.2f}x**')

    except Exception:
        logger.exception('speed command error')
        await ctx.send('❌ Could not change playback speed.')


@bot.command(name='loop')
async def loop_cmd(ctx, *, arg: str = ''):
    """Set loop mode: song, queue, or off. No argument shows current status."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        arg = arg.strip().lower()

        if arg == 'song':
            state.loop_mode = 'song'
            await ctx.send('🔂 Looping the **current song**.')
        elif arg == 'queue':
            state.loop_mode = 'queue'
            await ctx.send('🔁 Looping the **entire queue**.')
        elif arg == 'off':
            state.loop_mode = 'off'
            await ctx.send('➡️ Loop is now **off**.')
        else:
            await ctx.send(
                f'Current loop mode: **{state.loop_mode}**\n'
                f'Usage: `!loop song` / `!loop queue` / `!loop off`'
            )

    except Exception:
        logger.exception('loop command error')
        await ctx.send('❌ Could not update loop mode.')


@bot.command(name='skip')
@commands.cooldown(1, SKIP_COOLDOWN_SECONDS, commands.BucketType.user)
async def skip(ctx):
    """Vote to skip, or skip instantly if admin/high hierarchy."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if not state.voice_client or not (state.voice_client.is_playing() or state.voice_client.is_paused()):
            return await ctx.send('Nothing is playing.')

        target = state.current_song.get('webpage_url') if state.current_song else 'current-song'

        async def do_skip():
            await execute_skip(ctx, state)

        await add_vote_or_execute(
            ctx,
            state,
            action='skip',
            target=target,
            payload={},
            execute_func=do_skip,
        )

    except Exception:
        logger.exception('skip command error')
        await ctx.send('❌ Could not skip.')


@bot.command(name='vote')
async def vote_cmd(ctx, action: str = ''):
    """Vote for a pending action. Example: !vote skip / !vote volume / !vote queue"""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        action = action.strip().lower()

        if action not in ('skip', 'volume', 'queue'):
            return await ctx.send('Usage: `!vote skip`, `!vote volume`, or `!vote queue`')

        state = bot.get_state(ctx.guild.id)
        expire_old_votes(state)

        vote = state.votes.get(action)

        if not vote:
            return await ctx.send(f'No active **{action}** vote.')

        if not user_is_in_bot_voice(ctx, state) and not can_override_votes(ctx):
            return await ctx.send('❌ You need to be in the same voice channel as the bot.')

        threshold, total = get_vote_info(ctx, state)

        if can_override_votes(ctx):
            state.votes.pop(action, None)
            await execute_pending_vote(ctx, state, vote)
            return

        vote['voters'].add(ctx.author.id)
        count = len(vote['voters'])

        if count >= threshold:
            state.votes.pop(action, None)
            await execute_pending_vote(ctx, state, vote)
            return

        await ctx.send(
            f'🗳️ Vote counted for **{action}**: **{count}/{threshold}** votes '
            f'({total} users in voice).'
        )

    except Exception:
        logger.exception('vote command error')
        await ctx.send('❌ Could not vote.')


@bot.command(name='votes')
async def votes_cmd(ctx):
    """Show active votes."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)
        expire_old_votes(state)

        if not state.votes:
            return await ctx.send('No active votes.')

        threshold, total = get_vote_info(ctx, state)

        lines = ['🗳️ **Active Votes**']

        for action, vote in state.votes.items():
            count = len(vote.get('voters', []))
            target = vote.get('target', 'unknown')
            lines.append(f'`{action}` — {count}/{threshold} votes — {target}')

        lines.append(f'\nUsers in voice: {total}')
        lines.append('Use `!vote skip`, `!vote volume`, or `!vote queue`.')

        await ctx.send('\n'.join(lines))

    except Exception:
        logger.exception('votes command error')
        await ctx.send('❌ Could not show votes.')


@bot.command(name='queue')
async def queue_cmd(ctx):
    """Show the current song, the real user queue, and the return stack."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.current_song is None and len(state.queue) == 0 and len(state.forward_stack) == 0:
            return await ctx.send('The queue is empty.')

        queue_text = ''

        if state.current_song:
            position = bot.get_current_position(ctx.guild.id)
            duration = state.current_song.get('duration') or 0

            queue_text += (
                f'🎵 **Now playing:** {state.current_song["title"]} '
                f'[`{format_duration(position)}/{format_duration(duration)}`] '
                f'(requested by {state.current_song["requester"]})\n\n'
            )

        if len(state.forward_stack) > 0:
            queue_text += (
                f'**Returning after previous:** `{len(state.forward_stack)}` song(s)\n'
            )

            for i, song in enumerate(list(state.forward_stack)[:5], 1):
                queue_text += (
                    f'{i}. {song["title"]} '
                    f'[{format_duration(song["duration"])}] '
                    f'(requested by {song["requester"]})\n'
                )

            if len(state.forward_stack) > 5:
                queue_text += f'*...and {len(state.forward_stack) - 5} more return song(s).*\n'

            queue_text += '\n'

        if len(state.queue) > 0:
            queue_text += (
                f'**Up next:** `{len(state.queue)}` queued '
                f'(new-song add limit: `{MAX_QUEUE_SIZE}`)\n'
            )

            for i, song in enumerate(list(state.queue)[:15], 1):
                queue_text += (
                    f'{i}. {song["title"]} '
                    f'[{format_duration(song["duration"])}] '
                    f'(requested by {song["requester"]})\n'
                )

            if len(state.queue) > 15:
                queue_text += f'\n*...and {len(state.queue) - 15} more songs.*'
        else:
            queue_text += '*No more songs in queue.*'

        await ctx.send(queue_text)

    except Exception:
        logger.exception('queue command error')
        await ctx.send('❌ Could not show queue.')


@bot.command(name='stop')
async def stop(ctx):
    """Stop playback and clear the queue."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client:
            state.stop_requested = True
            state.queue.clear()
            state.forward_stack.clear()
            state.votes.clear()
            state.current_song = None
            state.current_source = None
            state.ignore_after_count = 0

            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()

            bot.start_idle_timer(ctx.guild.id)

            await ctx.send('⏹️ Stopped and cleared the queue.')
        else:
            await ctx.send('Nothing is playing.')

    except Exception:
        logger.exception('stop command error')
        await ctx.send('❌ Could not stop playback.')


@bot.command(name='pause')
async def pause(ctx):
    """Pause the current song."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client and state.voice_client.is_playing():
            state.voice_client.pause()

            if state.paused_at is None:
                state.paused_at = time.monotonic()

            await ctx.send('⏸️ Paused.')
        else:
            await ctx.send('Nothing is playing.')

    except Exception:
        logger.exception('pause command error')
        await ctx.send('❌ Could not pause.')


@bot.command(name='resume')
async def resume(ctx):
    """Resume a paused song."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client and state.voice_client.is_paused():
            if state.paused_at is not None:
                state.total_paused_time += max(0.0, time.monotonic() - state.paused_at)
                state.paused_at = None

            state.voice_client.resume()
            await ctx.send('▶️ Resumed.')
        else:
            await ctx.send('Nothing is paused.')

    except Exception:
        logger.exception('resume command error')
        await ctx.send('❌ Could not resume.')


@bot.command(name='leave')
async def leave(ctx):
    """Disconnect the bot from voice and clear the queue."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client and state.voice_client.is_connected():
            state.stop_requested = True
            state.queue.clear()
            state.forward_stack.clear()
            state.votes.clear()
            state.current_song = None
            state.current_source = None

            bot.cancel_idle_timer(ctx.guild.id)
            bot.cancel_alone_timer(ctx.guild.id)

            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()

            await state.voice_client.disconnect(force=True)
            state.voice_client = None

            await ctx.send('👋 Left the voice channel.')
        else:
            await ctx.send('I am not connected to a voice channel.')

    except Exception:
        logger.exception('leave command error')
        await ctx.send('❌ Could not leave voice channel.')


@bot.command(name='shuffle')
async def shuffle(ctx):
    """Randomize the order of songs in the queue."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if len(state.queue) < 2:
            return await ctx.send('Not enough songs in the queue to shuffle.')

        queue_list = list(state.queue)
        random.shuffle(queue_list)
        state.queue = deque(queue_list)

        await ctx.send(f'🔀 Shuffled {len(state.queue)} songs in the queue.')

    except Exception:
        logger.exception('shuffle command error')
        await ctx.send('❌ Could not shuffle queue.')


@bot.command(name='volume')
@commands.cooldown(1, VOLUME_COOLDOWN_SECONDS, commands.BucketType.user)
async def volume_cmd(ctx, *, arg: str = ''):
    """Vote to set volume 0-100, or set instantly if admin/high hierarchy."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        arg = arg.strip()

        if not arg:
            return await ctx.send(f'🔊 Current volume: **{int(state.volume * 100)}%**')

        try:
            vol = int(arg)
        except ValueError:
            return await ctx.send('Use a number between 0 and 100. Example: `!volume 50`')

        if vol < 0 or vol > 100:
            return await ctx.send('Volume must be between 0 and 100.')

        async def do_volume():
            await execute_volume(ctx, state, vol)

        await add_vote_or_execute(
            ctx,
            state,
            action='volume',
            target=f'volume:{vol}',
            payload={'volume': vol},
            execute_func=do_volume,
        )

    except Exception:
        logger.exception('volume command error')
        await ctx.send('❌ Could not set volume.')


@bot.command(name='help')
async def help_cmd(ctx):
    """Show the list of available commands."""
    help_text = (
        '🎵 **Music Bot Commands**\n\n'
        '`!play <YouTube search or URL>` — Search YouTube and play/add song\n'
        '`!pause` — Pause the current song\n'
        '`!resume` — Resume playback\n'
        '`!stop` — Stop and clear the queue\n'
        '`!skip` — Vote/admin skip current song\n'
        '`!previous` / `!prev` — Go to previous song\n'
        '`!forward <seconds>` / `!ff <seconds>` — Forward in current song\n'
        '`!back <seconds>` / `!rewind <seconds>` — Backward in current song\n'
        '`!seek <time>` — Seek to timestamp, example `!seek 2:03`\n'
        '`!speed <0.5-2.0>` — Change playback speed\n'
        '`!queue` — Show the queue\n'
        '`!np` / `!progress` — Now playing + progress bar\n'
        '`!loop song/queue/off` — Set loop mode\n'
        '`!shuffle` — Shuffle the queue\n'
        '`!volume <0-100>` — Vote/admin set volume\n'
        '`!vote skip/volume/queue` — Vote for pending action\n'
        '`!votes` — Show active votes\n'
        '`!leave` — Disconnect the bot\n'
        '`!help` — Show this list\n\n'
        f'New-song add limit: **{MAX_QUEUE_SIZE}** queued songs. '
        f'Previous-song history remembers the last **{HISTORY_LIMIT}** songs. '
        '`!previous` uses separate history/return stacks, so it does not pollute the real queue. '
        'Only YouTube searches/URLs are allowed.'
    )

    await ctx.send(help_text)


# -------------------------------------------------
# MAIN
# -------------------------------------------------

def main():
    token = os.getenv('DISCORD_TOKEN')

    if not token:
        raise RuntimeError('DISCORD_TOKEN is missing. Add it to your .env file.')

    bot.run(token)


if __name__ == '__main__':
    main()
