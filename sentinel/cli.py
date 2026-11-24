"""
Command line interface.

    sentinel check         check all containers right now and print problems
    sentinel watch         keep running: detect, save and print new problems,
                           recover containers with Ansible, notify on Telegram
    sentinel watch --no-recover   the same, but never change containers
    sentinel history       show problems saved earlier
    sentinel demo          see what Sentinel does, with a fake app (about 10 seconds)
    sentinel notify-test   check the Telegram setup and send a test message
    sentinel users add +91XXXXXXXXXX --name Ravi [--admin]
                           allow a phone number to register for Telegram alerts
    sentinel users list | remove <phone>

This file connects the collector (Docker), the rules (diagnosis), the
store (SQLite), the ticket printer, the recovery manager (Ansible) and
the notifier (Telegram) together.
"""

import argparse
import sys
import threading
from datetime import datetime

import docker

from sentinel import automation
from sentinel import bot
from sentinel import collector
from sentinel import demo
from sentinel import memory
from sentinel import notifier as notifier_module
from sentinel import recovery
from sentinel import rules
from sentinel import store
from sentinel import tracker
from sentinel import users
from sentinel.notifier import Notifier
from sentinel.ticket import print_ticket


print_lock = threading.Lock()


def connect_to_docker():
    """
    Connect to Docker, or exit with a clear message instead of a traceback.
    """
    try:
        client = docker.from_env()
        client.ping()
    except docker.errors.DockerException as error:
        print("Could not connect to Docker.")
        print("Make sure Docker is running and your user is allowed to use it")
        print("(on Linux: add yourself to the 'docker' group).")
        print()
        print("Details: " + str(error))
        sys.exit(1)

    return client


def show(ticket_id, when, trigger, evidence, findings):
    """
    Print one ticket. The lock stops two threads from printing on top
    of each other.
    """
    print_lock.acquire()

    print_ticket(
        ticket_id,
        when,
        trigger,
        evidence["name"],
        evidence["image"],
        evidence["exit_code"],
        evidence["error"],
        findings,
        evidence["logs"],
    )

    print_lock.release()


def safe_print(text):
    with print_lock:
        print(text)


# Set by `sentinel watch`. None for the one-off commands.
recovery_manager = None


def handle_problem(trigger, evidence):
    """
    Called by the collector for every problem it finds.
    Diagnoses it, saves it, and prints it only if it is new.
    New problems are also handed to the recovery manager (watch mode).
    """
    findings = tracker.diagnose_with_history(evidence)
    ticket_id, is_new = store.save_diagnosis(trigger, evidence, findings)

    if is_new:
        when = datetime.now().strftime("%H:%M:%S")
        show(ticket_id, when, trigger, evidence, findings)
        if recovery_manager is not None:
            recovery_manager.on_failure(trigger, evidence, findings)


def make_leak_handler(notifier):
    """
    Called by the memory watcher when a container looks like it is leaking.
    The container is still running, so nothing is restarted: the owner is
    warned before it runs out of memory. (If it does, the out-of-memory
    recovery raises its limit.)
    """
    def on_leak(container, description):
        evidence = collector.get_evidence(container.client, container.id)
        if evidence is None:
            return
        evidence["finished_at"] = datetime.now().isoformat(timespec="seconds")

        finding = rules.make_finding(
            description + " This looks like a memory leak.",
            "Find what in the app keeps holding memory (caches, growing lists, unclosed connections). "
            "Short term: restart it in a quiet moment, or raise --memory. Watch it live:",
            "docker stats " + evidence["name"],
        )
        ticket_id, is_new = store.save_diagnosis("memory leak", evidence, [finding])
        when = datetime.now().strftime("%H:%M:%S")
        show(ticket_id, when, "memory leak", evidence, [finding])
        notifier.notify("memory_leak", "possible memory leak in " + evidence["name"], [
            description,
            "It is still running. If it hits the limit, Sentinel will raise the limit automatically.",
        ], container=evidence["name"])

    return on_leak


