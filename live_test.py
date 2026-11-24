#!/usr/bin/env python3
"""
DockerSentinel live test.

Runs the whole workflow for real, with no manual steps:

    1. checks the prerequisites (Docker, Ansible, community.docker, Telegram)
    2. starts `sentinel watch` in the background
    3. creates containers, breaks them on purpose, and waits for Sentinel to
       diagnose, recover them with Ansible and send the notifications
    4. prints a PASS / FAIL summary and removes everything it created

    python3 live_test.py                       all scenarios (about 4 minutes)
    python3 live_test.py restart rollback      only some scenarios
    python3 live_test.py --list                show the scenarios
    python3 live_test.py --keep                do not remove containers at the end

Telegram: if SENTINEL_TELEGRAM_TOKEN is set, registered users receive the
alerts on their phones (see README, "Telegram setup"). Without it, the
alerts are only printed here.

Full Sentinel output is saved to live_test.log.
"""

import io
import os
import queue
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

LOG_FILE = os.path.join(ROOT, "live_test.log")
RUN_ID = time.strftime("%H%M%S")
LABEL = "sentinel-live-test"
MB = 1024 * 1024

GREEN, RED, YELLOW, BOLD, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[1m", "\033[2m", "\033[0m"
if not sys.stdout.isatty():
    GREEN = RED = YELLOW = BOLD = DIM = RESET = ""


def say(text=""):
    print(text, flush=True)


def step(text):
    say(BOLD + "\n== " + text + RESET)


# ---------------------------------------------------------------------------
# prerequisites
# ---------------------------------------------------------------------------

def check_prerequisites():
    step("Checking prerequisites")
    problems = []

    try:
        import docker
        client = docker.from_env()
        client.ping()
        say("  " + GREEN + "ok" + RESET + "  Docker is running")
    except ImportError:
        problems.append("Python docker library missing:  sudo apt install python3-docker   (or pip install docker)")
        client = None
    except Exception as error:
        problems.append("Cannot connect to Docker: " + str(error))
        client = None

    if shutil.which("ansible-playbook"):
        say("  " + GREEN + "ok" + RESET + "  Ansible is installed")
        collections = subprocess.run(["ansible-galaxy", "collection", "list", "community.docker"],
                                     capture_output=True, text=True)
        if "community.docker" in collections.stdout:
            say("  " + GREEN + "ok" + RESET + "  Ansible collection community.docker is installed")
        else:
            problems.append("Ansible collection missing:  ansible-galaxy collection install community.docker")
    else:
        problems.append("Ansible missing:  sudo apt install ansible")

    from sentinel import notifier, users
    if notifier.is_configured():
        active = [user for user in users.list_users() if user["status"] == users.STATUS_ACTIVE]
        if active:
            say("  " + GREEN + "ok" + RESET + "  Telegram: alerts go to " + ", ".join(u["name"] for u in active))
        else:
            say("  " + YELLOW + "!!" + RESET + "  Telegram token is set, but nobody is registered yet - "
                "alerts are only printed (see README, 'Telegram setup')")
    else:
        say("  " + YELLOW + "!!" + RESET + "  Telegram not set up (SENTINEL_TELEGRAM_TOKEN) - alerts are only printed here")

    if problems:
        say()
        for problem in problems:
            say("  " + RED + "MISSING" + RESET + "  " + problem)
        sys.exit(1)

    try:
        client.images.get("alpine")
    except Exception:
        say("  pulling alpine image...")
        client.images.pull("alpine", tag="latest")

    return client


# ---------------------------------------------------------------------------
# Sentinel running in the background
# ---------------------------------------------------------------------------

class SentinelProcess:
    """
    Runs `python3 -m sentinel watch` and keeps every output line, so the
    test can wait for the notifications it expects.
    """

    def __init__(self):
        self.lines = []
        self.lock = threading.Lock()
        self.process = None
        self.log = None

    def start(self):
        environment = dict(os.environ)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["SENTINEL_MEMORY_INTERVAL"] = "2"   # fast memory sampling for the leak test
        environment["NO_COLOR"] = "1"
        environment["PYTHONPATH"] = ROOT + os.pathsep + environment.get("PYTHONPATH", "")

        self.log = open(LOG_FILE, "w")
        self.process = subprocess.Popen([sys.executable, "-m", "sentinel", "watch"], cwd=ROOT,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        text=True, env=environment)
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            line = line.rstrip("\n")
            self.log.write(line + "\n")
            self.log.flush()
            with self.lock:
                self.lines.append(line)
            if line.startswith(("[notify]", "[recovery]", "[bot]")):
                say("  " + DIM + "sentinel> " + RESET + line)
            elif line.startswith("         ") and self.lines and len(self.lines) > 1:
                previous = self.lines[-2]
                if previous.startswith("[notify]") or previous.startswith("         "):
                    say("  " + DIM + "sentinel> " + RESET + line)

    def count(self):
        with self.lock:
            return len(self.lines)

    def wait_for(self, texts, since=0, timeout=90):
        """
        Wait until one output line contains ALL of the given texts.
        Returns True if it appeared in time.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self.lock:
                for line in self.lines[since:]:
                    if all(text in line for text in texts):
                        return True
            if self.process.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    def stop(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
        if self.log:
            self.log.close()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

class Scenario:
    def __init__(self, client, sentinel):
        self.client = client
        self.sentinel = sentinel
        self.checks = []

    def check(self, label, ok):
        self.checks.append((label, ok))
        mark = GREEN + "PASS" + RESET if ok else RED + "FAIL" + RESET
        say("  [" + mark + "] " + label)
        return ok

    def expect(self, label, texts, since, timeout=90):
        return self.check(label, self.sentinel.wait_for(texts, since, timeout))

    def run_container(self, name, command, image="alpine", **options):
        self.remove(name)
        labels = {LABEL: RUN_ID}
        return self.client.containers.run(image, command, name=name, detach=True, labels=labels, **options)

    def remove(self, name):
        import docker
        try:
            self.client.containers.get(name).remove(force=True)
        except docker.errors.NotFound:
            pass

    def container(self, name):
        import docker
        try:
            return self.client.containers.get(name)
        except docker.errors.NotFound:
            return None


def build_image(client, tag, files):
    """
    Build an image from in-memory files {"Dockerfile": "...", "app.sh": "..."}.
    """
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        for name, content in files.items():
            data = content.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    archive.seek(0)
    image, _ = client.images.build(fileobj=archive, custom_context=True, tag=tag, rm=True,
                                   labels={LABEL: RUN_ID})
    return image


def wait_good_run(app_key, timeout=45):
    """
    Wait until Sentinel has saved a good run of this app (15 s running).
    """
    from sentinel import store
    deadline = time.time() + timeout
    while time.time() < deadline:
        if store.last_good_snapshot(app_key) is not None:
            return True
        time.sleep(1)
    return False


# ---------------------------------------------------------------------------
# scenarios
# ---------------------------------------------------------------------------

def scenario_restart(s):
    """Temporary failure: 'connection refused' once, then it works -> restart."""
    name = "st-restart-" + RUN_ID
    say("  container " + name + ": fails once with 'connection refused', works after a restart")
    since = s.sentinel.count()
    s.run_container(name, ["sh", "-c",
                           "if [ ! -f /tmp/started ]; then touch /tmp/started; "
                           "echo 'db:5432 connect: connection refused'; exit 1; fi; "
                           "echo 'app running'; sleep 3600"])
    s.expect("failure alert sent", [name, "crashed"], since)
    s.expect("Ansible restart.yml ran", ["restart.yml", name], since)
    s.expect("recovered alert sent", [name, "recovered"], since, timeout=60)
    container = s.container(name)
    s.check("container is running again", container is not None and container.status == "running")


def scenario_rollback(s):
    """A new image build broke it -> roll back to the last good image."""
    name = "st-rollback-" + RUN_ID
    tag = "sentinel-live-app:" + RUN_ID
    app = ('if [ ! -f config.txt ]; then echo "Error: config.txt not found"; exit 1; fi\n'
           'echo "app running with $(cat config.txt)"\nsleep 3600\n')
    good = build_image(s.client, tag, {
        "Dockerfile": "FROM alpine\nWORKDIR /app\nCOPY config.txt .\nCOPY app.sh .\nCMD [\"sh\", \"app.sh\"]\n",
        "config.txt": "mode=production\n", "app.sh": app})
    say("  image v1 (works) built: " + good.short_id)
    s.run_container(name, None, image=tag)
    say("  running v1, waiting until Sentinel saves it as a good run (15 s)...")
    s.check("good run of v1 saved", wait_good_run("name:" + name))

    bad = build_image(s.client, tag, {
        "Dockerfile": "FROM alpine\nWORKDIR /app\nCOPY app.sh .\nCMD [\"sh\", \"app.sh\"]\n",
        "app.sh": app})
    say("  image v2 built WITHOUT 'COPY config.txt .': " + bad.short_id + " - redeploying")
    since = s.sentinel.count()
    s.run_container(name, None, image=tag)
    s.expect("failure alert sent", [name, "crashed"], since)
    s.expect("Ansible rollback.yml ran", ["rollback.yml", name], since)
    s.expect("recovered alert sent", [name, "recovered"], since, timeout=60)
    container = s.container(name)
    s.check("container runs the v1 image again",
            container is not None and container.status == "running" and container.image.id == good.id)


def scenario_memory(s):
    """Out of memory -> raise the memory limit (32 MB -> 48 MB)."""
    name = "st-memory-" + RUN_ID
    say("  container " + name + ": needs 40 MB, limit is 32 MB")
    since = s.sentinel.count()
    s.run_container(name, ["sh", "-c",
                           "rm -f /dev/shm/data; "
                           "dd if=/dev/zero of=/dev/shm/data bs=1M count=40 2>/dev/null || exit 1; "
                           "echo 'loaded 40 MB of data'; sleep 3600"],
                    mem_limit="32m", memswap_limit="32m", shm_size="128m")
    s.expect("out-of-memory alert sent", [name, "crashed"], since)
    s.expect("Ansible raise_memory.yml ran", ["raise_memory.yml", name], since)
    s.expect("recovered alert sent", [name, "recovered"], since, timeout=60)
    container = s.container(name)
    memory = container.attrs["HostConfig"]["Memory"] if container else 0
    s.check("memory limit is now 48 MB (was 32 MB)", memory == 48 * MB)


def scenario_fails_again(s):
    """Keeps failing after recovery -> 'failed again' alerts, then 'cannot recover'."""
    name = "st-failagain-" + RUN_ID
    say("  container " + name + ": always fails with 'connection refused'")
    since = s.sentinel.count()
    s.run_container(name, ["sh", "-c", "echo 'api:8080 connect: connection refused'; exit 1"])
    s.expect("failure alert sent", [name, "crashed"], since)
    s.expect("'failed again after recovery' alert sent", [name, "FAILED AGAIN after recovery"], since)
    s.expect("'cannot be recovered' alert sent", [name, "cannot be recovered automatically"], since, timeout=120)


def scenario_needs_human(s):
    """Start command does not exist -> restart cannot help -> alert a human."""
    import docker
    name = "st-human-" + RUN_ID
    say("  container " + name + ": its start command does not exist")
    since = s.sentinel.count()
    s.remove(name)
    container = s.client.containers.create("alpine", ["no-such-command"], name=name, labels={LABEL: RUN_ID})
    try:
        container.start()
    except docker.errors.APIError:
        pass  # expected: Docker cannot start it
    s.expect("'cannot be recovered' alert sent", [name, "cannot be recovered automatically"], since)


def scenario_memory_leak(s):
    """Memory grows steadily towards the limit -> memory leak warning."""
    name = "st-leak-" + RUN_ID
    say("  container " + name + ": memory grows 2 MB every second, limit 64 MB")
    since = s.sentinel.count()
    s.run_container(name, ["sh", "-c",
                           "i=0; while [ $i -lt 28 ]; do "
                           "dd if=/dev/zero of=/dev/shm/leak$i bs=1M count=2 2>/dev/null; "
                           "i=$((i+1)); sleep 1; done; sleep 3600"],
                    mem_limit="64m", memswap_limit="64m", shm_size="128m")
    s.expect("memory leak alert sent", ["possible memory leak in " + name], since, timeout=90)


SCENARIOS = [
    ("restart", scenario_restart),
    ("rollback", scenario_rollback),
    ("memory", scenario_memory),
    ("fails-again", scenario_fails_again),
    ("needs-human", scenario_needs_human),
    ("memory-leak", scenario_memory_leak),
]


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def cleanup(client):
    step("Cleaning up")
    for container in client.containers.list(all=True, filters={"label": LABEL + "=" + RUN_ID}):
        container.remove(force=True)
        say("  removed container " + container.name)
    for image in client.images.list(filters={"label": LABEL + "=" + RUN_ID}):
        try:
            client.images.remove(image.id, force=True)
        except Exception:
            pass
    say("  removed test images")


def main():
    arguments = sys.argv[1:]
    keep = "--keep" in arguments
    names = [a for a in arguments if not a.startswith("--")]

    if "--list" in arguments:
        for name, function in SCENARIOS:
            say("  %-12s %s" % (name, function.__doc__))
        return

    selected = [(n, f) for n, f in SCENARIOS if not names or n in names]
    if not selected:
        say("Unknown scenario. Use --list.")
        sys.exit(2)

    client = check_prerequisites()

    step("Starting sentinel watch (output saved to live_test.log)")
    sentinel = SentinelProcess()
    sentinel.start()
    if not sentinel.wait_for(["is watching your containers"], timeout=20):
        say(RED + "Sentinel did not start. See live_test.log." + RESET)
        sentinel.stop()
        sys.exit(1)
    time.sleep(2)

    results = []
    try:
        for name, function in selected:
            step("Scenario: " + name + " - " + function.__doc__)
            scenario = Scenario(client, sentinel)
            started = time.time()
            try:
                function(scenario)
            except Exception as error:
                scenario.check("scenario ran without errors (" + str(error) + ")", False)
            passed = all(ok for _, ok in scenario.checks)
            results.append((name, passed, len(scenario.checks), time.time() - started))
    except KeyboardInterrupt:
        say("\nInterrupted.")
    finally:
        sentinel.stop()
        if not keep:
            cleanup(client)

    step("Summary")
    for name, passed, count, seconds in results:
        mark = GREEN + "PASS" + RESET if passed else RED + "FAIL" + RESET
        say("  [" + mark + "] %-12s %d checks, %d s" % (name, count, seconds))
    failed = [name for name, passed, _, _ in results if not passed]
    say()
    if failed:
        say(RED + "Failed: " + ", ".join(failed) + RESET + "   (full Sentinel output: live_test.log)")
        sys.exit(1)
    say(GREEN + "All scenarios passed." + RESET)


if __name__ == "__main__":
    main()
