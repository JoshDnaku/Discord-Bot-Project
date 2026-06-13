# Music Bot Commands

## Playback

| Command | Description |
|---------|-------------|
| `!play <search>` | Searches YouTube and plays the top result. If a song is already playing, adds to queue. |
| `!pause` | Pauses the current song. |
| `!resume` | Resumes a paused song. |
| `!stop` | Stops playback and clears the entire queue. |
| `!skip` | Skips the current song and plays the next one in the queue. |

## Queue

| Command | Description |
|---------|-------------|
| `!queue` | Shows the current song and upcoming songs in the queue. |
| `!shuffle` | Randomizes the order of songs in the queue. |

## Info

| Command | Description |
|---------|-------------|
| `!np` | Shows details about the current song (title, duration, who requested it, loop status). |
| `!volume <0-100>` | Sets the playback volume. No argument shows current volume. |

## Loop

| Command | Description |
|---------|-------------|
| `!loop song` | Repeats the current song until turned off. |
| `!loop queue` | Repeats the entire queue — when it ends, it starts over. |
| `!loop off` | Turns off looping (default). |
| `!loop` | Shows the current loop mode. |

## Voice

| Command | Description |
|---------|-------------|
| `!leave` | Disconnects the bot from the voice channel and clears the queue. |

## Utility

| Command | Description |
|---------|-------------|
| `!help` | Lists all available commands in chat. |

## Notes

- You must be in a voice channel to use `!play`.
- The bot will join your voice channel automatically when you play a song.
- If the bot is in a different voice channel, it will move to yours.
- The bot will auto-leave after 30 seconds if everyone leaves the voice channel.
- The bot will auto-leave after 3 minutes of no music playing.
- If a song fails to play, the bot will skip it and move to the next one in the queue.
- Friendly error messages are shown if something goes wrong (bad search, connection issues, etc.).
