import json
import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import urlopen

from fastapi import FastAPI, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("incident-response")

EVIDENCE_DIR = Path(os.getenv("INCIDENT_DIR", "/data/incidents"))
WORKDIR = os.getenv("AGENT_WORKDIR", "/workspace")
AGENT_CMD = os.getenv("AGENT_CMD", "opencode")
LOKI_URL = os.getenv("LOKI_URL", "http://loki:3100")
TEMPO_URL = os.getenv("TEMPO_URL", "http://tempo:3200")
PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")

app = FastAPI(title="Incident Responder")


def _get(url: str) -> dict:
    try:
        with urlopen(url, timeout=10) as response:
            return json.loads(response.read().decode())
    except Exception as exc:  # pragma: no cover - best effort evidence
        return {"error": str(exc)}


def fetch_logs(route: str | None) -> dict:
    query = '{service_name="order-tracker"}'
    params = urlencode({"query": query, "limit": 100})
    return _get(f"{LOKI_URL}/loki/api/v1/query_range?{params}")


def fetch_traces(route: str | None) -> dict:
    return _get(f"{TEMPO_URL}/api/search?limit=20")


def fetch_metrics(route: str | None) -> dict:
    query = 'sum by (http_route, http_status_code) (increase(http_server_requests_total[5m]))'
    params = urlencode({"query": query})
    return _get(f"{PROMETHEUS_URL}/api/v1/query?{params}")


def save_evidence(payload: dict, alert: dict, route: str | None) -> Path:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = EVIDENCE_DIR / f"incident-{stamp}.json"
    evidence = {
        "received_at": stamp,
        "alert": alert,
        "raw_payload": payload,
        "affected_endpoint": route,
        "logs": fetch_logs(route),
        "traces": fetch_traces(route),
        "metrics": fetch_metrics(route),
    }
    path.write_text(json.dumps(evidence, indent=2))
    logger.info("saved evidence to %s", path)
    return path


def build_prompt(alert: dict, evidence_path: Path, route: str | None) -> str:
    annotations = alert.get("annotations", {})
    return (
        "You are the on-call engineer for the Order Tracker app in this repository.\n"
        "A Grafana alert just fired. Investigate and fix the root cause in the code.\n\n"
        f"Alert name: {alert.get('labels', {}).get('alertname', 'unknown')}\n"
        f"Summary: {annotations.get('summary', '')}\n"
        f"Description: {annotations.get('description', '')}\n"
        f"Affected endpoint: {route or annotations.get('endpoint', 'unknown')}\n"
        f"Dashboard: {annotations.get('dashboard_url', '')}\n\n"
        f"Relevant metrics, logs, and traces were captured at: {evidence_path}\n"
        "Read that file, inspect the app code, and apply a minimal fix.\n"
        "Do not start long-running servers. Do not commit.\n"
        "End your final message with a one-line summary of what was wrong."
    )


def run_agent(alert: dict, evidence_path: Path, route: str | None) -> None:
    response_path = evidence_path.parent / (evidence_path.stem + ".response.txt")
    try:
        prompt = build_prompt(alert, evidence_path, route)
        logger.info("starting coding agent in %s", WORKDIR)
        result = subprocess.run(
            [AGENT_CMD, "run", "--auto", "--dir", WORKDIR, prompt],
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=int(os.getenv("AGENT_TIMEOUT", "1800")),
        )
        output = result.stdout or ""
        if result.stderr:
            output += "\n[stderr]\n" + result.stderr
        logger.info("agent finished with code %s", result.returncode)
    except Exception as exc:
        logger.exception("agent run failed: %s", exc)
        output = f"[agent error] {exc}"
    logger.info("agent output:\n%s", output)
    response_path.write_text(output)
    last_line = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    logger.info("agent final line: %s", last_line)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.post("/alerts")
async def receive_alerts(request: Request):
    payload = await request.json()
    logger.info("received alert payload: %s", json.dumps(payload))
    alerts = payload.get("alerts") or [payload]
    alert = alerts[0]
    labels = alert.get("labels", {})
    annotations = alert.get("annotations", {})
    route = annotations.get("endpoint") or labels.get("http_route")
    evidence_path = save_evidence(payload, alert, route)
    threading.Thread(
        target=run_agent, args=(alert, evidence_path, route), daemon=True
    ).start()
    return {"status": "accepted", "evidence": str(evidence_path)}
