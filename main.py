import discord
import os
import random
import asyncio
import functools
from dotenv import load_dotenv
import yt_dlp
from collections import deque

# Load the bot token from .env file
load_dotenv()

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


class MusicBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.voice_client = None       # Tracks the current voice connection
        self.queue = deque()           # The song queue — songs waiting to play
        self.current_song = None       # Info about what's currently playing
        self.loop_mode = 'off'         # Loop mode: 'off', 'song', or 'queue'
        self.volume = 0.5             # Default volume (50%)
        self.idle_timer = None         # Timer for auto-leave when idle
        self.alone_timer = None        # Timer for auto-leave when alone in channel
        self.text_channel = None       # Store last text channel for auto-leave messages

    async def on_ready(self):
        """Runs when the bot successfully connects to Discord."""
        print(f'Logged on as {self.user}!')

    def start_idle_timer(self):
        """Starts a 3-minute timer. If no song plays before it expires, bot leaves."""
        if self.idle_timer:
            self.idle_timer.cancel()

        async def idle_disconnect():
            await asyncio.sleep(180)  # Wait 3 minutes
            if self.voice_client and self.voice_client.is_connected():
                if not self.voice_client.is_playing() and not self.voice_client.is_paused():
                    if self.text_channel:
                        await self.text_channel.send('👋 Left the voice channel (idle for 3 minutes).')
                    await self.voice_client.disconnect()
                    self.voice_client = None
                    self.queue.clear()
                    self.current_song = None

        self.idle_timer = asyncio.ensure_future(idle_disconnect())

    def cancel_idle_timer(self):
        """Cancels the idle timer (called when a new song starts playing)."""
        if self.idle_timer:
            self.idle_timer.cancel()
            self.idle_timer = None

    async def on_voice_state_update(self, member, before, after):
        """Fires when someone joins, leaves, or moves voice channels."""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        if member == self.user:
            return

        bot_channel = self.voice_client.channel

        if len(bot_channel.members) == 1:
            if self.alone_timer:
                self.alone_timer.cancel()

            async def alone_disconnect():
                await asyncio.sleep(30)
                if self.voice_client and self.voice_client.is_connected():
                    if len(self.voice_client.channel.members) == 1:
                        if self.text_channel:
                            await self.text_channel.send('👋 Left the voice channel (nobody else here).')
                        self.voice_client.stop()
                        await self.voice_client.disconnect()
                        self.voice_client = None
                        self.queue.clear()
                        self.current_song = None

            self.alone_timer = asyncio.ensure_future(alone_disconnect())
        else:
            if self.alone_timer:
                self.alone_timer.cancel()
                self.alone_timer = None

    def play_next(self, error=None):
        """Called when a song finishes. Plays the next song in queue if there is one."""
        if error:
            print(f'Player error: {error}')
            if self.text_channel:
                asyncio.ensure_future(
                    self.text_channel.send('⚠️ Error playing song, skipping to next...')
                )

        # LOOP SONG: replay the same song again
        if self.loop_mode == 'song' and self.current_song:
            try:
                source = discord.FFmpegPCMAudio(self.current_song['url'], **FFMPEG_OPTIONS)
                source = discord.PCMVolumeTransformer(source, volume=self.volume)
                self.voice_client.play(source, after=self.play_next)
            except Exception as e:
                print(f'Error replaying song: {e}')
                self.loop_mode = 'off'
                self.play_next()
            return

        # LOOP QUEUE: put the song that just finished back at the end of the queue
        if self.loop_mode == 'queue' and self.current_song:
            self.queue.append(self.current_song)

        # If there's nothing left in the queue, we're done
        if len(self.queue) == 0:
            self.current_song = None
            self.start_idle_timer()
            return

        # Grab the next song from the front of the queue
        next_song = self.queue.popleft()
        self.current_song = next_song

        # Play it, and when THIS song finishes, call play_next again
        try:
            source = discord.FFmpegPCMAudio(next_song['url'], **FFMPEG_OPTIONS)
            source = discord.PCMVolumeTransformer(source, volume=self.volume)
            self.voice_client.play(source, after=self.play_next)
        except Exception as e:
            print(f'Error playing next song: {e}')
            if self.text_channel:
                asyncio.ensure_future(
                    self.text_channel.send(f'⚠️ Error playing **{next_song["title"]}**, skipping...')
                )
            self.play_next()

    async def on_message(self, message):
        """Runs every time someone sends a message the bot can see."""

        if message.author == self.user:
            return

        # --- !play command ---
        if message.content.startswith('!play'):
            query = message.content[5:].strip()

            if not query:
                await message.channel.send("❌ You need to tell me what to play! Example: `!play lofi beats`")
                return

            self.text_channel = message.channel

            if message.author.voice is None:
                await message.channel.send("❌ You need to be in a voice channel!")
                return

            voice_channel = message.author.voice.channel

            try:
                if self.voice_client is None or not self.voice_client.is_connected():
                    self.voice_client = await voice_channel.connect()
                elif self.voice_client.channel != voice_channel:
                    await self.voice_client.move_to(voice_channel)
            except Exception as e:
                await message.channel.send(f"❌ Couldn't connect to the voice channel: {e}")
                return

            await message.channel.send(f'🔎 Searching for: **{query}**')

            # Run yt-dlp in a background thread so the bot doesn't freeze
            try:
                loop = asyncio.get_event_loop()
                song_info = await loop.run_in_executor(
                    None, functools.partial(search_youtube, query)
                )

                if song_info is None:
                    await message.channel.send("❌ No results found for that search.")
                    return

                song_info['requester'] = message.author.display_name

            except Exception as e:
                await message.channel.send("❌ Couldn't find or load that song. Try a different search.")
                print(f'yt-dlp error: {e}')
                return

            # If something is already playing, add to queue
            if self.voice_client.is_playing() or self.voice_client.is_paused():
                self.queue.append(song_info)
                position = len(self.queue)
                await message.channel.send(
                    f'📋 Added to queue (#{position}): **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
                )
            else:
                # Nothing is playing, start immediately
                self.current_song = song_info
                self.cancel_idle_timer()
                try:
                    source = discord.FFmpegPCMAudio(song_info['url'], **FFMPEG_OPTIONS)
                    source = discord.PCMVolumeTransformer(source, volume=self.volume)
                    self.voice_client.play(source, after=self.play_next)
                    await message.channel.send(
                        f'🎵 Now playing: **{song_info["title"]}** [{format_duration(song_info["duration"])}]'
                    )
                except Exception as e:
                    await message.channel.send("❌ Error playing that song. Try again or try a different one.")
                    print(f'Playback error: {e}')
                    self.current_song = None

        # --- !np (now playing) command ---
        elif message.content == '!np':
            if self.current_song is None:
                await message.channel.send('Nothing is playing right now.')
            else:
                song = self.current_song
                loop_status = ''
                if self.loop_mode == 'song':
                    loop_status = '\n🔂 **Loop:** Song'
                elif self.loop_mode == 'queue':
                    loop_status = '\n🔁 **Loop:** Queue'

                np_text = (
                    f'🎵 **Now Playing**\n'
                    f'**Title:** {song["title"]}\n'
                    f'**Duration:** {format_duration(song["duration"])}\n'
                    f'**Requested by:** {song["requester"]}'
                    f'{loop_status}'
                )
                await message.channel.send(np_text)

        # --- !loop command ---
        elif message.content.startswith('!loop'):
            arg = message.content[5:].strip().lower()

            if arg == 'song':
                self.loop_mode = 'song'
                await message.channel.send('🔂 Looping the **current song**.')
            elif arg == 'queue':
                self.loop_mode = 'queue'
                await message.channel.send('🔁 Looping the **entire queue**.')
            elif arg == 'off':
                self.loop_mode = 'off'
                await message.channel.send('➡️ Loop is now **off**.')
            else:
                await message.channel.send(
                    f'Current loop mode: **{self.loop_mode}**\n'
                    f'Usage: `!loop song` / `!loop queue` / `!loop off`'
                )

        # --- !skip command ---
        elif message.content == '!skip':
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.stop()
                await message.channel.send('⏭️ Skipped!')
            else:
                await message.channel.send('Nothing is playing.')

        # --- !queue command ---
        elif message.content == '!queue':
            if self.current_song is None and len(self.queue) == 0:
                await message.channel.send('The queue is empty.')
                return

            queue_text = ''

            if self.current_song:
                queue_text += (
                    f'🎵 **Now playing:** {self.current_song["title"]} '
                    f'[{format_duration(self.current_song["duration"])}] '
                    f'(requested by {self.current_song["requester"]})\n\n'
                )

            if len(self.queue) > 0:
                queue_text += '**Up next:**\n'
                for i, song in enumerate(list(self.queue)[:15], 1):
                    queue_text += f'{i}. {song["title"]} [{format_duration(song["duration"])}] (requested by {song["requester"]})\n'

                if len(self.queue) > 15:
                    queue_text += f'\n*...and {len(self.queue) - 15} more songs.*'
            else:
                queue_text += '*No more songs in queue.*'

            await message.channel.send(queue_text)

        # --- !stop command ---
        elif message.content == '!stop':
            if self.voice_client:
                self.queue.clear()
                self.current_song = None
                self.voice_client.stop()
                await message.channel.send('⏹️ Stopped and cleared the queue.')

        # --- !pause command ---
        elif message.content == '!pause':
            if self.voice_client and self.voice_client.is_playing():
                self.voice_client.pause()
                await message.channel.send('⏸️ Paused.')

        # --- !resume command ---
        elif message.content == '!resume':
            if self.voice_client and self.voice_client.is_paused():
                self.voice_client.resume()
                await message.channel.send('▶️ Resumed.')

        # --- !leave command ---
        elif message.content == '!leave':
            if self.voice_client and self.voice_client.is_connected():
                self.queue.clear()
                self.current_song = None
                await self.voice_client.disconnect()
                self.voice_client = None
                await message.channel.send('👋 Left the voice channel.')

        # --- !help command ---
        elif message.content == '!help':
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
            await message.channel.send(help_text)

        # --- !shuffle command ---
        elif message.content == '!shuffle':
            if len(self.queue) < 2:
                await message.channel.send('Not enough songs in the queue to shuffle.')
                return

            queue_list = list(self.queue)
            random.shuffle(queue_list)
            self.queue = deque(queue_list)

            await message.channel.send(f'🔀 Shuffled {len(self.queue)} songs in the queue.')

        # --- !volume command ---
        elif message.content.startswith('!volume'):
            arg = message.content[7:].strip()

            if not arg:
                await message.channel.send(f'🔊 Current volume: **{int(self.volume * 100)}%**')
                return

            try:
                vol = int(arg)
            except ValueError:
                await message.channel.send('Use a number between 0 and 100. Example: `!volume 50`')
                return

            if vol < 0 or vol > 100:
                await message.channel.send('Volume must be between 0 and 100.')
                return

            self.volume = vol / 100

            if self.voice_client and self.voice_client.source:
                self.voice_client.source.volume = self.volume

            await message.channel.send(f'🔊 Volume set to **{vol}%**')


# Set up intents (permissions for what events the bot receives)
intents = discord.Intents.default()
intents.message_content = True   # Required to read message text
intents.voice_states = True      # Required for on_voice_state_update (auto-leave)

# Create and run the bot
client = MusicBot(intents=intents)
client.run(os.getenv('DISCORD_TOKEN'))
