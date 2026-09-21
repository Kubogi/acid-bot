# Acid Bot

Acid Bot is a small Discord bot that stays in one voice channel until an authorized controller tells it to leave. It remembers the selected channel across restarts, reconnects after network failures or kicks, and does not play or record audio.

## What it does

- `/voice join` — join your current voice channel and save it as the target.
- `/voice move <channel>` — move to another voice channel and save it.
- `/voice leave` — disconnect and turn off automatic recovery.
- `/voice rejoin` — reconnect to the last saved channel and turn recovery back on.
- `/voice status` — show the target, actual connection, uptime, and latest error.

Only Discord user IDs listed in `AUTHORIZED_USER_IDS` can control the bot; server administrators are not automatically authorized. Replies are visible only to the person who ran the command. The bot supports regular voice channels, not Stage channels.

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

## 2. Get your server and controller IDs

1. In Discord, open **User Settings > Advanced** and enable **Developer Mode**.
2. Right-click your server icon and select **Copy Server ID**.
3. Right-click each person who should control the bot and select **Copy User ID**.

Use the server ID as `GUILD_ID`. Put the controller IDs in `AUTHORIZED_USER_IDS`, separated by commas. At least one controller is required.

## 3. Deploy on an Ubuntu or Debian VPS

Use Ubuntu 22.04 or newer, or Debian 12 or newer, so the distribution provides Python 3.10+. SSH into the VPS using your normal account. Running as `root` is acceptable for this intentionally simple setup; no additional Linux user or service is required.

Install Git, Python, the Discord voice dependencies, Node.js, and npm:

```bash
apt update
apt install -y git python3 python3-venv python3-dev libffi-dev libnacl-dev nodejs npm
```

Install PM2 globally:

```bash
npm install -g pm2
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

Replace the sample values with your bot token, server ID, and controller user IDs:

```dotenv
DISCORD_TOKEN=your-real-bot-token
GUILD_ID=your-server-id
AUTHORIZED_USER_IDS=your-user-id,another-controller-user-id
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

## 4. Keep it running with PM2

The included `ecosystem.config.js` tells PM2 to run `bot.py` with the virtual environment's Python interpreter. Start it from the repository directory and save the process list:

```bash
cd ~/acid-bot
pm2 start ecosystem.config.js
pm2 save
```

If you cloned the project somewhere other than `~/acid-bot`, use that path instead. PM2 now keeps the bot running after you disconnect from SSH and restarts it if the process crashes.

Useful commands:

```bash
# Show process health
pm2 status

# Follow live logs
pm2 logs acid-bot

# Restart or stop the bot
pm2 restart acid-bot
pm2 stop acid-bot
```

This setup intentionally does not run `pm2 startup`, because that command creates a system service. After the VPS itself reboots, restore the saved process list manually:

```bash
pm2 resurrect
```

## Updating the bot

Pull the latest code, update dependencies, and restart the PM2 process:

```bash
cd ~/acid-bot
git pull --ff-only
source .venv/bin/activate
python -m pip install -r requirements.txt
pm2 restart acid-bot
pm2 save
```

## Using the bot

Join a regular voice channel and run `/voice join`. The bot saves that channel in `data/voice_state.json` and reconnects to it after bot restarts, temporary network outages, external moves, or forced disconnects.

Run `/voice leave` before intentionally removing it from voice. This disables recovery but retains the last target, so `/voice rejoin` can resume it later.

## Local development and tests

Python 3.10 or newer is required.

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

### You are not authorized to control the bot

- Copy your Discord user ID again and confirm it appears in `AUTHORIZED_USER_IDS`.
- Use raw numeric IDs separated by commas, not usernames or `@mentions`.
- Restart the process with `pm2 restart acid-bot` after changing `.env`.

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
