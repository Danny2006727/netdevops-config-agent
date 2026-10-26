# NetDevOps Phase 3 - Configuration Agent (Guided Activity 2)

Google ADK agent that changes hostname, interface description, shutdown / no shutdown on a containerlab FRR leaf-spine lab (`dc01`).
Flow: validate -> ask confirmation -> execute -> verify. Every change is logged to `documentation/change_audit.jsonl`.

## Layout
- `containerlab/` - dc01 lab (spine1, leaf1, leaf2, host1, host2)
- `agents/dc_config/` - the configuration agent
- `agents/dc_monitor/tools.py` - read-only monitoring tools reused by the config agent (health checks after a change)
- `tests/` - 15 unit tests against a fake FRR lab
- `documentation/config_agent_log.md` - prompts, safety design, test results

## Run
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    python3 -m unittest discover -s tests -v
    sudo containerlab deploy -t containerlab/dc01.clab.yml
    cp agents/dc_config/.env.example agents/dc_config/.env   # add GOOGLE_API_KEY
    cd agents && adk web

Open http://localhost:8000 and choose `dc_config`.

Changes are running-config only: redeploying the lab resets them.
