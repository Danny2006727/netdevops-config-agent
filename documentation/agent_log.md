# Monitoring Agent Log (Guided Activity 1)

Agent: `dc_monitor` (read-only). Tools: `list_devices`, `run_check`, `get_resource_usage`, `ping_test`.
One row per prompt. "Verified by" is a command I ran myself, not the agent's own answer. **TODO** = fill in from your run.

| # | Date | Prompt | Tools the agent called | Agent answer (short) | Verified by (manual command) | Correct? |
|---|------|--------|------------------------|----------------------|------------------------------|----------|
| 1 | | How healthy is the datacentre? | list_devices, run_check, get_resource_usage, ping_test | **TODO** (healthy baseline) | `docker exec clab-dc01-spine1 vtysh -c "show bgp summary"` | **TODO** |
| 2 | | Is leaf1 connected to the spine? | **TODO** | **TODO** | `docker exec clab-dc01-leaf1 vtysh -c "show bgp summary"` | **TODO** |
| 3 | | Can host1 reach host2? | ping_test | **TODO** | `docker exec clab-dc01-host1 ping -c 3 192.168.102.10` | **TODO** |
| 4 | 29 Oct | How healthy is the datacentre? (after the lab's links were lost) | all four | Reported problems: BGP sessions on spine1 (to leaf1 and leaf2), leaf1 and leaf2 stuck in Active; fabric interfaces missing or down; host ping 100% loss. Resources and eth0/lo fine | `docker exec clab-dc01-leaf1 ip -br link` and `show bgp summary` | **TODO** (confirm against what the commands showed) |

## Failure tests

Break something on purpose, ask the agent, and record whether it noticed.

    docker exec clab-dc01-leaf1 ip link set eth1 down      # break
    docker exec clab-dc01-leaf1 ip link set eth1 up        # fix

| # | What I broke | Agent noticed? | Notes |
|---|---|---|---|
| 1 | leaf1 eth1 down | **TODO** | |
| 2 | Lab links lost (29 Oct, containers restarted without a redeploy) | Yes: reported all three BGP sessions Active and no host connectivity | Fixed with `containerlab destroy` then `deploy`; baseline returned to Established |

## Other observations
- Gemini API quota: a daily limit was reached once (error showed a `retryDelay` of about 6 hours). Fix: change `GEMINI_MODEL` or the API key.
- `adk web` must be started from the `agents/` folder, and each agent folder needs its own `.env`.
