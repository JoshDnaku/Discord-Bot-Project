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

# Configure logging — outputs timestamp, log level, and message
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger(__name__)

# yt-dlp options: we only want audio, not video
YDL_OPTIONS = {
    'format': 'bestaudio/best',       # Get the best audio quality available
    'noplaylist': True,                # Don't download entire playlists, just one video
    'quiet': True,                     # Don't spam terminal with logs
    'no_warnings': True,               # Suppress warning messages
    'default_search': 'ytsearch',      # If input isn't a URL, search YouTube automatically
}

# FFmpeg options: reconnect settings help if the stream drops briefly
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5',
    'options': '-vn'  # -vn means "no video", only process audio
}


def format_duration(seconds):
    """Converts seconds into a nice mm:ss or hh:mm:ss format."""
    if not seconds:
        return 'Unknown'
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours > 0:
        return f'{hours}:{minutes:02d}:{secs:02d}'
    return f'{minutes}:{secs:02d}'


def search_youtube(query):
    """Searches YouTube and returns song info. Runs in a thread so it doesn't block the bot."""
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        info = ydl.extract_info(query, download=False)

        if 'entries' in info:
            if len(info['entries']) == 0:
                return None
            info = info['entries'][0]

        return {
            'url': info['url'],
            'title': info.get('title', query),
            'duration': info.get('duration', 0),
            'requester': None
        }


class MusicState:
    """Holds all music-related state for a single guild (server).
    Each guild gets its own MusicState instance so multiple servers
    can use the bot simultaneously without stepping on each other."""

    def __init__(self):
        self.voice_client = None       # Tracks the current voice connection
        self.queue = deque()           # The song queue — songs waiting to play
        self.current_song = None       # Info about what's currently playing
        self.loop_mode = 'off'         # Loop mode: 'off', 'song', or 'queue'
        self.volume = 0.5              # Default volume (50%)
        self.idle_timer = None         # Timer for auto-leave when idle
        self.alone_timer = None        # Timer for auto-leave when alone in channel
        self.text_channel = None       # Store last text channel for auto-leave messages


