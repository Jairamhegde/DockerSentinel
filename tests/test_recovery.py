"""
Tests for the recovery decision, the memory-leak check and the Ansible
variables. Pure logic only: no Docker, no Ansible, no network needed.

    python3 -m unittest discover -s tests
"""

import os
import sys
import types
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sentinel import automation, memory, notifier, recovery  # noqa: E402

MB = 1024 * 1024


def evidence(**overrides):
    base = {
        "id": "abc", "name": "web", "image": "web", "status": "exited", "exit_code": 1,
        "oom_killed": False, "error": "", "health": "", "restart_count": 0, "logs": "",
    }
    base.update(overrides)
    return base


def container(memory_limit=0, policy="no", image_id="sha256:new"):
    return {"memory": memory_limit, "restart_policy": policy, "image_id": image_id}


def diff(kind, key, old="a", new="b"):
    return {"kind": kind, "key": key, "old": old, "new": new}


class DecideTests(unittest.TestCase):

    def decide(self, trigger="crashed", ev=None, diffs=(), box=None, good=None, tried=()):
        return recovery.decide(trigger, ev or evidence(), list(diffs), box or container(), good, list(tried))

    def test_oom_with_limit_raises_memory_by_half(self):
        result = self.decide(ev=evidence(oom_killed=True, exit_code=137), box=container(256 * MB))
        self.assertEqual(result["action"], "raise_memory")
        self.assertEqual(result["memory"], 384 * MB)

    def test_oom_raised_twice_gives_up_as_leak(self):
        result = self.decide(ev=evidence(oom_killed=True), box=container(256 * MB),
                             tried=["raise_memory", "raise_memory"])
        self.assertEqual(result["action"], "give_up")
        self.assertIn("memory leak", result["reason"])

    def test_oom_without_limit_restarts_once(self):
        ev = evidence(oom_killed=True)
        self.assertEqual(self.decide(ev=ev)["action"], "restart")
        self.assertEqual(self.decide(ev=ev, tried=["restart"])["action"], "give_up")

    def test_removed_dockerfile_step_rolls_back(self):
        diffs = [diff("removed", "build: COPY config.txt .", "3fa2", None)]
        ev = evidence(logs="Error: config.txt not found")
        result = self.decide(ev=ev, diffs=diffs, good="sha256:old")
        self.assertEqual(result["action"], "rollback")
        self.assertEqual(result["image"], "sha256:old")

    def test_changed_cmd_in_new_image_rolls_back(self):
        diffs = [diff("changed", "command", "sh app.sh", "sh app2.sh"), diff("changed", "image.id")]
        self.assertEqual(self.decide(diffs=diffs, good="sha256:old")["action"], "rollback")

    def test_no_rollback_without_good_image(self):
        diffs = [diff("removed", "build: COPY config.txt .")]
        ev = evidence(logs="cat: can't open 'config.txt': No such file or directory")
        self.assertEqual(self.decide(ev=ev, diffs=diffs, good=None)["action"], "give_up")

    def test_rollback_tried_already_then_gives_up(self):
        diffs = [diff("removed", "build: COPY config.txt .")]
        ev = evidence(exit_code=127)
        self.assertEqual(self.decide(ev=ev, diffs=diffs, good="sha256:old", tried=["rollback"])["action"], "give_up")

    def test_removed_env_var_needs_a_human(self):
        diffs = [diff("removed", "env.API_KEY"), diff("changed", "image.id")]
        result = self.decide(diffs=diffs, good="sha256:old")
        self.assertEqual(result["action"], "give_up")
        self.assertIn("API_KEY", result["reason"])

    def test_permanent_errors_are_not_restarted(self):
        for ev in (evidence(exit_code=127), evidence(error="Bind for 0.0.0.0:80 failed: port is already allocated"),
                   evidence(logs="ModuleNotFoundError: No module named 'flask'")):
            self.assertEqual(self.decide(ev=ev)["action"], "give_up")
        self.assertEqual(self.decide(trigger="failed to start", ev=evidence(error="boom"))["action"], "give_up")

    def test_connection_refused_restarts(self):
        result = self.decide(ev=evidence(logs="connect: connection refused"))
        self.assertEqual(result["action"], "restart")

    def test_nothing_changed_restarts_then_gives_up(self):
        self.assertEqual(self.decide()["action"], "restart")
        self.assertEqual(self.decide(tried=["restart"])["action"], "restart")
        self.assertEqual(self.decide(tried=["restart", "restart"])["action"], "give_up")

    def test_restart_policy_means_wait(self):
        self.assertEqual(self.decide(box=container(policy="always"))["action"], "wait")

    def test_crash_loop_gives_up(self):
        self.assertEqual(self.decide(trigger="restart loop", ev=evidence(restart_count=6))["action"], "give_up")

    def test_max_attempts(self):
        tried = ["restart", "rollback", "restart"]
        self.assertEqual(self.decide(tried=tried)["action"], "give_up")


