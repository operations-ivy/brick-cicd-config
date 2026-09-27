"""Checks on brick9000 itself: sshd, and its own vitals."""

import os
import subprocess


def sshd_running() -> bool:
    """True if sshd is up, either as a service or socket-activated."""
    try:
        result = subprocess.run(["systemctl", "is-active", "--quiet", "ssh.service"], timeout=5)
        if result.returncode == 0:
            return True
        result = subprocess.run(["systemctl", "is-active", "--quiet", "ssh.socket"], timeout=5)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class LocalVitals:
    """brick9000's own CPU %, memory %, CPU temperature and root disk %.

    brick9000 isn't a cluster node and runs no node_exporter, so these come
    straight from /proc and /sys. CPU % is measured between calls.
    """

    def __init__(self, name: str):
        self.name = name
        self._cpu = self._cpu_times()

    @staticmethod
    def _cpu_times() -> tuple[int, int]:
        with open("/proc/stat") as f:
            fields = [int(v) for v in f.readline().split()[1:]]
        idle = fields[3] + fields[4]  # idle + iowait
        return idle, sum(fields)

    def read(self) -> dict:
        vitals = {"node": self.name}
        idle, total = self._cpu_times()
        d_idle, d_total = idle - self._cpu[0], total - self._cpu[1]
        self._cpu = (idle, total)
        if d_total > 0:
            vitals["cpu"] = round(100 * (1 - d_idle / d_total), 1)

        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, value = line.split(":", 1)
                mem[key] = int(value.split()[0])
        vitals["mem"] = round(100 * (1 - mem["MemAvailable"] / mem["MemTotal"]), 1)

        try:
            with open("/sys/class/thermal/thermal_zone0/temp") as f:
                vitals["temp"] = round(int(f.read()) / 1000, 1)
        except OSError:
            pass

        st = os.statvfs("/")
        vitals["disk"] = round(100 * (1 - st.f_bavail / st.f_blocks), 1)
        return vitals
