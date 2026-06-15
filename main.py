import discord
import os
import random
import asyncio
import functools
import logging
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

YDL_OPTIONS = {
    # Use the Android player client — it returns audio URLs without YouTube's
    # session-locking that causes 403 errors when FFmpeg tries to fetch them.
    'format': 'bestaudio/best',
    'noplaylist': True,
    'quiet': True,
    'no_warnings': True,
    'default_search': 'ytsearch',
    'cachedir': False,
    'extractor_args': {
        'youtube': {
            'player_client': ['android'],
        }
    },
}

FFMPEG_OPTIONS = {
    'before_options': (
        '-nostdin '
        '-reconnect 1 '
        '-reconnect_streamed 1 '
        '-reconnect_at_eof 1 '
        '-reconnect_on_network_error 1 '
        '-reconnect_delay_max 10'
    ),
    'options': '-vn'
}


def build_ffmpeg_options(http_headers):
    """Build FFmpeg options including HTTP headers required by YouTube to avoid 403."""
    opts = dict(FFMPEG_OPTIONS)
    if http_headers:
        # Format headers as "Key: Value\r\n" lines for FFmpeg
        header_lines = ''.join(f'{k}: {v}\\r\\n' for k, v in http_headers.items())
        opts['before_options'] = f'{opts["before_options"]} -headers "{header_lines}"'
    return opts

IDLE_LEAVE_SECONDS = 180
ALONE_LEAVE_SECONDS = 30


# -------------------------------------------------
# HELPERS
# -------------------------------------------------

def format_duration(seconds):
    """Converts seconds into a nice mm:ss or hh:mm:ss format."""
    try:
        if not seconds:
            return 'Unknown'

        minutes, secs = divmod(int(seconds), 60)
        hours, minutes = divmod(minutes, 60)

        if hours > 0:
            return f'{hours}:{minutes:02d}:{secs:02d}'

        return f'{minutes}:{secs:02d}'

    except Exception as e:
        logger.exception(f'format_duration error: {e}')
        return 'Unknown'


def search_youtube(query):
    """Searches YouTube and returns song info. Runs in a thread so it does not block the bot."""
    try:
        if not query or not query.strip():
            return None

        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)

            if not info:
                return None

            if 'entries' in info:
                entries = info.get('entries') or []

                if len(entries) == 0:
                    return None

                info = entries[0]

            url = info.get('url')

            if not url:
                logger.error('yt-dlp did not return a playable URL.')
                return None

            return {
                'url': url,
                'title': info.get('title', query),
                'duration': info.get('duration', 0),
                'requester': None,
                'http_headers': info.get('http_headers') or {},
            }

    except Exception as e:
        logger.exception(f'yt-dlp search error: {e}')
        return None


def get_human_user_ids_in_channel(guild, channel, bot_user_id):
    """
    Safer human detection for voice channels.

    We avoid relying only on len(channel.members), because during voice leave/move
    events Discord cache can briefly look wrong.
    """
    human_user_ids = set()

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

    # Fallback source: channel.members
    # This prevents false "alone" detection if voice_states is temporarily incomplete.
    for guild_member in getattr(channel, 'members', []):
        if guild_member.id == bot_user_id:
            continue

        if guild_member.bot:
            continue

        human_user_ids.add(guild_member.id)

    return list(human_user_ids)


# -------------------------------------------------
# MUSIC STATE
# -------------------------------------------------