class MusicBot(commands.Bot):
    """Subclass of commands.Bot that holds all the music state on the bot itself."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Per-guild music state. Key = guild_id, value = MusicState instance.
        self.music_states = {}
        # Reference to the main asyncio event loop. Captured in on_ready so that
        # callbacks running in Discord's audio thread can schedule coroutines
        # back onto the main loop in a thread-safe way.
        self.loop_ref = None

    def get_state(self, guild_id):
        """Return the MusicState for a guild, creating one if it doesn't exist yet."""
        if guild_id not in self.music_states:
            self.music_states[guild_id] = MusicState()
        return self.music_states[guild_id]

    def start_idle_timer(self, guild_id):
        """Starts a 3-minute timer. If no song plays before it expires, bot leaves."""
        state = self.get_state(guild_id)

        # Cancel any existing idle timer
        if state.idle_timer:
            state.idle_timer.cancel()

        async def idle_disconnect():
            await asyncio.sleep(180)  # Wait 3 minutes
            if state.voice_client and state.voice_client.is_connected():
                if not state.voice_client.is_playing() and not state.voice_client.is_paused():
                    if state.text_channel:
                        await state.text_channel.send('👋 Left the voice channel (idle for 3 minutes).')
                    await state.voice_client.disconnect()
                    state.voice_client = None
                    state.queue.clear()
                    state.current_song = None

        # Schedule on the main event loop in a thread-safe way. start_idle_timer can be
        # invoked from play_next, which runs in Discord's audio thread — so we cannot
        # use asyncio.ensure_future here (it requires a running loop in the current thread).
        state.idle_timer = asyncio.run_coroutine_threadsafe(idle_disconnect(), self.loop_ref)

    def cancel_idle_timer(self, guild_id):
        """Cancels the idle timer (called when a new song starts playing)."""
        state = self.get_state(guild_id)
        if state.idle_timer:
            state.idle_timer.cancel()
            state.idle_timer = None

    def play_next(self, guild_id, error=None):
        """Called when a song finishes. Plays the next song in queue if there is one."""
        state = self.get_state(guild_id)

        if error:
            logger.error(f'Player error: {error}')
            if state.text_channel:
                # play_next runs in Discord's audio thread, so we must schedule
                # the message send onto the main event loop in a thread-safe way.
                asyncio.run_coroutine_threadsafe(
                    state.text_channel.send('⚠️ Error playing song, skipping to next...'),
                    self.loop_ref
                )

        # LOOP SONG: replay the same song again
        if state.loop_mode == 'song' and state.current_song:
            try:
                source = discord.FFmpegPCMAudio(state.current_song['url'], **FFMPEG_OPTIONS)
                source = discord.PCMVolumeTransformer(source, volume=state.volume)
                state.voice_client.play(source, after=lambda e: self.play_next(guild_id, e))
            except Exception as e:
                logger.error(f'Error replaying song: {e}')
                state.loop_mode = 'off'
                self.play_next(guild_id)
            return

        # LOOP QUEUE: put the song that just finished back at the end of the queue
        if state.loop_mode == 'queue' and state.current_song:
            state.queue.append(state.current_song)

        # If there's nothing left in the queue, we're done
        if len(state.queue) == 0:
            state.current_song = None
            self.start_idle_timer(guild_id)
            return

        # Grab the next song from the front of the queue
        next_song = state.queue.popleft()
        state.current_song = next_song

        # Play it, and when THIS song finishes, call play_next again
        try:
            source = discord.FFmpegPCMAudio(next_song['url'], **FFMPEG_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)
            state.voice_client.play(source, after=lambda e: self.play_next(guild_id, e))
        except Exception as e:
            logger.error(f'Error playing next song: {e}')
            if state.text_channel:
                # play_next runs in Discord's audio thread, so we must schedule
                # the message send onto the main event loop in a thread-safe way.
                asyncio.run_coroutine_threadsafe(
                    state.text_channel.send(f'⚠️ Error playing **{next_song["title"]}**, skipping...'),
                    self.loop_ref
                )
            self.play_next(guild_id)


# Set up intents (permissions for what events the bot receives)
intents = discord.Intents.default()
intents.message_content = True   # Required to read message text
intents.voice_states = True      # Required for on_voice_state_update (auto-leave)

# Create the bot — disable the default !help so our custom one works
bot = MusicBot(command_prefix='!', intents=intents, help_command=None)


@bot.event
async def on_ready():
    """Runs when the bot successfully connects to Discord."""
    # Capture the running event loop so audio-thread callbacks can schedule
    # coroutines back onto it via asyncio.run_coroutine_threadsafe.
    bot.loop_ref = asyncio.get_running_loop()
    logger.info(f'Logged on as {bot.user}!')


@bot.event
async def on_voice_state_update(member, before, after):
    """Fires when someone joins, leaves, or moves voice channels."""
    if not bot.voice_client or not bot.voice_client.is_connected():
        return

    if member == bot.user:
        return

    bot_channel = bot.voice_client.channel

    if len(bot_channel.members) == 1:
        if bot.alone_timer:
            bot.alone_timer.cancel()

        async def alone_disconnect():
            await asyncio.sleep(30)
            if bot.voice_client and bot.voice_client.is_connected():
                if len(bot.voice_client.channel.members) == 1:
                    if bot.text_channel:
                        await bot.text_channel.send('👋 Left the voice channel (nobody else here).')
                    bot.voice_client.stop()
                    await bot.voice_client.disconnect()
                    bot.voice_client = None
                    bot.queue.clear()
                    bot.current_song = None

        bot.alone_timer = asyncio.ensure_future(alone_disconnect())
    else:
        if bot.alone_timer:
            bot.alone_timer.cancel()
            bot.alone_timer = None


