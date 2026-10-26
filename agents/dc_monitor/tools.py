"""Read-only monitoring tools for the containerlab datacentre (lab name: dc01).

The agent can only call the functions below, and only with the device names and
check names listed here, so it cannot run arbitrary commands on the lab.
"""
import ipaddress
import subprocess

LAB = "dc01"
ROUTERS = ["spine1", "leaf1", "leaf2"]
HOSTS = ["host1", "host2"]

# check name -> the one vtysh command it is allowed to run
CHECKS = {
    "interfaces": "show interface brief",
    "bgp": "show bgp summary",
    "routes": "show ip route",
    "version": "show version",
}


def _container(name: str) -> str:
    return f"clab-{LAB}-{name}"


def _run(cmd: list, timeout: int = 15) -> dict:
    """Run a command without a shell and return a result the agent can read."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "command timed out"}
    except FileNotFoundError:
        return {"status": "error", "error": "docker was not found on this machine"}
    if result.returncode != 0:
        return {"status": "error", "error": (result.stderr or result.stdout).strip()}
    return {"status": "success", "output": result.stdout.strip()}


def list_devices() -> dict:
    """Lists the routers and hosts in the datacentre lab.

    Returns:
        dict: the router names (spine1, leaf1, leaf2) and host names (host1, host2).
    """
    return {"status": "success", "routers": ROUTERS, "hosts": HOSTS}


def run_check(device: str, check: str) -> dict:
    """Runs one read-only check on a router.

    Args:
        device: router name, one of spine1, leaf1, leaf2.
        check: one of interfaces, bgp, routes, version.

    Returns:
        dict: status and the raw command output.
    """
    if device not in ROUTERS:
        return {"status": "error", "error": f"unknown router '{device}', use one of {ROUTERS}"}
    if check not in CHECKS:
        return {"status": "error", "error": f"unknown check '{check}', use one of {list(CHECKS)}"}
    return _run(["docker", "exec", _container(device), "vtysh", "-c", CHECKS[check]])


def get_resource_usage(device: str) -> dict:
    """Gets the CPU and memory use of one device's container.

    Args:
        device: any router or host name from list_devices.

    Returns:
        dict: status and a one-line CPU and memory summary.
    """
    if device not in ROUTERS + HOSTS:
        return {"status": "error", "error": f"unknown device '{device}'"}
    fmt = "CPU {{.CPUPerc}}, memory {{.MemUsage}} ({{.MemPerc}})"
    return _run(["docker", "stats", "--no-stream", "--format", fmt, _container(device)])


def ping_test(source_host: str, target_ip: str) -> dict:
    """Pings an IP address from one of the lab hosts to test end-to-end connectivity.

    Args:
        source_host: host name, host1 or host2.
        target_ip: the IPv4 address to ping, for example 192.168.102.10.

    Returns:
        dict: status and the ping output (with the packet loss line).
    """
    if source_host not in HOSTS:
        return {"status": "error", "error": f"unknown host '{source_host}', use one of {HOSTS}"}
    try:
        ip = ipaddress.IPv4Address(target_ip)
    except ValueError:
        return {"status": "error", "error": f"'{target_ip}' is not a valid IPv4 address"}
    return _run(["docker", "exec", _container(source_host), "ping", "-c", "3", "-W", "1", str(ip)], timeout=20)
