const path = require("node:path");

module.exports = {
  apps: [
    {
      name: "acid-bot",
      cwd: __dirname,
      script: "bot.py",
      interpreter: path.join(__dirname, ".venv", "bin", "python"),
      autorestart: true,
      restart_delay: 5000,
      max_memory_restart: "256M",
      time: true,
    },
  ],
};
