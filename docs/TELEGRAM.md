# Telegram bot

Part of the [CLK documentation](../README.md). Chat-control CLK from your phone.

## Telegram Bot

Two-way chat control for CLK. The bot lets you kick off runs, watch live
status updates, tail the activity log, and cancel tasks from anywhere
Telegram works — no SSH, no port forwarding, no public URL. It connects
via long polling, so it works behind NAT (your home network, a Pi behind
a router, a Docker container).

### How it works

`clk-telegram-bot` is a separate process that:

1. Long-polls Telegram's servers for messages from allowlisted users.
2. Translates commands into calls against the local CLK REST API
   (`clk-api`, default `http://127.0.0.1:8001`).
3. Tails `.clk/logs/activity.jsonl` and pushes interesting events (agent
   dispatches, action applied, iteration outcomes, errors) to subscribed
   chats in real time.

Access is gated by a numeric-user-ID allowlist. Unknown users get a single
canned reply that prints their own user ID (so the operator can add them)
and are otherwise ignored.

### One-time setup (any platform)

Three steps. The wizard automates the last two:

1. **Create the bot** with [@BotFather](https://t.me/BotFather):
   - Open Telegram, message `@BotFather`.
   - Send `/newbot`. Pick a display name and a unique username that ends
     in `bot` (e.g. `my_clk_bot`).
   - BotFather replies with an HTTP API token like
     `123456789:AAH...xyz`. Copy it.
2. **Run the wizard**:
   ```bash
   ./scripts/telegram_setup_wizard.sh
   ```
   The wizard:
   - Validates the token by calling `getMe` against Telegram.
   - Prints "Send any message to your new bot, then press Enter".
   - Reads `getUpdates` to capture your numeric user ID automatically
     (you can also enter one manually).
   - Writes `CLK_TELEGRAM_BOT_TOKEN`, `CLK_TELEGRAM_ALLOWED_USERS`, and
     `CLK_TELEGRAM_ENABLED=true` to `.env` (preserving other keys).
3. **Start the bot**:
   ```bash
   # Make sure the REST API is running first (so the bot has something to drive):
   clk-api &
   # Then start the bot:
   clk-telegram-bot
   ```

The wizard is idempotent: re-run any time to rotate the token, add more
allowed users, or re-discover your ID after switching accounts.

**You should now see:** in your Telegram chat with the new bot, sending
`/start` replies with your user ID and the help text. Sending `/status`
lists workspaces.

### Setup inside Docker

`kickoff.sh` offers Telegram setup automatically the first time it runs
without a token configured. The image already includes
`python-telegram-bot`, the wizard script, and the `clk-telegram-bot`
entry point.

```bash
# 1. Create an empty config file on the host (once).
touch ~/clk.env

# 2. Run kickoff with --setup; answer "y" at the Telegram prompt.
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk --setup
```

To run only the Telegram wizard (no kickoff prompts):

```bash
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  --entrypoint scripts/telegram_setup_wizard.sh \
  clk
```

Once `~/clk.env` has the Telegram keys, run the bot in its own container
alongside `clk-api`:

```bash
# REST API server (port 8001 published so the bot container can reach it)
docker run -d --name clk-api \
  -v ~/clk.env:/app/.env \
  -v clk-workspaces:/workspaces \
  -p 127.0.0.1:8001:8001 \
  --entrypoint python clk -m clk_harness.api

# Telegram bot — talks to clk-api via Docker's bridge network
docker run -d --name clk-telegram-bot \
  --link clk-api \
  -v ~/clk.env:/app/.env \
  -v clk-workspaces:/workspaces \
  -e CLK_API_HOST=clk-api \
  -e CLK_API_PORT=8001 \
  --entrypoint clk-telegram-bot clk
```

The bot makes **outbound** HTTPS calls to `api.telegram.org`, so no
inbound port forwarding is needed. The default Docker bridge network is
enough.

### Setup on Raspberry Pi (systemd)

Install CLK via the [Pi extension](PROVIDERS.md#pi-extension) or `pip install
'clk-harness[api,telegram]'`, then drop two systemd units:

```ini
# /etc/systemd/system/clk-api.service
[Unit]
Description=CLK REST API
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/clk
EnvironmentFile=/home/pi/clk/.env
ExecStart=/usr/local/bin/clk-api
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/clk-telegram-bot.service
[Unit]
Description=CLK Telegram bot
After=clk-api.service
Requires=clk-api.service

[Service]
User=pi
WorkingDirectory=/home/pi/clk
EnvironmentFile=/home/pi/clk/.env
ExecStart=/usr/local/bin/clk-telegram-bot
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable both: `sudo systemctl enable --now clk-api clk-telegram-bot`.

**You should now see:** from your phone, `/status` returns the current
workspace list. Sending `/run improve the README` kicks off a CLK run and
the bot replies with a task ID.

### Commands

| Command | Effect |
|---------|--------|
| `/start` | Greet, show your user ID, indicate whether allowlisted |
| `/help` | Show this command list |
| `/status` | List workspaces and last task ID |
| `/run <objective>` | Start a single CLK run with the given objective |
| `/loop [args]` | Start the Ralph / autoresearch loop |
| `/plan <topic>` | Run the planning workflow |
| `/idea <text>` | Capture an idea |
| `/cancel [task_id]` | Cancel a running task (latest if omitted) |
| `/tail [N]` | Print the last N lines of `activity.jsonl` (default 20) |
| `/subscribe` | Receive live event pushes in this chat |
| `/unsubscribe` | Stop receiving live event pushes |
| `/workspace <id>` | Set the default workspace for this chat |

Any plain text (no slash) from an allowlisted user is treated as
`/run <text>` — so you can just describe what you want.

### Adding more allowed users

Either re-run `scripts/telegram_setup_wizard.sh` (it appends new IDs to
the existing list) or edit `CLK_TELEGRAM_ALLOWED_USERS` in `.env`
directly:

```bash
# .env
CLK_TELEGRAM_ALLOWED_USERS=123456789,987654321,555666777
```

Restart `clk-telegram-bot` to pick up the change.

### Troubleshooting

- **Bot doesn't reply.** Send `/start` and check the reply for your user
  ID. If you get the "Not allowlisted" message, add the ID to
  `CLK_TELEGRAM_ALLOWED_USERS` and restart the bot.
- **`token rejected by Telegram`** (during the wizard). The token is
  wrong or was revoked. Get a fresh one from BotFather with `/token`.
- **No live updates** even after `/subscribe`. Confirm that the bot can
  read the activity log: `CLK_TELEGRAM_ACTIVITY_LOG` overrides the
  default path, or the bot auto-detects
  `$CLK_WORKSPACES_DIR/<workspace>/.clk/logs/activity.jsonl`.
- **`clk-telegram-bot --check-config` exits non-zero.** It prints which
  variable is missing (`2` = token, `3` = empty allowlist).
- **Kickoff prompts every run.** Set `CLK_TELEGRAM_SKIP=true` in `.env`
  to permanently suppress the "Set up Telegram bot now?" prompt.
