"""Configuration tools for the containerlab datacentre (lab name: dc01).

Safety design (validate -> confirm -> execute -> verify):

  propose_change  validates the request, reads the CURRENT value from the router,
                  works out the risk, and stores a pending change. It changes nothing.
  apply_change    executes the pending change, but ONLY if the user has replied in a
                  LATER message than the proposal (checked with the ADK invocation id),
                  the change id matches, and the proposal has not expired. It then reads
                  the router back to verify, and writes an audit log entry.
  cancel_change   throws the pending change away.

The model can only change what these functions allow: four actions, on routers
spine1/leaf1/leaf2, on interfaces eth1/eth2 only. Commands are built as argument
lists (no shell), and every value is checked against a strict pattern first.
"""
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

LAB = "dc01"
ROUTERS = ["spine1", "leaf1", "leaf2"]
CHANGEABLE_INTERFACES = ["eth1", "eth2"]      # eth0 is the management link, lo is loopback
ACTIONS = ["hostname", "description", "shutdown", "no_shutdown"]
PENDING_TTL_SECONDS = 300                      # a proposal expires after 5 minutes
PENDING_KEY = "pending_change"

HOSTNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,30}[A-Za-z0-9]$|^[A-Za-z]$")
DESCRIPTION_RE = re.compile(r"^[A-Za-z0-9 _.,:/()+-]{1,80}$")

# What each interface is for, so the agent can explain the impact of a shutdown.
INTERFACE_ROLES = {
    ("spine1", "eth1"): "the link to leaf1 (BGP session 10.0.1.2)",
    ("spine1", "eth2"): "the link to leaf2 (BGP session 10.0.2.2)",
    ("leaf1", "eth1"): "the uplink to spine1 (its only BGP session)",
    ("leaf1", "eth2"): "the link to host1",
    ("leaf2", "eth1"): "the uplink to spine1 (its only BGP session)",
    ("leaf2", "eth2"): "the link to host2",
}

# vtysh prints these on every call; they are noise, not errors.
_NOISE = ("vtysh.conf", "processing failure", "setsockopt")

AUDIT_LOG = Path(os.getenv(
    "CONFIG_AUDIT_LOG",
    Path(__file__).resolve().parents[2] / "documentation" / "change_audit.jsonl",
))


# ---------------------------------------------------------------- helpers
def _container(name: str) -> str:
    return f"clab-{LAB}-{name}"


def _run(cmd: list, timeout: int = 20) -> dict:
    """Run a command without a shell and return its output."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "command timed out", "output": ""}
    except FileNotFoundError:
        return {"ok": False, "error": "docker was not found on this machine", "output": ""}
    text = ((r.stdout or "") + "\n" + (r.stderr or "")).strip()
    if r.returncode != 0:
        return {"ok": False, "error": text or f"exit code {r.returncode}", "output": text}
    return {"ok": True, "error": "", "output": text}


def _vtysh_errors(output: str) -> list:
    """Lines starting with '%' are FRR errors, except the harmless startup noise."""
    return [ln.strip() for ln in output.splitlines()
            if ln.strip().startswith("%") and not any(n in ln for n in _NOISE)]


def _parse_running_config(text: str) -> dict:
    """Pull the hostname and per-interface description/shutdown out of show running-config."""
    cfg = {"hostname": None, "interfaces": {}}
    current = None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if line.startswith("hostname "):
            cfg["hostname"] = stripped.split(None, 1)[1]
        elif line.startswith("interface "):
            current = stripped.split(None, 1)[1]
            cfg["interfaces"].setdefault(current, {"description": None, "shutdown": False})
        elif current and line.startswith(" description "):
            cfg["interfaces"][current]["description"] = stripped.split(None, 1)[1]
        elif current and stripped == "shutdown":
            cfg["interfaces"][current]["shutdown"] = True
        elif stripped in ("exit", "!") or (line and not line.startswith(" ")):
            if not line.startswith("interface "):
                current = None
    return cfg


def _read_config(device: str) -> dict:
    res = _run(["docker", "exec", _container(device), "vtysh", "-c", "show running-config"])
    if not res["ok"]:
        return {"ok": False, "error": res["error"]}
    cfg = _parse_running_config(res["output"])
    cfg["ok"] = True
    cfg["raw"] = res["output"]
    return cfg


def _link_state(device: str, interface: str) -> str:
    """Operational state from the kernel (UP / DOWN), independent of FRR's config."""
    res = _run(["docker", "exec", _container(device), "ip", "-br", "link", "show", interface])
    if not res["ok"]:
        return "UNKNOWN"
    parts = res["output"].split()
    return parts[1].upper() if len(parts) > 1 else "UNKNOWN"


