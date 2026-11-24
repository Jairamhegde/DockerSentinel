"""
Tests for user registration, preferences and alert routing.
Uses a temporary database and a fake Telegram API (no network).

    python3 -m unittest discover -s tests
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sentinel import bot, notifier, users  # noqa: E402

OWNER_CHAT = 111
STRANGER_CHAT = 222


class FakeApi:
    """Records every Telegram call instead of sending it."""

    def __init__(self):
        self.calls = []

    def __call__(self, method, params, timeout=None):
        self.calls.append((method, params))
        return {}

    def texts(self, chat_id):
        return [p["text"] for m, p in self.calls if m == "sendMessage" and p["chat_id"] == chat_id]


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def notify(self, kind, title, lines, container=None):
        self.sent.append((kind, title, lines))


def message(chat_id, text=None, contact=None, sender_id=None):
    msg = {"chat": {"id": chat_id, "type": "private"},
           "from": {"id": sender_id or chat_id, "first_name": "Ravi"}}
    if text is not None:
        msg["text"] = text
    if contact is not None:
        msg["contact"] = contact
    return {"update_id": 1, "message": msg}


def share_contact(chat_id, phone, own=True):
    contact = {"phone_number": phone, "user_id": chat_id if own else 999}
    return message(chat_id, contact=contact)


class UsersTestCase(unittest.TestCase):

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        os.environ["SENTINEL_DB"] = os.path.join(self.folder.name, "test.db")
        self.api = FakeApi()
        self.admin_messages = FakeNotifier()
        self.bot = bot.Bot(api=self.api, printer=lambda text: None, notifier=self.admin_messages,
                           list_containers=lambda: ["web", "db"])

    def tearDown(self):
        os.environ.pop("SENTINEL_DB", None)
        self.folder.cleanup()

    def register_owner(self):
        users.add_user("+91 98765 43210", "Ravi", admin=True)
        self.bot.handle_update(share_contact(OWNER_CHAT, "919876543210"))
        return users.find_by_chat(OWNER_CHAT)


class PhoneTests(unittest.TestCase):

    def test_normalize(self):
        self.assertEqual(users.normalize_phone("+91 98765-43210"), "+919876543210")
        self.assertEqual(users.normalize_phone("919876543210"), "+919876543210")

    def test_admin_must_give_country_code(self):
        with self.assertRaises(users.PhoneError):
            users.normalize_phone("98765 43210", require_plus=True)
        with self.assertRaises(users.PhoneError):
            users.normalize_phone("+12")
        with self.assertRaises(users.PhoneError):
            users.normalize_phone("+91 abc")

    def test_mask(self):
        self.assertEqual(users.mask_phone("+919876543210"), "+91******3210")


class RegistrationTests(UsersTestCase):

    def test_start_asks_for_phone_number(self):
        self.bot.handle_update(message(STRANGER_CHAT, "/start"))
        method, params = self.api.calls[-1]
        self.assertTrue(params["reply_markup"]["keyboard"][0][0]["request_contact"])

    def test_allowed_number_is_activated(self):
        user = self.register_owner()
        self.assertEqual(user["status"], users.STATUS_ACTIVE)
        self.assertEqual(user["chat_id"], OWNER_CHAT)
        self.assertIn("registered", self.api.texts(OWNER_CHAT)[-1])

    def test_unknown_number_is_refused_and_admins_are_told(self):
        self.bot.handle_update(share_contact(STRANGER_CHAT, "+15550001111"))
        self.assertIsNone(users.find_by_chat(STRANGER_CHAT))
        self.assertIn("not allowed", self.api.texts(STRANGER_CHAT)[-1])
        self.assertEqual(self.admin_messages.sent[-1][1], "registration refused")

    def test_someone_elses_contact_is_refused(self):
        users.add_user("+919876543210", "Ravi")
        self.bot.handle_update(share_contact(STRANGER_CHAT, "919876543210", own=False))
        self.assertIsNone(users.find_by_chat(STRANGER_CHAT))
        self.assertIn("YOUR OWN", self.api.texts(STRANGER_CHAT)[-1])

    def test_commands_need_registration(self):
        self.bot.handle_update(message(STRANGER_CHAT, "/status"))
        self.assertIn("not registered", self.api.texts(STRANGER_CHAT)[-1])

    def test_group_chats_are_ignored(self):
        update = message(STRANGER_CHAT, "/start")
        update["message"]["chat"]["type"] = "group"
        self.bot.handle_update(update)
        self.assertIn("private chat", self.api.texts(STRANGER_CHAT)[-1])

    def test_stop_and_start_again(self):
        self.register_owner()
        self.bot.handle_update(message(OWNER_CHAT, "/stop"))
        self.assertEqual(users.find_by_chat(OWNER_CHAT)["status"], users.STATUS_STOPPED)
        self.bot.handle_update(message(OWNER_CHAT, "/mute 1h"))
        self.assertIn("off", self.api.texts(OWNER_CHAT)[-1])
        self.bot.handle_update(message(OWNER_CHAT, "/start"))
        self.assertEqual(users.find_by_chat(OWNER_CHAT)["status"], users.STATUS_ACTIVE)


class PreferenceTests(UsersTestCase):

    def press(self, data):
        self.bot.handle_update({"update_id": 2, "callback_query": {
            "id": "q1", "data": data, "message": {"chat": {"id": OWNER_CHAT}, "message_id": 5}}})

    def test_alert_buttons_toggle(self):
        self.register_owner()
        self.press("alert:recovered")
        self.assertNotIn("recovered", users.find_by_chat(OWNER_CHAT)["alert_types"])
        self.press("alert:recovered")
        self.assertIn("recovered", users.find_by_chat(OWNER_CHAT)["alert_types"])
        self.assertIn("editMessageReplyMarkup", [m for m, p in self.api.calls])

    def test_container_buttons_and_text(self):
        self.register_owner()
        self.press("ct:web")
        self.assertEqual(users.find_by_chat(OWNER_CHAT)["containers"], ["web"])
        self.press("ct:web")
        self.assertIsNone(users.find_by_chat(OWNER_CHAT)["containers"])
        self.bot.handle_update(message(OWNER_CHAT, "/containers db cache"))
        self.assertEqual(users.find_by_chat(OWNER_CHAT)["containers"], ["db", "cache"])
        self.bot.handle_update(message(OWNER_CHAT, "/containers all"))
        self.assertIsNone(users.find_by_chat(OWNER_CHAT)["containers"])

    def test_mute(self):
        self.register_owner()
        self.bot.handle_update(message(OWNER_CHAT, "/mute 2h"))
        muted = datetime.fromisoformat(users.find_by_chat(OWNER_CHAT)["muted_until"])
        self.assertAlmostEqual((muted - datetime.now()).total_seconds(), 7200, delta=5)
        self.bot.handle_update(message(OWNER_CHAT, "/mute forever"))
        self.assertIn("/mute 30m", self.api.texts(OWNER_CHAT)[-1])
        self.bot.handle_update(message(OWNER_CHAT, "/unmute"))
        self.assertIsNone(users.find_by_chat(OWNER_CHAT)["muted_until"])

    def test_users_command_is_admin_only(self):
        users.add_user("+447700900123", "Sam")
        self.bot.handle_update(share_contact(STRANGER_CHAT, "447700900123"))
        self.bot.handle_update(message(STRANGER_CHAT, "/users"))
        self.assertIn("Only admins", self.api.texts(STRANGER_CHAT)[-1])
        self.register_owner()
        self.bot.handle_update(message(OWNER_CHAT, "/users"))
        self.assertIn("Sam", self.api.texts(OWNER_CHAT)[-1])


class RoutingTests(UsersTestCase):

    def user(self, **overrides):
        base = {"status": users.STATUS_ACTIVE, "chat_id": 1, "role": "member", "muted_until": None,
                "alert_types": list(users.ALERT_TYPES), "containers": None}
        base.update(overrides)
        return base

    def test_wants(self):
        now = datetime(2026, 1, 1, 12, 0)
        self.assertTrue(users.wants(self.user(), "failed", "web", now))
        self.assertFalse(users.wants(self.user(status=users.STATUS_INVITED), "failed", "web", now))
        self.assertFalse(users.wants(self.user(alert_types=["memory_leak"]), "failed", "web", now))
        self.assertFalse(users.wants(self.user(containers=["db"]), "failed", "web", now))
        muted = (now + timedelta(hours=1)).isoformat()
        self.assertFalse(users.wants(self.user(muted_until=muted), "failed", "web", now))
        self.assertFalse(users.wants(self.user(), "admin", None, now))
        self.assertTrue(users.wants(self.user(role="admin"), "admin", None, now))

    def test_notifier_sends_only_to_interested_users(self):
        self.register_owner()
        users.add_user("+447700900123", "Sam")
        self.bot.handle_update(share_contact(STRANGER_CHAT, "447700900123"))
        users.update("+447700900123", containers=["db"])

        printed = []
        alerts = notifier.Notifier(printer=printed.append)
        alerts.enabled = True  # route without starting the network thread
        alerts.notify("failed", "web crashed", ["Cause: x"], container="web")

        queued = []
        while not alerts.outbox.empty():
            queued.append(alerts.outbox.get()[0])
        self.assertEqual(queued, [OWNER_CHAT])
        self.assertIn("Ravi", printed[-1])


class DurationTests(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(bot.parse_duration("30m"), timedelta(minutes=30))
        self.assertEqual(bot.parse_duration("2h"), timedelta(hours=2))
        self.assertEqual(bot.parse_duration("1d"), timedelta(days=1))
        self.assertIsNone(bot.parse_duration("0h"))
        self.assertIsNone(bot.parse_duration("45d"))
        self.assertIsNone(bot.parse_duration("soon"))


if __name__ == "__main__":
    unittest.main()
