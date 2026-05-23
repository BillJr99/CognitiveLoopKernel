"""Telegram bot entry point for CLK.

Run via ``clk-telegram-bot`` (console script) or ``python -m
clk_harness.integrations.telegram.bot``. Long-polls Telegram, dispatches
commands to the local CLK REST API, and pushes live activity-log events
to subscribed chats.

Environment variables:
    CLK_TELEGRAM_BOT_TOKEN     - bot token from @BotFather (required)
    CLK_TELEGRAM_ALLOWED_USERS - comma-separated numeric user IDs
    CLK_API_HOST / CLK_API_PORT - where the local REST API is listening
    CLK_TELEGRAM_WORKSPACE     - default workspace ID for commands
    CLK_TELEGRAM_ACTIVITY_LOG  - explicit path to activity.jsonl (else
                                 auto-detected per workspace)
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Optional, Set

from .auth import is_allowed, load_allowlist
from .clk_client import CLKClient, default_base_url
from .formatters import TELEGRAM_MAX, chunk, code_block, redact_token, tail
from .streamer import Coalescer, format_event, interesting_events

log = logging.getLogger("clk.telegram")

# python-telegram-bot is an optional dependency. Import lazily so that
# importing this module for unit tests (which mock out the bot) does not
# require the package to be installed.
try:  # pragma: no cover
    from telegram import Update
    from telegram.ext import (
        Application,
        ApplicationBuilder,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )

    _PTB_AVAILABLE = True
except BaseException:  # pragma: no cover
    # Catch broadly: importing python-telegram-bot pulls in cryptography,
    # which can fail with pyo3_runtime.PanicException on misconfigured
    # systems. We treat any import-time failure as "not available" so the
    # rest of the module (and --check-config) still works.
    _PTB_AVAILABLE = False


HELP_TEXT = """\
CLK Telegram bot commands:
  /help                 - this message
  /status               - workspaces + recent task summary
  /run <objective>      - kick off a single CLK run
  /loop [args]          - kick off the Ralph/autoresearch loop
  /plan <topic>         - run the planning workflow
  /idea <text>          - capture an idea
  /cancel [task_id]     - cancel a running task (latest if omitted)
  /tail [N]             - last N activity-log events (default 20)
  /subscribe            - start receiving live event pushes here
  /unsubscribe          - stop pushes
  /workspace <id>       - set default workspace for this chat
