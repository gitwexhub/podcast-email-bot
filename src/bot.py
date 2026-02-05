"""Telegram bot handlers and main entry point."""

import asyncio
import logging
import re
import smtplib
from typing import Optional
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
)

from src.config import get_config, init_config
from src.processors.podcast import PodcastProcessor
from src.processors.article import ArticleProcessor
from src.processors.thread import ThreadProcessor
from src.storage.vault import VaultWriter
from src.storage.vectors import VectorStore
from src.storage.summaries import SummaryStorage
from src.storage.categories import CategoryStorage
from src.ai.learning import LearningSystem
from src.digest.daily import DailyDigest, DigestScheduler
from src.security import AccessControl, sanitize_error_message, validate_url

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Conversation states for podcast flow
PODCAST_MODE_SELECT = 1
PODCAST_INTERACTIVE = 2
PODCAST_REVIEW = 3


class KnowledgeBot:
    """Main bot class coordinating all components."""

    def __init__(self):
        self.config = get_config()
        self.vault = VaultWriter(self.config.obsidian.vault_path)
        self.vector_store = VectorStore(self.config.obsidian.vault_path / ".vectors.db")
        self.summary_storage = SummaryStorage(self.config.obsidian.vault_path / ".summaries.json")
        self.category_storage = CategoryStorage(self.config.obsidian.vault_path / ".categories.json")
        self.learning = LearningSystem(self.config.obsidian.vault_path / ".learning.json")
        self.podcast_processor = PodcastProcessor(self.config, self.vault)
        self.article_processor = ArticleProcessor(self.config, self.vault)
        self.thread_processor = ThreadProcessor(self.config, self.vault)

        # Access control - only allow configured users
        self.access_control = AccessControl(self.config.telegram.allowed_users)

        # Daily digest (will be configured with Telegram callback later)
        self.daily_digest = DailyDigest(
            config=self.config,
            vault=self.vault,
            vector_store=self.vector_store,
        )
        self.digest_scheduler = DigestScheduler(self.daily_digest)
        self._telegram_app = None

        # Active podcast sessions for interactive mode
        # Key: user_id, Value: session data
        self.podcast_sessions = {}

        # Power state - when False, only /poweron and /status work
        self.is_powered_on = True

    def _check_access(self, update: Update) -> bool:
        """Check if the user is authorized. Returns True if allowed."""
        user = update.effective_user
        if not user:
            return False
        return self.access_control.is_allowed(user.id)

    async def _check_power_and_access(self, update: Update, command_name: str = "") -> bool:
        """Check both power state and access. Returns True if command should proceed."""
        if not self._check_access(update):
            await self._deny_access(update)
            return False

        if not self.is_powered_on:
            await update.message.reply_text(
                "🔴 Bot is in sleep mode (saving resources).\n\n"
                "Use /poweron to wake it up."
            )
            return False

        return True

    async def _deny_access(self, update: Update) -> None:
        """Send access denied message."""
        user = update.effective_user
        logger.warning(f"Unauthorized access attempt by user {user.id if user else 'unknown'}")
        await update.message.reply_text(
            "❌ You are not authorized to use this bot.\n"
            f"Your user ID is: `{user.id if user else 'unknown'}`\n"
            "Contact the bot owner to request access.",
            parse_mode="Markdown",
        )

    def set_telegram_app(self, app: Application) -> None:
        """Set the Telegram app for sending digest messages."""
        self._telegram_app = app

        # Configure digest to send Telegram messages
        async def send_telegram_digest(message: str):
            if self._telegram_app:
                # This would need the chat_id - we'll store it from /start
                pass  # TODO: Implement once we track user chat IDs

        self.daily_digest.send_telegram_message = send_telegram_digest

    def start_scheduler(self) -> None:
        """Start the daily digest scheduler."""
        self.digest_scheduler.start()
        logger.info("Daily digest scheduler started")

    def stop_scheduler(self) -> None:
        """Stop the daily digest scheduler."""
        self.digest_scheduler.stop()

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /start command."""
        await update.message.reply_text(
            "👋 Welcome to Podcast Email Bot!\n\n"
            "I turn podcasts into professional email summaries.\n\n"
            "**Commands:**\n"
            "/podcast <url> - Process and summarize a podcast\n"
            "/lookup - Browse your saved summaries\n"
            "/stats - View learning progress\n"
            "/help - Full command list\n\n"
            "**Supported:** Spotify, Apple Podcasts, RSS feeds\n\n"
            "Just paste a podcast link to get started!",
            parse_mode="Markdown",
        )

    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /help command."""
        await update.message.reply_text(
            "📚 **Podcast Knowledge Bot Help**\n\n"
            "**Process Podcasts:**\n"
            "• `/podcast <url>` - Process a Spotify or Apple Podcasts episode\n"
            "• Just paste a podcast link to start\n\n"
            "**Summary Modes:**\n"
            "• **AI-Only** - Let AI generate the full summary automatically\n"
            "• **Interactive** - Add your own notes while it transcribes, then AI enhances them\n\n"
            "**Browse & Organize:**\n"
            "• `/lookup` - Browse folders, recent, or search your library\n"
            "• `/organize` - AI-powered folder organization\n\n"
            "**Other:**\n"
            "• `/status` - Check bot status (works anytime)\n"
            "• `/stop` - Cancel stuck processes\n"
            "• `/poweron` / `/poweroff` - Wake/sleep the bot\n"
            "• `/stats` - View learning progress\n\n"
            "**Tip:** Use /poweroff when done to save resources!",
            parse_mode="Markdown",
        )

    async def stats_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stats command - show learning statistics."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        stats = self.learning.get_stats()
        prefs = self.learning.preferences

        # Build preferences summary
        learned_prefs = []
        if prefs.common_feedback_patterns:
            learned_prefs.append(f"• Learned patterns: {len(prefs.common_feedback_patterns)}")
        if prefs.favorite_topics:
            learned_prefs.append(f"• Topics of interest: {', '.join(prefs.favorite_topics[:5])}")

        prefs_text = "\n".join(learned_prefs) if learned_prefs else "• Still learning your preferences..."

        await update.message.reply_text(
            "📊 **Learning Statistics**\n\n"
            f"**Summary Performance:**\n"
            f"• Podcasts processed: {stats['total_processed']}\n"
            f"• Approved first try: {stats['approved_first_try']}\n"
            f"• Edits requested: {stats['edits_requested']}\n"
            f"• First-try approval rate: {stats['approval_rate']}\n\n"
            f"**Current Preferences:**\n"
            f"• Summary length: {stats['preferred_length']}\n"
            f"• Tone: {stats['preferred_tone']}\n\n"
            f"**What I've Learned:**\n"
            f"{prefs_text}\n\n"
            "_The more feedback you give, the better I get at matching your style!_",
            parse_mode="Markdown",
        )

    async def podcast_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle /podcast command - starts the podcast conversation flow."""
        if not await self._check_power_and_access(update):
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text(
                "Please provide a podcast URL.\n"
                "Usage: `/podcast <spotify-link>` or `/podcast <rss-url>`",
                parse_mode="Markdown",
            )
            return ConversationHandler.END

        url = context.args[0]

        # Validate URL for SSRF protection
        is_valid, error = validate_url(url)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid URL: {error}")
            return ConversationHandler.END

        user_id = update.effective_user.id
        chat_id = update.effective_chat.id

        # Store initial session data - ask for mode BEFORE transcription
        self.podcast_sessions[user_id] = {
            "url": url,
            "transcript": None,  # Will be populated after transcription
            "metadata": None,
            "user_details": [],
            "user_insights": [],
            "mode": None,
            "draft_email": None,
            "transcription_task": None,
            "transcription_complete": False,
            "transcription_error": None,
            "chat_id": chat_id,
        }

        # Ask for mode selection FIRST
        keyboard = [
            [InlineKeyboardButton("1️⃣ AI picks the highlights", callback_data="podcast_mode_1")],
            [InlineKeyboardButton("2️⃣ I'll highlight what matters", callback_data="podcast_mode_2")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🎙️ **Podcast Processing**\n\n"
            "Both options generate an AI-written email summary.\n"
            "The difference is who decides what's important:\n\n"
            "**Option 1:** AI analyzes the transcript and picks the key details/insights\n\n"
            "**Option 2:** YOU tell the AI what stood out while listening, then AI writes the email around your highlights\n\n"
            "_Either way, you'll review and can give feedback before saving._",
            parse_mode="Markdown",
            reply_markup=reply_markup,
        )
        return PODCAST_MODE_SELECT

    async def _run_transcription(self, user_id: int, url: str, app: Application) -> None:
        """Run transcription in the background and update session when done."""
        session = self.podcast_sessions.get(user_id)
        if not session:
            return

        chat_id = session["chat_id"]

        # Status callback to send updates to user
        async def status_callback(msg: str):
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=msg,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning(f"Failed to send status message: {e}")

        try:
            # Process the podcast with status callback
            result = await self.podcast_processor.process_transcript_only(
                url, status_callback=status_callback
            )

            # Update session with results
            session["transcript"] = result["transcript"]
            session["metadata"] = result["metadata"]
            session["transcription_complete"] = True
            session["duration_str"] = result["duration_str"]

            # Notify user that transcription is complete
            if session["mode"] == "ai_only":
                # For AI-only mode, automatically generate summary
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ **Transcription complete!**\n\n"
                    f"**{result['metadata'].title}**\n"
                    f"Duration: {result['duration_str']}\n\n"
                    "🤖 Now generating AI summary...",
                    parse_mode="Markdown",
                )
                # Generate the summary
                await self._generate_and_send_summary(user_id, app)
            else:
                # For interactive mode, just notify them
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"✅ **Transcription complete!**\n\n"
                    f"**{result['metadata'].title}**\n"
                    f"Duration: {result['duration_str']}\n\n"
                    f"You have {len(session['user_details'])} details and {len(session['user_insights'])} insights so far.\n"
                    "Use `/end` when you're ready to generate the summary.",
                    parse_mode="Markdown",
                )

        except Exception as e:
            logger.exception("Error during transcription")
            session["transcription_error"] = str(e)
            session["transcription_complete"] = True
            try:
                await app.bot.send_message(
                    chat_id=chat_id,
                    text=f"Transcription failed: {sanitize_error_message(e)}\n\nPlease try again with /podcast",
                )
            except Exception:
                pass

    def _split_long_message(self, text: str, max_length: int = 4000) -> list[str]:
        """Split a message into chunks at paragraph boundaries."""
        if len(text) <= max_length:
            return [text]

        chunks = []
        current_chunk = ""
        paragraphs = text.split("\n\n")

        for para in paragraphs:
            if len(current_chunk) + len(para) + 2 <= max_length:
                current_chunk += para + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                # Handle paragraphs longer than max_length
                if len(para) > max_length:
                    # Split at sentence boundaries or hard limit
                    while len(para) > max_length:
                        chunks.append(para[:max_length])
                        para = para[max_length:]
                current_chunk = para + "\n\n"

        if current_chunk:
            chunks.append(current_chunk.strip())

        return chunks

    async def _send_long_message(self, chat_id: int, text: str, app: Application,
                                  parse_mode: str = "Markdown", reply_markup=None) -> None:
        """Send a message via app.bot, splitting into chunks if too long."""
        chunks = self._split_long_message(text)

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await app.bot.send_message(
                chat_id=chat_id,
                text=chunk,
                parse_mode=parse_mode,
                reply_markup=reply_markup if is_last else None,
            )

    async def _reply_long_message(self, update: Update, text: str,
                                   parse_mode: str = "Markdown", reply_markup=None) -> None:
        """Send a reply message, splitting into chunks if too long."""
        chunks = self._split_long_message(text)

        for i, chunk in enumerate(chunks):
            is_last = (i == len(chunks) - 1)
            await update.message.reply_text(
                text=chunk,
                parse_mode=parse_mode,
                reply_markup=reply_markup if is_last else None,
            )

    async def _generate_and_send_summary(self, user_id: int, app: Application) -> None:
        """Generate summary and send for review."""
        session = self.podcast_sessions.get(user_id)
        if not session:
            return

        chat_id = session["chat_id"]

        # Check if we have a valid transcript
        if not session.get("transcript"):
            logger.error(f"No transcript available for user {user_id}")
            await app.bot.send_message(
                chat_id=chat_id,
                text="❌ **Error:** Transcription failed - no transcript available.\n\n"
                     "Please try again with `/podcast <url>`",
                parse_mode="Markdown",
            )
            # Clean up the failed session
            if user_id in self.podcast_sessions:
                del self.podcast_sessions[user_id]
            return

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            email_content = await summarizer.generate_podcast_email(
                transcript=session["transcript"],
                metadata=session["metadata"],
                user_details=session["user_details"],
                user_insights=session["user_insights"],
                learned_preferences=self.learning.get_prompt_context(),
            )

            session["draft_email"] = email_content

            # Send for review
            keyboard = [
                [InlineKeyboardButton("✅ Approve & Save", callback_data="podcast_approve")],
                [InlineKeyboardButton("✏️ Provide Feedback", callback_data="podcast_feedback")],
                [InlineKeyboardButton("❌ Cancel", callback_data="podcast_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Use chunked sending for long messages
            full_text = f"📧 **Draft Summary:**\n\n{email_content}\n\n─────────────────\nDoes this look good?"
            await self._send_long_message(chat_id, full_text, app, reply_markup=reply_markup)

        except Exception as e:
            logger.exception("Error generating summary")
            await app.bot.send_message(
                chat_id=chat_id,
                text=f"❌ Error generating summary: {sanitize_error_message(e)}",
            )

    async def podcast_mode_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle mode selection for podcast processing."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session:
            await query.edit_message_text("Session expired. Please start again with /podcast")
            return ConversationHandler.END

        url = session["url"]

        if query.data == "podcast_mode_1":
            # AI-only mode
            session["mode"] = "ai_only"
            await query.edit_message_text(
                "🤖 **AI-Only Mode Selected**\n\n"
                "Starting podcast processing. I'll notify you when ready!\n\n"
                "_This may take several minutes depending on podcast length._"
            )

            # Start transcription in background
            app = context.application
            asyncio.create_task(self._run_transcription(user_id, url, app))

            # Return END because we'll send messages asynchronously
            return ConversationHandler.END

        elif query.data == "podcast_mode_2":
            # Interactive mode
            session["mode"] = "interactive"
            await query.edit_message_text(
                "📝 **You're Highlighting Mode**\n\n"
                "🎵 Transcription is starting in the background...\n\n"
                "**Add what stood out to you while listening:**\n"
                "• `/detail <text>` - A key fact, stat, or point\n"
                "• `/insight <text>` - Your takeaway or connection\n"
                "• `/end` - Done adding, generate the email\n\n"
                "Example:\n"
                "`/detail 80% of fintech startups fail in first 2 years`\n"
                "`/insight This validates our conservative approach to funding`\n\n"
                "_AI will write the email around YOUR highlights._",
                parse_mode="Markdown",
            )

            # Start transcription in background
            app = context.application
            asyncio.create_task(self._run_transcription(user_id, url, app))

            return PODCAST_INTERACTIVE

        return ConversationHandler.END

    async def podcast_detail_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle /detail command in interactive mode."""
        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session or session.get("mode") != "interactive":
            await update.message.reply_text("No active podcast session. Start with /podcast")
            return ConversationHandler.END

        if not context.args:
            await update.message.reply_text("Please provide the detail text.\nUsage: `/detail <your detail>`", parse_mode="Markdown")
            return PODCAST_INTERACTIVE

        detail = " ".join(context.args)
        session["user_details"].append(detail)

        await update.message.reply_text(
            f"✅ Detail added ({len(session['user_details'])} total)\n\n"
            f"Continue adding `/detail` or `/insight`, or use `/end` when done.",
            parse_mode="Markdown",
        )
        return PODCAST_INTERACTIVE

    async def podcast_insight_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle /insight command in interactive mode (for podcast session)."""
        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session or session.get("mode") != "interactive":
            # Not in podcast session, use regular insight command
            return await self._regular_insight_command(update, context)

        if not context.args:
            await update.message.reply_text("Please provide the insight text.\nUsage: `/insight <your insight>`", parse_mode="Markdown")
            return PODCAST_INTERACTIVE

        insight = " ".join(context.args)
        session["user_insights"].append(insight)

        await update.message.reply_text(
            f"💡 Insight added ({len(session['user_insights'])} total)\n\n"
            f"Continue adding `/detail` or `/insight`, or use `/end` when done.",
            parse_mode="Markdown",
        )
        return PODCAST_INTERACTIVE

    async def podcast_end_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle /end command to finish interactive mode."""
        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session or session.get("mode") != "interactive":
            await update.message.reply_text("No active podcast session.")
            return ConversationHandler.END

        if not session["user_details"] and not session["user_insights"]:
            await update.message.reply_text(
                "You haven't added any details or insights yet.\n"
                "Add some with `/detail` or `/insight`, then use `/end`.",
                parse_mode="Markdown",
            )
            return PODCAST_INTERACTIVE

        # Check if transcription is complete
        if not session.get("transcription_complete"):
            await update.message.reply_text(
                "⏳ **Waiting for transcription to complete...**\n\n"
                f"You have {len(session['user_details'])} details and {len(session['user_insights'])} insights ready.\n\n"
                "_I'll generate the summary as soon as transcription finishes._",
                parse_mode="Markdown",
            )
            # Wait for transcription in a loop
            while not session.get("transcription_complete"):
                await asyncio.sleep(2)

        # Check if there was an error
        if session.get("transcription_error"):
            await update.message.reply_text(
                f"❌ Transcription failed: {session['transcription_error']}\n\nPlease try again with /podcast"
            )
            return ConversationHandler.END

        await update.message.reply_text("🤖 Generating summary with your insights... Please wait.")

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            email_content = await summarizer.generate_podcast_email(
                transcript=session["transcript"],
                metadata=session["metadata"],
                user_details=session["user_details"],
                user_insights=session["user_insights"],
                learned_preferences=self.learning.get_prompt_context(),
            )

            session["draft_email"] = email_content

            # Send for review
            keyboard = [
                [InlineKeyboardButton("✅ Approve & Save", callback_data="podcast_approve")],
                [InlineKeyboardButton("✏️ Provide Feedback", callback_data="podcast_feedback")],
                [InlineKeyboardButton("❌ Cancel", callback_data="podcast_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            full_text = f"📧 **Draft Summary:**\n\n{email_content}\n\n─────────────────\nDoes this look good?"
            await self._reply_long_message(update, full_text, reply_markup=reply_markup)
            return PODCAST_REVIEW

        except Exception as e:
            logger.exception("Error generating summary")
            await update.message.reply_text(f"❌ Error: {sanitize_error_message(e)}")
            return ConversationHandler.END

    async def podcast_review_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle approve/feedback callbacks for podcast review (within ConversationHandler)."""
        return await self._handle_podcast_review(update, context, in_conversation=True)

    async def podcast_review_standalone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle approve/feedback callbacks for podcast review (standalone, outside ConversationHandler)."""
        await self._handle_podcast_review(update, context, in_conversation=False)

    async def _handle_podcast_review(self, update: Update, context: ContextTypes.DEFAULT_TYPE, in_conversation: bool = False) -> int:
        """Handle approve/feedback callbacks for podcast review."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session:
            await query.edit_message_text("Session expired. Please start again with /podcast")
            return ConversationHandler.END if in_conversation else None

        if query.data == "podcast_approve":
            # Record approval for learning (check if this was first try)
            was_edited = session.get('edit_count', 0) > 0
            self.learning.record_feedback(
                podcast_title=session['metadata'].title,
                feedback_text="approved" if was_edited else "approved_first_try",
                feedback_type="approve",
            )

            # Save to summary storage
            await query.edit_message_text("💾 Saving...")

            try:
                # Save to summary storage (JSON)
                summary_id = self.summary_storage.save_summary(
                    title=session['metadata'].title,
                    email_content=session['draft_email'],
                    transcript=session['transcript'],
                    show=session['metadata'].show_name,
                    url=session['metadata'].url,
                    duration=session.get('duration_str'),
                )

                # Also save to Obsidian vault as markdown backup
                try:
                    self.vault.save_podcast_email(
                        metadata=session['metadata'],
                        email_content=session['draft_email'],
                        transcript=session['transcript'],
                    )
                    logger.info(f"Saved markdown backup to vault: {session['metadata'].title}")
                except Exception as vault_err:
                    logger.warning(f"Failed to save vault backup (non-critical): {vault_err}")

                # Store the saved ID for potential editing
                if not hasattr(self, '_saved_summaries'):
                    self._saved_summaries = {}
                self._saved_summaries[user_id] = {
                    'id': summary_id,
                    'title': session['metadata'].title,
                    'show': session['metadata'].show_name,
                }

                # Categorize the summary
                folder_msg = await self._categorize_saved_summary(
                    summary_id=summary_id,
                    title=session['metadata'].title,
                    show_name=session['metadata'].show_name,
                    summary_text=session['draft_email'],
                )

                # Update the saving message to show success
                await query.edit_message_text("✅ **Saved!**")

                # Show options in a separate message
                keyboard = [
                    [InlineKeyboardButton("✏️ Edit", callback_data="saved_edit")],
                    [InlineKeyboardButton("📧 Send as Email", callback_data="saved_email")],
                    [InlineKeyboardButton("✅ Done", callback_data="saved_done")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                done_text = f"{folder_msg}\n\n" if folder_msg else ""
                done_text += "**What's next?**\n\nUse `/lookup` anytime to view and edit your saved summaries."

                await query.message.reply_text(
                    done_text,
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )

                # Clean up session
                del self.podcast_sessions[user_id]
                return ConversationHandler.END if in_conversation else None

            except Exception as e:
                logger.exception("Error saving podcast")
                await query.message.reply_text(f"❌ Error saving: {sanitize_error_message(e)}")
                return ConversationHandler.END if in_conversation else None

        elif query.data == "podcast_feedback":
            # Store that we're waiting for feedback
            if not hasattr(self, '_feedback_state'):
                self._feedback_state = {}
            self._feedback_state[user_id] = True

            await query.edit_message_text(
                "📝 Please type your feedback or changes you'd like made.\n\n"
                "For example: \"Make it more concise\" or \"Add more emphasis on the mindset section\"",
            )
            return PODCAST_REVIEW if in_conversation else None

        elif query.data == "podcast_cancel":
            # Cancel the podcast session
            if user_id in self.podcast_sessions:
                del self.podcast_sessions[user_id]
            if hasattr(self, '_feedback_state') and user_id in self._feedback_state:
                del self._feedback_state[user_id]
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Podcast session cancelled.")
            return ConversationHandler.END if in_conversation else None

        return ConversationHandler.END if in_conversation else None

    async def podcast_feedback_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle feedback text for regenerating podcast summary."""
        user_id = update.effective_user.id
        session = self.podcast_sessions.get(user_id)

        if not session:
            await update.message.reply_text("Session expired. Please start again with /podcast")
            return ConversationHandler.END

        feedback = update.message.text
        await update.message.reply_text("🔄 Regenerating with your feedback...")

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            # Record feedback for learning
            self.learning.record_feedback(
                podcast_title=session["metadata"].title,
                feedback_text=feedback,
                feedback_type="edit",
            )

            # Track edit count for this session
            session['edit_count'] = session.get('edit_count', 0) + 1

            email_content = await summarizer.generate_podcast_email(
                transcript=session["transcript"],
                metadata=session["metadata"],
                user_details=session["user_details"],
                user_insights=session["user_insights"],
                feedback=feedback,
                previous_draft=session["draft_email"],
                learned_preferences=self.learning.get_prompt_context(),
            )

            session["draft_email"] = email_content

            # Send for review again
            keyboard = [
                [InlineKeyboardButton("✅ Approve & Save", callback_data="podcast_approve")],
                [InlineKeyboardButton("✏️ More Feedback", callback_data="podcast_feedback")],
                [InlineKeyboardButton("❌ Cancel", callback_data="podcast_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            full_text = f"📧 **Updated Draft:**\n\n{email_content}\n\n─────────────────\nDoes this look good now?"
            await self._reply_long_message(update, full_text, reply_markup=reply_markup)
            return PODCAST_REVIEW

        except Exception as e:
            logger.exception("Error regenerating summary")
            await update.message.reply_text(f"❌ Error: {sanitize_error_message(e)}")
            return ConversationHandler.END

    async def _save_podcast_to_vault(self, session: dict) -> str:
        """Save the approved podcast summary to Obsidian vault."""
        metadata = session["metadata"]

        # Save the email-style content to vault
        vault_path = self.vault.save_podcast_email(
            metadata=metadata,
            email_content=session["draft_email"],
            transcript=session["transcript"],
        )

        # Also add to scratchpad
        self.vault.save_content_to_scratchpad(
            content_type="podcast",
            title=metadata.title,
            summary=session["draft_email"][:500] + "...",
            vault_path=vault_path,
        )

        return vault_path

    async def _categorize_saved_summary(
        self,
        summary_id: str,
        title: str,
        show_name: Optional[str],
        summary_text: str,
    ) -> str:
        """Categorize a newly saved summary. Returns a status message."""
        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            folder_tree = self.category_storage.list_tree()
            result = await summarizer.categorize_summary(
                title=title,
                show_name=show_name,
                summary_text=summary_text,
                folder_tree=folder_tree,
            )

            folder_path = result.get("folder_path", [])
            create_new = result.get("create_new", False)

            if not folder_path:
                return ""

            # Find or create the folder
            category_id = self._resolve_folder_path(folder_path, result)

            if category_id:
                self.category_storage.add_summary(summary_id, category_id)
                # Update summary's categories list
                cats = self.category_storage.get_categories_for_summary(summary_id)
                self.summary_storage.update_categories(summary_id, [c.id for c in cats])

                cat = self.category_storage.get_category(category_id)
                emoji = cat.emoji if cat else ""
                folder_name = cat.name if cat else folder_path[-1]
                folder_msg = f"{emoji} Filed in: **{folder_name}**"
            else:
                folder_msg = ""

            # Check if we should trigger reorganization (every 5 saves)
            save_count = self.category_storage.increment_save_count()
            if save_count % 5 == 0 and self.category_storage.total_categories() > 2:
                reorg_msg = await self._trigger_reorganization()
                if reorg_msg:
                    folder_msg += f"\n\n{reorg_msg}"

            # Export categories to vault as backup
            try:
                self.category_storage.export_to_markdown(self.config.obsidian.vault_path)
            except Exception as e:
                logger.warning(f"Failed to export categories backup: {e}")

            return folder_msg

        except Exception as e:
            logger.warning(f"Categorization failed (non-critical): {e}")
            return ""

    def _resolve_folder_path(self, folder_path: list[str], ai_result: dict) -> Optional[str]:
        """Resolve a folder path to a category ID, creating folders as needed."""
        if not folder_path:
            return None

        # Find or create parent folder
        parent_name = folder_path[0]
        parent_matches = self.category_storage.find_by_name(parent_name)
        parent_id = None

        # Look for exact match at root level
        for m in parent_matches:
            if m.parent_id is None and m.name.lower() == parent_name.lower():
                parent_id = m.id
                break

        if not parent_id:
            # Create the parent folder
            emoji = ai_result.get("emoji", "") if len(folder_path) == 1 else ""
            desc = ai_result.get("description", "") if len(folder_path) == 1 else ""
            parent_id = self.category_storage.create_category(
                name=parent_name,
                emoji=emoji,
                description=desc,
            )

        if len(folder_path) == 1:
            return parent_id

        # Find or create child folder
        child_name = folder_path[1]
        children = self.category_storage.get_children(parent_id)
        child_id = None

        for child in children:
            if child.name.lower() == child_name.lower():
                child_id = child.id
                break

        if not child_id:
            child_id = self.category_storage.create_category(
                name=child_name,
                emoji=ai_result.get("emoji", ""),
                description=ai_result.get("description", ""),
                parent_id=parent_id,
            )

        return child_id

    async def _trigger_reorganization(self) -> str:
        """Trigger AI-powered folder reorganization. Returns user-facing message."""
        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            folder_tree = self.category_storage.list_tree()

            # Build summary titles map
            summary_titles = {}
            for cat_data in folder_tree:
                cat = self.category_storage.get_category(cat_data["id"])
                if cat:
                    for sid in cat.summary_ids:
                        s = self.summary_storage.get_summary(sid)
                        if s:
                            label = s.title
                            if s.show:
                                label += f" ({s.show})"
                            summary_titles[sid] = label
                for child_data in cat_data.get("children", []):
                    child = self.category_storage.get_category(child_data["id"])
                    if child:
                        for sid in child.summary_ids:
                            s = self.summary_storage.get_summary(sid)
                            if s:
                                label = s.title
                                if s.show:
                                    label += f" ({s.show})"
                                summary_titles[sid] = label

            operations = await summarizer.reorganize_folders(folder_tree, summary_titles)

            if not operations:
                return ""

            changes = self.category_storage.apply_reorganization(operations)

            if changes:
                changes_text = "\n".join(f"  • {c}" for c in changes[:5])
                return f"🔄 **Reorganized folders:**\n{changes_text}"

            return ""

        except Exception as e:
            logger.warning(f"Reorganization failed (non-critical): {e}")
            return ""

    async def podcast_cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Cancel the podcast conversation."""
        user_id = update.effective_user.id
        if user_id in self.podcast_sessions:
            del self.podcast_sessions[user_id]
        await update.message.reply_text("❌ Podcast session cancelled.")
        return ConversationHandler.END

    async def cancel_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /cancel command - cancel any active operation."""
        if not self._check_access(update):
            return

        user_id = update.effective_user.id
        cancelled = []

        # Clear podcast session
        if user_id in self.podcast_sessions:
            del self.podcast_sessions[user_id]
            cancelled.append("Podcast processing")

        # Clear feedback state
        if hasattr(self, '_feedback_state') and user_id in self._feedback_state:
            del self._feedback_state[user_id]
            cancelled.append("Feedback mode")

        # Clear edit state
        if hasattr(self, '_edit_state') and user_id in self._edit_state:
            del self._edit_state[user_id]
            cancelled.append("Edit mode")

        # Clear email state
        if hasattr(self, '_email_state') and user_id in self._email_state:
            del self._email_state[user_id]
            cancelled.append("Email mode")

        # Clear lookup state
        if hasattr(self, '_lookup_state') and user_id in self._lookup_state:
            del self._lookup_state[user_id]
            cancelled.append("Lookup mode")

        # Clear folder action state
        if hasattr(self, '_folder_action_state') and user_id in self._folder_action_state:
            del self._folder_action_state[user_id]
            cancelled.append("Folder action mode")

        if cancelled:
            await update.message.reply_text(f"❌ Cancelled: {', '.join(cancelled)}")
        else:
            await update.message.reply_text("Nothing to cancel.")

    async def podcast_timeout(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """Handle conversation timeout - clean up stale sessions."""
        user_id = update.effective_user.id if update.effective_user else None
        if user_id and user_id in self.podcast_sessions:
            del self.podcast_sessions[user_id]
        logger.info(f"Podcast conversation timed out for user {user_id}")
        return ConversationHandler.END

    async def article_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /article command."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        if not context.args:
            await update.message.reply_text(
                "Please provide an article URL.\n" "Usage: `/article <url>`",
                parse_mode="Markdown",
            )
            return

        url = context.args[0]

        # Validate URL for SSRF protection
        is_valid, error = validate_url(url)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid URL: {error}")
            return

        await update.message.reply_text("📰 Extracting article...")

        try:
            result = await self.article_processor.process(url)
            await update.message.reply_text(
                f"✅ Article saved!\n\n"
                f"**{result.title}**\n"
                f"Author: {result.author or 'Unknown'}\n"
                f"Saved to: `{result.vault_path}`\n\n"
                f"Summary:\n{result.summary[:500]}...",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Error processing article")
            await update.message.reply_text(f"❌ Error processing article: {sanitize_error_message(e)}")

    async def thread_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /thread command."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        if not context.args:
            await update.message.reply_text(
                "Please provide an X/Twitter thread URL.\n" "Usage: `/thread <url>`",
                parse_mode="Markdown",
            )
            return

        url = context.args[0]

        # Validate URL for SSRF protection
        is_valid, error = validate_url(url)
        if not is_valid:
            await update.message.reply_text(f"❌ Invalid URL: {error}")
            return

        await update.message.reply_text("🧵 Capturing thread...")

        try:
            result = await self.thread_processor.process(url)
            await update.message.reply_text(
                f"✅ Thread saved!\n\n"
                f"**@{result.author}**\n"
                f"Tweets: {result.tweet_count}\n"
                f"Saved to: `{result.vault_path}`\n\n"
                f"Summary:\n{result.summary[:500]}...",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Error processing thread")
            await update.message.reply_text(f"❌ Error processing thread: {sanitize_error_message(e)}")


    def _escape_markdown(self, text: str) -> str:
        """Escape special Markdown characters for Telegram."""
        # Characters that need escaping in Telegram Markdown
        special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
        for char in special_chars:
            text = text.replace(char, f'\\{char}')
        return text

    def _calculate_eta(self, item: dict) -> str:
        """Calculate estimated time remaining until email is ready."""
        import time

        status = item['status']

        if status == 'downloading':
            return "downloading..."
        elif status == 'complete':
            return ""
        elif status == 'error':
            return "failed"
        elif status == 'summarizing':
            return "~30s"
        elif status != 'transcribing':
            return ""

        started_at = item.get('started_at')
        duration_seconds = item.get('duration_seconds')

        if not started_at or not duration_seconds:
            return "calculating..."

        elapsed = time.time() - started_at

        # Check if using cloud transcription (much faster)
        if self.config.whisper.mode == "cloud":
            # Cloud: ~1 min per 30 min of audio + 30s for summary
            transcription_total = (duration_seconds / 30) * 60  # ~2x faster than realtime
            remaining_transcription = max(0, transcription_total - elapsed)
            remaining_total = remaining_transcription + 30
        else:
            # Local: ~0.8x realtime on CPU with base model
            transcription_total = duration_seconds * 0.8
            remaining_transcription = max(0, transcription_total - elapsed)
            remaining_total = remaining_transcription + 30

        if remaining_total < 60:
            return f"~{int(remaining_total)}s"
        elif remaining_total < 3600:
            return f"~{int(remaining_total / 60)}m"
        else:
            hours = int(remaining_total / 3600)
            mins = int((remaining_total % 3600) / 60)
            return f"~{hours}h {mins}m"

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /status command - always works, even in sleep mode."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        # Power state
        power_status = "🟢 AWAKE" if self.is_powered_on else "🔴 SLEEP MODE"

        # Get processing queue status
        podcast_queue = self.podcast_processor.get_queue_status()

        # Get content counts
        total_items = self.vector_store.count()

        # Get active sessions
        active_sessions = len(self.podcast_sessions)

        # Count saved summaries
        summary_count = len(self.summary_storage.list_summaries(limit=1000))

        status_msg = f"📊 Bot Status: {power_status}\n\n"
        status_msg += f"Saved summaries: {summary_count}\n"
        status_msg += f"Active sessions: {active_sessions}\n"

        if podcast_queue:
            status_msg += "\nProcessing Queue:\n"
            for item in podcast_queue:
                title = item['title'][:35] + "..." if len(item['title']) > 35 else item['title']
                eta = self._calculate_eta(item)
                if eta:
                    status_msg += f"• {title}\n  ⏳ {item['status']} ({eta})\n"
                else:
                    status_msg += f"• {title}: {item['status']}\n"

        if self.is_powered_on:
            status_msg += "\nCommands: /podcast, /lookup, /stop, /poweroff"
        else:
            status_msg += "\nUse /poweron to wake the bot."

        await update.message.reply_text(status_msg)

    async def stop_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /stop command - cancel all active processes for this user."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        user_id = update.effective_user.id
        cancelled = []

        # Clear podcast session
        if user_id in self.podcast_sessions:
            session = self.podcast_sessions[user_id]
            title = session.get("metadata", {})
            if hasattr(title, "title"):
                title = title.title
            else:
                title = session.get("url", "Unknown")[:30]
            del self.podcast_sessions[user_id]
            cancelled.append(f"Podcast session: {title}")

        # Clear feedback state
        if hasattr(self, '_feedback_state') and user_id in self._feedback_state:
            del self._feedback_state[user_id]
            cancelled.append("Feedback input mode")

        # Clear edit state
        if hasattr(self, '_edit_state') and user_id in self._edit_state:
            del self._edit_state[user_id]
            cancelled.append("Edit mode")

        # Clear email state
        if hasattr(self, '_email_state') and user_id in self._email_state:
            del self._email_state[user_id]
            cancelled.append("Email input mode")

        # Clear lookup state
        if hasattr(self, '_lookup_state') and user_id in self._lookup_state:
            del self._lookup_state[user_id]
            cancelled.append("Lookup mode")

        # Clear folder action state
        if hasattr(self, '_folder_action_state') and user_id in self._folder_action_state:
            del self._folder_action_state[user_id]
            cancelled.append("Folder action mode")

        # Clear saved summaries selection
        if hasattr(self, '_saved_summaries') and user_id in self._saved_summaries:
            del self._saved_summaries[user_id]

        # Clear items from processor queue for this user (if trackable)
        # Note: We can't easily track which queue items belong to which user
        # but clearing the session should stop new callbacks from being processed

        if cancelled:
            msg = "🛑 Stopped:\n" + "\n".join(f"• {item}" for item in cancelled)
            msg += "\n\nYou can start fresh with /podcast"
        else:
            msg = "✓ No active processes to stop.\n\nUse /podcast to start a new session."

        await update.message.reply_text(msg)

    async def poweron_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /poweron command - wake the bot from sleep mode."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        if self.is_powered_on:
            await update.message.reply_text(
                "🟢 Bot is already awake!\n\n"
                "Use /podcast <url> to process a podcast.\n"
                "Use /poweroff when done to save resources."
            )
        else:
            self.is_powered_on = True
            await update.message.reply_text(
                "🟢 Bot is now awake!\n\n"
                "Use /podcast <url> to process a podcast.\n"
                "Use /poweroff when done to save resources."
            )

    async def poweroff_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /poweroff command - put bot in sleep mode to save resources."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        user_id = update.effective_user.id

        # Clear any active sessions for this user first
        if user_id in self.podcast_sessions:
            del self.podcast_sessions[user_id]

        # Unload heavy resources
        self.podcast_processor.unload_whisper_model()

        if not self.is_powered_on:
            await update.message.reply_text(
                "🔴 Bot is already in sleep mode.\n\n"
                "Use /poweron to wake it up."
            )
        else:
            self.is_powered_on = False
            await update.message.reply_text(
                "🔴 Bot is now in sleep mode (saving resources).\n\n"
                "Only /poweron and /status will work.\n"
                "Use /poweron when you're ready to process podcasts."
            )

    async def digest_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /digest command to manually trigger digest."""
        if not self._check_access(update):
            await self._deny_access(update)
            return

        await update.message.reply_text("📚 Generating daily digest...")

        try:
            vault_path = await self.digest_scheduler.trigger_now()
            if vault_path:
                await update.message.reply_text(
                    f"✅ Daily digest generated!\n"
                    f"Saved to: `{vault_path}`",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    "No content was processed today. Nothing to digest!"
                )
        except Exception as e:
            logger.exception("Error generating digest")
            await update.message.reply_text(f"❌ Error generating digest: {sanitize_error_message(e)}")

    async def lookup_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /lookup command - show folder tree + recent summaries."""
        # Use effective_message to work from both commands and callbacks
        message = update.effective_message
        if not message:
            return

        if not self._check_access(update):
            return
        if not self.is_powered_on:
            await message.reply_text("🔴 Bot is in sleep mode. Use /poweron to wake it up.")
            return

        user_id = update.effective_user.id
        summaries = self.summary_storage.list_summaries(limit=5)

        if not summaries and self.category_storage.total_categories() == 0:
            await message.reply_text(
                "📂 No podcast summaries found yet.\n\n"
                "Process some podcasts with /podcast first!"
            )
            return

        # Build the library view
        msg = "📚 **Your Podcast Library**\n\n"

        # Show folder tree
        tree_display = self.category_storage.format_tree_display()
        if tree_display:
            msg += tree_display + "\n\n"

        # Show uncategorized count
        all_ids = self.summary_storage.list_all_ids()
        uncategorized = self.category_storage.get_uncategorized_summaries(all_ids)
        if uncategorized:
            msg += f"📋 Uncategorized ({len(uncategorized)})\n\n"

        # Show recent summaries
        if summaries:
            msg += "─────────────────\n"
            msg += "📋 **Recent:**\n"
            for i, item in enumerate(summaries, 1):
                show_info = f" — {item.show}" if item.show else ""
                date_str = ""
                if item.created_at:
                    try:
                        from datetime import datetime as dt
                        d = dt.fromisoformat(item.created_at)
                        date_str = f" ({d.strftime('%b %d')})"
                    except (ValueError, TypeError):
                        pass
                msg += f"  {i}. {item.title}{show_info}{date_str}\n"

        msg += "\n─────────────────\n"
        msg += "Reply with:\n"
        msg += "• A **number** to view a recent summary\n"
        msg += "• A **folder name** to browse that folder\n"
        msg += "• A **question** to search your library"

        # Store lookup state
        if not hasattr(self, '_lookup_state'):
            self._lookup_state = {}
        self._lookup_state[user_id] = {
            "mode": "library",
            "recent": summaries,
            "page": 0,
        }

        await message.reply_text(msg, parse_mode="Markdown")

    async def lookup_selection(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle selection from lookup - number, folder name, or search query."""
        if not self._check_access(update):
            return

        user_id = update.effective_user.id
        if not hasattr(self, '_lookup_state') or user_id not in self._lookup_state:
            return

        text = update.message.text.strip()
        state = self._lookup_state[user_id]

        # Check if it's a number (recent summary selection or folder item)
        try:
            selection = int(text)
            if state.get("mode") == "library":
                # Select from recent list
                summaries = state.get("recent", [])
                if 1 <= selection <= len(summaries):
                    await self._show_summary_detail(update, summaries[selection - 1])
                    return
                else:
                    await update.message.reply_text(f"Please enter a number between 1 and {len(summaries)}")
                    return
            elif state.get("mode") == "folder":
                # Select from folder item list
                items = state.get("items", [])
                if 1 <= selection <= len(items):
                    item = items[selection - 1]
                    summary = self.summary_storage.get_summary(item["id"])
                    if summary:
                        await self._show_summary_detail(update, summary)
                    return
                else:
                    await update.message.reply_text(f"Please enter a number between 1 and {len(items)}")
                    return
        except ValueError:
            pass

        # Check if it matches a folder name
        folder_matches = self.category_storage.find_by_name(text)
        if folder_matches:
            # Show the first matching folder
            await self._show_folder_contents(update, folder_matches[0].id)
            return

        # Otherwise, treat as a search query
        await self._handle_search(update, text)

    async def _show_summary_detail(self, update: Update, item) -> None:
        """Show a single summary with action buttons."""
        user_id = update.effective_user.id

        if not hasattr(self, '_saved_summaries'):
            self._saved_summaries = {}
        self._saved_summaries[user_id] = {
            'id': item.id,
            'title': item.title,
            'show': item.show,
            'transcript': item.transcript,
        }

        # Get categories for this summary
        cats = self.category_storage.get_categories_for_summary(item.id)
        cat_info = ""
        if cats:
            cat_names = [f"{c.emoji} {c.name}" for c in cats]
            cat_info = f"📁 {', '.join(cat_names)}\n"

        keyboard = [
            [InlineKeyboardButton("✏️ Edit", callback_data="saved_edit")],
            [InlineKeyboardButton("📧 Send as Email", callback_data="saved_email")],
            [InlineKeyboardButton("📁 Move to Folder", callback_data="saved_move")],
            [InlineKeyboardButton("🗑️ Delete", callback_data="saved_delete")],
            [InlineKeyboardButton("📚 Back to Library", callback_data="saved_back")],
            [InlineKeyboardButton("✅ Done", callback_data="saved_done")],
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        show_info = f" from **{item.show}**" if item.show else ""
        date_str = item.created_at[:10] if item.created_at else ""
        full_text = (
            f"📧 **{item.title}**{show_info}\n"
            f"📅 {date_str}\n"
            f"{cat_info}"
            f"\n─────────────────\n\n"
            f"{item.email_content}"
        )
        await self._reply_long_message(update, full_text, reply_markup=reply_markup)

    async def _show_folder_contents(self, update: Update, category_id: str, page: int = 0) -> None:
        """Show contents of a folder with pagination."""
        user_id = update.effective_user.id
        cat = self.category_storage.get_category(category_id)
        if not cat:
            await update.message.reply_text("Folder not found.")
            return

        msg = f"{cat.emoji} **{cat.name}**\n\n"

        # Show sub-folders first
        children = self.category_storage.get_children(category_id)
        if children:
            for child in children:
                count = len(child.summary_ids)
                msg += f"  {child.emoji} {child.name} ({count})\n"
            msg += "\n"

        # Show summaries in this folder with pagination
        items_per_page = 10
        start = page * items_per_page
        summary_items = []

        for sid in cat.summary_ids:
            s = self.summary_storage.get_summary(sid)
            if s:
                summary_items.append({"id": s.id, "title": s.title, "show": s.show})

        total = len(summary_items)
        page_items = summary_items[start:start + items_per_page]

        if page_items:
            for i, item in enumerate(page_items, start + 1):
                show_info = f" — {item['show']}" if item.get('show') else ""
                msg += f"  {i}. {item['title']}{show_info}\n"

            if total > items_per_page:
                msg += f"\nShowing {start + 1}-{min(start + items_per_page, total)} of {total}\n"
        else:
            msg += "  _(empty folder)_\n"

        msg += "\n─────────────────\n"

        # Build navigation buttons
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"folder_page_{category_id}_{page-1}"))
        if start + items_per_page < total:
            nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"folder_page_{category_id}_{page+1}"))

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([
            InlineKeyboardButton("➕ New Sub-folder", callback_data=f"folder_new_{category_id}"),
            InlineKeyboardButton("✏️ Rename", callback_data=f"folder_rename_{category_id}"),
        ])
        keyboard.append([
            InlineKeyboardButton("📦 Move Folder", callback_data=f"folder_move_{category_id}"),
            InlineKeyboardButton("🗑️ Delete Folder", callback_data=f"folder_delete_{category_id}"),
        ])
        keyboard.append([InlineKeyboardButton("📚 Back to Library", callback_data="saved_back")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        # Store folder browse state
        if not hasattr(self, '_lookup_state'):
            self._lookup_state = {}
        self._lookup_state[user_id] = {
            "mode": "folder",
            "category_id": category_id,
            "items": page_items,
            "page": page,
        }

        msg += "Reply with a **number** to view, or a **folder name** to browse."
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

    async def _handle_search(self, update: Update, query: str) -> None:
        """Handle search query - try substring match first, then AI semantic search."""
        user_id = update.effective_user.id

        # First: try simple substring match on titles and show names
        all_summaries = self.summary_storage.list_summaries(limit=100)
        query_lower = query.lower()

        simple_matches = []
        for s in all_summaries:
            if query_lower in s.title.lower() or (s.show and query_lower in s.show.lower()):
                simple_matches.append(s)

        if simple_matches:
            msg = f"🔍 **Found {len(simple_matches)} match{'es' if len(simple_matches) != 1 else ''}:**\n\n"
            display = simple_matches[:5]
            for i, item in enumerate(display, 1):
                show_info = f" — {item.show}" if item.show else ""
                msg += f"  {i}. {item.title}{show_info}\n"

            if not hasattr(self, '_lookup_state'):
                self._lookup_state = {}
            self._lookup_state[user_id] = {
                "mode": "folder",
                "items": [{"id": s.id, "title": s.title, "show": s.show} for s in display],
                "page": 0,
            }

            msg += "\nReply with a number to view."
            await update.message.reply_text(msg, parse_mode="Markdown")
            return

        # No simple match: try semantic search via AI
        if not all_summaries:
            await update.message.reply_text("🔍 No results found.")
            return

        await update.message.reply_text("🔍 Searching...")

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            summary_list = [
                {
                    "id": s.id,
                    "title": s.title,
                    "show": s.show or "",
                    "preview": s.email_content[:150] if s.email_content else "",
                }
                for s in all_summaries[:40]
            ]

            results = await summarizer.search_summaries(query, summary_list)

            if not results:
                await update.message.reply_text("🔍 No relevant results found for your query.")
                return

            msg = f"🔍 **Found {len(results)} match{'es' if len(results) != 1 else ''}:**\n\n"
            items_for_state = []
            for i, r in enumerate(results[:5], 1):
                relevance = "★" * min(r.get("relevance", 3), 5)
                title = r.get("title", "Unknown")
                reason = r.get("reason", "")
                msg += f"  {i}. {title}\n     {relevance} _{reason}_\n\n"
                items_for_state.append({"id": r["id"], "title": title, "show": ""})

            if not hasattr(self, '_lookup_state'):
                self._lookup_state = {}
            self._lookup_state[user_id] = {
                "mode": "folder",
                "items": items_for_state,
                "page": 0,
            }

            msg += "Reply with a number to view."
            await update.message.reply_text(msg, parse_mode="Markdown")

        except Exception as e:
            logger.warning(f"Search failed: {e}")
            await update.message.reply_text("🔍 Search failed. Try a different query.")

    async def saved_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle actions on saved summaries (edit, email, back, done)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        saved_info = self._saved_summaries.get(user_id) if hasattr(self, '_saved_summaries') else None

        if query.data == "saved_done":
            # Clean up and dismiss
            if hasattr(self, '_lookup_state') and user_id in self._lookup_state:
                del self._lookup_state[user_id]
            if hasattr(self, '_saved_summaries') and user_id in self._saved_summaries:
                del self._saved_summaries[user_id]
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("👍 All done!")

        elif query.data == "saved_back":
            # Go back to lookup list
            if hasattr(self, '_saved_summaries') and user_id in self._saved_summaries:
                del self._saved_summaries[user_id]
            await query.edit_message_reply_markup(reply_markup=None)
            # Trigger lookup again
            await self.lookup_command(update, context)

        elif query.data == "saved_edit":
            if saved_info:
                # Store edit state for this user
                if not hasattr(self, '_edit_state'):
                    self._edit_state = {}
                self._edit_state[user_id] = saved_info

                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    f"✏️ **Edit Mode**\n\n"
                    f"Type your feedback to regenerate the email summary using AI.\n\n"
                    f"Examples:\n"
                    f"• \"Make it shorter\"\n"
                    f"• \"Add more emphasis on the key insights\"\n"
                    f"• \"Include more specific numbers/stats\"\n\n"
                    f"Or type `/cancel` to go back.",
                    parse_mode="Markdown",
                )
            else:
                await query.message.reply_text("❌ No summary selected.")

        elif query.data == "saved_delete":
            if saved_info:
                # Show confirmation
                keyboard = [
                    [InlineKeyboardButton("⚠️ Yes, Delete", callback_data="saved_delete_confirm")],
                    [InlineKeyboardButton("❌ Cancel", callback_data="saved_delete_cancel")],
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)

                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    f"🗑️ **Delete this summary?**\n\n"
                    f"**{saved_info['title']}**\n\n"
                    f"This will permanently remove the file from your vault.",
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )
            else:
                await query.message.reply_text("❌ No summary selected.")

        elif query.data == "saved_delete_confirm":
            if saved_info and 'id' in saved_info:
                # Delete from storage
                success = self.summary_storage.delete_summary(saved_info['id'])
                await query.edit_message_reply_markup(reply_markup=None)
                if success:
                    await query.message.reply_text(
                        f"✅ **Deleted:** {saved_info['title']}\n\n"
                        f"Use `/lookup` to view remaining summaries."
                    )
                else:
                    await query.message.reply_text("❌ Failed to delete.")

                # Clean up state
                if hasattr(self, '_saved_summaries') and user_id in self._saved_summaries:
                    del self._saved_summaries[user_id]
                if hasattr(self, '_lookup_state') and user_id in self._lookup_state:
                    del self._lookup_state[user_id]
            else:
                await query.message.reply_text("❌ No summary selected.")

        elif query.data == "saved_delete_cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Delete cancelled.")

        elif query.data == "saved_move":
            if saved_info:
                # Show folder picker for moving summary
                keyboard = []
                for cat in self.category_storage.get_flat_list():
                    indent = "  " if cat.parent_id else ""
                    keyboard.append([InlineKeyboardButton(
                        f"{indent}{cat.emoji} {cat.name}",
                        callback_data=f"summary_moveto_{saved_info['id']}_{cat.id}"
                    )])
                keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="summary_move_cancel")])

                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(
                    f"📁 **Move:** {saved_info['title']}\n\nSelect destination folder:",
                    parse_mode="Markdown",
                    reply_markup=reply_markup,
                )
            else:
                await query.message.reply_text("❌ No summary selected.")

        elif query.data == "saved_email":
            if not self.config.email.enabled:
                await query.message.reply_text(
                    "📧 Email is not configured.\n\n"
                    "To enable email, update `config.yaml` with your Resend API key."
                )
                return

            if saved_info:
                # Store that we're waiting for email address
                if not hasattr(self, '_email_state'):
                    self._email_state = {}
                self._email_state[user_id] = saved_info

                await query.edit_message_reply_markup(reply_markup=None)

                # Check if we have a saved default email
                default_email = self.learning.get_default_email()
                if default_email:
                    # Offer quick send to saved email
                    keyboard = [
                        [InlineKeyboardButton(f"📧 Send to {default_email}", callback_data="email_quick_send")],
                        [InlineKeyboardButton("📝 Use different email", callback_data="email_enter_new")],
                        [InlineKeyboardButton("❌ Cancel", callback_data="email_cancel")],
                    ]
                    reply_markup = InlineKeyboardMarkup(keyboard)
                    await query.message.reply_text(
                        "📧 **Send as Email**\n\n"
                        f"Send to your saved email?",
                        parse_mode="Markdown",
                        reply_markup=reply_markup,
                    )
                else:
                    # First time - ask for email with Resend explanation
                    resend_note = ""
                    if self.config.email.provider == "resend":
                        resend_note = (
                            "\n\n⚠️ **Resend free tier:** You can only send to the email "
                            "you used to sign up for Resend. This will be saved for future use."
                        )
                    await query.message.reply_text(
                        "📧 **Send as Email**\n\n"
                        f"Reply with your email address:{resend_note}\n\n"
                        "Or type `/cancel` to go back.",
                        parse_mode="Markdown",
                    )
            else:
                await query.message.reply_text("❌ No summary selected.")

        elif query.data == "email_quick_send":
            # Quick send to saved email
            default_email = self.learning.get_default_email()
            if not default_email:
                await query.message.reply_text("❌ No saved email found.")
                return

            saved_info = self._email_state.get(user_id) if hasattr(self, '_email_state') else None
            if not saved_info:
                await query.message.reply_text("❌ Session expired. Please try again.")
                return

            await query.edit_message_text("📧 Sending...")

            summary_obj = self.summary_storage.get_summary(saved_info['id'])
            if summary_obj:
                subject = f"Podcast Summary: {summary_obj.title}"
                success = await self._send_email(default_email, subject, summary_obj.email_content)
                if success:
                    await query.edit_message_text(f"✅ Email sent to {default_email}!")
                else:
                    await query.edit_message_text("❌ Failed to send email. Check your email configuration.")

            # Clean up state
            if hasattr(self, '_email_state') and user_id in self._email_state:
                del self._email_state[user_id]

        elif query.data == "email_enter_new":
            # User wants to enter a different email
            resend_note = ""
            if self.config.email.provider == "resend":
                resend_note = (
                    "\n\n⚠️ **Resend free tier:** You can only send to the email "
                    "you used to sign up for Resend."
                )
            await query.edit_message_text(
                "📧 **Send as Email**\n\n"
                f"Reply with your email address:{resend_note}\n\n"
                "Or type `/cancel` to go back.",
                parse_mode="Markdown",
            )

        elif query.data == "email_cancel":
            if hasattr(self, '_email_state') and user_id in self._email_state:
                del self._email_state[user_id]
            await query.edit_message_text("📧 Email cancelled.")

    def _markdown_to_html(self, text: str) -> str:
        """Convert markdown-style text to HTML for email formatting."""
        import html as html_lib

        # Escape HTML entities first
        text = html_lib.escape(text)

        # Convert markdown to HTML
        # Bold: **text** -> <strong>text</strong>
        text = re.sub(r'\*\*([^*]+)\*\*', r'<strong>\1</strong>', text)

        # Italic: _text_ -> <em>text</em> (but not in the middle of words)
        text = re.sub(r'(?<![a-zA-Z])_([^_]+)_(?![a-zA-Z])', r'<em>\1</em>', text)

        # Line breaks
        text = text.replace('\n\n', '</p><p>')
        text = text.replace('\n', '<br>')

        # Wrap in paragraphs and add email styling
        html = f"""
        <html>
        <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
        <p>{text}</p>
        </body>
        </html>
        """
        return html

    async def _send_email(self, to_email: str, subject: str, body: str) -> bool:
        """Send an email with the podcast summary.

        Supports two providers:
        - resend: Easy API-based email (recommended)
        - smtp: Traditional SMTP (Gmail, etc.)
        """
        if not self.config.email.enabled:
            return False

        try:
            # Convert markdown to HTML for better formatting
            html_body = self._markdown_to_html(body)

            if self.config.email.provider == "resend":
                return await self._send_email_resend(to_email, subject, body, html_body)
            else:
                return await self._send_email_smtp(to_email, subject, body, html_body)
        except Exception as e:
            logger.exception(f"Error sending email: {e}")
            return False

    async def _send_email_resend(self, to_email: str, subject: str, text_body: str, html_body: str) -> bool:
        """Send email via Resend API."""
        import resend

        resend.api_key = self.config.email.resend_api_key

        params = {
            "from": self.config.email.from_email,
            "to": [to_email],
            "subject": subject,
            "text": text_body,
            "html": html_body,
        }

        resend.Emails.send(params)
        return True

    async def _send_email_smtp(self, to_email: str, subject: str, text_body: str, html_body: str) -> bool:
        """Send email via SMTP."""
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = self.config.email.sender_email
        msg['To'] = to_email

        # Plain text version (fallback)
        text_part = MIMEText(text_body, 'plain')
        msg.attach(text_part)

        # HTML version (preferred)
        html_part = MIMEText(html_body, 'html')
        msg.attach(html_part)

        # Connect and send
        with smtplib.SMTP(self.config.email.smtp_server, self.config.email.smtp_port) as server:
            server.starttls()
            server.login(self.config.email.sender_email, self.config.email.sender_password)
            server.sendmail(self.config.email.sender_email, to_email, msg.as_string())

        return True

    async def _handle_podcast_feedback_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle podcast feedback input (standalone mode). Returns True if handled."""
        user_id = update.effective_user.id

        # Check if user is in feedback mode
        if not hasattr(self, '_feedback_state') or user_id not in self._feedback_state:
            return False

        # Check if they have an active podcast session
        session = self.podcast_sessions.get(user_id)
        if not session:
            del self._feedback_state[user_id]
            return False

        # Process the feedback
        feedback = update.message.text
        await update.message.reply_text("🔄 Regenerating with your feedback...")

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            # Record feedback for learning
            self.learning.record_feedback(
                podcast_title=session["metadata"].title,
                feedback_text=feedback,
                feedback_type="edit",
            )

            # Track edit count for this session
            session['edit_count'] = session.get('edit_count', 0) + 1

            email_content = await summarizer.generate_podcast_email(
                transcript=session["transcript"],
                metadata=session["metadata"],
                user_details=session.get("user_details", []),
                user_insights=session.get("user_insights", []),
                feedback=feedback,
                previous_draft=session.get("draft_email"),
                learned_preferences=self.learning.get_prompt_context(),
            )

            session["draft_email"] = email_content

            # Send for review again
            keyboard = [
                [InlineKeyboardButton("✅ Approve & Save", callback_data="podcast_approve")],
                [InlineKeyboardButton("✏️ More Feedback", callback_data="podcast_feedback")],
                [InlineKeyboardButton("❌ Cancel", callback_data="podcast_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            full_text = f"📧 **Updated Draft:**\n\n{email_content}\n\n─────────────────\nDoes this look good now?"
            await self._reply_long_message(update, full_text, reply_markup=reply_markup)

            # Clear feedback state
            del self._feedback_state[user_id]

        except Exception as e:
            logger.exception("Error regenerating summary")
            await update.message.reply_text(f"❌ Error: {sanitize_error_message(e)}")
            del self._feedback_state[user_id]

        return True

    async def _handle_edit_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle edit feedback input. Returns True if handled."""
        user_id = update.effective_user.id
        if not hasattr(self, '_edit_state') or user_id not in self._edit_state:
            return False

        text = update.message.text.strip()

        # Check for cancel
        if text.lower() == '/cancel':
            del self._edit_state[user_id]
            await update.message.reply_text("✏️ Edit cancelled.")
            return True

        edit_info = self._edit_state[user_id]

        # Get the current summary and transcript from storage
        summary_obj = self.summary_storage.get_summary(edit_info['id']) if 'id' in edit_info else None

        if not summary_obj:
            await update.message.reply_text("❌ Could not read the summary.")
            del self._edit_state[user_id]
            return True

        current_summary = summary_obj.email_content
        transcript = summary_obj.transcript

        await update.message.reply_text("🔄 Regenerating with your feedback...")

        try:
            from src.ai.summarizer import Summarizer
            summarizer = Summarizer(self.config)

            # Create a minimal metadata object
            from src.storage.vault import PodcastMetadata
            metadata = PodcastMetadata(
                title=edit_info['title'],
                show_name=edit_info.get('show'),
            )

            # Record feedback for learning
            self.learning.record_feedback(
                podcast_title=edit_info.get('title', 'Unknown'),
                feedback_text=text,
                feedback_type="edit",
            )

            # Regenerate with feedback
            new_summary = await summarizer.generate_podcast_email(
                transcript=transcript,
                metadata=metadata,
                user_details=[],
                user_insights=[],
                feedback=text,
                previous_draft=current_summary,
                learned_preferences=self.learning.get_prompt_context(),
            )

            # Show preview with approve/more feedback options
            keyboard = [
                [InlineKeyboardButton("✅ Save Changes", callback_data="edit_save")],
                [InlineKeyboardButton("✏️ More Feedback", callback_data="edit_more")],
                [InlineKeyboardButton("❌ Cancel", callback_data="edit_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)

            # Store the new summary for saving
            edit_info['new_summary'] = new_summary

            full_text = f"📧 **Updated Email:**\n\n{new_summary}\n\n─────────────────\nLook good?"
            await self._reply_long_message(update, full_text, reply_markup=reply_markup)

        except Exception as e:
            logger.exception("Error regenerating summary")
            await update.message.reply_text(f"❌ Error: {sanitize_error_message(e)}")
            del self._edit_state[user_id]

        return True

    async def edit_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle edit save/more feedback/cancel actions."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        edit_info = self._edit_state.get(user_id) if hasattr(self, '_edit_state') else None

        if query.data == "edit_save":
            if edit_info and 'new_summary' in edit_info and 'id' in edit_info:
                # Save the updated summary to storage
                success = self.summary_storage.update_summary(edit_info['id'], edit_info['new_summary'])
                await query.edit_message_reply_markup(reply_markup=None)
                if success:
                    await query.message.reply_text("✅ **Changes saved!**\n\nUse `/lookup` to view your updated summary.")
                else:
                    await query.message.reply_text("❌ Failed to save changes.")
                del self._edit_state[user_id]
            else:
                await query.message.reply_text("❌ No changes to save.")

        elif query.data == "edit_more":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "✏️ Type more feedback to further refine the email.\n\n"
                "Or type `/cancel` to discard changes.",
                parse_mode="Markdown",
            )

        elif query.data == "edit_cancel":
            if hasattr(self, '_edit_state') and user_id in self._edit_state:
                del self._edit_state[user_id]
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Changes discarded.")

    async def folder_action_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle folder management callbacks (new, rename, move, delete, page)."""
        query = update.callback_query
        await query.answer()

        user_id = update.effective_user.id
        data = query.data

        if data.startswith("folder_page_"):
            # Pagination: folder_page_{category_id}_{page}
            parts = data.replace("folder_page_", "").rsplit("_", 1)
            if len(parts) == 2:
                category_id, page = parts[0], int(parts[1])
                await query.edit_message_reply_markup(reply_markup=None)
                # Re-show folder with new page
                await self._show_folder_contents_via_query(query, category_id, page)

        elif data.startswith("folder_new_"):
            # Create sub-folder
            parent_id = data.replace("folder_new_", "")
            if not hasattr(self, '_folder_action_state'):
                self._folder_action_state = {}
            self._folder_action_state[user_id] = {
                "action": "new_subfolder",
                "parent_id": parent_id,
            }
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                "📁 **New Sub-folder**\n\n"
                "Type the folder name (with optional emoji prefix):\n"
                "Example: `🤖 AI & Machine Learning`\n\n"
                "Or type `/cancel` to go back.",
                parse_mode="Markdown",
            )

        elif data.startswith("folder_rename_"):
            category_id = data.replace("folder_rename_", "")
            cat = self.category_storage.get_category(category_id)
            if not cat:
                await query.message.reply_text("Folder not found.")
                return
            if not hasattr(self, '_folder_action_state'):
                self._folder_action_state = {}
            self._folder_action_state[user_id] = {
                "action": "rename",
                "category_id": category_id,
            }
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"✏️ **Rename Folder:** {cat.emoji} {cat.name}\n\n"
                "Type the new name (with optional emoji prefix):\n"
                "Example: `🎯 New Name`\n\n"
                "Or type `/cancel` to go back.",
                parse_mode="Markdown",
            )

        elif data.startswith("folder_move_"):
            category_id = data.replace("folder_move_", "")
            cat = self.category_storage.get_category(category_id)
            if not cat:
                await query.message.reply_text("Folder not found.")
                return

            # Show folder picker for re-parenting
            keyboard = []
            keyboard.append([InlineKeyboardButton("📚 Top Level (no parent)", callback_data=f"folder_moveto_{category_id}_none")])
            for root in self.category_storage.list_root_categories():
                if root.id != category_id:
                    keyboard.append([InlineKeyboardButton(
                        f"{root.emoji} {root.name}",
                        callback_data=f"folder_moveto_{category_id}_{root.id}"
                    )])
            keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="folder_move_cancel")])

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(
                f"📦 **Move Folder:** {cat.emoji} {cat.name}\n\n"
                "Select new parent:",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        elif data.startswith("folder_moveto_"):
            # folder_moveto_{category_id}_{new_parent_id_or_none}
            parts = data.replace("folder_moveto_", "").rsplit("_", 1)
            if len(parts) == 2:
                category_id = parts[0]
                new_parent = None if parts[1] == "none" else parts[1]
                success = self.category_storage.move_category(category_id, new_parent)
                await query.edit_message_reply_markup(reply_markup=None)
                if success:
                    await query.message.reply_text("✅ Folder moved! Use `/lookup` to see the updated library.")
                else:
                    await query.message.reply_text("❌ Could not move folder (check nesting constraints).")

        elif data == "folder_move_cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Move cancelled.")

        elif data.startswith("folder_delete_"):
            category_id = data.replace("folder_delete_", "")
            cat = self.category_storage.get_category(category_id)
            if not cat:
                await query.message.reply_text("Folder not found.")
                return

            count = len(cat.summary_ids)
            children_count = len(self.category_storage.get_children(category_id))

            keyboard = [
                [InlineKeyboardButton("⚠️ Yes, Delete Folder", callback_data=f"folder_delete_confirm_{category_id}")],
                [InlineKeyboardButton("❌ Cancel", callback_data="folder_delete_cancel")],
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_reply_markup(reply_markup=None)

            warning = ""
            if count > 0:
                parent_info = "parent folder" if cat.parent_id else "uncategorized"
                warning = f"\n\n⚠️ {count} podcast(s) will be moved to {parent_info}."
            if children_count > 0:
                warning += f"\n⚠️ {children_count} sub-folder(s) will be re-parented."

            await query.message.reply_text(
                f"🗑️ **Delete Folder:** {cat.emoji} {cat.name}?{warning}",
                parse_mode="Markdown",
                reply_markup=reply_markup,
            )

        elif data.startswith("folder_delete_confirm_"):
            category_id = data.replace("folder_delete_confirm_", "")
            cat = self.category_storage.get_category(category_id)
            name = f"{cat.emoji} {cat.name}" if cat else "folder"
            self.category_storage.delete_category(category_id)
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text(f"✅ Deleted {name}. Use `/lookup` to see updated library.")

        elif data == "folder_delete_cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Delete cancelled.")

        elif data.startswith("summary_moveto_"):
            # summary_moveto_{summary_id}_{category_id}
            parts = data.replace("summary_moveto_", "").split("_", 1)
            if len(parts) == 2:
                summary_id, target_cat_id = parts
                # Remove from all current categories
                current_cats = self.category_storage.get_categories_for_summary(summary_id)
                for c in current_cats:
                    self.category_storage.remove_summary(summary_id, c.id)
                # Add to new category
                self.category_storage.add_summary(summary_id, target_cat_id)
                # Update summary record
                new_cats = self.category_storage.get_categories_for_summary(summary_id)
                self.summary_storage.update_categories(summary_id, [c.id for c in new_cats])

                target = self.category_storage.get_category(target_cat_id)
                name = f"{target.emoji} {target.name}" if target else "folder"
                await query.edit_message_reply_markup(reply_markup=None)
                await query.message.reply_text(f"✅ Moved to {name}!")

        elif data == "summary_move_cancel":
            await query.edit_message_reply_markup(reply_markup=None)
            await query.message.reply_text("❌ Move cancelled.")

    async def _show_folder_contents_via_query(self, query, category_id: str, page: int = 0) -> None:
        """Show folder contents in response to a callback query."""
        cat = self.category_storage.get_category(category_id)
        if not cat:
            await query.message.reply_text("Folder not found.")
            return

        user_id = query.from_user.id
        msg = f"{cat.emoji} **{cat.name}**\n\n"

        children = self.category_storage.get_children(category_id)
        if children:
            for child in children:
                count = len(child.summary_ids)
                msg += f"  {child.emoji} {child.name} ({count})\n"
            msg += "\n"

        items_per_page = 10
        start = page * items_per_page
        summary_items = []
        for sid in cat.summary_ids:
            s = self.summary_storage.get_summary(sid)
            if s:
                summary_items.append({"id": s.id, "title": s.title, "show": s.show})

        total = len(summary_items)
        page_items = summary_items[start:start + items_per_page]

        if page_items:
            for i, item in enumerate(page_items, start + 1):
                show_info = f" — {item['show']}" if item.get('show') else ""
                msg += f"  {i}. {item['title']}{show_info}\n"
            if total > items_per_page:
                msg += f"\nShowing {start + 1}-{min(start + items_per_page, total)} of {total}\n"
        else:
            msg += "  _(empty folder)_\n"

        msg += "\n─────────────────\n"
        msg += "Reply with a **number** to view, or a **folder name** to browse."

        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"folder_page_{category_id}_{page-1}"))
        if start + items_per_page < total:
            nav_buttons.append(InlineKeyboardButton("➡️ Next", callback_data=f"folder_page_{category_id}_{page+1}"))

        keyboard = []
        if nav_buttons:
            keyboard.append(nav_buttons)
        keyboard.append([InlineKeyboardButton("📚 Back to Library", callback_data="saved_back")])

        reply_markup = InlineKeyboardMarkup(keyboard)

        if not hasattr(self, '_lookup_state'):
            self._lookup_state = {}
        self._lookup_state[user_id] = {
            "mode": "folder",
            "category_id": category_id,
            "items": page_items,
            "page": page,
        }

        await query.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)

    async def organize_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle /organize command - manually trigger AI reorganization."""
        if not await self._check_power_and_access(update):
            return

        if self.category_storage.total_categories() == 0:
            # If no categories exist, batch-categorize all existing summaries
            all_summaries = self.summary_storage.list_summaries(limit=100)
            if not all_summaries:
                await update.message.reply_text("No podcasts to organize yet!")
                return

            await update.message.reply_text(f"📂 Organizing {len(all_summaries)} podcasts into folders...")

            categorized = 0
            for s in all_summaries:
                result_msg = await self._categorize_saved_summary(
                    summary_id=s.id,
                    title=s.title,
                    show_name=s.show,
                    summary_text=s.email_content,
                )
                if result_msg:
                    categorized += 1

            tree = self.category_storage.format_tree_display()
            await update.message.reply_text(
                f"✅ **Organized {categorized} podcasts!**\n\n{tree}\n\n"
                "Use `/lookup` to browse your library.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text("🔄 Reorganizing folders...")
            reorg_msg = await self._trigger_reorganization()
            if reorg_msg:
                await update.message.reply_text(
                    f"{reorg_msg}\n\nUse `/lookup` to browse.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("✅ Your folder structure looks good — no changes needed.")

    async def _handle_folder_action_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle folder action text input (new subfolder name, rename). Returns True if handled."""
        user_id = update.effective_user.id
        if not hasattr(self, '_folder_action_state') or user_id not in self._folder_action_state:
            return False

        text = update.message.text.strip()

        if text.lower() == '/cancel':
            del self._folder_action_state[user_id]
            await update.message.reply_text("❌ Cancelled.")
            return True

        state = self._folder_action_state[user_id]

        # Parse emoji from input (first character if it's an emoji, or first word before space)
        emoji = ""
        name = text
        if text and not text[0].isalnum():
            # First char might be an emoji
            # Simple heuristic: if first char is non-ASCII, treat as emoji
            first_char = text[0]
            if ord(first_char) > 127:
                emoji = first_char
                name = text[1:].strip()
            # Check for multi-byte emoji (up to 2 chars)
            elif len(text) > 1 and ord(text[1]) > 127:
                emoji = text[:2]
                name = text[2:].strip()

        if not name:
            await update.message.reply_text("Please provide a valid name.")
            return True

        if state["action"] == "new_subfolder":
            parent_id = state["parent_id"]
            try:
                cat_id = self.category_storage.create_category(
                    name=name,
                    emoji=emoji,
                    parent_id=parent_id,
                )
                await update.message.reply_text(
                    f"✅ Created folder: {emoji} **{name}**\n\n"
                    "Use `/lookup` to browse.",
                    parse_mode="Markdown",
                )
            except ValueError as e:
                await update.message.reply_text(f"❌ {e}")

        elif state["action"] == "rename":
            category_id = state["category_id"]
            success = self.category_storage.rename_category(
                category_id, name, emoji if emoji else None
            )
            if success:
                await update.message.reply_text(
                    f"✅ Renamed to: {emoji} **{name}**\n\n"
                    "Use `/lookup` to browse.",
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text("❌ Failed to rename folder.")

        del self._folder_action_state[user_id]
        return True

    async def _handle_email_input(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        """Handle email address input. Returns True if handled."""
        user_id = update.effective_user.id
        if not hasattr(self, '_email_state') or user_id not in self._email_state:
            return False

        text = update.message.text.strip()

        # Check for cancel
        if text.lower() == '/cancel':
            del self._email_state[user_id]
            await update.message.reply_text("📧 Email cancelled.")
            return True

        # Validate email address
        email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
        if not re.match(email_pattern, text):
            await update.message.reply_text(
                "❌ Invalid email address. Please enter a valid email or `/cancel` to go back.",
                parse_mode="Markdown",
            )
            return True

        # Get the summary info
        saved_info = self._email_state[user_id]

        # Get summary from storage
        summary_obj = self.summary_storage.get_summary(saved_info['id']) if 'id' in saved_info else None
        if not summary_obj:
            await update.message.reply_text("❌ Could not read the summary.")
            del self._email_state[user_id]
            return True

        await update.message.reply_text("📧 Sending email...")

        # Send the email
        show_info = f" ({saved_info['show']})" if saved_info.get('show') else ""
        subject = f"Podcast Summary: {saved_info['title']}{show_info}"

        success = await self._send_email(text, subject, summary_obj.email_content)

        if success:
            # Save the email for future quick sends
            self.learning.set_default_email(text)
            await update.message.reply_text(
                f"✅ Email sent to `{text}`!\n\n"
                "_Your email has been saved for quick sending next time._",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ Failed to send email. Check your email configuration in `config.yaml`.",
                parse_mode="Markdown",
            )

        # Clean up
        del self._email_state[user_id]
        return True

    async def handle_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Handle text messages - for lookup selection and podcast links."""
        if not self._check_access(update):
            return

        message = update.message
        text = message.text or message.caption or ""

        # Check for podcast feedback input (standalone mode)
        if await self._handle_podcast_feedback_input(update, context):
            return

        # Check for edit input first
        if await self._handle_edit_input(update, context):
            return

        # Check for email input
        if await self._handle_email_input(update, context):
            return

        # Check for folder action input (new subfolder, rename)
        if await self._handle_folder_action_input(update, context):
            return

        # Check for lookup selection
        user_id = update.effective_user.id
        if hasattr(self, '_lookup_state') and user_id in self._lookup_state:
            await self.lookup_selection(update, context)
            return

        # Check for podcast URLs in the message
        url_pattern = r"https?://[^\s]+"
        urls = re.findall(url_pattern, text)

        if urls:
            url = urls[0]

            # Validate URL for SSRF protection
            is_valid, error = validate_url(url)
            if not is_valid:
                await update.message.reply_text(f"❌ Invalid URL: {error}")
                return

            # Only process podcast links
            if "spotify.com" in url or "podcasts.apple.com" in url:
                await update.message.reply_text("🎙️ Detected podcast link. Processing...")
                # Create mock context with args
                mock_context = type("Context", (), {"args": [url]})()
                await self.podcast_command(update, mock_context)
            else:
                await update.message.reply_text(
                    "📝 Note: Currently I only process podcast links.\n"
                    "Use `/podcast <url>` for Spotify or Apple Podcasts links."
                )
        else:
            # Save as a note
            if text and update.message.forward_date:  # Only save forwarded messages as notes
                try:
                    result = self.vault.save_note(text, source="forwarded")
                    await update.message.reply_text(
                        f"📝 Forwarded message saved as note!\n" f"Saved to: `{result}`",
                        parse_mode="Markdown",
                    )
                except Exception as e:
                    logger.exception("Error saving forwarded note")
                    await update.message.reply_text(f"❌ Error: {sanitize_error_message(e)}")


def main():
    """Main entry point for the bot."""
    # Load configuration
    config_path = Path(__file__).parent.parent / "config.yaml"
    init_config(config_path)
    config = get_config()

    # Startup info
    import os as _os
    groq_configured = bool(config.whisper.groq_api_key)
    fallback_key = _os.environ.get("OPENAI_WHISPER_KEY", "").strip()
    if not fallback_key or fallback_key.startswith("gsk_"):
        fallback_key = config.whisper.openai_api_key if (config.whisper.openai_api_key and not config.whisper.openai_api_key.startswith("gsk_")) else ""
    openai_fallback = bool(fallback_key)
    user_count = len(config.telegram.allowed_users)
    logger.info(f"Whisper mode: {config.whisper.mode}, Groq: {groq_configured}, OpenAI fallback: {openai_fallback}, users: {user_count}")

    # Create bot instance
    bot = KnowledgeBot()

    # Build application
    application = Application.builder().token(config.telegram.bot_token).build()

    # Link bot to application for digest messages
    bot.set_telegram_app(application)

    # Podcast conversation handler with mode selection and review flow
    podcast_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("podcast", bot.podcast_command)],
        states={
            PODCAST_MODE_SELECT: [
                CallbackQueryHandler(bot.podcast_mode_callback, pattern="^podcast_mode_"),
            ],
            PODCAST_INTERACTIVE: [
                CommandHandler("detail", bot.podcast_detail_command),
                CommandHandler("insight", bot.podcast_insight_command),
                CommandHandler("end", bot.podcast_end_command),
                CommandHandler("cancel", bot.podcast_cancel),
            ],
            PODCAST_REVIEW: [
                CallbackQueryHandler(bot.podcast_review_callback, pattern="^podcast_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, bot.podcast_feedback_text),
            ],
            ConversationHandler.TIMEOUT: [
                MessageHandler(filters.ALL, bot.podcast_timeout),
            ],
        },
        fallbacks=[CommandHandler("cancel", bot.podcast_cancel)],
        per_user=True,
        per_chat=True,
        conversation_timeout=600,  # 10 minute timeout to prevent stuck states
    )

    # Add handlers
    application.add_handler(CommandHandler("start", bot.start))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CommandHandler("stats", bot.stats_command))
    application.add_handler(CommandHandler("cancel", bot.cancel_command))
    application.add_handler(podcast_conv_handler)
    application.add_handler(CommandHandler("lookup", bot.lookup_command))
    application.add_handler(CommandHandler("organize", bot.organize_command))
    application.add_handler(CommandHandler("status", bot.status_command))
    application.add_handler(CommandHandler("stop", bot.stop_command))
    application.add_handler(CommandHandler("poweron", bot.poweron_command))
    application.add_handler(CommandHandler("poweroff", bot.poweroff_command))
    application.add_handler(CommandHandler("digest", bot.digest_command))

    # Handle podcast approve/feedback callbacks (standalone, outside ConversationHandler)
    # This catches callbacks from AI-only mode where the conversation has ended
    application.add_handler(CallbackQueryHandler(bot.podcast_review_standalone, pattern="^podcast_(approve|feedback|cancel)$"))

    # Handle saved summary actions (edit, email, back, done)
    application.add_handler(CallbackQueryHandler(bot.saved_action_callback, pattern="^saved_"))

    # Handle edit actions (save, more feedback, cancel)
    application.add_handler(CallbackQueryHandler(bot.edit_action_callback, pattern="^edit_"))

    # Handle email quick-send callbacks
    application.add_handler(CallbackQueryHandler(bot.saved_action_callback, pattern="^email_"))

    # Handle folder management actions (new, rename, move, delete, pagination)
    application.add_handler(CallbackQueryHandler(bot.folder_action_callback, pattern="^folder_"))

    # Handle summary move to folder
    application.add_handler(CallbackQueryHandler(bot.folder_action_callback, pattern="^summary_move"))

    # Handle text messages (for lookup selection, email input, podcast links, and forwarded notes)
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, bot.handle_message)
    )

    # Use post_init to start scheduler after event loop is running
    async def post_init(app: Application) -> None:
        bot.start_scheduler()

    async def post_shutdown(app: Application) -> None:
        bot.stop_scheduler()

    application.post_init = post_init
    application.post_shutdown = post_shutdown

    # Start the bot
    logger.info("Starting Knowledge Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
