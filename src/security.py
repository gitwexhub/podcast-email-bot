"""Security utilities for Knowledge Bot."""

import ipaddress
import logging
import os
import re
import socket
from functools import wraps
from typing import Callable
from urllib.parse import urlparse

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# Blocked IP ranges for SSRF protection
BLOCKED_IP_RANGES = [
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("10.0.0.0/8"),       # Private
    ipaddress.ip_network("172.16.0.0/12"),    # Private
    ipaddress.ip_network("192.168.0.0/16"),   # Private
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local / Cloud metadata
    ipaddress.ip_network("0.0.0.0/8"),        # Current network
    ipaddress.ip_network("224.0.0.0/4"),      # Multicast
    ipaddress.ip_network("240.0.0.0/4"),      # Reserved
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 private
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]

# Blocked hostnames
BLOCKED_HOSTNAMES = [
    "localhost",
    "metadata.google.internal",
    "metadata.google.com",
    "169.254.169.254",
]


def validate_url(url: str) -> tuple[bool, str]:
    """Validate a URL is safe to fetch.

    Returns (is_valid, error_message).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    # Check scheme
    if parsed.scheme not in ("http", "https"):
        return False, "Only HTTP/HTTPS URLs are allowed"

    # Check hostname exists
    hostname = parsed.hostname
    if not hostname:
        return False, "URL must have a hostname"

    # Check blocked hostnames
    hostname_lower = hostname.lower()
    for blocked in BLOCKED_HOSTNAMES:
        if hostname_lower == blocked or hostname_lower.endswith(f".{blocked}"):
            return False, f"Access to {hostname} is blocked"

    # Resolve hostname and check IP
    try:
        # Get all IPs for the hostname
        addr_info = socket.getaddrinfo(hostname, None)
        for family, _, _, _, sockaddr in addr_info:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                for blocked_range in BLOCKED_IP_RANGES:
                    if ip in blocked_range:
                        return False, f"Access to internal/private IPs is blocked"
            except ValueError:
                continue
    except socket.gaierror:
        return False, f"Could not resolve hostname: {hostname}"

    return True, ""


def sanitize_for_logging(text: str, max_length: int = 100) -> str:
    """Sanitize text for safe logging (remove sensitive patterns)."""
    # Remove potential API keys
    text = re.sub(r"sk-[a-zA-Z0-9-]+", "[REDACTED]", text)
    # Remove potential tokens
    text = re.sub(r"\b[0-9]+:[A-Za-z0-9_-]+\b", "[REDACTED]", text)
    # Truncate
    if len(text) > max_length:
        text = text[:max_length] + "..."
    return text


def sanitize_error_message(error: Exception) -> str:
    """Create a safe error message without exposing internal details."""
    error_str = str(error)

    # Remove file paths
    error_str = re.sub(r"/[^\s]+/", "[path]/", error_str)
    # Remove line numbers
    error_str = re.sub(r"line \d+", "line [N]", error_str)
    # Truncate
    if len(error_str) > 200:
        error_str = error_str[:200] + "..."

    return error_str


def sanitize_path_component(name: str) -> str:
    """Sanitize a string to be safe as a path component.

    More aggressive than _sanitize_filename - removes all potentially
    dangerous characters and path separators.
    """
    # Remove null bytes
    name = name.replace("\x00", "")
    # Remove path separators and parent directory references
    name = re.sub(r"[/\\]", "", name)
    name = re.sub(r"\.\.+", ".", name)
    # Remove other problematic characters
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "", name)
    # Replace whitespace with dashes
    name = re.sub(r"\s+", "-", name)
    # Limit length
    name = name[:50]
    # Remove leading/trailing dots and dashes
    name = name.strip(".-")
    # Default if empty
    if not name:
        name = "unnamed"
    return name


class AccessControl:
    """Manages user access to the bot."""

    def __init__(self, allowed_user_ids: list[int] | None = None):
        """Initialize access control.

        Args:
            allowed_user_ids: List of Telegram user IDs allowed to use the bot.
                              If None or empty, bot is open to everyone (not recommended).
        """
        self.allowed_user_ids = set(allowed_user_ids) if allowed_user_ids else set()

    def is_allowed(self, user_id: int) -> bool:
        """Check if a user is allowed to use the bot."""
        if not self.allowed_user_ids:
            # No restrictions configured - allow everyone (not recommended)
            return True
        return user_id in self.allowed_user_ids

    def add_user(self, user_id: int) -> None:
        """Add a user to the allowed list."""
        self.allowed_user_ids.add(user_id)

    def remove_user(self, user_id: int) -> None:
        """Remove a user from the allowed list."""
        self.allowed_user_ids.discard(user_id)


def require_auth(access_control: AccessControl):
    """Decorator to require authentication for a handler."""
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(self, update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
            user = update.effective_user
            if not user:
                await update.message.reply_text("❌ Could not identify user.")
                return

            if not access_control.is_allowed(user.id):
                logger.warning(f"Unauthorized access attempt by user {user.id} ({user.username})")
                await update.message.reply_text(
                    "❌ You are not authorized to use this bot.\n"
                    "Contact the bot owner to request access."
                )
                return

            return await func(self, update, context, *args, **kwargs)
        return wrapper
    return decorator


def get_secret(env_var: str, config_value: str) -> str:
    """Get a secret from environment variable, falling back to config.

    This allows secrets to be provided via environment variables
    instead of storing them in the config file.
    """
    return os.environ.get(env_var, config_value)
