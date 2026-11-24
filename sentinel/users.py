"""
Users (alert subscribers).

Who receives Telegram alerts, and what each person wants to receive.

How a person gets alerts:
  1. The admin allows their phone number:   sentinel users add +91 98765 43210 --name Ravi
  2. The person opens the bot in Telegram, presses Start and taps
     "Share my phone number". Telegram proves the number is theirs.
  3. Sentinel matches the number, saves their chat and activates them.

Unknown numbers are refused, so strangers who find the bot get nothing.

Every user chooses for themselves (in the bot):
  - alert types   which kinds of messages (ALERT_TYPES)
  - containers    all containers, or only some
  - mute          no messages until a given time

Saved in the same SQLite database as the tickets, table `users`.
"""

import json
from datetime import datetime

from sentinel import store


ALERT_TYPES = {
    "failed": "Container failed",
    "failed_again": "Failed again after recovery",
    "recovered": "Container recovered",
    "cannot_recover": "Cannot be recovered automatically",
    "memory_leak": "Possible memory leak",
}

# Messages that are not alerts: sent to everyone (info) or to admins only.
SYSTEM_KINDS = ("info", "admin")

STATUS_INVITED = "invited"      # number allowed, person has not registered yet
STATUS_ACTIVE = "active"        # registered, receives alerts
STATUS_STOPPED = "stopped"      # sent /stop, or blocked the bot

CREATE_USERS_TABLE = """
CREATE TABLE IF NOT EXISTS users (
    phone         TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL,
    status        TEXT NOT NULL,
    chat_id       INTEGER UNIQUE,
    telegram_name TEXT NOT NULL DEFAULT '',
    alert_types   TEXT NOT NULL,
    containers    TEXT,
    muted_until   TEXT,
    added_at      TEXT NOT NULL,
    registered_at TEXT
)
"""

CREATE_SETTINGS_TABLE = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


class PhoneError(ValueError):
    pass


def normalize_phone(text, require_plus=False):
    """
    "+91 98765-43210" -> "+919876543210"

    Telegram sends contact numbers with or without "+", so only the digits
    are compared. The admin must type the country code (require_plus), so
    that "9876543210" cannot silently fail to match "919876543210".
    """
    text = text.strip()
    if require_plus and not text.startswith("+"):
        raise PhoneError("Write the number with its country code, starting with +  (for example +91 98765 43210).")

    digits = ""
    for char in text:
        if char.isdigit():
            digits += char
        elif char not in "+ -().":
            raise PhoneError("'" + text + "' is not a phone number.")

    if len(digits) < 8 or len(digits) > 15:
        raise PhoneError("'" + text + "' does not look like a full international phone number.")

    return "+" + digits


def mask_phone(phone):
    if len(phone) <= 7:
        return phone
    return phone[:3] + "*" * (len(phone) - 7) + phone[-4:]


def connect():
    connection = store.connect()
    connection.execute(CREATE_USERS_TABLE)
    connection.execute(CREATE_SETTINGS_TABLE)
    return connection


def now_text():
    return datetime.now().isoformat(timespec="seconds")


def to_user(row):
    if row is None:
        return None
    user = dict(row)
    user["alert_types"] = json.loads(user["alert_types"])
    user["containers"] = None if user["containers"] is None else json.loads(user["containers"])
    return user


# -- admin ---------------------------------------------------------------------

def add_user(phone, name, admin=False):
    """
    Allow a phone number. Returns (user, created). An existing user keeps
    their registration and preferences; only name and role are updated.
    """
    phone = normalize_phone(phone, require_plus=True)
    role = "admin" if admin else "member"
    connection = connect()

    existing = connection.execute("SELECT phone FROM users WHERE phone = ?", (phone,)).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO users (phone, name, role, status, alert_types, added_at) VALUES (?, ?, ?, ?, ?, ?)",
            (phone, name, role, STATUS_INVITED, json.dumps(list(ALERT_TYPES)), now_text()),
        )
    else:
        connection.execute("UPDATE users SET name = ?, role = ? WHERE phone = ?", (name, role, phone))

    connection.commit()
    user = to_user(connection.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone())
    connection.close()
    return user, existing is None


def remove_user(phone):
    phone = normalize_phone(phone)
    connection = connect()
    cursor = connection.execute("DELETE FROM users WHERE phone = ?", (phone,))
    connection.commit()
    connection.close()
    return cursor.rowcount == 1


def list_users():
    connection = connect()
    rows = connection.execute("SELECT * FROM users ORDER BY role, name").fetchall()
    connection.close()
    return [to_user(row) for row in rows]


def admins():
    return [user for user in list_users() if user["role"] == "admin" and user["status"] == STATUS_ACTIVE]


# -- registration from Telegram ------------------------------------------------

def find_by_phone(phone):
    connection = connect()
    row = connection.execute("SELECT * FROM users WHERE phone = ?", (phone,)).fetchone()
    connection.close()
    return to_user(row)


def find_by_chat(chat_id):
    connection = connect()
    row = connection.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    connection.close()
    return to_user(row)


def register(phone, chat_id, telegram_name):
    """
    Link an allowed phone number to a Telegram chat and activate it.
    Returns the user, or None if the number is not allowed.
    """
    user = find_by_phone(phone)
    if user is None:
        return None

    connection = connect()
    # A chat belongs to one number: free it if it was linked to another one.
    connection.execute("UPDATE users SET chat_id = NULL, status = ? WHERE chat_id = ? AND phone != ?",
                       (STATUS_STOPPED, chat_id, phone))
    connection.execute(
        "UPDATE users SET chat_id = ?, telegram_name = ?, status = ?, registered_at = ? WHERE phone = ?",
        (chat_id, telegram_name, STATUS_ACTIVE, now_text(), phone),
    )
    connection.commit()
    connection.close()
    return find_by_phone(phone)


def update(phone, **fields):
    """
    Change some fields of one user. Lists are stored as JSON.
    """
    columns = []
    values = []
    for key, value in fields.items():
        if key in ("alert_types", "containers") and value is not None:
            value = json.dumps(value)
        columns.append(key + " = ?")
        values.append(value)
    values.append(phone)

    connection = connect()
    connection.execute("UPDATE users SET " + ", ".join(columns) + " WHERE phone = ?", values)
    connection.commit()
    connection.close()
    return find_by_phone(phone)


def stop_chat(chat_id):
    """
    The person blocked the bot or sent /stop.
    """
    connection = connect()
    connection.execute("UPDATE users SET status = ? WHERE chat_id = ?", (STATUS_STOPPED, chat_id))
    connection.commit()
    connection.close()


# -- who gets a message --------------------------------------------------------

def wants(user, kind, container, now=None):
    """
    Pure logic: should this user get a message of this kind about this
    container right now?
    """
    if user["status"] != STATUS_ACTIVE or user["chat_id"] is None:
        return False

    if kind == "admin":
        return user["role"] == "admin"
    if kind == "info":
        return True

    if now is None:
        now = datetime.now()
    if user["muted_until"] and datetime.fromisoformat(user["muted_until"]) > now:
        return False

    if kind not in user["alert_types"]:
        return False

    if user["containers"] is not None and container is not None and container not in user["containers"]:
        return False

    return True


def recipients(kind, container):
    return [user for user in list_users() if wants(user, kind, container)]


# -- small key/value settings (the bot's update offset) ------------------------

def get_setting(key, default=None):
    connection = connect()
    row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    connection.close()
    return default if row is None else row["value"]


def set_setting(key, value):
    connection = connect()
    connection.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    connection.commit()
    connection.close()
