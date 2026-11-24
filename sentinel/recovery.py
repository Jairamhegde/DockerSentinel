"""
Recovery.

After a failure is diagnosed, Sentinel decides what to do about it and
does it with an Ansible playbook (see automation.py):

    root cause found by the diagnosis           action
    ------------------------------------------  ---------------------------
    out of memory, container has a limit        raise_memory (x1.5)
    new image build broke it (Dockerfile step,  rollback to the last good
      new code, new start command)                image
    temporary problem (service not ready,       restart
      health check failing, nothing changed)
    needs a human (missing env var, bad port,   give_up -> Telegram alert
      wrong command, missing file, crash loop,
      tried MAX_ATTEMPTS times already)

decide() is pure logic, so every rule can be tested without Docker.
RecoveryManager keeps track of each failing container ("incident"),
runs the playbooks in a background thread and sends the notifications:

    failed               every failure, with the action Sentinel takes
    failed again         it was recovered (or an action ran) and failed again
    recovered            it has been running fine for RECOVERED_AFTER seconds
    cannot recover       a human has to fix it
"""

import queue
import threading
from datetime import datetime, timedelta

from sentinel import changes


MAX_ATTEMPTS = 3
MAX_MEMORY_RAISES = 2
MEMORY_FACTOR = 1.5
RECOVERED_AFTER = 15
INCIDENT_WINDOW = timedelta(minutes=10)
MUTE_AFTER_GIVE_UP = timedelta(minutes=30)

AUTO_RESTART_POLICIES = ("always", "unless-stopped", "on-failure")

# Error text that a restart can never fix: the image or the settings are wrong.
PERMANENT_PATTERNS = [
    "port is already allocated",
    "address already in use",
    "exec format error",
    "modulenotfounderror",
    "cannot find module",
    "permission denied",
    "no such file or directory",
    "file not found",
    "command not found",
]

PERMANENT_EXIT_CODES = (126, 127, 139)

# Changes the user made to the container settings. Rolling back the image
# does not undo these, and Sentinel cannot guess the right value.
SETTING_PREFIXES = ("env.", "mount.", "port.", "network.", "limit.cpus")


def plan(action, reason, **extra):
    result = {"action": action, "reason": reason}
    result.update(extra)
    return result


def label(action):
    return {
        "restart": "restart the container",
        "raise_memory": "raise the memory limit",
        "rollback": "roll back to the last good image",
    }.get(action, action)


def image_was_rebuilt(differences):
    for difference in differences:
        if difference["key"] == "image.id" or difference["key"].startswith("build: "):
            return True
    return False


def is_setting_change(difference):
    return difference["key"].startswith(SETTING_PREFIXES)


def is_permanent(trigger, evidence):
    if trigger == "failed to start":
        return True
    if evidence["exit_code"] in PERMANENT_EXIT_CODES:
        return True
    text = (evidence["logs"] + " " + evidence["error"]).lower()
    for pattern in PERMANENT_PATTERNS:
        if pattern in text:
            return True
    return False


