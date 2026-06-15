# Discord Music Bot

A simple Discord music bot built with Python, Pycord, yt-dlp, and FFmpeg.
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
* Per-server music state support

## Requirements

Use Python 3.12 or newer.

Recommended `requirements.txt`:

```txt
py-cord[voice]==2.8.0
python-dotenv==1.2.1
yt-dlp==2025.10.14
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
git clone <your-repo-url>
cd Discord-Bot-Project
```

Or open the existing project folder directly.

### 2. Create a virtual environment

```bat
python -m venv venv312
venv312\Scripts\activate
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
python -m pip install --upgrade pip setuptools wheel
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
├── requirements.txt
├── .env
└── README.md
```

## Important Notes

### Use Pycord, not discord.py

This project uses Pycord:

```txt
py-cord[voice]==2.8.0
```

Do not install `discord.py` and `py-cord` together in the same environment, because both provide the `discord` Python module and can conflict.

If needed, clean the environment with:

```bat
python -m pip uninstall -y discord.py discord py-cord
python -m pip install -r requirements.txt
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

Older Python versions may show warnings from newer packages such as yt-dlp.
Use Python 3.12 or newer for this project.

## Troubleshooting

### Bot logs in but cannot join voice

Make sure you are using:

```txt
py-cord[voice]==2.8.0
```

Then verify:

```bat
python -c "import discord; print(discord.__version__); print(discord.__file__)"
```

Expected version should be `2.8.0` or newer.

### Bot joins voice but no music plays

Check FFmpeg:

```bat
ffmpeg -version
```

Also check that your bot has permission to speak in the voice channel.

### Song starts and instantly ends

This can happen if YouTube gives an unstable stream URL. Try:

```bat
python -m pip install -U yt-dlp
```

Then restart the bot and try another song.

### `.env` token not found

Make sure `.env` is in the same folder as `main.py`:

```env
DISCORD_TOKEN=your_bot_token_here
```

## Security

Never share your Discord bot token publicly.

Add this to `.gitignore`:

```gitignore
.env
venv/
venv312/
__pycache__/
*.pyc
```

If your token is leaked, reset it immediately from the Discord Developer Portal.

## License

This project is for personal and educational use. Add your preferred license if you plan to publish it publicly.