def run_check():
    client = connect_to_docker()
    problems = collector.find_current_problems(client)

    if len(problems) == 0:
        print("No problems found. All containers look fine.")
        return

    print("Found " + str(len(problems)) + " container(s) with problems.")
    print()

    for trigger, evidence in problems:
        findings = tracker.diagnose_with_history(evidence)
        ticket_id, is_new = store.save_diagnosis(trigger, evidence, findings)
        when = datetime.now().strftime("%H:%M:%S")
        show(ticket_id, when, trigger, evidence, findings)


def run_watch(recover):
    global recovery_manager

    client = connect_to_docker()

    print("DockerSentinel is watching your containers... (Ctrl+C to stop)")
    print("Tickets are saved to " + store.get_db_path())

    notifier = Notifier(printer=safe_print)
    if notifier.enabled:
        active = [user for user in users.list_users() if user["status"] == users.STATUS_ACTIVE]
        username = bot.bot_username()
        print("Telegram alerts: ON - " + str(len(active)) + " registered user(s)")
        if username:
            print("    New users register at https://t.me/" + username +
                  " (admin allows numbers with 'sentinel users add')")
    else:
        print("Telegram alerts: OFF (set SENTINEL_TELEGRAM_TOKEN, see 'sentinel notify-test')")

    if not recover:
        print("Auto-recovery: OFF (--no-recover). Sentinel only diagnoses and notifies.")
    elif automation.ansible_available():
        print("Auto-recovery: ON (Ansible playbooks in " + automation.PLAYBOOK_DIR + ")")
    else:
        print("Auto-recovery: ON, but Ansible is NOT installed - recovery will fail until you install it:")
        print("    sudo apt install ansible        (Fedora: sudo dnf install ansible)")
    print()

    recovery_manager = recovery.RecoveryManager(notifier, safe_print, enabled=recover)
    recovery_manager.start()

    tracker.record_running_containers(client)

    def on_start(container_id):
        tracker.record_start(client, container_id)

    def on_tick(scan_client):
        tracker.promote_candidates(scan_client)
        recovery_manager.on_tick(scan_client)

    scanner = threading.Thread(
        target=collector.scan_failed_starts,
        args=(handle_problem, on_tick),
        daemon=True,
    )
    scanner.start()

    memory_thread = threading.Thread(
        target=memory.watch_memory,
        args=(make_leak_handler(notifier),),
        daemon=True,
    )
    memory_thread.start()

    if notifier.enabled:
        def list_containers():
            return [container.name for container in client.containers.list(all=True)]

        telegram_bot = bot.Bot(printer=safe_print, notifier=notifier, list_containers=list_containers)
        threading.Thread(target=telegram_bot.run, daemon=True).start()

    collector.watch_events(client, handle_problem, on_start, tracker.record_exit)


def run_history(limit):
    history = store.load_history(limit)

    if len(history) == 0:
        print("No problems saved yet.")
        return

    for item in history:
        evidence = {
            "name": item["container_name"],
            "image": item["image"],
            "exit_code": item["exit_code"],
            "error": item["error"],
            "logs": item["logs"],
        }
        when = item["detected_at"].replace("T", " ")
        show(item["id"], when, item["trigger"], evidence, item["findings"])


def run_notify_test():
    """
    Check the Telegram setup and send a test message to every registered user.
    """
    if not notifier_module.is_configured():
        print("SENTINEL_TELEGRAM_TOKEN is not set.")
        print("  1. In Telegram, open @BotFather, send /newbot and follow the steps.")
        print("  2. export SENTINEL_TELEGRAM_TOKEN=\"<the token BotFather gives you>\"")
        print("  3. Allow your own number:   sentinel users add +91XXXXXXXXXX --name \"You\" --admin")
        print("  4. Start Sentinel (sentinel watch), open your bot in Telegram, press Start")
        print("     and share your phone number.")
        return

    username = bot.bot_username()
    if username == "":
        print("The token was refused by Telegram. Check SENTINEL_TELEGRAM_TOKEN.")
        return
    print("Bot: https://t.me/" + username)

    active = [user for user in users.list_users() if user["status"] == users.STATUS_ACTIVE]
    if len(active) == 0:
        print("No registered users yet. Add a number with 'sentinel users add', start 'sentinel watch',")
        print("then open the bot link above, press Start and share your phone number.")
        return

    text = notifier_module.format_message("info", "test message", [
        "Telegram alerts are working. Send /status to see what you receive.",
    ])
    for user in active:
        try:
            notifier_module.send_message(user["chat_id"], text)
            print("  sent to " + user["name"] + " (" + users.mask_phone(user["phone"]) + ")")
        except notifier_module.TelegramError as error:
            print("  FAILED for " + user["name"] + ": " + str(error))