Plain text (no slash) is treated as /run <text>.
"""


# ---------------------------------------------------------------------------
# Bot-wide state container
# ---------------------------------------------------------------------------


class BotState:
    def __init__(
        self,
        client: CLKClient,
        allowlist: Set[int],
        *,
        workspace: Optional[str],
        activity_log: Optional[Path],
        token: Optional[str] = None,
    ) -> None:
        self.client = client
        self.allowlist = allowlist
        self.workspace = workspace
        self.activity_log = activity_log
        self.token = token
        self.subscribers: Set[int] = set()
        self.chat_workspace: dict[int, str] = {}
        self.last_task_id: Optional[str] = None

    def workspace_for(self, chat_id: int) -> Optional[str]:
        return self.chat_workspace.get(chat_id, self.workspace)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _reply(update, text: str, *, token: Optional[str] = None) -> None:
    """Reply, redacting any bot token and chunking to Telegram limits."""
    body = redact_token(text, token)
    for piece in chunk(body, TELEGRAM_MAX):
        await update.effective_chat.send_message(piece)


def _state(context) -> BotState:
    return context.application.bot_data["state"]


def _user_id(update) -> Optional[int]:
    if update is None or update.effective_user is None:
        return None
    return update.effective_user.id


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update, context) -> None:  # pragma: no cover - thin wrapper
    state = _state(context)
    uid = _user_id(update)
    allowed = is_allowed(uid, state.allowlist)
    if allowed:
        await _reply(
            update,
            f"Hello! You are user ID `{uid}` and are allowlisted.\n\n{HELP_TEXT}",
            token=state.token,
        )
    else:
        await _reply(
            update,
            f"Your user ID is `{uid}`. Ask the operator to add it to "
            "CLK_TELEGRAM_ALLOWED_USERS to use this bot.",
            token=state.token,
        )


async def cmd_help(update, context) -> None:  # pragma: no cover
    await _reply(update, HELP_TEXT, token=_state(context).token)


async def cmd_status(update, context) -> None:  # pragma: no cover
    state = _state(context)
    try:
        ws = await state.client.list_workspaces()
        lines = ["Workspaces:"]
        for w in ws[:20]:
            lines.append(f"  - {w.get('id') or w.get('name') or w}")
        lines.append(f"\nDefault workspace: {state.workspace_for(update.effective_chat.id) or '(none)'}")
        lines.append(f"Last task: {state.last_task_id or '(none)'}")
        await _reply(update, "\n".join(lines), token=state.token)
    except Exception as exc:
        await _reply(update, f"Error contacting CLK API: {exc}", token=state.token)


async def _start_command(update, context, command: str, args_text: str) -> None:
    state = _state(context)
    chat_id = update.effective_chat.id
    workspace = state.workspace_for(chat_id)
    if not workspace:
        try:
            ws = await state.client.create_workspace()
            workspace = ws.get("id") or ws.get("workspace_id") or ws.get("name")
            state.chat_workspace[chat_id] = workspace
        except Exception as exc:
            await _reply(update, f"No workspace and could not create one: {exc}", token=state.token)
            return
    args = [a for a in args_text.strip().split() if a] if args_text else []
    try:
        resp = await state.client.start_task(workspace, command, args=args)
        task_id = resp.get("task_id") or resp.get("id") or "?"
        state.last_task_id = task_id
        await _reply(
            update,
            f"Started `{command}` in `{workspace}` (task `{task_id}`).",
            token=state.token,
        )
    except Exception as exc:
        await _reply(update, f"Failed to start `{command}`: {exc}", token=state.token)


async def cmd_run(update, context) -> None:  # pragma: no cover
    text = " ".join(context.args) if context.args else ""
    await _start_command(update, context, "run", text)


async def cmd_loop(update, context) -> None:  # pragma: no cover
    text = " ".join(context.args) if context.args else ""
    await _start_command(update, context, "loop", text)


async def cmd_plan(update, context) -> None:  # pragma: no cover
    text = " ".join(context.args) if context.args else ""
    await _start_command(update, context, "plan", text)


async def cmd_idea(update, context) -> None:  # pragma: no cover
    text = " ".join(context.args) if context.args else ""
    await _start_command(update, context, "idea", text)


async def cmd_cancel(update, context) -> None:  # pragma: no cover
    state = _state(context)
    tid = (context.args[0] if context.args else None) or state.last_task_id
    if not tid:
        await _reply(update, "No task to cancel.", token=state.token)
        return
    try:
        await state.client.cancel_task(tid)
        await _reply(update, f"Cancelled task `{tid}`.", token=state.token)
    except Exception as exc:
        await _reply(update, f"Cancel failed: {exc}", token=state.token)


async def cmd_tail(update, context) -> None:  # pragma: no cover
    state = _state(context)
    n = 20
    if context.args:
        try:
            n = max(1, min(200, int(context.args[0])))
        except ValueError:
            pass
    path = state.activity_log
    if path is None or not path.exists():
        await _reply(update, "No activity log available.", token=state.token)
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        last = tail(fh, n)
    text = code_block("".join(last) or "(empty)")
    await _reply(update, text, token=state.token)


async def cmd_subscribe(update, context) -> None:  # pragma: no cover
    state = _state(context)
    state.subscribers.add(update.effective_chat.id)
    await _reply(update, "Subscribed to live event pushes.", token=state.token)


async def cmd_unsubscribe(update, context) -> None:  # pragma: no cover
    state = _state(context)
    state.subscribers.discard(update.effective_chat.id)
    await _reply(update, "Unsubscribed from live event pushes.", token=state.token)


async def cmd_workspace(update, context) -> None:  # pragma: no cover
    state = _state(context)
    if not context.args:
        await _reply(update, f"Current workspace: {state.workspace_for(update.effective_chat.id) or '(none)'}", token=state.token)
        return
    state.chat_workspace[update.effective_chat.id] = context.args[0]
    await _reply(update, f"Workspace set to `{context.args[0]}`.", token=state.token)


async def on_plain_text(update, context) -> None:  # pragma: no cover
    text = (update.message.text or "").strip() if update.message else ""
    if not text:
        return
    await _start_command(update, context, "run", text)


async def on_denied(update, context) -> None:  # pragma: no cover
    state = _state(context)
    uid = _user_id(update)
    log.warning("denied update from user_id=%s", uid)
    await _reply(
        update,
        f"Not allowlisted. Your user ID is `{uid}`. Ask the operator to add it.",
        token=state.token,
    )


# ---------------------------------------------------------------------------
# Background pusher: activity log -> subscribers
# ---------------------------------------------------------------------------


async def _push_loop(application, state: BotState) -> None:  # pragma: no cover
    if state.activity_log is None:
        log.info("no activity log path; push loop disabled")
        return
    coalescer = Coalescer()
    async for evt in interesting_events(state.activity_log):
        msg = coalescer.feed(evt) or format_event(evt)
        if not state.subscribers:
            continue
        body = redact_token(msg, state.token)
        for chat_id in list(state.subscribers):
            try:
                await application.bot.send_message(chat_id=chat_id, text=body[:TELEGRAM_MAX])
            except Exception as exc:
                log.warning("send_message to %s failed: %s", chat_id, exc)


# ---------------------------------------------------------------------------
# App construction
# ---------------------------------------------------------------------------


def _activity_log_path(workspace: Optional[str]) -> Optional[Path]:
    explicit = os.environ.get("CLK_TELEGRAM_ACTIVITY_LOG")
    if explicit:
        return Path(explicit)
    workspaces_dir = Path(os.environ.get("CLK_WORKSPACES_DIR", "/workspaces"))
    if workspace:
        candidate = workspaces_dir / workspace / ".clk" / "logs" / "activity.jsonl"
        return candidate
    return None


def build_application(
    token: str,
    state: "BotState",
):  # pragma: no cover - exercised via run_bot integration
    if not _PTB_AVAILABLE:
        raise RuntimeError(
            "python-telegram-bot is not installed. Install with: pip install '.[telegram]'"
        )
    app = ApplicationBuilder().token(token).build()
    app.bot_data["state"] = state

    allow_filter = filters.User(user_id=list(state.allowlist)) if state.allowlist else filters.User(user_id=[0])

    app.add_handler(CommandHandler("start", cmd_start, filters=allow_filter))
    app.add_handler(CommandHandler("help", cmd_help, filters=allow_filter))
    app.add_handler(CommandHandler("status", cmd_status, filters=allow_filter))
    app.add_handler(CommandHandler("run", cmd_run, filters=allow_filter))
    app.add_handler(CommandHandler("loop", cmd_loop, filters=allow_filter))
    app.add_handler(CommandHandler("plan", cmd_plan, filters=allow_filter))
    app.add_handler(CommandHandler("idea", cmd_idea, filters=allow_filter))
    app.add_handler(CommandHandler("cancel", cmd_cancel, filters=allow_filter))
    app.add_handler(CommandHandler("tail", cmd_tail, filters=allow_filter))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe, filters=allow_filter))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe, filters=allow_filter))
    app.add_handler(CommandHandler("workspace", cmd_workspace, filters=allow_filter))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & allow_filter, on_plain_text))
    app.add_handler(MessageHandler(filters.ALL & ~allow_filter, on_denied))

    return app


async def run_bot(
    token: str,
    *,
    allowlist: Set[int],
    workspace: Optional[str],
    base_url: Optional[str] = None,
) -> None:  # pragma: no cover
    activity = _activity_log_path(workspace)
    client = CLKClient(base_url=base_url or default_base_url())
    state = BotState(
        client=client,
        allowlist=allowlist,
        workspace=workspace,
        activity_log=activity,
        token=token,
    )
    app = build_application(token, state)

    async with client:
        await app.initialize()
        await app.start()
        push_task = asyncio.create_task(_push_loop(app, state))
        try:
            await app.updater.start_polling()
            # Block forever; the polling task keeps running.
            await asyncio.Event().wait()
        finally:
            push_task.cancel()
            await app.updater.stop()
            await app.stop()
            await app.shutdown()


# ---------------------------------------------------------------------------
# Console entry
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="clk-telegram-bot", description=__doc__)
    p.add_argument("--token", default=os.environ.get("CLK_TELEGRAM_BOT_TOKEN"))
    p.add_argument(
        "--allow-user",
        action="append",
        type=int,
        default=[],
        help="numeric Telegram user ID (may repeat)",
    )
    p.add_argument("--workspace", default=os.environ.get("CLK_TELEGRAM_WORKSPACE"))
    p.add_argument("--base-url", default=os.environ.get("CLK_API_BASE_URL"))
    p.add_argument(
        "--check-config",
        action="store_true",
        help="validate configuration (token, allowlist) and exit",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: Optional[list] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO))

    token = args.token
    allowlist = load_allowlist(extra_ids=args.allow_user)

    if args.check_config:
        if not token:
            print("CLK_TELEGRAM_BOT_TOKEN is not set", file=sys.stderr)
            return 2
        if not allowlist:
            print("CLK_TELEGRAM_ALLOWED_USERS is empty (no users allowlisted)", file=sys.stderr)
            return 3
        print(f"ok: token present, {len(allowlist)} user(s) allowlisted")
        return 0

    if not token:
        print("error: --token or CLK_TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
        return 2
    if not allowlist:
        print(
            "error: allowlist is empty. Set CLK_TELEGRAM_ALLOWED_USERS or pass --allow-user.",
            file=sys.stderr,
        )
        return 3

    asyncio.run(
        run_bot(
            token,
            allowlist=allowlist,
            workspace=args.workspace,
            base_url=args.base_url,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