class MusicState:
    """Holds all music-related state for a single Discord server."""

    def __init__(self):
        self.voice_client = None
        self.queue = deque()
        self.current_song = None
        self.loop_mode = 'off'
        self.volume = 0.5
        self.idle_timer = None
        self.alone_timer = None
        self.text_channel = None

        # Prevents stop/skip from accidentally triggering loop replay or Queue ended spam
        self.stop_requested = False
        self.skip_requested = False


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

        except Exception as e:
            logger.exception(f'run_threadsafe error: {e}')

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

        except Exception as e:
            logger.exception(f'cancel_idle_timer error: {e}')

    def cancel_alone_timer(self, guild_id):
        """Cancels the alone timer for a guild."""
        try:
            state = self.get_state(guild_id)

            if state.alone_timer:
                state.alone_timer.cancel()
                state.alone_timer = None

        except Exception as e:
            logger.exception(f'cancel_alone_timer error: {e}')

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
                    current_state.current_song = None
                    current_state.stop_requested = False
                    current_state.skip_requested = False

                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.exception(f'idle_disconnect error: {e}')

            if self.loop_ref and not self.loop_ref.is_closed():
                state.idle_timer = asyncio.run_coroutine_threadsafe(idle_disconnect(), self.loop_ref)
            else:
                logger.error('Cannot start idle timer: bot loop is not ready.')

        except Exception as e:
            logger.exception(f'start_idle_timer error: {e}')

    def play_next(self, guild_id, error=None):
        """Called when a song finishes. Plays the next song in queue if there is one."""
        try:
            state = self.get_state(guild_id)

            if error:
                logger.error(f'Player error: {error}')

                if state.text_channel:
                    self.run_threadsafe(
                        state.text_channel.send('⚠️ Error playing song, checking the queue...')
                    )

            # If !stop triggered this callback, do nothing
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

            # If !skip triggered this callback, do not loop/re-add the skipped song
            if state.skip_requested:
                state.skip_requested = False
                state.current_song = None

            else:
                # LOOP SONG: replay same song
                if state.loop_mode == 'song' and state.current_song:
                    try:
                        ffmpeg_opts = build_ffmpeg_options(state.current_song.get('http_headers'))
                        source = discord.FFmpegPCMAudio(
                            state.current_song['url'],
                            **ffmpeg_opts
                        )
                        source = discord.PCMVolumeTransformer(source, volume=state.volume)

                        state.voice_client.play(
                            source,
                            after=lambda e: self.play_next(guild_id, e)
                        )

                        return

                    except Exception as e:
                        logger.exception(f'Error replaying song: {e}')
                        state.loop_mode = 'off'

                        if state.text_channel:
                            self.run_threadsafe(
                                state.text_channel.send('⚠️ Loop failed, disabling song loop.')
                            )

                # LOOP QUEUE: put finished song back at the end
                if state.loop_mode == 'queue' and state.current_song:
                    state.queue.append(state.current_song)

            # Queue is empty
            if len(state.queue) == 0:
                state.current_song = None
                self.start_idle_timer(guild_id)
                return

            # Play next queued song
            next_song = state.queue.popleft()
            state.current_song = next_song

            try:
                ffmpeg_opts = build_ffmpeg_options(next_song.get('http_headers'))
                source = discord.FFmpegPCMAudio(
                    next_song['url'],
                    **ffmpeg_opts
                )
                source = discord.PCMVolumeTransformer(source, volume=state.volume)

                state.voice_client.play(
                    source,
                    after=lambda e: self.play_next(guild_id, e)
                )

                if state.text_channel:
                    self.run_threadsafe(
                        state.text_channel.send(
                            f'🎵 Now playing: **{next_song["title"]}** '
                            f'[{format_duration(next_song["duration"])}]'
                        )
                    )

            except Exception as e:
                logger.exception(f'Error playing next song: {e}')

                if state.text_channel:
                    self.run_threadsafe(
                        state.text_channel.send(
                            f'⚠️ Error playing **{next_song["title"]}**, skipping...'
                        )
                    )

                state.current_song = None
                self.play_next(guild_id)

        except Exception as e:
            logger.exception(f'play_next fatal error: {e}')


# -------------------------------------------------
# BOT SETUP
# -------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

bot = MusicBot(command_prefix='!', intents=intents, help_command=None)


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
                current_state.current_song = None

                if current_state.voice_client.is_playing() or current_state.voice_client.is_paused():
                    current_state.voice_client.stop()

                await current_state.voice_client.disconnect(force=True)
                current_state.voice_client = None

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception(f'alone_disconnect error: {e}')

        state.alone_timer = asyncio.create_task(alone_disconnect())

    except Exception as e:
        logger.exception(f'on_voice_state_update error: {e}')


# -------------------------------------------------
# COMMANDS
# -------------------------------------------------

@bot.command(name='play')
async def play(ctx, *, query: str = ''):
    """Search YouTube and play a song. Adds to queue if something is already playing."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        query = query.strip()

        if not query:
            return await ctx.send('❌ You need to tell me what to play! Example: `!play lofi beats`')

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

        except Exception as e:
            logger.exception(f'Voice connection error: {e}')
            return await ctx.send(f"❌ Couldn't connect to the voice channel: {e}")

        await ctx.send(f'🔎 Searching for: **{query}**')

        try:
            loop = asyncio.get_running_loop()

            song_info = await loop.run_in_executor(
                None,
                functools.partial(search_youtube, query)
            )

            if song_info is None:
                return await ctx.send('❌ No results found for that search.')

            song_info['requester'] = ctx.author.display_name

        except Exception as e:
            logger.exception(f'yt-dlp error: {e}')
            return await ctx.send("❌ Couldn't find or load that song. Try a different search.")

        # Already playing: queue it
        if state.voice_client.is_playing() or state.voice_client.is_paused():
            state.queue.append(song_info)
            position = len(state.queue)

            return await ctx.send(
                f'📋 Added to queue (#{position}): '
                f'**{song_info["title"]}** [{format_duration(song_info["duration"])}]'
            )

        # Nothing playing: start now
        state.current_song = song_info
        state.stop_requested = False
        state.skip_requested = False

        try:
            ffmpeg_opts = build_ffmpeg_options(song_info.get('http_headers'))
            source = discord.FFmpegPCMAudio(song_info['url'], **ffmpeg_opts)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)

            state.voice_client.play(
                source,
                after=lambda e: bot.play_next(ctx.guild.id, e)
            )

            await ctx.send(
                f'🎵 Now playing: **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
            )

        except Exception as e:
            logger.exception(f'Playback error: {e}')
            state.current_song = None
            await ctx.send('❌ Error playing that song. Try again or try a different one.')

    except Exception as e:
        logger.exception(f'play command error: {e}')
        await ctx.send('❌ Unexpected error in play command.')


@bot.command(name='np')
async def now_playing(ctx):
    """Show details about the song currently playing."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.current_song is None:
            return await ctx.send('Nothing is playing right now.')

        song = state.current_song

        loop_status = ''

        if state.loop_mode == 'song':
            loop_status = '\n🔂 **Loop:** Song'
        elif state.loop_mode == 'queue':
            loop_status = '\n🔁 **Loop:** Queue'

        await ctx.send(
            f'🎵 **Now Playing**\n'
            f'**Title:** {song["title"]}\n'
            f'**Duration:** {format_duration(song["duration"])}\n'
            f'**Requested by:** {song["requester"]}'
            f'{loop_status}'
        )

    except Exception as e:
        logger.exception(f'np command error: {e}')
        await ctx.send('❌ Could not show now playing.')


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

    except Exception as e:
        logger.exception(f'loop command error: {e}')
        await ctx.send('❌ Could not update loop mode.')


