"""Tests for agents/dc_config/config_tools.py using a fake FRR lab (no Docker needed).

Run from the repo root:   python3 -m unittest discover -s tests -v
"""
import json
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agents" / "dc_config"))
import config_tools as ct  # noqa: E402

NOISE = "% Can't open configuration file /etc/frr/vtysh.conf due to 'No such file or directory'.\n"


class FakeLab:
    """Pretends to be docker exec ... vtysh / ip for three routers."""

    def __init__(self):
        self.devices = {n: {"hostname": n, "frr_hostname": n, "ifaces": {"eth1": {"desc": None, "down": False},
                                                       "eth2": {"desc": None, "down": False}}}
                        for n in ("spine1", "leaf1", "leaf2")}
        self.ignore_changes = False     # accept commands but do nothing (read-back will mismatch)
        self.lab_down = False
        self.reject = False             # router answers "% Unknown command"
        self.calls = []

    def _config_text(self, d):
        out = ["Building configuration...", "", "Current configuration:", "!", "frr version 10.2.1",
               "frr defaults traditional", f"hostname {d['frr_hostname']}", "log stdout", "!"]
        for name, i in d["ifaces"].items():
            if i["desc"] or i["down"]:
                out.append(f"interface {name}")
                if i["desc"]:
                    out.append(f" description {i['desc']}")
                if i["down"]:
                    out.append(" shutdown")
                out += ["exit", "!"]
        out += ["router bgp 65101", " bgp router-id 10.255.0.2", "exit", "!"]
        if d["hostname"] != d["frr_hostname"]:          # real FRR: a fresh vtysh adds its own (OS) hostname line
            out.append(f"hostname {d['hostname']}")
        out.append("end")
        return "\n".join(out)

    def run(self, cmd, capture_output=True, text=True, timeout=20):
        self.calls.append(cmd)
        res = lambda rc, out="", err="": types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)
        dev = cmd[2].replace("clab-dc01-", "")
        if self.lab_down:
            return res(1, "", f"Error response from daemon: No such container: {cmd[2]}")
        d = self.devices[dev]
        if cmd[3] == "hostname":                             # the container's own hostname
            if len(cmd) == 4:
                return res(0, d["hostname"] + "\n")
            if self.reject:
                return res(1, "", "hostname: you must be root to change the host name")
            if not self.ignore_changes:
                d["hostname"] = cmd[4]
            return res(0, "")
        if cmd[3] == "ip":                                   # ip -br link show ethN
            i = d["ifaces"][cmd[-1]]
            return res(0, f"{cmd[-1]}@if23  {'DOWN' if i['down'] else 'UP'}  aa:bb:cc:dd:ee:ff <BROADCAST>\n")
        args = [cmd[k + 1] for k in range(4, len(cmd), 2) if cmd[k] == "-c"]
        if args == ["show running-config"]:
            return res(0, self._config_text(d), NOISE)
        cur = None
        for a in args:
            if self.reject and a not in ("configure terminal",):
                return res(0, "", NOISE + f"% Unknown command: {a}\n")
            if a == "configure terminal":
                continue
            if a.startswith("hostname "):
                pass          # real FRR: vtysh's own `hostname` only renames that one session, nothing persists
            elif a.startswith("interface "):
                cur = a.split(" ", 1)[1]
            elif cur and a.startswith("description "):
                if not self.ignore_changes:
                    d["ifaces"][cur]["desc"] = a.split(" ", 1)[1]
            elif cur and a == "shutdown":
                if not self.ignore_changes:
                    d["ifaces"][cur]["down"] = True
            elif cur and a == "no shutdown":
                if not self.ignore_changes:
                    d["ifaces"][cur]["down"] = False
            else:
                return res(0, "", NOISE + f"% Unknown command: {a}\n")
        return res(0, "", NOISE)


class Ctx:
    def __init__(self, state, invocation_id):
        self.state, self.invocation_id = state, invocation_id