def decide(trigger, evidence, differences, container, good_image_id, tried):
    """
    Choose the recovery action.

      trigger        "crashed", "failed to start", "unhealthy", "restart loop",
                     "process killed (OOM)"
      evidence       collected by collector.get_evidence()
      differences    ranked changes since the last good run (may be empty)
      container      {"memory": bytes, "restart_policy": str, "image_id": str}
      good_image_id  image of the last good run, if it still exists locally
      tried          actions already tried in this incident, oldest first

    Returns {"action": restart | raise_memory | rollback | wait | give_up,
             "reason": plain-English explanation, ...extra}.
    """
    if len(tried) >= MAX_ATTEMPTS:
        return plan("give_up", "Tried " + str(len(tried)) + " times (" + ", ".join(label(a) for a in tried) +
                    ") and it still fails.")

    # 1. Out of memory -> more memory.
    if evidence["oom_killed"] or trigger == "process killed (OOM)":
        limit = container["memory"]
        if limit <= 0:
            if "restart" in tried:
                return plan("give_up", "It ran out of memory without a memory limit, so the whole machine "
                            "is short of memory. Restarting did not help.")
            return plan("restart", "It ran out of memory without a memory limit (the machine was short of "
                        "memory). A restart frees the memory.")
        if tried.count("raise_memory") >= MAX_MEMORY_RAISES:
            return plan("give_up", "It still runs out of memory after the limit was raised " +
                        str(MAX_MEMORY_RAISES) + " times. This looks like a memory leak in the app.")
        new_limit = int(limit * MEMORY_FACTOR)
        new_limit = (new_limit // (1024 * 1024)) * 1024 * 1024
        return plan("raise_memory", "It was killed for using more than its " + str(limit // (1024 * 1024)) +
                    " MB memory limit. Raising it to " + str(new_limit // (1024 * 1024)) + " MB.",
                    memory=new_limit)

    top = differences[0] if differences else None

    # 2. A new image build broke it -> roll back to the image that worked.
    can_roll_back = (good_image_id is not None and good_image_id != container["image_id"]
                     and image_was_rebuilt(differences))
    if can_roll_back and "rollback" not in tried and not (top and is_setting_change(top)):
        cause = changes.describe(top) if top else "The image changed."
        return plan("rollback", "It worked with the previous image. Most likely cause: " + cause,
                    image=good_image_id)

    # 3. The user changed a setting -> only a human knows the right value.
    if top is not None and is_setting_change(top):
        return plan("give_up", "A container setting changed since it last worked: " + changes.describe(top) +
                    " Sentinel cannot safely guess the right value.")

    # 4. Errors a restart can never fix.
    if is_permanent(trigger, evidence):
        return plan("give_up", "Restarting will not fix this: the image or its settings are wrong "
                    "(see the cause and fix below).")

    if trigger == "restart loop" or evidence["restart_count"] > 3:
        return plan("give_up", "It is stuck in a crash loop (" + str(evidence["restart_count"]) +
                    " restarts). Restarting again will not help.")

    # 5. Docker's own restart policy already restarts it.
    if container["restart_policy"] in AUTO_RESTART_POLICIES and trigger == "crashed":
        return plan("wait", "Docker's restart policy (" + container["restart_policy"] +
                    ") restarts it automatically. Watching whether that works.")

    # 6. Temporary problem -> restart.
    if tried.count("restart") >= 2:
        return plan("give_up", "Restarted twice and it failed again each time.")

    text = (evidence["logs"] + " " + evidence["error"]).lower()
    if "connection refused" in text:
        reason = "It could not reach a service it needs. That service may just not have been ready - restarting."
    elif trigger == "unhealthy":
        reason = "Its health check keeps failing - restarting it."
    elif top is None:
        reason = "Nothing changed since it last worked, so this looks temporary - restarting."
    else:
        reason = "No known permanent cause found - restarting."
    return plan("restart", reason)


class Incident:
    """
    One container that failed, and what Sentinel did about it.
    """

    def __init__(self, name):
        self.name = name
        self.tried = []
        self.last_failure = None
        self.action_at = None
        self.waiting = False
        self.recovered_at = None
        self.gave_up_at = None


class RecoveryManager:
    """
    Receives every diagnosed failure, decides, runs Ansible, notifies.
    """

    def __init__(self, notifier, printer, enabled=True):
        self.notifier = notifier
        self.printer = printer
        self.enabled = enabled
        self.incidents = {}
        self.lock = threading.Lock()
        self.jobs = queue.Queue()
        self.client = None

    def notify(self, container, kind, title, lines):
        self.notifier.notify(kind, title, lines, container=container)

    # -- called from the watching threads ---------------------------------

    def start(self):
        thread = threading.Thread(target=self.work, daemon=True)
        thread.start()

    def on_failure(self, trigger, evidence, findings):
        self.jobs.put((trigger, evidence, findings))

    def on_tick(self, client):
        """
        Called every few seconds: confirm recoveries.
        """
        with self.lock:
            waiting = [incident for incident in self.incidents.values() if incident.waiting]

        for incident in waiting:
            if self.is_running_well(client, incident):
                with self.lock:
                    incident.waiting = False
                    incident.recovered_at = datetime.now()
                how = label(incident.tried[-1]) + " (Ansible)" if incident.tried else "Docker's restart policy"
                self.notify(incident.name, "recovered", incident.name + " recovered", [
                    "It has been running fine for " + str(RECOVERED_AFTER) + " s after: " + how + ".",
                ])

    # -- background worker --------------------------------------------------

    def work(self):
        import docker

        self.client = docker.from_env()
        while True:
            trigger, evidence, findings = self.jobs.get()
            try:
                self.handle(trigger, evidence, findings)
            except Exception as error:  # never let one failure stop recovery
                self.printer("[recovery] error while handling " + evidence["name"] + ": " + str(error))

    def get_incident(self, name, now):
        with self.lock:
            incident = self.incidents.get(name)
            if incident is not None and incident.last_failure is not None:
                if now - incident.last_failure > INCIDENT_WINDOW:
                    incident = None
            if incident is None:
                incident = Incident(name)
                self.incidents[name] = incident
            return incident

    def handle(self, trigger, evidence, findings):
        from sentinel import automation
        from sentinel import tracker

        now = datetime.now()
        name = evidence["name"]
        incident = self.get_incident(name, now)

        failed_again = len(incident.tried) > 0 or incident.recovered_at is not None
        incident.last_failure = now
        incident.waiting = False

        if incident.gave_up_at is not None and now - incident.gave_up_at < MUTE_AFTER_GIVE_UP:
            self.printer("[recovery] " + name + " failed again; already reported as needing a human.")
            return

        cause_lines = describe_findings(findings)
        attempt = len(incident.tried) + 1

        container = self.read_container(evidence["id"], name)
        if container is None:
            self.give_up(incident, trigger, "The container was removed, so it cannot be recovered "
                         "(was it started with --rm?).", cause_lines, findings)
            return

        differences, good_image_id = tracker.change_analysis(evidence)
        if good_image_id is not None and not self.image_exists(good_image_id):
            good_image_id = None

        decision = decide(trigger, evidence, differences, container["info"], good_image_id, incident.tried)
        action = decision["action"]

        kind = "failed_again" if failed_again else "failed"
        title = name + (" FAILED AGAIN after recovery" if failed_again else " " + trigger)

        if action == "give_up":
            self.give_up(incident, trigger, decision["reason"], cause_lines, findings, kind, title)
            return

        if action == "wait":
            incident.waiting = True
            self.notify(name, kind, title, cause_lines + ["Action: none needed. " + decision["reason"]])
            return

        if not self.enabled:
            self.notify(name, kind, title, cause_lines + [
                "Suggested action: " + label(action) + ". " + decision["reason"],
                "(Auto-recovery is off: sentinel watch --no-recover)",
            ])
            return

        self.notify(name, kind, title, cause_lines + [
            "Action " + str(attempt) + "/" + str(MAX_ATTEMPTS) + ": " + label(action) + " with Ansible.",
            decision["reason"],
        ])

        attrs = container["attrs"]
        if action == "restart":
            variables = automation.restart_vars(attrs)
        elif action == "raise_memory":
            variables = automation.raise_memory_vars(attrs, decision["memory"])
        else:
            image_config = self.image_config(attrs["Image"])
            variables = automation.rollback_vars(attrs, image_config, decision["image"])

        self.printer("[recovery] running Ansible playbook '" + automation.PLAYBOOKS[action] + "' for " + name + " ...")
        ok, output = automation.run_playbook(action, variables)

        incident.tried.append(action)
        incident.action_at = datetime.now()

        if ok:
            incident.waiting = True
            self.printer("[recovery] playbook finished; waiting " + str(RECOVERED_AFTER) + " s to confirm " +
                         name + " is running fine.")
            return

        self.printer("[recovery] playbook FAILED:\n" + output)
        self.give_up(incident, trigger, "The Ansible playbook '" + action + "' failed: " +
                     last_line(output), cause_lines, findings, "failed_again", name + " recovery failed")

    def give_up(self, incident, trigger, reason, cause_lines, findings, kind=None, title=None):
        incident.gave_up_at = datetime.now()
        incident.waiting = False
        fix = ""
        if findings:
            fix = "Suggested fix: " + findings[0]["fix"]
        tried = ""
        if incident.tried:
            tried = "Already tried: " + ", ".join(label(a) for a in incident.tried) + "."
        self.notify(incident.name, "cannot_recover", incident.name + " cannot be recovered automatically",
                             ["Problem: " + trigger] + cause_lines + ["Why: " + reason, tried, fix,
                                                                      "A human needs to fix this."])

    # -- Docker helpers ------------------------------------------------------

    def read_container(self, container_id, name):
        import docker

        for key in (container_id, name):
            try:
                container = self.client.containers.get(key)
            except docker.errors.NotFound:
                continue
            attrs = container.attrs
            host = attrs["HostConfig"]
            return {
                "attrs": attrs,
                "info": {
                    "memory": host.get("Memory", 0),
                    "restart_policy": (host.get("RestartPolicy") or {}).get("Name", ""),
                    "image_id": attrs["Image"],
                },
            }
        return None

    def image_exists(self, image_id):
        import docker

        try:
            self.client.images.get(image_id)
        except docker.errors.ImageNotFound:
            return False
        return True

    def image_config(self, image_id):
        import docker

        try:
            return self.client.images.get(image_id).attrs.get("Config") or {}
        except docker.errors.ImageNotFound:
            return {}

    def is_running_well(self, client, incident):
        import docker

        try:
            container = client.containers.get(incident.name)
        except docker.errors.NotFound:
            return False

        state = container.attrs["State"]
        if not state["Running"]:
            return False
        if "Health" in state and state["Health"]["Status"] != "healthy":
            return False

        started = parse_docker_time(state["StartedAt"])
        if started is None:
            return False
        running_for = (datetime.now().astimezone() - started).total_seconds()
        return running_for >= RECOVERED_AFTER


def describe_findings(findings):
    if not findings:
        return []
    first = findings[0]
    lines = ["Cause: " + first["cause"]]
    for detail in first.get("details", [])[:2]:
        lines.append("  - " + detail)
    return lines


def last_line(output):
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "no output"


def parse_docker_time(text):
    """
    "2026-11-24T10:15:30.123456789Z" -> aware datetime (nanoseconds cut).
    """
    if not text or text.startswith("0001-"):
        return None
    text = text.replace("Z", "+00:00")
    if "." in text:
        head, _, rest = text.partition(".")
        digits = ""
        zone = ""
        for i, char in enumerate(rest):
            if char.isdigit():
                digits += char
            else:
                zone = rest[i:]
                break
        text = head + "." + digits[:6].ljust(6, "0") + zone
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