# --- !play command ---
@bot.command(name='play')
async def play(ctx, *, query: str = ''):
    """Search YouTube and play a song. Adds to queue if something is already playing."""
    state = bot.get_state(ctx.guild.id)
    query = query.strip()

    if not query:
        await ctx.send("❌ You need to tell me what to play! Example: `!play lofi beats`")
        return

    state.text_channel = ctx.channel

    if ctx.author.voice is None:
        await ctx.send("❌ You need to be in a voice channel!")
        return

    voice_channel = ctx.author.voice.channel

    try:
        if state.voice_client is None or not state.voice_client.is_connected():
            state.voice_client = await voice_channel.connect()
        elif state.voice_client.channel != voice_channel:
            await state.voice_client.move_to(voice_channel)
    except Exception as e:
        await ctx.send(f"❌ Couldn't connect to the voice channel: {e}")
        return

    await ctx.send(f'🔎 Searching for: **{query}**')

    # Run yt-dlp in a background thread so the bot doesn't freeze
    try:
        loop = asyncio.get_event_loop()
        song_info = await loop.run_in_executor(
            None, functools.partial(search_youtube, query)
        )

        if song_info is None:
            await ctx.send("❌ No results found for that search.")
            return

        song_info['requester'] = ctx.author.display_name

    except Exception as e:
        await ctx.send("❌ Couldn't find or load that song. Try a different search.")
        logger.error(f'yt-dlp error: {e}')
        return

    # If something is already playing, add to queue
    if state.voice_client.is_playing() or state.voice_client.is_paused():
        state.queue.append(song_info)
        position = len(state.queue)
        await ctx.send(
            f'📋 Added to queue (#{position}): **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
        )
    else:
        # Nothing is playing, start immediately
        state.current_song = song_info
        bot.cancel_idle_timer(ctx.guild.id)
        try:
            source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=state.volume)
            state.voice_client.play(source, after=lambda e: bot.play_next(ctx.guild.id, e))
            await ctx.send(
                f'🎵 Now playing: **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
            )
        except Exception as e:
            await ctx.send("❌ Error playing that song. Try again or try a different one.")
            logger.error(f'Playback error: {e}')
            state.current_song = None


# --- !np (now playing) command ---
@bot.command(name='np')
async def now_playing(ctx):
    """Show details about the song currently playing."""
    state = bot.get_state(ctx.guild.id)
    if state.current_song is None:
        await ctx.send('Nothing is playing right now.')
    else:
        song = state.current_song
        loop_status = ''
        if state.loop_mode == 'song':
            loop_status = '\n🔂 **Loop:** Song'
        elif state.loop_mode == 'queue':
            loop_status = '\n🔁 **Loop:** Queue'

        np_text = (
            f'🎵 **Now Playing**\n'
            f'**Title:** {song["title"]}\n'
            f'**Duration:** {format_duration(song["duration"])}\n'
            f'**Requested by:** {song["requester"]}'
            f'{loop_status}'
        )
        await ctx.send(np_text)


# --- !loop command ---
@bot.command(name='loop')
async def loop_cmd(ctx, *, arg: str = ''):
    """Set loop mode: song, queue, or off. No argument shows current status."""
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


# --- !skip command ---
@bot.command(name='skip')
async def skip(ctx):
    """Skip to the next song in the queue."""
    state = bot.get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.stop()  # Stopping triggers play_next via the "after" callback
        # Brief pause so discord.py's voice client has time to fully release the
        # current audio source before the after-callback's play_next attempts to
        # start the next one. Without this, voice_client.play() inside play_next
        # races the cleanup from stop() and fails silently — the message goes
        # through but the queued song never starts.
        await asyncio.sleep(0.5)
        await ctx.send('⏭️ Skipped!')
    else:
        await ctx.send('Nothing is playing.')