class ConfigToolTests(unittest.TestCase):
    def setUp(self):
        self.lab = FakeLab()
        self.tmp = tempfile.TemporaryDirectory()
        self.patches = [mock.patch.object(subprocess, "run", self.lab.run),
                        mock.patch.object(ct, "AUDIT_LOG", Path(self.tmp.name) / "audit.jsonl")]
        for p in self.patches:
            p.start()
        self.state = {}

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def turn(self, n):
        """A context for chat turn n (each user message is a new invocation)."""
        return Ctx(self.state, f"inv-{n}")

    def propose(self, n=1, **kw):
        return ct.propose_change(tool_context=self.turn(n), **kw)

    def audit(self):
        return [json.loads(x) for x in (Path(self.tmp.name) / "audit.jsonl").read_text().splitlines()]

    # ---- the confirm gate
    def test_propose_changes_nothing(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        self.assertEqual(r["status"], "needs_confirmation")
        self.assertEqual(self.lab.devices["leaf1"]["hostname"], "leaf1")
        self.assertFalse(any("configure terminal" in c for call in self.lab.calls for c in call))

    def test_cannot_apply_in_same_turn(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        a = ct.apply_change(r["change_id"], self.turn(1))
        self.assertEqual(a["status"], "error")
        self.assertIn("not confirmed", a["error"])
        self.assertEqual(self.lab.devices["leaf1"]["hostname"], "leaf1")
        self.assertIsNotNone(self.state[ct.PENDING_KEY])           # still pending

    def test_hostname_full_flow(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        a = ct.apply_change(r["change_id"], self.turn(2))
        self.assertEqual(a["status"], "verified")
        self.assertEqual(a["evidence"], {"hostname_before": "leaf1", "hostname_after": "BranchA", "vtysh_shows": "BranchA"})
        # real FRR keeps the saved config name until redeploy; the agent must not pretend otherwise
        self.assertEqual(self.lab.devices["leaf1"]["frr_hostname"], "leaf1")
        self.assertIn("docker exec clab-dc01-leaf1 hostname BranchA", r["commands_that_will_run"])
        self.assertEqual([e["event"] for e in self.audit()], ["proposed", "applied"])
        self.assertTrue(self.audit()[-1]["verified"])

    def test_change_is_single_use(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        self.assertEqual(ct.apply_change(r["change_id"], self.turn(2))["status"], "verified")
        self.assertEqual(ct.apply_change(r["change_id"], self.turn(3))["status"], "error")

    def test_wrong_id_none_pending_and_expiry(self):
        self.assertEqual(ct.apply_change("abc123", self.turn(2))["status"], "error")       # nothing pending
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        self.assertIn("mismatch", ct.apply_change("zzzzzz", self.turn(2))["error"])
        self.state[ct.PENDING_KEY]["created"] = time.time() - ct.PENDING_TTL_SECONDS - 5
        self.assertIn("expired", ct.apply_change(r["change_id"], self.turn(2))["error"])
        self.assertEqual(self.lab.devices["leaf1"]["hostname"], "leaf1")

    def test_cancel(self):
        self.propose(device="leaf1", action="hostname", value="BranchA")
        self.assertEqual(ct.cancel_change(self.turn(2))["status"], "cancelled")
        self.assertEqual(ct.cancel_change(self.turn(2))["status"], "nothing_pending")

    def test_new_proposal_replaces_old(self):
        a = self.propose(device="leaf1", action="hostname", value="One")
        b = self.propose(device="leaf1", action="hostname", value="Two")
        self.assertEqual(ct.apply_change(a["change_id"], self.turn(2))["status"], "error")
        self.assertEqual(ct.apply_change(b["change_id"], self.turn(2))["status"], "verified")
        self.assertEqual(self.lab.devices["leaf1"]["hostname"], "Two")

    # ---- the four actions
    def test_description(self):
        r = self.propose(device="leaf1", action="description", interface="eth1", value="Uplink to spine1")
        self.assertEqual(r["risk"], "LOW")
        a = ct.apply_change(r["change_id"], self.turn(2))
        self.assertEqual(a["status"], "verified")
        self.assertEqual(a["evidence"]["description_after"], "Uplink to spine1")
        again = self.propose(3, device="leaf1", action="description", interface="eth1", value="Uplink to spine1")
        self.assertEqual(again["status"], "no_change")

    def test_shutdown_and_no_shutdown(self):
        r = self.propose(device="leaf1", action="shutdown", interface="eth1")
        self.assertEqual(r["risk"], "HIGH")
        self.assertIn("uplink to spine1", r["impact"])
        a = ct.apply_change(r["change_id"], self.turn(2))
        self.assertEqual(a["status"], "verified")
        self.assertEqual(a["evidence"], {"config_shutdown": True, "kernel_link_state": "DOWN"})
        self.assertEqual(self.propose(3, device="leaf1", action="shutdown", interface="eth1")["status"], "no_change")
        r2 = self.propose(4, device="leaf1", action="no_shutdown", interface="eth1")
        a2 = ct.apply_change(r2["change_id"], self.turn(5))
        self.assertEqual(a2["status"], "verified")
        self.assertEqual(a2["evidence"], {"config_shutdown": False, "kernel_link_state": "UP"})

    # ---- validation (nothing may reach the router)
    def test_rejects_bad_requests(self):
        bad = [
            dict(device="host1", action="hostname", value="x"),
            dict(device="leaf9", action="hostname", value="x"),
            dict(device="leaf1; reboot", action="hostname", value="x"),
            dict(device="leaf1", action="reload"),
            dict(device="leaf1", action="hostname", value="bad name"),
            dict(device="leaf1", action="hostname", value="leaf1; reboot"),
            dict(device="leaf1", action="hostname", value="-bad"),
            dict(device="leaf1", action="hostname", value="bad-"),
            dict(device="leaf1", action="hostname", value="a" * 40),
            dict(device="leaf1", action="hostname", value=""),
            dict(device="leaf1", action="hostname", interface="eth1", value="ok"),
            dict(device="leaf1", action="shutdown", interface="eth0"),
            dict(device="leaf1", action="shutdown", interface="lo"),
            dict(device="leaf1", action="shutdown", interface=""),
            dict(device="leaf1", action="shutdown", interface="eth1; reboot"),
            dict(device="leaf1", action="shutdown", interface="eth1", value="x"),
            dict(device="leaf1", action="description", interface="eth1", value="x'; rm -rf /"),
            dict(device="leaf1", action="description", interface="eth1", value="a!b"),
            dict(device="leaf1", action="description", interface="eth1", value=""),
            dict(device="leaf1", action="description", interface="eth1", value="x" * 81),
        ]
        for kw in bad:
            with self.subTest(kw=kw):
                self.assertEqual(self.propose(**kw)["status"], "error")
        self.assertEqual(self.lab.calls, [])                       # the lab was never even contacted
        self.assertNotIn(ct.PENDING_KEY, self.state)

    def test_hostname_edge_values_accepted(self):
        for v in ("A", "BranchA", "branch-a-1", "a" * 32):
            self.assertEqual(self.propose(device="leaf1", action="hostname", value=v)["status"], "needs_confirmation", v)

    # ---- failure handling
    def test_router_rejects_command(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        self.lab.reject = True
        a = ct.apply_change(r["change_id"], self.turn(2))
        self.assertEqual(a["status"], "error")
        self.assertIn("rejected", a["error"])
        self.assertEqual(self.audit()[-1]["event"], "failed")
        self.assertIsNone(self.state[ct.PENDING_KEY])

    def test_readback_mismatch_is_not_reported_as_success(self):
        r = self.propose(device="leaf1", action="shutdown", interface="eth1")
        self.lab.ignore_changes = True
        a = ct.apply_change(r["change_id"], self.turn(2))
        self.assertEqual(a["status"], "applied_not_verified")
        self.assertIn("warning", a)
        self.assertFalse(self.audit()[-1]["verified"])

    def test_lab_not_running(self):
        self.lab.lab_down = True
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        self.assertEqual(r["status"], "error")
        self.assertIn("lab running", r["error"])

    def test_history(self):
        r = self.propose(device="leaf1", action="hostname", value="BranchA")
        ct.apply_change(r["change_id"], self.turn(2))
        h = ct.get_change_history(5)
        self.assertEqual([e["event"] for e in h["entries"]], ["proposed", "applied"])


if __name__ == "__main__":
    unittest.main()
