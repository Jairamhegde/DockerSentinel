"""
Notifier.

Sends alerts to the registered users on Telegram (see users.py for who
gets what, and bot.py for how people register).

The Telegram Bot API is called with the Python standard library (urllib).
Messages go through one background queue, so:
  - a slow network never delays diagnosis or recovery
  - Telegram's rate limit (HTTP 429) is respected: wait and retry
  - a user who blocked the bot (HTTP 403) is unsubscribed automatically

The bot token is the only setting:  export SENTINEL_TELEGRAM_TOKEN="123456:ABC..."
(create a bot with @BotFather in Telegram). Without it, alerts are only
printed in the terminal.
"""

import json
import os
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from sentinel import users


API_URL = "https://api.telegram.org/bot"
TIMEOUT_SECONDS = 10
MAX_MESSAGE_LENGTH = 4000
MAX_RETRIES = 3

ICONS = {
    "failed": "\U0001F534",          # red circle
    "failed_again": "⚠️",  # warning sign
    "recovered": "✅",           # check mark
    "cannot_recover": "\U0001F198",  # SOS
    "memory_leak": "\U0001F9E0",     # brain (memory)
    "info": "ℹ️",
    "admin": "\U0001F511",           # key
}


class TelegramError(Exception):
    def __init__(self, message, status=0, retry_after=0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def get_token():
    return os.environ.get("SENTINEL_TELEGRAM_TOKEN", "").strip()


def is_configured():
    return get_token() != ""


def call_api(method, params, timeout=TIMEOUT_SECONDS, token=None):
    """
    One Telegram Bot API call. Returns the "result" part of the answer.
    Raises TelegramError (with the HTTP status) on any failure.
    """
    if token is None:
        token = get_token()
    if token == "":
        raise TelegramError("SENTINEL_TELEGRAM_TOKEN is not set.")

    body = {}
    for key, value in params.items():
        body[key] = json.dumps(value) if isinstance(value, (dict, list)) else value

    request = urllib.request.Request(API_URL + token + "/" + method,
                                     data=urllib.parse.urlencode(body).encode("utf-8"))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            answer = json.loads(error.read().decode("utf-8"))
        except ValueError:
            answer = {}
        retry_after = (answer.get("parameters") or {}).get("retry_after", 0)
        raise TelegramError(answer.get("description", str(error)), error.code, retry_after)
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise TelegramError("Could not reach Telegram: " + str(error))

    if not answer.get("ok", False):
        raise TelegramError(answer.get("description", "Telegram refused the request."))
    return answer["result"]


def format_message(kind, title, lines):
    """
    Plain text (no Markdown), so names with _ or * never break the message.
    """
    text = ICONS.get(kind, "") + " DockerSentinel: " + title
    for line in lines:
        if line:
            text += "\n" + line
    if len(text) > MAX_MESSAGE_LENGTH:
        text = text[:MAX_MESSAGE_LENGTH - 3] + "..."
    return text


def send_message(chat_id, text, reply_markup=None):
    params = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        params["reply_markup"] = reply_markup
    return call_api("sendMessage", params)


class Notifier:
    """
    Prints every alert in the terminal, and sends it on Telegram to each
    registered user who wants it.
    """

    def __init__(self, printer=print, find_recipients=users.recipients):
        self.printer = printer
        self.find_recipients = find_recipients
        self.enabled = is_configured()
        self.outbox = queue.Queue()
        self.last_error_at = 0
        if self.enabled:
            threading.Thread(target=self.send_loop, daemon=True).start()

    def notify(self, kind, title, lines, container=None):
        text = format_message(kind, title, lines)

        recipients = []
        if self.enabled:
            try:
                recipients = self.find_recipients(kind, container)
            except Exception as error:
                self.printer("[notify] could not read the user list: " + str(error))

        if kind != "admin":
            if not self.enabled:
                where = ""
            elif recipients:
                where = "  -> Telegram: " + ", ".join(user["name"] for user in recipients)
            else:
                where = "  -> Telegram: nobody subscribed to this alert"
            self.printer("[notify] " + text.replace("\n", "\n         ") + where)

        for user in recipients:
            self.outbox.put((user["chat_id"], text, 0))

    def send_loop(self):
        while True:
            chat_id, text, tries = self.outbox.get()
            try:
                send_message(chat_id, text)
            except TelegramError as error:
                self.handle_error(error, chat_id, text, tries)

    def handle_error(self, error, chat_id, text, tries):
        if error.status == 429 and tries < MAX_RETRIES:
            time.sleep(max(error.retry_after, 1))
            self.outbox.put((chat_id, text, tries + 1))
            return

        if error.status == 403:
            # The person blocked the bot or deleted the chat.
            users.stop_chat(chat_id)
            self.printer("[notify] a user blocked the bot; they are unsubscribed (chat " + str(chat_id) + ").")
            return

        if error.status == 0 and tries < MAX_RETRIES:
            time.sleep(5)
            self.outbox.put((chat_id, text, tries + 1))
            return

        # Do not flood the terminal when Telegram is down.
        if time.time() - self.last_error_at > 60:
            self.last_error_at = time.time()
            self.printer("[notify] Telegram message failed: " + str(error))