def run_users(arguments):
    """
    sentinel users add / list / remove
    """
    if arguments.users_command == "add":
        phone = " ".join(arguments.phone)
        try:
            user, created = users.add_user(phone, arguments.name or "", admin=arguments.admin)
        except users.PhoneError as error:
            print(error)
            sys.exit(1)
        if not user["name"]:
            users.update(user["phone"], name=user["phone"])
        what = "Added" if created else "Updated"
        print(what + " " + user["phone"] + (" as admin" if arguments.admin else "") + ".")
        if user["status"] == users.STATUS_INVITED:
            username = bot.bot_username() if notifier_module.is_configured() else ""
            link = "https://t.me/" + username if username else "your Sentinel bot in Telegram"
            print("Next: they open " + link + ", press Start and share their phone number.")
        return

    if arguments.users_command == "remove":
        try:
            removed = users.remove_user(" ".join(arguments.phone))
        except users.PhoneError as error:
            print(error)
            sys.exit(1)
        print("Removed." if removed else "No user with that number.")
        return

    all_users = users.list_users()
    if len(all_users) == 0:
        print("No users yet. Add one:  sentinel users add +91XXXXXXXXXX --name \"Name\" [--admin]")
        return
    print("%-18s %-16s %-7s %-8s %s" % ("NAME", "PHONE", "ROLE", "STATUS", "ALERTS / CONTAINERS"))
    for user in all_users:
        alerts = "all" if len(user["alert_types"]) == len(users.ALERT_TYPES) else ",".join(user["alert_types"]) or "none"
        containers = "all" if user["containers"] is None else ",".join(user["containers"])
        print("%-18s %-16s %-7s %-8s %s / %s" % (user["name"][:18], user["phone"], user["role"],
                                                 user["status"], alerts, containers))


def main():
    parser = argparse.ArgumentParser(
        prog="sentinel",
        description="Detects, diagnoses and recovers failing Docker containers.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("check", help="check all containers right now")
    watch_parser = commands.add_parser("watch", help="keep watching, recover containers and notify the owner")
    watch_parser.add_argument("--no-recover", action="store_true",
                              help="only diagnose and notify, never change containers")

    history_parser = commands.add_parser("history", help="show saved problems")
    history_parser.add_argument("-n", type=int, default=10, help="how many to show (default 10)")

    commands.add_parser("demo", help="see what Sentinel does, with a fake app (about 10 seconds)")
    commands.add_parser("notify-test", help="check the Telegram setup and send a test message")

    users_parser = commands.add_parser("users", help="manage who receives Telegram alerts")
    users_commands = users_parser.add_subparsers(dest="users_command", required=True)
    add_parser = users_commands.add_parser("add", help="allow a phone number to register for alerts")
    add_parser.add_argument("phone", nargs="+", help="with country code, e.g. +91 98765 43210")
    add_parser.add_argument("--name", help="the person's name")
    add_parser.add_argument("--admin", action="store_true", help="also receives admin messages")
    remove_parser = users_commands.add_parser("remove", help="remove a user")
    remove_parser.add_argument("phone", nargs="+")
    users_commands.add_parser("list", help="show all users")

    arguments = parser.parse_args()

    try:
        if arguments.command == "check":
            run_check()
        elif arguments.command == "notify-test":
            run_notify_test()
        elif arguments.command == "users":
            run_users(arguments)
        elif arguments.command == "watch":
            run_watch(not arguments.no_recover)
        elif arguments.command == "history":
            run_history(arguments.n)
        elif arguments.command == "demo":
            demo.run_demo(connect_to_docker())
    except KeyboardInterrupt:
        print()
        print("Stopped.")


if __name__ == "__main__":
    main()
