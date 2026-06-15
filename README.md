# Discord Music Bot

A simple Discord music bot built with Python, discord.py, yt-dlp, and FFmpeg.
The bot can join a voice channel, search YouTube, play audio, queue songs, skip, pause, resume, loop, shuffle, adjust volume, and auto-leave when idle or alone.

## Features

* Play music from YouTube search queries
* Queue multiple songs
* Skip, stop, pause, and resume playback
* Show current song with `!np`
* Show queue with `!queue`
* Loop current song or entire queue
* Shuffle queue
* Set playback volume
* Auto-leave after being idle
* Auto-leave when alone in a voice channel
* Per-server (multi-guild) music state support

## Requirements

Use Python 3.12 or newer.

Recommended `requirements.txt`:

```txt
discord.py[voice]>=2.7.1
python-dotenv>=1.0.0
yt-dlp>=2025.1.1
PyNaCl>=1.5.0
```

You also need FFmpeg installed and available in PATH.

Check FFmpeg with:

```bat
ffmpeg -version
```

If this command does not work, install FFmpeg and restart your terminal or VS Code.

## Setup

### 1. Clone or download the project

```bat
git clone https://github.com/JoshDnaku/Discord-Bot-Project.git
cd Discord-Bot-Project
```

Or open the existing project folder directly.

### 2. Create a virtual environment

```bat
py -3.12 -m venv venv
venv\Scripts\activate
```

Check Python version:

```bat
python --version
```

Expected:

```txt
Python 3.12.x
```

### 3. Install dependencies

```bat
python -m pip install --upgrade pip
python -m pip install -U "discord.py[voice]" yt-dlp python-dotenv PyNaCl
```

Or, if a `requirements.txt` exists:

```bat
python -m pip install -r requirements.txt
```

### 4. Create a `.env` file

Create a file named `.env` in the project root:

```env
DISCORD_TOKEN=your_bot_token_here
```

Never upload or commit your `.env` file.

### 5. Enable Discord bot intents

In the Discord Developer Portal:

1. Open your application
2. Go to **Bot**
3. Enable **Message Content Intent**
4. Enable any required voice/server permissions when inviting the bot

The bot needs permissions such as:

* View Channels
* Send Messages
* Connect
* Speak
* Use Voice Activity

### 6. Run the bot

```bat
python main.py
```

If everything is working, you should see the bot log in successfully.

## Commands

| Command           | Description                       |
| ----------------- | --------------------------------- |
| `!play <search>`  | Search YouTube and play a song    |
| `!pause`          | Pause the current song            |
| `!resume`         | Resume playback                   |
| `!stop`           | Stop playback and clear the queue |
| `!skip`           | Skip to the next song             |
| `!queue`          | Show the current queue            |
| `!np`             | Show the currently playing song   |
| `!loop song`      | Loop the current song             |
| `!loop queue`     | Loop the entire queue             |
| `!loop off`       | Disable looping                   |
| `!shuffle`        | Shuffle the queue                 |
| `!volume <0-100>` | Set playback volume               |
| `!leave`          | Disconnect the bot from voice     |
| `!help`           | Show the command list             |

## Example Usage

```txt
!play never gonna give you up
!play siinamota
!queue
!skip
!volume 60
!loop queue
!stop
!leave
```

## Project Structure

```txt
Discord-Bot-Project/
├── main.py
├── COMMANDS.md
├── .env
├── .gitignore
└── README.md
```

## Important Notes

### discord.py voice extras are required

The `[voice]` extra installs PyNaCl and other voice dependencies. Without it, the bot will fail to join voice channels.

```bat
python -m pip install -U "discord.py[voice]"
```

### FFmpeg must be visible to the bot

If the bot says:

```txt
ffmpeg was not found
```

then FFmpeg is not visible from the terminal running the bot.

Check:

```bat
where ffmpeg
ffmpeg -version
```

If you installed FFmpeg while VS Code was already open, restart VS Code completely.

### Python 3.9 is not recommended

Older Python versions show deprecation warnings from discord.py.
Use Python 3.12 or newer for this project.

## Troubleshooting

### Bot logs in but cannot join voice

Make sure voice extras are installed:

```bat
python -m pip install -U "discord.py[voice]"
```

Then verify:

```bat
python -c "import discord; print(discord.__version__); print(discord.__file__)"
```

Expected version should be `2.7.0` or newer.

### Bot joins voice but no music plays (FFmpeg returns code 1)

Usually a YouTube extraction issue. Update yt-dlp:

```bat
python -m pip install -U yt-dlp
```

Then restart the bot and try another song.

If you still get `403 Forbidden` errors, the bot uses the Android player client to avoid YouTube's session-locking — make sure `main.py` keeps this option in `YDL_OPTIONS`:

```python
'extractor_args': {
    'youtube': {
        'player_client': ['android'],
    }
}
```

### Voice connection times out

If you see `asyncio.exceptions.TimeoutError` during voice connection, the default 30-second timeout may not be enough. The bot already passes `timeout=60.0` to `voice_channel.connect(...)`, but if you still hit timeouts, check your firewall and antivirus.

### `.env` token not found

Make sure `.env` is in the same folder as `main.py`:

```env
DISCORD_TOKEN=your_bot_token_here
```

## Security

Never share your Discord bot token publicly.

The `.gitignore` already excludes:

```gitignore
.env
venv/
venv_new/
__pycache__/
*.pyc
*.log
```

If your token is leaked, reset it immediately from the Discord Developer Portal.

## License

This project is for personal and educational use. Add your preferred license if you plan to publish it publicly.
