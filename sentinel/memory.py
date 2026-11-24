"""
Memory leak detection.

A memory leak does not crash a container at once: its memory use just
keeps growing until it hits the limit and is killed. Sentinel samples the
memory of every running container and warns BEFORE that happens.

A container "looks like it is leaking" when, over the last WINDOW samples:
  - memory went up in almost every sample (at least RISING_SHARE of them)
  - it grew by at least MIN_GROWTH (20%) in total
  - and it is getting close to its limit (at least NEAR_LIMIT = 70%),
    or, without a limit, it grew by at least NO_LIMIT_GROWTH (50%)

With the default 30 s interval and 10 samples, that is a steady rise over
about 5 minutes. A warning is sent at most once per COOLDOWN per container.

The interval can be changed with SENTINEL_MEMORY_INTERVAL (seconds).
"""

import os
import time
from collections import deque
from datetime import datetime, timedelta


WINDOW = 10
RISING_SHARE = 0.8
MIN_GROWTH = 0.20
NEAR_LIMIT = 0.70
NO_LIMIT_GROWTH = 0.50
COOLDOWN = timedelta(minutes=30)
DEFAULT_INTERVAL = 30


def get_interval():
    value = os.environ.get("SENTINEL_MEMORY_INTERVAL", "")
    try:
        seconds = float(value)
    except ValueError:
        return DEFAULT_INTERVAL
    return max(seconds, 1.0)


def format_mb(number):
    return str(round(number / (1024 * 1024))) + " MB"


def working_set(stats):
    """
    Memory really in use, the same number `docker stats` shows:
    total usage minus file cache the kernel can drop at any time.
    Returns (used_bytes, limit_bytes), or None if stats are missing.
    """
    memory = stats.get("memory_stats") or {}
    usage = memory.get("usage")
    limit = memory.get("limit")
    if usage is None or limit is None:
        return None

    details = memory.get("stats") or {}
    cache = details.get("inactive_file", details.get("total_inactive_file", 0))
    if cache < usage:
        usage = usage - cache

    return usage, limit


def check_leak(samples, limit, has_limit):
    """
    Pure logic: decide if a list of memory samples (bytes, oldest first)
    looks like a leak. Returns a description string, or None.
    """
    if len(samples) < WINDOW:
        return None

    recent = list(samples)[-WINDOW:]
    first = recent[0]
    last = recent[-1]
    if first <= 0:
        return None

    rising = 0
    for i in range(1, len(recent)):
        if recent[i] > recent[i - 1]:
            rising = rising + 1

    if rising < RISING_SHARE * (len(recent) - 1):
        return None

    growth = (last - first) / first
    if growth < MIN_GROWTH:
        return None

    if has_limit:
        share = last / limit
        if share < NEAR_LIMIT:
            return None
        return ("Memory keeps growing: " + format_mb(first) + " -> " + format_mb(last) +
                " (+" + str(round(growth * 100)) + "%), now " + str(round(share * 100)) +
                "% of its " + format_mb(limit) + " limit.")

    if growth < NO_LIMIT_GROWTH:
        return None
    return ("Memory keeps growing: " + format_mb(first) + " -> " + format_mb(last) +
            " (+" + str(round(growth * 100)) + "%) and the container has no memory limit.")


class LeakWatcher:
    """
    Keeps the recent memory samples of every running container.
    """

    def __init__(self):
        self.samples = {}
        self.last_warning = {}

    def add_sample(self, container_id, used, limit, has_limit, now=None):
        """
        Record one sample. Returns a leak description when a NEW warning
        should be sent, otherwise None.
        """
        if now is None:
            now = datetime.now()

        history = self.samples.get(container_id)
        if history is None:
            history = deque(maxlen=WINDOW)
            self.samples[container_id] = history
        history.append(used)

        description = check_leak(history, limit, has_limit)
        if description is None:
            return None

        warned_at = self.last_warning.get(container_id)
        if warned_at is not None and now - warned_at < COOLDOWN:
            return None

        self.last_warning[container_id] = now
        return description

    def forget_missing(self, running_ids):
        for container_id in list(self.samples):
            if container_id not in running_ids:
                del self.samples[container_id]


def watch_memory(on_leak):
    """
    Runs forever in its own thread, with its own Docker connection.
    on_leak(container, description) is called for every new warning.
    """
    import docker

    client = docker.from_env()
    watcher = LeakWatcher()

    while True:
        running_ids = set()

        for container in client.containers.list():
            running_ids.add(container.id)
            try:
                stats = client.api.stats(container.id, stream=False, one_shot=True)
            except docker.errors.APIError:
                continue

            numbers = working_set(stats)
            if numbers is None:
                continue

            used, limit = numbers
            has_limit = container.attrs["HostConfig"]["Memory"] > 0
            description = watcher.add_sample(container.id, used, limit, has_limit)
            if description is not None:
                on_leak(container, description)

        watcher.forget_missing(running_ids)
        time.sleep(get_interval())