def _audit(entry: dict) -> None:
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), **entry}
        with open(AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # never let a logging problem change the result of a change


def _commands_for(action: str, interface: str, value: str) -> list:
    """The exact vtysh -c arguments for a change."""
    cmds = ["configure terminal"]
    if action == "hostname":
        cmds.append(f"hostname {value}")
    else:
        cmds.append(f"interface {interface}")
        if action == "description":
            cmds.append(f"description {value}")
        elif action == "shutdown":
            cmds.append("shutdown")
        elif action == "no_shutdown":
            cmds.append("no shutdown")
    return cmds


# ---------------------------------------------------------------- tools
def propose_change(device: str, action: str, tool_context, interface: str = "", value: str = "") -> dict:
    """Step 1 of a configuration change: validate it and describe it. Changes NOTHING.

    Call this first for every change request. It checks the request, reads the current
    value from the router, and stores a pending change that the user must confirm.

    Args:
        device: router name, one of spine1, leaf1, leaf2.
        action: one of hostname, description, shutdown, no_shutdown.
        interface: eth1 or eth2. Required for description, shutdown and no_shutdown. Leave empty for hostname.
        value: the new hostname, or the new interface description. Leave empty for shutdown and no_shutdown.

    Returns:
        dict: status 'needs_confirmation' with a summary to show the user, or 'error' / 'no_change' with the reason.
    """
    device, action, interface, value = (device or "").strip(), (action or "").strip(), (interface or "").strip(), (value or "").strip()

    # ---- validate
    if device not in ROUTERS:
        return {"status": "error", "error": f"unknown router '{device}'. Routers are {ROUTERS}. Hosts cannot be configured."}
    if action not in ACTIONS:
        return {"status": "error", "error": f"unsupported action '{action}'. Supported actions are {ACTIONS}."}
    if action == "hostname":
        if interface:
            return {"status": "error", "error": "a hostname change does not take an interface."}
        if not HOSTNAME_RE.match(value):
            return {"status": "error", "error": "invalid hostname. Use 1-32 letters, digits or hyphens, starting with a letter and not ending with a hyphen."}
    else:
        if interface not in CHANGEABLE_INTERFACES:
            return {"status": "error", "error": f"interface '{interface}' cannot be changed. Allowed: {CHANGEABLE_INTERFACES}. eth0 is the management link and lo is the loopback."}
        if action == "description" and not DESCRIPTION_RE.match(value):
            return {"status": "error", "error": "invalid description. Use 1-80 characters: letters, digits, spaces and _ . , : / ( ) + -"}
        if action in ("shutdown", "no_shutdown") and value:
            return {"status": "error", "error": f"{action} does not take a value."}

    # ---- read the current state
    cfg = _read_config(device)
    if not cfg["ok"]:
        return {"status": "error", "error": f"could not read {device}: {cfg['error']}. Is the lab running?"}

    iface = cfg["interfaces"].get(interface, {"description": None, "shutdown": False}) if interface else {}
    if action == "hostname":
        previous, change_text = cfg["hostname"], f"rename {device}'s FRR hostname from '{cfg['hostname']}' to '{value}'"
        no_change = cfg["hostname"] == value
    elif action == "description":
        previous = iface.get("description")
        change_text = f"set the description of {device} {interface} to '{value}' (currently: {previous!r})"
        no_change = previous == value
    elif action == "shutdown":
        previous = "shutdown" if iface.get("shutdown") else "up"
        change_text = f"shut down {device} {interface}"
        no_change = bool(iface.get("shutdown"))
    else:
        previous = "shutdown" if iface.get("shutdown") else "up"
        change_text = f"bring up {device} {interface} (no shutdown)"
        no_change = not iface.get("shutdown")

    if no_change:
        return {"status": "no_change", "message": f"{device} is already in that state ({previous!r}); nothing to do."}

    # ---- risk
    risk, impact = "LOW", "Cosmetic change only. Traffic is not affected."
    if action == "shutdown":
        role = INTERFACE_ROLES.get((device, interface), "a lab link")
        risk = "HIGH"
        impact = f"{interface} is {role}. Shutting it down will drop that link and anything that depends on it."
    elif action == "hostname":
        impact = "Changes the name shown in the FRR prompt and configuration only. The Docker container name (clab-dc01-...) does not change, so the monitoring tools still work."
    elif action == "no_shutdown":
        impact = "Restores the link. BGP may take 10-30 seconds to re-establish."

    commands = _commands_for(action, interface, value)
    change = {
        "id": uuid.uuid4().hex[:6],
        "device": device, "action": action, "interface": interface, "value": value,
        "previous": previous, "commands": commands,
        "risk": risk, "impact": impact, "summary": change_text,
        "proposed_in": tool_context.invocation_id,
        "created": time.time(),
    }
    tool_context.state[PENDING_KEY] = change
    _audit({"event": "proposed", "id": change["id"], "device": device, "action": action,
            "interface": interface, "value": value, "previous": previous, "risk": risk})

    return {
        "status": "needs_confirmation",
        "change_id": change["id"],
        "summary": change_text,
        "device": device, "current_value": previous, "risk": risk, "impact": impact,
        "commands_that_will_run": commands[1:],
        "expires_in_seconds": PENDING_TTL_SECONDS,
        "instruction": "Show this summary to the user and ask them to reply yes or no. Do NOT call apply_change in this turn.",
    }


def apply_change(change_id: str, tool_context) -> dict:
    """Step 2: execute the pending change after the user has confirmed, then verify it.

    Only call this after the user has replied 'yes' in a message AFTER the proposal.

    Args:
        change_id: the change_id returned by propose_change.

    Returns:
        dict: status 'verified', 'applied_not_verified' or 'error', with before/after evidence.
    """
    pending = tool_context.state.get(PENDING_KEY)
    if not pending:
        return {"status": "error", "error": "there is no pending change. Call propose_change first."}
    if (change_id or "").strip() != pending["id"]:
        return {"status": "error", "error": f"change id mismatch. The pending change is {pending['id']}."}
    if time.time() - pending["created"] > PENDING_TTL_SECONDS:
        tool_context.state[PENDING_KEY] = None
        _audit({"event": "expired", "id": pending["id"]})
        return {"status": "error", "error": "the proposal expired. Propose the change again."}
    if pending["proposed_in"] == tool_context.invocation_id:
        return {"status": "error", "error": "the user has not confirmed yet. Ask them to reply yes in a new message, then call apply_change."}

    device, action, interface, value = pending["device"], pending["action"], pending["interface"], pending["value"]

    # ---- execute
    cmd = ["docker", "exec", _container(device), "vtysh"]
    for c in pending["commands"]:
        cmd += ["-c", c]
    res = _run(cmd)
    tool_context.state[PENDING_KEY] = None            # one use only
    errors = _vtysh_errors(res["output"])
    if not res["ok"] or errors:
        detail = errors or res["error"]
        _audit({"event": "failed", "id": pending["id"], "device": device, "action": action, "error": str(detail)})
        return {"status": "error", "error": f"the router rejected the change: {detail}. Nothing was verified."}

    # ---- verify (read back from the router, do not trust the command's success)
    after = _read_config(device)
    evidence, ok = {}, False
    if after["ok"]:
        if action == "hostname":
            evidence = {"hostname_before": pending["previous"], "hostname_after": after["hostname"]}
            ok = after["hostname"] == value
        else:
            ai = after["interfaces"].get(interface, {"description": None, "shutdown": False})
            link = _link_state(device, interface)
            if action == "description":
                evidence = {"description_before": pending["previous"], "description_after": ai["description"]}
                ok = ai["description"] == value
            elif action == "shutdown":
                evidence = {"config_shutdown": ai["shutdown"], "kernel_link_state": link}
                ok = ai["shutdown"] and link == "DOWN"
            else:
                evidence = {"config_shutdown": ai["shutdown"], "kernel_link_state": link}
                ok = (not ai["shutdown"]) and link == "UP"
    _audit({"event": "applied", "id": pending["id"], "device": device, "action": action,
            "interface": interface, "value": value, "previous": pending["previous"],
            "verified": ok, "evidence": evidence})

    result = {"status": "verified" if ok else "applied_not_verified", "change_id": pending["id"],
              "device": device, "summary": pending["summary"], "evidence": evidence}
    if not ok:
        result["warning"] = ("The command ran but the read-back did not match what was requested. "
                             "Do not tell the user it worked. Offer to propose the reverse change.")
    elif action in ("shutdown", "no_shutdown"):
        result["suggestion"] = "Offer to run a health check; BGP can take up to 30 seconds to settle."
    return result


def cancel_change(tool_context) -> dict:
    """Discard the pending change when the user says no or changes their mind.

    Returns:
        dict: status 'cancelled' or 'nothing_pending'.
    """
    pending = tool_context.state.get(PENDING_KEY)
    if not pending:
        return {"status": "nothing_pending"}
    tool_context.state[PENDING_KEY] = None
    _audit({"event": "cancelled", "id": pending["id"]})
    return {"status": "cancelled", "change_id": pending["id"]}


def get_change_history(limit: int = 5) -> dict:
    """Show the most recent entries from the audit log (read-only).

    Args:
        limit: how many entries to return, 1 to 20.

    Returns:
        dict: status and the latest audit entries, newest last.
    """
    limit = max(1, min(int(limit), 20))
    try:
        lines = AUDIT_LOG.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {"status": "success", "entries": []}
    return {"status": "success", "entries": [json.loads(ln) for ln in lines[-limit:] if ln.strip()]}
