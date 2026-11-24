"""
Telegram bot.

Runs inside `sentinel watch` (its own thread). It asks Telegram for new
messages (long polling), so no web server or public address is needed.

Registration:
    /start                -> "Share my phone number" button
    (person taps it)      -> Telegram sends their verified contact
                          -> allowed number? activate : refuse + tell admins

Commands for registered users:
    /status               what you receive
    /alerts               choose alert types (tap to switch on/off)
    /containers           choose containers (tap), or /containers web db
    /mute 2h              no alerts for 2 hours (also 30m, 1d); /unmute
    /stop                 no more alerts; /start to turn them back on
    /users                (admins) list of users
    /help

All the logic is in Bot.handle_update(), which only needs an `api`
function, so it can be tested without Telegram.
"""

import re
import time
from datetime import datetime, timedelta

from sentinel import notifier as telegram
from sentinel import users


POLL_SECONDS = 25
MAX_CONTAINER_BUTTONS = 30

COMMANDS = [
    ("status", "What you receive"),
    ("alerts", "Choose alert types"),
    ("containers", "Choose containers"),
    ("mute", "Pause alerts, e.g. /mute 2h"),
    ("unmute", "Resume alerts"),
    ("stop", "Stop all alerts"),
    ("help", "All commands"),
]

HELP = (
    "Commands:\n"
    "/status - what you receive\n"
    "/alerts - choose alert types\n"
    "/containers - choose containers (or: /containers web db, /containers all)\n"
    "/mute 2h - pause alerts (30m, 2h, 1d); /unmute\n"
    "/stop - stop all alerts (/start turns them back on)"
)

CONTACT_KEYBOARD = {
    "keyboard": [[{"text": "\U0001F4F1 Share my phone number", "request_contact": True}]],
    "one_time_keyboard": True,
    "resize_keyboard": True,
}
REMOVE_KEYBOARD = {"remove_keyboard": True}

DURATION = re.compile(r"^(\d+)\s*([mhd])$")


def parse_duration(text):
    """
    "30m" / "2h" / "1d" -> timedelta, or None.
    """
    match = DURATION.match(text.strip().lower())
    if match is None:
        return None
    number = int(match.group(1))
    if number <= 0:
        return None
    unit = {"m": "minutes", "h": "hours", "d": "days"}[match.group(2)]
    delta = timedelta(**{unit: number})
    if delta > timedelta(days=30):
        return None
    return delta


def describe_user(user, now=None):
    if now is None:
        now = datetime.now()
    types = [users.ALERT_TYPES[t] for t in users.ALERT_TYPES if t in user["alert_types"]]
    lines = [
        "Name: " + user["name"] + ("  (admin)" if user["role"] == "admin" else ""),
        "Phone: " + users.mask_phone(user["phone"]),
        "Alerts: " + (", ".join(types) if types else "none"),
        "Containers: " + ("all" if user["containers"] is None else ", ".join(user["containers"]) or "none"),
    ]
    if user["muted_until"] and datetime.fromisoformat(user["muted_until"]) > now:
        lines.append("Muted until " + datetime.fromisoformat(user["muted_until"]).strftime("%H:%M, %d %b"))
    if user["status"] != users.STATUS_ACTIVE:
        lines.append("Alerts are OFF (send /start to turn them on).")
    return "\n".join(lines)


def alerts_keyboard(user):
    rows = []
    for kind, label in users.ALERT_TYPES.items():
        mark = "✅ " if kind in user["alert_types"] else "⬜ "
        rows.append([{"text": mark + label, "callback_data": "alert:" + kind}])
    rows.append([{"text": "Select all", "callback_data": "alert:*"}])
    return {"inline_keyboard": rows}


def containers_keyboard(user, names):
    selected = user["containers"]
    all_names = sorted(set(names) | set(selected or []))
    rows = [[{"text": ("✅ " if selected is None else "⬜ ") + "All containers", "callback_data": "ct:*"}]]
    for name in all_names[:MAX_CONTAINER_BUTTONS]:
        if len(("ct:" + name).encode("utf-8")) > 64:
            continue
        mark = "✅ " if selected is not None and name in selected else "⬜ "
        rows.append([{"text": mark + name, "callback_data": "ct:" + name}])
    return {"inline_keyboard": rows}


def toggle_alert(user, kind):
    if kind == "*":
        return list(users.ALERT_TYPES)
    current = set(user["alert_types"])
    if kind in current:
        current.discard(kind)
    elif kind in users.ALERT_TYPES:
        current.add(kind)
    return [t for t in users.ALERT_TYPES if t in current]


