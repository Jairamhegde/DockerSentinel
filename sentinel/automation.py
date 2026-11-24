"""
Automation (Ansible).

Runs the recovery playbooks in sentinel/playbooks/ with `ansible-playbook`
on this machine (connection: local), using the community.docker collection.

    restart.yml        restart the container
    raise_memory.yml   raise its memory limit and start it
    rollback.yml       re-create it from the last good image

The functions that build the playbook variables are pure (they only read
`docker inspect` dictionaries), so they can be tested without Docker.

Playbook variables can contain environment values (passwords, API keys),
so they are written to a private temporary file that is deleted right
after the playbook has run, and never stored or printed.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile


PLAYBOOK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "playbooks")

PLAYBOOKS = {
    "restart": "restart.yml",
    "raise_memory": "raise_memory.yml",
    "rollback": "rollback.yml",
}

TIMEOUT_SECONDS = 180
OUTPUT_LINES_KEPT = 15


def ansible_available():
    return shutil.which("ansible-playbook") is not None


def container_name(attrs):
    return attrs["Name"].lstrip("/")


def restart_vars(attrs):
    return {
        "container_name": container_name(attrs),
        "container_image": attrs["Image"],
    }


def raise_memory_vars(attrs, new_memory):
    variables = {
        "container_name": container_name(attrs),
        "container_image": attrs["Image"],
        "memory": str(new_memory),
    }
    # Docker's default swap limit is twice the memory limit; -1 means unlimited.
    if attrs["HostConfig"].get("MemorySwap", 0) != -1:
        variables["memory_swap"] = str(new_memory * 2)
    return variables


def format_port(container_port, binding):
    """
    ("80/tcp", {"HostIp": "", "HostPort": "8080"})  ->  "8080:80"
    """
    port, _, protocol = container_port.partition("/")
    target = port if protocol in ("", "tcp") else port + "/" + protocol

    host_port = (binding or {}).get("HostPort", "")
    host_ip = (binding or {}).get("HostIp", "")
    if host_port == "":
        return target
    if host_ip not in ("", "0.0.0.0", "::"):
        return host_ip + ":" + host_port + ":" + target
    return host_port + ":" + target


def format_mount(mount):
    mode = "" if mount.get("RW", True) else ":ro"
    if mount["Type"] == "volume":
        return mount["Name"] + ":" + mount["Destination"] + mode
    return mount["Source"] + ":" + mount["Destination"] + mode


def rollback_vars(attrs, current_image_config, good_image_id):
    """
    Variables to re-create a container from an older image.

    `current_image_config` is the Config section of the image the failing
    container uses. A setting that is the same as that image's default
    (CMD, ENTRYPOINT, ENV, WORKDIR, USER, labels) came from the new image,
    not from the user, so it is left out: the old image's own default
    applies again. Settings the user gave to `docker run` are kept.
    """
    config = attrs["Config"]
    host = attrs["HostConfig"]
    image = current_image_config or {}

    variables = {
        "container_name": container_name(attrs),
        "image": good_image_id,
    }

    if config.get("Cmd") and config["Cmd"] != image.get("Cmd"):
        variables["command"] = config["Cmd"]

    if config.get("Entrypoint") and config["Entrypoint"] != image.get("Entrypoint"):
        variables["entrypoint"] = config["Entrypoint"]

    image_env = set(image.get("Env") or [])
    env = {}
    for item in config.get("Env") or []:
        if item in image_env:
            continue
        name, _, value = item.partition("=")
        env[name] = value
    if env:
        variables["env"] = env

    if config.get("WorkingDir") and config["WorkingDir"] != image.get("WorkingDir"):
        variables["working_dir"] = config["WorkingDir"]

    if config.get("User") and config["User"] != image.get("User"):
        variables["user"] = config["User"]

    ports = []
    for container_port, bindings in (host.get("PortBindings") or {}).items():
        for binding in bindings or [None]:
            ports.append(format_port(container_port, binding))
    if ports:
        variables["ports"] = ports

    volumes = [format_mount(mount) for mount in attrs.get("Mounts") or []]
    if volumes:
        variables["volumes"] = volumes

    policy = (host.get("RestartPolicy") or {}).get("Name", "")
    if policy not in ("", "no"):
        variables["restart_policy"] = policy

    if host.get("Memory", 0) > 0:
        variables["memory"] = str(host["Memory"])

    network_mode = host.get("NetworkMode", "")
    if network_mode not in ("", "default", "bridge"):
        variables["network_mode"] = network_mode

    image_labels = image.get("Labels") or {}
    labels = {}
    for key, value in (config.get("Labels") or {}).items():
        if image_labels.get(key) != value:
            labels[key] = value
    if labels:
        variables["labels"] = labels

    return variables


def run_playbook(action, variables):
    """
    Run one recovery playbook. Returns (ok, output) where output is the
    last lines of Ansible's output, for the terminal and notifications.
    """
    if not ansible_available():
        return False, "Ansible is not installed (ansible-playbook not found)."

    playbook = os.path.join(PLAYBOOK_DIR, PLAYBOOKS[action])

    # The docker modules run with the same Python as Sentinel, which already
    # has the docker library installed.
    variables = dict(variables)
    variables["ansible_python_interpreter"] = sys.executable

    handle, vars_path = tempfile.mkstemp(prefix="sentinel-", suffix=".json")
    try:
        with os.fdopen(handle, "w") as file:
            json.dump(variables, file)

        command = ["ansible-playbook", "-i", "localhost,", "-c", "local", playbook, "-e", "@" + vars_path]
        environment = dict(os.environ)
        environment["ANSIBLE_NOCOLOR"] = "1"
        environment["ANSIBLE_LOCALHOST_WARNING"] = "False"
        environment["ANSIBLE_INVENTORY_UNPARSED_WARNING"] = "False"
        environment["ANSIBLE_RETRY_FILES_ENABLED"] = "False"

        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=TIMEOUT_SECONDS, env=environment)
        except subprocess.TimeoutExpired:
            return False, "Ansible did not finish within " + str(TIMEOUT_SECONDS) + " seconds."
    finally:
        os.remove(vars_path)

    output = (result.stdout + "\n" + result.stderr).strip().splitlines()
    tail = "\n".join(line for line in output[-OUTPUT_LINES_KEPT:] if line.strip() != "")
    return result.returncode == 0, tail
