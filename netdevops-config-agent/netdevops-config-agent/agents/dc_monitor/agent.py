import os

from google.adk.agents import Agent

from .tools import get_resource_usage, list_devices, ping_test, run_check

# Set GEMINI_MODEL in .env to use a different Gemini model.
MODEL = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

root_agent = Agent(
    name="dc_monitor",
    model=MODEL,
    description="Monitors a containerlab leaf-spine datacentre and reports its health in plain language.",
    instruction=(
        "You are a network monitoring assistant for a small datacentre lab with one spine "
        "(spine1), two leaves (leaf1, leaf2) and two hosts (host1, host2). "
        "You are read-only: you cannot change any configuration. "
        "When asked about health, call the tools: list_devices, then run_check for "
        "interfaces and bgp on each router, get_resource_usage for each device, and "
        "ping_test between the hosts. Do not guess; only report what the tools returned. "
        "Never paste raw command output unless the user asks for it. Instead answer with: "
        "1) an overall verdict (Healthy, Degraded or Down), 2) a short list of what is "
        "fine, 3) a short list of problems with the device name, and 4) one suggested "
        "next step if anything is wrong. If a tool returns an error, say so plainly."
    ),
    tools=[list_devices, run_check, get_resource_usage, ping_test],
)