def toggle_container(user, name):
    if name == "*":
        return None
    selected = user["containers"]
    if selected is None:
        return [name]
    if name in selected:
        selected = [n for n in selected if n != name]
    else:
        selected = selected + [name]
    return selected or None


class Bot:

    def __init__(self, api=telegram.call_api, printer=print, notifier=None, list_containers=None):
        self.api = api
        self.printer = printer
        self.notifier = notifier
        self.list_containers = list_containers or (lambda: [])

    # -- polling loop ---------------------------------------------------------

    def run(self):
        try:
            self.api("setMyCommands", {"commands": [{"command": c, "description": d} for c, d in COMMANDS]})
        except telegram.TelegramError:
            pass

        offset = int(users.get_setting("telegram_offset", "0"))
        warned_conflict = False

        while True:
            try:
                updates = self.api("getUpdates", {"offset": offset, "timeout": POLL_SECONDS,
                                                  "allowed_updates": ["message", "callback_query"]},
                                   timeout=POLL_SECONDS + 10)
            except telegram.TelegramError as error:
                if error.status == 409 and not warned_conflict:
                    warned_conflict = True
                    self.printer("[bot] another program is reading this bot's messages "
                                 "(is a second 'sentinel watch' running?).")
                time.sleep(5)
                continue

            for update in updates:
                try:
                    self.handle_update(update)
                except Exception as error:  # one bad message must not stop the bot
                    self.printer("[bot] could not handle a message: " + str(error))
                offset = update["update_id"] + 1
                users.set_setting("telegram_offset", offset)

    # -- messages -------------------------------------------------------------

    def reply(self, chat_id, text, markup=None):
        params = {"chat_id": chat_id, "text": text}
        if markup is not None:
            params["reply_markup"] = markup
        self.api("sendMessage", params)

    def tell_admins(self, title, lines):
        if self.notifier is not None:
            self.notifier.notify("admin", title, lines)

    def handle_update(self, update):
        if "callback_query" in update:
            self.handle_button(update["callback_query"])
            return

        message = update.get("message")
        if not message:
            return

        chat_id = message["chat"]["id"]
        if message["chat"].get("type") != "private":
            self.reply(chat_id, "Please message me in a private chat.")
            return

        if "contact" in message:
            self.handle_contact(message)
            return

        text = message.get("text", "").strip()
        if not text.startswith("/"):
            self.reply(chat_id, HELP)
            return

        words = text.split()
        command = words[0].split("@")[0].lower()
        args = words[1:]
        user = users.find_by_chat(chat_id)

        if command == "/start":
            self.handle_start(chat_id, user)
            return

        if user is None:
            self.reply(chat_id, "You are not registered yet. Tap the button to share your phone number.",
                       CONTACT_KEYBOARD)
            return

        if user["status"] != users.STATUS_ACTIVE and command not in ("/status", "/help"):
            self.reply(chat_id, "Your alerts are off. Send /start to turn them back on.")
            return

        handlers = {
            "/help": lambda: self.reply(chat_id, HELP),
            "/status": lambda: self.reply(chat_id, describe_user(user)),
            "/alerts": lambda: self.reply(chat_id, "Tap to switch alert types on or off:", alerts_keyboard(user)),
            "/containers": lambda: self.handle_containers(chat_id, user, args),
            "/mute": lambda: self.handle_mute(chat_id, user, args),
            "/unmute": lambda: self.handle_unmute(chat_id, user),
            "/stop": lambda: self.handle_stop(chat_id, user),
            "/users": lambda: self.handle_users(chat_id, user),
        }
        handler = handlers.get(command)
        if handler is None:
            self.reply(chat_id, "Unknown command.\n\n" + HELP)
        else:
            handler()

    def handle_start(self, chat_id, user):
        if user is None:
            self.reply(chat_id,
                       "Welcome to DockerSentinel alerts.\n\n"
                       "To receive alerts, share your phone number with the button below. "
                       "Only numbers added by your admin are accepted.",
                       CONTACT_KEYBOARD)
            return

        if user["status"] != users.STATUS_ACTIVE:
            user = users.update(user["phone"], status=users.STATUS_ACTIVE)
            self.reply(chat_id, "Alerts are on again.\n\n" + describe_user(user), REMOVE_KEYBOARD)
            return

        self.reply(chat_id, "You are registered.\n\n" + describe_user(user) + "\n\n" + HELP, REMOVE_KEYBOARD)

    def handle_contact(self, message):
        chat_id = message["chat"]["id"]
        contact = message["contact"]
        sender = message.get("from", {})

        # Only accept the person's own number, not a contact they forward.
        if contact.get("user_id") != sender.get("id"):
            self.reply(chat_id, "Please share YOUR OWN number with the button below.", CONTACT_KEYBOARD)
            return

        try:
            phone = users.normalize_phone(contact.get("phone_number", ""))
        except users.PhoneError:
            self.reply(chat_id, "Telegram sent a number that could not be read. Please try again.", CONTACT_KEYBOARD)
            return

        telegram_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")]))
        if sender.get("username"):
            telegram_name += " (@" + sender["username"] + ")"

        user = users.register(phone, chat_id, telegram_name.strip())

        if user is None:
            self.reply(chat_id,
                       "Sorry, " + users.mask_phone(phone) + " is not allowed to receive alerts.\n"
                       "Ask your admin to add you:\n  sentinel users add " + phone + " --name \"Your Name\"",
                       REMOVE_KEYBOARD)
            self.printer("[bot] refused registration from " + users.mask_phone(phone) + " " + telegram_name)
            self.tell_admins("registration refused", [
                telegram_name + " tried to register with " + phone + ", which is not on the user list.",
                "To allow it: sentinel users add " + phone + " --name \"...\"",
            ])
            return

        self.reply(chat_id, "✅ You are registered, " + user["name"] + ".\n\n" + describe_user(user) +
                   "\n\n" + HELP, REMOVE_KEYBOARD)
        self.printer("[bot] " + user["name"] + " (" + users.mask_phone(phone) + ") registered for alerts.")
        self.tell_admins("new user registered", [user["name"] + " (" + users.mask_phone(phone) + ", " +
                                                 telegram_name + ") now receives alerts."])

    def handle_containers(self, chat_id, user, args):
        if args:
            if len(args) == 1 and args[0].lower() == "all":
                users.update(user["phone"], containers=None)
                self.reply(chat_id, "You will get alerts for all containers.")
            else:
                users.update(user["phone"], containers=args)
                self.reply(chat_id, "You will get alerts only for: " + ", ".join(args))
            return
        self.reply(chat_id, "Tap containers to choose them (or 'All containers'):",
                   containers_keyboard(user, self.list_containers()))

    def handle_mute(self, chat_id, user, args):
        delta = parse_duration(args[0] if args else "1h")
        if delta is None:
            self.reply(chat_id, "Use for example: /mute 30m, /mute 2h, /mute 1d (at most 30 days).")
            return
        until = datetime.now() + delta
        users.update(user["phone"], muted_until=until.isoformat(timespec="seconds"))
        self.reply(chat_id, "\U0001F507 Muted until " + until.strftime("%H:%M, %d %b") + ". /unmute to resume.")

    def handle_unmute(self, chat_id, user):
        users.update(user["phone"], muted_until=None)
        self.reply(chat_id, "\U0001F514 Alerts resumed.")

    def handle_stop(self, chat_id, user):
        users.update(user["phone"], status=users.STATUS_STOPPED)
        self.reply(chat_id, "You will not get alerts any more. Send /start to turn them back on.")

    def handle_users(self, chat_id, user):
        if user["role"] != "admin":
            self.reply(chat_id, "Only admins can see the user list.")
            return
        lines = []
        for other in users.list_users():
            lines.append(other["name"] + "  " + users.mask_phone(other["phone"]) + "  " + other["status"] +
                         ("  (admin)" if other["role"] == "admin" else ""))
        self.reply(chat_id, "Users:\n" + "\n".join(lines))

    # -- inline buttons -------------------------------------------------------

    def handle_button(self, query):
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        data = query.get("data", "")
        user = users.find_by_chat(chat_id) if chat_id is not None else None

        if user is None or user["status"] != users.STATUS_ACTIVE:
            self.api("answerCallbackQuery", {"callback_query_id": query["id"],
                                             "text": "You are not registered. Send /start."})
            return

        if data.startswith("alert:"):
            user = users.update(user["phone"], alert_types=toggle_alert(user, data[len("alert:"):]))
            markup = alerts_keyboard(user)
        elif data.startswith("ct:"):
            user = users.update(user["phone"], containers=toggle_container(user, data[len("ct:"):]))
            markup = containers_keyboard(user, self.list_containers())
        else:
            self.api("answerCallbackQuery", {"callback_query_id": query["id"]})
            return

        try:
            self.api("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": message["message_id"],
                                                "reply_markup": markup})
        except telegram.TelegramError:
            pass  # "message is not modified" when nothing changed
        self.api("answerCallbackQuery", {"callback_query_id": query["id"], "text": "Saved"})


def bot_username():
    try:
        return telegram.call_api("getMe", {}).get("username", "")
    except telegram.TelegramError:
        return ""
