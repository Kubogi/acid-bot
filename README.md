# Acid Bot

Acid Bot is a small Discord bot that stays in one voice channel until an administrator tells it to leave. It remembers the selected channel across restarts, reconnects after network failures or kicks, and does not play or record audio.

## What it does

- `/voice join` — join your current voice channel and save it as the target.
- `/voice move <channel>` — move to another voice channel and save it.
- `/voice leave` — disconnect and turn off automatic recovery.
- `/voice rejoin` — reconnect to the last saved channel and turn recovery back on.
- `/voice status` — show the target, actual connection, uptime, and latest error.

Every command requires the Discord **Manage Server** permission. Replies are visible only to the person who ran the command. The bot supports regular voice channels, not Stage channels.

## 1. Create the Discord bot

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and select **New Application**.
2. Give the application a name, open **Bot**, and create the bot user if Discord asks you to.
3. On the Bot page, select **Reset Token**, copy the token, and keep it private. Never put it in GitHub or send it to anyone.
4. You do not need to enable Message Content, Server Members, or Presence privileged intents.
5. Open **Installation** (or **OAuth2 > URL Generator** on the older portal layout).
6. Enable the `bot` and `applications.commands` scopes.
7. Select only these bot permissions:
   - **View Channels**
   - **Connect**
8. Open the generated installation link, choose your Discord server, and authorize the bot.

The bot intentionally does not request Administrator, Speak, Mute Members, or Move Members.

## 2. Get your server ID

1. In Discord, open **User Settings > Advanced** and enable **Developer Mode**.
2. Right-click your server icon and select **Copy Server ID**.

You will use this value as `GUILD_ID`.

## 3. Deploy on an Ubuntu or Debian VPS

SSH into the VPS using your normal account. Running as `root` is acceptable for this intentionally simple setup; no additional Linux user or service is required.

Install Git, Python, the Discord voice dependencies, and `tmux`:

```bash
apt update
apt install -y git python3 python3-venv python3-dev libffi-dev libnacl-dev tmux
```

Clone this public repository and enter it:

```bash
git clone https://github.com/Kubogi/acid-bot.git
cd acid-bot
```

Create an isolated Python environment and install the pinned dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create your private configuration file:

```bash
cp .env.example .env
nano .env
```

Replace the sample values with your bot token and server ID:

```dotenv
DISCORD_TOKEN=your-real-bot-token
GUILD_ID=your-server-id
```

Save in `nano` with <kbd>Ctrl</kbd>+<kbd>O</kbd>, press <kbd>Enter</kbd>, then exit with <kbd>Ctrl</kbd>+<kbd>X</kbd>. Restrict the file so only your VPS user can read it:

```bash
chmod 600 .env
```

Run the bot once in the foreground:

```bash
python bot.py
```

Wait for logs saying the bot logged in and synced its command group. In Discord, type `/voice status`. If the command responds, stop the foreground process with <kbd>Ctrl</kbd>+<kbd>C</kbd>.

## 4. Keep it running with tmux

Create a named terminal session:

```bash
tmux new -s acid-bot
```

Inside that session, start the bot:

```bash
cd ~/acid-bot
source .venv/bin/activate
python bot.py
```

If you cloned the project somewhere other than `~/acid-bot`, use that path instead. Detach without stopping the bot by pressing <kbd>Ctrl</kbd>+<kbd>B</kbd>, releasing both keys, and then pressing <kbd>D</kbd>.

Useful commands:

```bash
# Reopen the bot console and view live logs
tmux attach -t acid-bot

# List sessions
tmux ls

# Stop the bot after attaching
# Press Ctrl+C in its console
```

`tmux` keeps the bot running when you disconnect from SSH. It does **not** start the bot automatically after the VPS reboots. After a reboot, repeat the `tmux new -s acid-bot` and startup commands above.

## Updating the bot

Attach to the session and stop the bot with <kbd>Ctrl</kbd>+<kbd>C</kbd>, then run:

```bash
cd ~/acid-bot
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
python bot.py
```

Detach from `tmux` again after the bot starts.

## Using the bot

Join a regular voice channel and run `/voice join`. The bot saves that channel in `data/voice_state.json` and reconnects to it after bot restarts, temporary network outages, external moves, or forced disconnects.

Run `/voice leave` before intentionally removing it from voice. This disables recovery but retains the last target, so `/voice rejoin` can resume it later.

## Local development and tests

Python 3.10 or newer is recommended.

```bash
python -m venv .venv
source .venv/bin/activate      # Windows PowerShell: .venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
cp .env.example .env           # Windows PowerShell: Copy-Item .env.example .env
python -m unittest discover -s tests -v
python bot.py
```

## Troubleshooting

### Slash commands do not appear

- Confirm `GUILD_ID` is the ID of the server where the bot was installed.
- Reinstall the bot with both `bot` and `applications.commands` scopes.
- Restart the process and look for the `Synced 1 command group(s)` log entry.

### The bot cannot connect

- Confirm it can **View Channel** and **Connect** in that channel, including channel-specific permission overrides.
- A full channel may reject the bot because it intentionally does not request **Move Members**.
- Run `/voice status` to see the latest connection error.

### Voice dependencies fail to install

Run the package installation command from the deployment section again, then recreate the virtual environment if necessary:

```bash
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

### The token was exposed

Immediately reset it on the Discord Developer Portal, replace `DISCORD_TOKEN` in `.env`, and restart the bot. The real `.env`, runtime state, logs, and virtual environment are excluded by `.gitignore`.

## License

No license has been granted. All rights are reserved by the repository owner.
