#!/usr/bin/env python3
"""
Deploy webhook server to Railway and update all 4 Retell agent LLMs
to point their tool webhook URLs to the new server.

USAGE:
    1. Install Railway CLI: npm install -g @railway/cli
    2. Login: railway login
    3. Run: python3 deploy_and_update_agents.py

Or if already deployed, just pass the URL:
    python3 deploy_and_update_agents.py --url https://your-app.railway.app
"""

import argparse
import json
import subprocess
import sys

RETELL_API_KEY = "key_12f1fbb716ca537c2651a70d2710"

AGENTS = {
    "shelby": {
        "agent_id": "agent_40da2f733e42df807a89c669d6",
        "llm_id":   "llm_31f7cbf0bf3a00d49d86564982ed",
    },
    "alex": {
        "agent_id": "agent_e6cafef912272207148d11893f",
        "llm_id":   "llm_f7f1cb7e8d6fbdeba7b053755c04",
    },
    "cole": {
        "agent_id": "agent_56e1def11bd5201bcdc1fedd6b",
        "llm_id":   "llm_840c10580ede36e1c51e1e7f52ad",
    },
    "jordan": {
        "agent_id": "agent_dd0928ae5479516c905c55ca4d",
        "llm_id":   "llm_cc59395320a34c04cd635b4bc0df",
    },
}

RETELL_CALL_OUTCOME_WEBHOOK = "/retell-call-outcome"


def build_tools(agent_name: str, base_url: str) -> list:
    base = base_url.rstrip("/")
    return [
        {
            "type": "end_call",
            "name": "end_call",
            "description": "End the call when the user says goodbye or the conversation is complete.",
            "speak_after_execution": True,
        },
        {
            "type": "custom",
            "name": "check_calendar_availability",
            "description": "Check available appointment time slots on the Helpful Homebuyers calendar for a specific date.",
            "parameters": {
                "type": "object",
                "properties": {
                    "date":     {"type": "string", "description": "Date in YYYY-MM-DD format"},
                    "timezone": {"type": "string", "description": "Caller timezone, e.g. 'America/New_York'"},
                },
                "required": ["date"],
            },
            "url": f"{base}/{agent_name}-check-calendar",
            "speak_during_execution": True,
            "execution_message_description": "Let me check what times we have available...",
            "speak_after_execution": True,
            "timeout_ms": 12000,
        },
        {
            "type": "custom",
            "name": "book_appointment",
            "description": "Book a consultation appointment. Call check_calendar_availability first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "GHL contact ID from call metadata"},
                    "start_time": {"type": "string", "description": "ISO 8601 datetime for appointment start"},
                    "title":      {"type": "string", "description": "Appointment title"},
                    "notes":      {"type": "string", "description": "Optional notes"},
                },
                "required": ["contact_id", "start_time"],
            },
            "url": f"{base}/{agent_name}-book-appointment",
            "speak_during_execution": True,
            "execution_message_description": "Let me get that booked for you right now...",
            "speak_after_execution": True,
            "timeout_ms": 12000,
        },
        {
            "type": "custom",
            "name": "send_sms",
            "description": "Send an SMS text message to the contact via GoHighLevel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "contact_id": {"type": "string", "description": "GHL contact ID"},
                    "message":    {"type": "string", "description": "SMS message text (under 160 chars)"},
                },
                "required": ["contact_id", "message"],
            },
            "url": f"{base}/{agent_name}-send-sms",
            "speak_during_execution": True,
            "execution_message_description": "I'm sending that to you right now...",
            "speak_after_execution": True,
            "timeout_ms": 10000,
        },
    ]


def retell_patch(llm_id: str, payload: dict) -> int:
    result = subprocess.run([
        "curl", "-s", "-w", "\nHTTP:%{http_code}", "-X", "PATCH",
        f"https://api.retellai.com/update-retell-llm/{llm_id}",
        "-H", f"Authorization: Bearer {RETELL_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
    ], capture_output=True, text=True, timeout=20)
    lines = (result.stdout + result.stderr).strip().split("\n")
    http_lines = [l for l in lines if l.startswith("HTTP:")]
    return int(http_lines[-1].replace("HTTP:", "")) if http_lines else 0


def retell_set_webhook(agent_id: str, webhook_url: str) -> int:
    result = subprocess.run([
        "curl", "-s", "-w", "\nHTTP:%{http_code}", "-X", "PATCH",
        f"https://api.retellai.com/update-agent/{agent_id}",
        "-H", f"Authorization: Bearer {RETELL_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"webhook_url": webhook_url}),
    ], capture_output=True, text=True, timeout=20)
    lines = (result.stdout + result.stderr).strip().split("\n")
    http_lines = [l for l in lines if l.startswith("HTTP:")]
    return int(http_lines[-1].replace("HTTP:", "")) if http_lines else 0


def retell_publish(agent_id: str) -> int:
    result = subprocess.run([
        "curl", "-s", "-w", "\nHTTP:%{http_code}", "-X", "POST",
        f"https://api.retellai.com/publish-agent/{agent_id}",
        "-H", f"Authorization: Bearer {RETELL_API_KEY}",
        "-H", "Content-Type: application/json",
        "-d", "{}",
    ], capture_output=True, text=True, timeout=15)
    lines = (result.stdout + result.stderr).strip().split("\n")
    http_lines = [l for l in lines if l.startswith("HTTP:")]
    return int(http_lines[-1].replace("HTTP:", "")) if http_lines else 0


def deploy_to_railway() -> str:
    """Deploy via Railway CLI and return the public URL."""
    print("Deploying to Railway...")
    result = subprocess.run(["railway", "up", "--detach"], capture_output=True, text=True, timeout=120)
    print(result.stdout)
    if result.returncode != 0:
        print("ERROR:", result.stderr)
        sys.exit(1)

    # Get the domain
    domain_result = subprocess.run(["railway", "domain"], capture_output=True, text=True, timeout=15)
    url = domain_result.stdout.strip()
    if not url.startswith("http"):
        url = "https://" + url
    return url


def update_agents(base_url: str):
    print(f"\nUpdating all 4 agents → {base_url}")
    outcome_url = base_url.rstrip("/") + RETELL_CALL_OUTCOME_WEBHOOK

    for name, ids in AGENTS.items():
        print(f"\n  [{name.upper()}]")
        tools = build_tools(name, base_url)
        code = retell_patch(ids["llm_id"], {"general_tools": tools})
        print(f"    LLM tools update: HTTP {code}")
        wh = retell_set_webhook(ids["agent_id"], outcome_url)
        print(f"    Webhook URL set:  HTTP {wh}")
        pub = retell_publish(ids["agent_id"])
        print(f"    Published:        HTTP {pub}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Skip deploy, use this base URL directly")
    args = parser.parse_args()

    if args.url:
        base_url = args.url.rstrip("/")
    else:
        base_url = deploy_to_railway()

    print(f"\nServer URL: {base_url}")
    update_agents(base_url)

    print("\n" + "=" * 55)
    print("DONE. All agents updated.")
    print(f"  Health check: {base_url}/health")
    print(f"  Call outcome: {base_url}/retell-call-outcome")
    print("=" * 55)


if __name__ == "__main__":
    main()
