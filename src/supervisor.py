"""Lightweight Telegram supervisor that starts/stops the main bot on demand.

This script uses minimal resources (~5MB RAM) and only listens for:
- /poweron - Start the full podcast bot
- /poweroff - Stop the full podcast bot
- /botstatus - Check if bot is running

Usage:
    python -m src.supervisor

The supervisor stays running 24/7 but uses almost no resources.
When you send /poweron, it launches the full bot.
When you send /poweroff, it stops the full bot to save resources.
"""

import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# Minimal logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Load config minimally - just need bot token and allowed users
def load_config():
    """Load minimal config (just token and allowed users)."""
    import yaml
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)
    return {
        "token": config["telegram"]["bot_token"],
        "allowed_users": config["telegram"].get("allowed_users", []),
    }


class BotSupervisor:
    """Lightweight supervisor to start/stop the main bot."""

    def __init__(self, config: dict):
        self.config = config
        self.bot_process: subprocess.Popen | None = None
        self.bot_dir = Path(__file__).parent.parent

    def is_authorized(self, user_id: int) -> bool:
        """Check if user is authorized."""
        allowed = self.config.get("allowed_users", [])
        return not allowed or user_id in allowed

    def is_bot_running(self) -> bool:
        """Check if the main bot process is running."""
        if self.bot_process is None:
            return False
        # Check if process is still alive
        return self.bot_process.poll() is None

    def start_bot(self) -> bool:
        """Start the main bot process."""
        if self.is_bot_running():
            return False  # Already running

        try:
            # Start the bot as a subprocess
            self.bot_process = subprocess.Popen(
                [sys.executable, "-m", "src.bot"],
                cwd=self.bot_dir,
                stdout=open(self.bot_dir / "bot.log", "a"),
                stderr=subprocess.STDOUT,
            )
            logger.info(f"Started bot process (PID: {self.bot_process.pid})")
            return True
        except Exception as e:
            logger.error(f"Failed to start bot: {e}")
            return False

    def stop_bot(self) -> bool:
        """Stop the main bot process."""
        if not self.is_bot_running():
            return False  # Not running

        try:
            # Send SIGTERM for graceful shutdown
            self.bot_process.terminate()
            try:
                self.bot_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                # Force kill if it doesn't stop
                self.bot_process.kill()
                self.bot_process.wait()

            logger.info("Stopped bot process")
            self.bot_process = None
            return True
        except Exception as e:
            logger.error(f"Failed to stop bot: {e}")
            return False

    async def poweron_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /poweron command."""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        if self.is_bot_running():
            await update.message.reply_text(
                "🟢 Bot is already running!\n\n"
                "Use /podcast <url> to process a podcast.\n"
                "Use /poweroff when done to save resources."
            )
        else:
            await update.message.reply_text("🔄 Starting bot...")
            if self.start_bot():
                await asyncio.sleep(2)  # Give it time to start
                await update.message.reply_text(
                    "🟢 Bot is now running!\n\n"
                    "Use /podcast <url> to process a podcast.\n"
                    "Use /poweroff when done to save resources."
                )
            else:
                await update.message.reply_text("❌ Failed to start bot. Check logs.")

    async def poweroff_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /poweroff command."""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        if not self.is_bot_running():
            await update.message.reply_text(
                "🔴 Bot is already off.\n\n"
                "Use /poweron to start it when needed."
            )
        else:
            await update.message.reply_text("🔄 Shutting down bot...")
            if self.stop_bot():
                await update.message.reply_text(
                    "🔴 Bot is now off (saving resources).\n\n"
                    "Use /poweron to start it when needed."
                )
            else:
                await update.message.reply_text("❌ Failed to stop bot. Check logs.")

    async def botstatus_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /botstatus command."""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        if self.is_bot_running():
            pid = self.bot_process.pid if self.bot_process else "unknown"
            await update.message.reply_text(
                f"🟢 Bot is RUNNING (PID: {pid})\n\n"
                "Commands available:\n"
                "• /podcast <url> - Process a podcast\n"
                "• /stop - Cancel stuck processes\n"
                "• /lookup - Browse saved summaries\n"
                "• /poweroff - Stop bot to save resources"
            )
        else:
            await update.message.reply_text(
                "🔴 Bot is OFF (saving resources)\n\n"
                "Use /poweron to start the bot."
            )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - always responds even if bot is busy."""
        if not self.is_authorized(update.effective_user.id):
            await update.message.reply_text("Not authorized.")
            return

        bot_running = self.is_bot_running()
        pid = self.bot_process.pid if self.bot_process else None

        # Try to get detailed status from bot's data files
        status_details = self._get_bot_status_details()

        if bot_running:
            msg = f"📊 Status (via supervisor)\n\n"
            msg += f"🟢 Bot: RUNNING (PID: {pid})\n"
            msg += f"📁 Saved summaries: {status_details['summary_count']}\n"

            if status_details['queue_info']:
                msg += f"\n⏳ Processing queue:\n{status_details['queue_info']}"
            else:
                msg += "\n✓ No items in processing queue\n"

            msg += "\n\nCommands:\n"
            msg += "• /stop - Cancel stuck processes\n"
            msg += "• /poweroff - Stop bot to save resources"
        else:
            msg = "📊 Status (via supervisor)\n\n"
            msg += "🔴 Bot: OFF (saving resources)\n"
            msg += f"📁 Saved summaries: {status_details['summary_count']}\n"
            msg += "\nUse /poweron to start the bot."

        await update.message.reply_text(msg)

    def _get_bot_status_details(self) -> dict:
        """Get status details by reading bot's data files directly."""
        import json

        details = {
            'summary_count': 0,
            'queue_info': '',
        }

        try:
            # Count summaries from storage file
            summaries_file = self.bot_dir / "data" / ".summaries.json"
            if not summaries_file.exists():
                # Try vault path
                import yaml
                config_path = self.bot_dir / "config.yaml"
                with open(config_path) as f:
                    config = yaml.safe_load(f)
                vault_path = Path(config.get('obsidian', {}).get('vault_path', ''))
                summaries_file = vault_path / ".summaries.json"

            if summaries_file.exists():
                with open(summaries_file) as f:
                    data = json.load(f)
                    details['summary_count'] = len(data.get('summaries', []))
        except Exception:
            pass

        return details


def main():
    """Run the lightweight supervisor."""
    config = load_config()
    supervisor = BotSupervisor(config)

    # Build minimal application
    application = Application.builder().token(config["token"]).build()

    # Register supervisor commands - these always work even if bot is busy
    application.add_handler(CommandHandler("poweron", supervisor.poweron_command))
    application.add_handler(CommandHandler("poweroff", supervisor.poweroff_command))
    application.add_handler(CommandHandler("botstatus", supervisor.botstatus_command))
    application.add_handler(CommandHandler("status", supervisor.status_command))

    # Handle shutdown gracefully
    def shutdown_handler(signum, frame):
        logger.info("Supervisor shutting down...")
        supervisor.stop_bot()  # Stop bot if running
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    logger.info("Supervisor started - waiting for /poweron command...")
    logger.info("Bot is OFF by default. Send /poweron in Telegram to start it.")

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
