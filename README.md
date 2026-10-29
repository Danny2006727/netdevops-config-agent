# NetDevOps Phase 3 - Datacentre agents (containerlab + Google ADK)

Two Google ADK agents for a containerlab FRR leaf-spine lab (`dc01`: spine1, leaf1, leaf2, host1, host2).

| Agent | Guided Activity | What it does |
|---|---|---|
| `dc_monitor` | 1 | Read-only health checks: interfaces, BGP, routing, resource usage, ping |
| `dc_config` | 2 | Changes hostname, interface description, shutdown / no shutdown. Flow: validate -> ask confirmation -> execute -> verify. Also has the monitoring tools. |

## Layout
- `containerlab/` - the dc01 lab
- `agents/dc_monitor/` - monitoring agent and its tools
- `agents/dc_config/` - configuration agent
- `tests/` - 15 unit tests for the config agent (fake FRR lab)
- `documentation/agent_log.md` - Activity 1 prompt / verification log
- `documentation/config_agent_log.md` - Activity 2 design, tests and findings
- `documentation/change_audit.jsonl` - written by dc_config (git-ignored)

## Run
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python3 -m unittest discover -s tests -v
    sudo containerlab deploy -t containerlab/dc01.clab.yml
    cp agents/dc_config/.env.example agents/dc_config/.env     # add GOOGLE_API_KEY
    cp agents/dc_config/.env agents/dc_monitor/.env
    cd agents && adk web

Open http://localhost:8000 and choose `dc_monitor` or `dc_config`. Run `adk web` from `agents/`.

## Notes
- Config changes are running-state only; redeploying the lab resets them.
- A hostname change renames the router container's hostname (vtysh prompt). FRR's saved config line stays until redeploy, because vtysh `hostname` only renames its own session.