class LeakTests(unittest.TestCase):

    def test_steady_growth_near_limit_is_a_leak(self):
        samples = [100 * MB + i * 40 * MB for i in range(10)]   # 100 -> 460 MB
        self.assertIsNotNone(memory.check_leak(samples, 600 * MB, True))

    def test_growth_far_from_limit_is_not_a_leak(self):
        samples = [100 * MB + i * 10 * MB for i in range(10)]   # 100 -> 190 MB of 2 GB
        self.assertIsNone(memory.check_leak(samples, 2048 * MB, True))

    def test_flat_or_noisy_memory_is_not_a_leak(self):
        flat = [300 * MB] * 10
        noisy = [300 * MB, 350 * MB] * 5
        self.assertIsNone(memory.check_leak(flat, 400 * MB, True))
        self.assertIsNone(memory.check_leak(noisy, 400 * MB, True))

    def test_no_limit_needs_fifty_percent_growth(self):
        self.assertIsNone(memory.check_leak([100 * MB + i * 4 * MB for i in range(10)], 16000 * MB, False))
        self.assertIsNotNone(memory.check_leak([100 * MB + i * 10 * MB for i in range(10)], 16000 * MB, False))

    def test_warning_only_once_per_cooldown(self):
        watcher = memory.LeakWatcher()
        now = datetime(2026, 1, 1, 12, 0)
        warnings = []
        for i in range(12):
            warnings.append(watcher.add_sample("c1", 100 * MB + i * 40 * MB, 600 * MB, True, now))
        self.assertEqual(len([w for w in warnings if w]), 1)
        later = now + timedelta(minutes=31)
        self.assertIsNotNone(watcher.add_sample("c1", 600 * MB, 700 * MB, True, later))

    def test_working_set_subtracts_cache(self):
        stats = {"memory_stats": {"usage": 500, "limit": 1000, "stats": {"inactive_file": 200}}}
        self.assertEqual(memory.working_set(stats), (300, 1000))


class AnsibleVarsTests(unittest.TestCase):

    def attrs(self):
        return {
            "Name": "/web",
            "Image": "sha256:new",
            "Config": {
                "Cmd": ["sh", "app2.sh"], "Entrypoint": None, "WorkingDir": "/app", "User": "",
                "Env": ["PATH=/usr/bin", "API_KEY=secret"], "Labels": {"from.image": "x", "team": "shop"},
            },
            "HostConfig": {
                "PortBindings": {"80/tcp": [{"HostIp": "", "HostPort": "8080"}], "53/udp": [{"HostIp": "127.0.0.1", "HostPort": "5353"}]},
                "RestartPolicy": {"Name": "unless-stopped"}, "Memory": 256 * MB, "MemorySwap": 512 * MB,
                "NetworkMode": "shopnet",
            },
            "Mounts": [{"Type": "bind", "Source": "/srv/cfg", "Destination": "/app/cfg", "RW": False},
                       {"Type": "volume", "Name": "data", "Destination": "/data", "RW": True}],
        }

    def test_rollback_keeps_user_settings_and_drops_new_image_defaults(self):
        image_config = {"Cmd": ["sh", "app2.sh"], "Env": ["PATH=/usr/bin"], "WorkingDir": "/app",
                        "Labels": {"from.image": "x"}}
        result = automation.rollback_vars(self.attrs(), image_config, "sha256:old")
        self.assertEqual(result["image"], "sha256:old")
        self.assertNotIn("command", result)          # came from the new image
        self.assertNotIn("working_dir", result)
        self.assertEqual(result["env"], {"API_KEY": "secret"})
        self.assertEqual(sorted(result["ports"]), ["127.0.0.1:5353:53/udp", "8080:80"])
        self.assertEqual(result["volumes"], ["/srv/cfg:/app/cfg:ro", "data:/data"])
        self.assertEqual(result["restart_policy"], "unless-stopped")
        self.assertEqual(result["network_mode"], "shopnet")
        self.assertEqual(result["labels"], {"team": "shop"})

    def test_rollback_keeps_command_given_to_docker_run(self):
        result = automation.rollback_vars(self.attrs(), {"Cmd": ["sh", "app.sh"]}, "sha256:old")
        self.assertEqual(result["command"], ["sh", "app2.sh"])

    def test_raise_memory_vars(self):
        result = automation.raise_memory_vars(self.attrs(), 384 * MB)
        self.assertEqual(result["memory"], str(384 * MB))
        self.assertEqual(result["memory_swap"], str(768 * MB))
        attrs = self.attrs()
        attrs["HostConfig"]["MemorySwap"] = -1
        self.assertNotIn("memory_swap", automation.raise_memory_vars(attrs, 384 * MB))

    def test_playbooks_exist(self):
        for name in automation.PLAYBOOKS.values():
            self.assertTrue(os.path.exists(os.path.join(automation.PLAYBOOK_DIR, name)), name)


class NotifierTests(unittest.TestCase):

    def test_message_format(self):
        text = notifier.format_message("recovered", "web recovered", ["line 1", "", "line 2"])
        self.assertTrue(text.startswith("✅ DockerSentinel: web recovered"))
        self.assertEqual(text.count("\n"), 2)

    def test_not_configured_does_not_send(self):
        os.environ.pop("SENTINEL_TELEGRAM_TOKEN", None)
        printed = []
        notifier.Notifier(printer=printed.append).notify("failed", "web crashed", ["Cause: x"])
        self.assertEqual(len(printed), 1)
        self.assertIn("web crashed", printed[0])


class DockerTimeTests(unittest.TestCase):

    def test_parse(self):
        parsed = recovery.parse_docker_time("2026-11-24T10:15:30.123456789Z")
        self.assertEqual(parsed.microsecond, 123456)
        self.assertIsNone(recovery.parse_docker_time("0001-01-01T00:00:00Z"))


if __name__ == "__main__":
    unittest.main()