@bot.command(name='skip')
async def skip(ctx):
    """Skip to the next song in the queue."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client and (state.voice_client.is_playing() or state.voice_client.is_paused()):
            state.skip_requested = True
            state.current_song = None
            state.voice_client.stop()
            await ctx.send('⏭️ Skipped!')
        else:
            await ctx.send('Nothing is playing.')

    except Exception as e:
        logger.exception(f'skip command error: {e}')
        await ctx.send('❌ Could not skip.')


@bot.command(name='queue')
async def queue_cmd(ctx):
    """Show the current song and upcoming queue."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.current_song is None and len(state.queue) == 0:
            return await ctx.send('The queue is empty.')

        queue_text = ''

        if state.current_song:
            queue_text += (
                f'🎵 **Now playing:** {state.current_song["title"]} '
                f'[{format_duration(state.current_song["duration"])}] '
                f'(requested by {state.current_song["requester"]})\n\n'
            )

        if len(state.queue) > 0:
            queue_text += '**Up next:**\n'

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

    except Exception as e:
        logger.exception(f'queue command error: {e}')
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
            state.current_song = None

            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()

            bot.start_idle_timer(ctx.guild.id)

            await ctx.send('⏹️ Stopped and cleared the queue.')
        else:
            await ctx.send('Nothing is playing.')

    except Exception as e:
        logger.exception(f'stop command error: {e}')
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
            await ctx.send('⏸️ Paused.')
        else:
            await ctx.send('Nothing is playing.')

    except Exception as e:
        logger.exception(f'pause command error: {e}')
        await ctx.send('❌ Could not pause.')


@bot.command(name='resume')
async def resume(ctx):
    """Resume a paused song."""
    try:
        if not ctx.guild:
            return await ctx.send('❌ This command can only be used inside a server.')

        state = bot.get_state(ctx.guild.id)

        if state.voice_client and state.voice_client.is_paused():
            state.voice_client.resume()
            await ctx.send('▶️ Resumed.')
        else:
            await ctx.send('Nothing is paused.')

    except Exception as e:
        logger.exception(f'resume command error: {e}')
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
            state.current_song = None

            bot.cancel_idle_timer(ctx.guild.id)
            bot.cancel_alone_timer(ctx.guild.id)

            if state.voice_client.is_playing() or state.voice_client.is_paused():
                state.voice_client.stop()

            await state.voice_client.disconnect(force=True)
            state.voice_client = None

            await ctx.send('👋 Left the voice channel.')
        else:
            await ctx.send('I am not connected to a voice channel.')

    except Exception as e:
        logger.exception(f'leave command error: {e}')
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

    except Exception as e:
        logger.exception(f'shuffle command error: {e}')
        await ctx.send('❌ Could not shuffle queue.')


@bot.command(name='volume')
async def volume_cmd(ctx, *, arg: str = ''):
    """Set the playback volume 0-100. No argument shows current volume."""
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

        state.volume = vol / 100

        if state.voice_client and state.voice_client.source:
            state.voice_client.source.volume = state.volume

        await ctx.send(f'🔊 Volume set to **{vol}%**')

    except Exception as e:
        logger.exception(f'volume command error: {e}')
        await ctx.send('❌ Could not set volume.')


@bot.command(name='help')
async def help_cmd(ctx):
    """Show the list of available commands."""
    help_text = (
        '🎵 **Music Bot Commands**\n\n'
        '`!play <search>` — Search and play a song\n'
        '`!pause` — Pause the current song\n'
        '`!resume` — Resume playback\n'
        '`!stop` — Stop and clear the queue\n'
        '`!skip` — Skip to next song\n'
        '`!queue` — Show the queue\n'
        '`!np` — Now playing info\n'
        '`!loop song/queue/off` — Set loop mode\n'
        '`!shuffle` — Shuffle the queue\n'
        '`!volume <0-100>` — Set volume\n'
        '`!leave` — Disconnect the bot\n'
        '`!help` — Show this list'
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