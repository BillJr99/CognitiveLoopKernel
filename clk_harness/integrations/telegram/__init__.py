"""Two-way Telegram bot for CognitiveLoopKernel.

Run via the `clk-telegram-bot` console script. Connects to Telegram via
long polling (no public URL needed) and talks to the local CLK REST API
on ``CLK_API_HOST:CLK_API_PORT`` (default 127.0.0.1:8001). Tails
``.clk/logs/activity.jsonl`` to push live status to subscribed chats.
"""