# --- !queue command ---
@bot.command(name='queue')
async def queue_cmd(ctx):
    """Show the current song and upcoming queue."""
    state = bot.get_state(ctx.guild.id)
    if state.current_song is None and len(state.queue) == 0:
        await ctx.send('The queue is empty.')
        return

    queue_text = ''

    if state.current_song:
        queue_text += (
            f'🎵 **Now playing:** {state.current_song["title"]} '
            f'[{format_duration(state.current_song["duration"])}] '
            f'(requested by {state.current_song["requester"]})\n\n'
        )

    if len(state.queue) > 0:
        queue_text += '**Up next:**\n'
        # Only show first 15 songs to avoid hitting Discord's message limit
        for i, song in enumerate(list(state.queue)[:15], 1):
            queue_text += f'{i}. {song["title"]} [{format_duration(song["duration"])}] (requested by {song["requester"]})\n'

        if len(state.queue) > 15:
            queue_text += f'\n*...and {len(state.queue) - 15} more songs.*'
    else:
        queue_text += '*No more songs in queue.*'

    await ctx.send(queue_text)


# --- !stop command ---
@bot.command(name='stop')
async def stop(ctx):
    """Stop playback and clear the queue."""
    state = bot.get_state(ctx.guild.id)
    if state.voice_client:
        state.queue.clear()
        state.current_song = None
        state.voice_client.stop()
        await ctx.send('⏹️ Stopped and cleared the queue.')


# --- !pause command ---
@bot.command(name='pause')
async def pause(ctx):
    """Pause the current song."""
    state = bot.get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_playing():
        state.voice_client.pause()
        await ctx.send('⏸️ Paused.')


# --- !resume command ---
@bot.command(name='resume')
async def resume(ctx):
    """Resume a paused song."""
    state = bot.get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_paused():
        state.voice_client.resume()
        await ctx.send('▶️ Resumed.')


# --- !leave command ---
@bot.command(name='leave')
async def leave(ctx):
    """Disconnect the bot from voice and clear the queue."""
    state = bot.get_state(ctx.guild.id)
    if state.voice_client and state.voice_client.is_connected():
        state.queue.clear()
        state.current_song = None
        await state.voice_client.disconnect()
        state.voice_client = None
        await ctx.send('👋 Left the voice channel.')


# --- !help command ---
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


# --- !shuffle command ---
@bot.command(name='shuffle')
async def shuffle(ctx):
    """Randomize the order of songs in the queue."""
    state = bot.get_state(ctx.guild.id)
    if len(state.queue) < 2:
        await ctx.send('Not enough songs in the queue to shuffle.')
        return

    # Convert deque to list, shuffle it, convert back
    queue_list = list(state.queue)
    random.shuffle(queue_list)
    state.queue = deque(queue_list)

    await ctx.send(f'🔀 Shuffled {len(state.queue)} songs in the queue.')


# --- !volume command ---
@bot.command(name='volume')
async def volume_cmd(ctx, *, arg: str = ''):
    """Set the playback volume (0-100). No argument shows current volume."""
    state = bot.get_state(ctx.guild.id)
    arg = arg.strip()

    # No argument — show current volume
    if not arg:
        await ctx.send(f'🔊 Current volume: **{int(state.volume * 100)}%**')
        return

    # Try to parse the number
    try:
        vol = int(arg)
    except ValueError:
        await ctx.send('Use a number between 0 and 100. Example: `!volume 50`')
        return

    # Clamp between 0 and 100
    if vol < 0 or vol > 100:
        await ctx.send('Volume must be between 0 and 100.')
        return

    # Set the volume (convert 0-100 to 0.0-1.0)
    state.volume = vol / 100

    # Apply to currently playing audio immediately
    if state.voice_client and state.voice_client.source:
        state.voice_client.source.volume = state.volume

    await ctx.send(f'🔊 Volume set to **{vol}%**')


# Run the bot
bot.run(os.getenv('DISCORD_TOKEN'))
