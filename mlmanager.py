import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

import requests


class Manager:
    def __init__(self):
        self.data = {}
        self.exit = threading.Event()
        with open("config.json") as json_file:
            self.data = json.load(json_file)
        self.url = urllib.parse.urljoin(
            self.data.get("frontendURL"), "api/get_data?show_devices=true"
        )
        self.hold = self.data["deviceHold"]
        self.allowed_devices = self.data["devices"]
        self.restart_enabled = self.data["restart"]["enabled"]
        self.restart_threshold = self.data["restart"]["threshold"]
        self.install_enabled = self.data["install"]["enabled"]
        self.install_threshold = self.data["install"]["threshold"]
        self.ipa_path = self.data["ipa"]
        self.save_screenshots = self.data["saveScreenshots"]
        self.device_action = {}
        self.debug = self.data.get("debug", False)
        if self.debug:
            # Our first handler is std_out, switch it to debug messages
            logger.handlers[0].setLevel(logging.DEBUG)
        self.heartbeat_time = self.data.get("heartbeatThreshold", 300)
        self.last_heartbeat = 0

    def run(self):
        logger.info("Start MacLessManager...")
        logger.info(f"Debug logging: {self.debug}")
        for sig in ("TERM", "HUP", "INT"):
            signal.signal(getattr(signal, "SIG" + sig), self.quit)

        while not self.exit.is_set():
            self.controller()
            self.exit.wait(30)

    def quit(self, signo, _frame):
        logger.info(f"Interrupted by {signo:d}, shutting down...")
        self.exit.set()

    def controller(self):
        devices = self.all_devices()
        devices_count = len(devices.keys())
        if not devices:
            logger.warning("Failed to load devices (or none connected)")
            time.sleep(1)

        status = self.device_status()
        status_count = len(status.keys())
        if not status:
            logger.warning("Failed to load status")
            time.sleep(1)

        if (self.current_time() - self.last_heartbeat) >= self.heartbeat_time:
            beat = f"Heartbeat {devices_count} connected, {status_count} status found"
            logger.info(beat)
            self.last_heartbeat = self.current_time()

        for device in devices:
            name = devices[device].decode("utf-8")
            if name not in status.keys():
                logger.debug(f"No RDM status for {name} skipping...")
                continue
            if self.allowed_devices and name not in self.allowed_devices:
                logger.debug("Device is not allowed skipping...")
                continue
            # Respect the last action so devices have enough time to start working
            last_action = self.device_action.get(name, 0)
            if (self.current_time() - last_action) <= self.hold:
                logger.debug(f"Need to wait longer before acting on {name}...")
                continue
            # Save device screenshot
            # TODO: We should respect timeouts and not screenshot every 30sec
            if self.save_screenshots:
                self.screenshot(device, name)
            # TODO: Install and restart happen in the same run. Need to delay restart action.
            if self.install_enabled and (
                status[name] + self.install_threshold <= self.current_time()
            ):
                if os.path.isfile(self.ipa_path):
                    logger.info(f"Installing ipa on device {name}...")
                    self.install(device)
                    self.device_action[name] = self.current_time()
                else:
                    logger.debug(f"No ipa file found at '{self.ipa_path}'")
            if self.restart_enabled and (
                status[name] + self.restart_threshold <= self.current_time()
            ):
                logger.info(f"Restarting device {name}...")
                self.restart(device)
                self.device_action[name] = self.current_time()

    def current_time(self):
        return int(time.time())

    def device_status(self):
        status = {}
        user = self.data.get("user")
        password = self.data.get("password")
        r = requests.get(self.url, auth=(user, password))
        if r.status_code == 200:
            json_data = r.json()["data"]
            devices = json_data["devices"]
            for d in devices:
                uuid = d["uuid"]
                seen = d["last_seen"]
                status[uuid] = seen
        return status

    def device_ids(self) -> list:
        cmd = ["idevice_id", "--list"]
        run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, err = run.communicate()
        output = output.decode("ascii").split("\n")
        if not output:
            data = []
        else:
            data = list(filter(None, output))
        return data

    def all_devices(self) -> dict:
        devices = {}
        uuids = self.device_ids()
        for uuid in uuids:
            cmd = ["idevicename", "--udid", str(uuid)]
            run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            output, err = run.communicate()
            devices[uuid] = output.strip()
        return devices

    def screenshot(self, uuid: str, name: str):
        cmd = ["idevicescreenshot", "--udid", uuid, f"{name}.png"]
        run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, err = run.communicate()
        output = output.decode("utf-8").strip()
        if "Screenshot saved to" not in output:
            logger.warning(f"Error taking screenshot on {name}: {output[0:36]}")

    def restart(self, uuid: str):
        cmd = ["idevicediagnostics", "restart", "--udid", uuid]
        run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, err = run.communicate()
        if err:
            logger.error(err)

    def install(self, uuid: str):
        ipa = self.ipa_path
        cmd = ["ios-deploy", "--bundle", ipa, "--id", uuid]
        run = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        output, err = run.communicate()
        if err:
            logger.error(err)


class LogFilter(logging.Filter):
    """Filters all messages with level < LEVEL"""
    # http://stackoverflow.com/a/24956305/408556
    def __init__(self, level):
        self.level = level

    def filter(self, record):
        return record.levelno < self.level


if __name__ == "__main__":
    # Set up a console logger
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.DEBUG)
    c_format = logging.Formatter(
        fmt="[%(asctime)s] [%(levelname)8s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    log_filter = LogFilter(logging.WARNING)
    stdout_handler = logging.StreamHandler(stream=sys.stdout)
    stdout_handler.addFilter(log_filter)
    stdout_handler.setLevel(logging.INFO)
    stdout_handler.setFormatter(c_format)
    stderr_handler = logging.StreamHandler(stream=sys.stderr)
    stderr_handler.setLevel(max(logging.INFO, logging.WARNING))
    stderr_handler.setFormatter(c_format)
    logger.addHandler(stdout_handler)
    logger.addHandler(stderr_handler)

    task = Manager()
    task.run()
