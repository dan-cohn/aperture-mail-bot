#!/usr/bin/env python3
"""
Aperture architecture diagram.

Install:  pip install diagrams
Run:      python scripts/architecture.py
Output:   aperture_architecture.png  (written to current directory)
"""
from diagrams import Diagram, Cluster, Edge
from diagrams.gcp.analytics import Pubsub
from diagrams.gcp.compute import ComputeEngine, Run
from diagrams.gcp.database import Firestore
from diagrams.gcp.devtools import ContainerRegistry, Scheduler, Tasks
from diagrams.gcp.ml import AIPlatform
from diagrams.gcp.security import Iam
from diagrams.onprem.client import Users
from diagrams.saas.cdn import Cloudflare
from diagrams.saas.chat import Telegram

GRAPH = {
    "fontsize": "16",
    "bgcolor": "white",
    "pad": "1.2",
    "splines": "ortho",
    "nodesep": "0.9",
    "ranksep": "1.4",
}

with Diagram(
    "Aperture — Gmail Triage Agent",
    filename="aperture_architecture",
    show=False,
    graph_attr=GRAPH,
    node_attr={"fontsize": "14"},
    edge_attr={"fontsize": "12"},
    direction="TB",
):
    # ── External ──────────────────────────────────────────────────────────────
    gmail    = Users("Gmail API")
    telegram = Telegram("Telegram")
    gemini   = AIPlatform("Gemini 2.5 Flash\n(AI Dev API)")
    user     = Users("User\n(Browser)")
    cf       = Cloudflare("Cloudflare\nTunnel")

    # ── GCP ───────────────────────────────────────────────────────────────────
    with Cluster("Google Cloud Platform  ·  aperture-prod-20260221  ·  us-central1"):

        pubsub    = Pubsub("Pub/Sub\naperture-gmail-push")
        scheduler = Scheduler("Cloud Scheduler\ndigest · snooze · watch")
        tasks     = Tasks("Cloud Tasks\naperture-tasks")
        firestore = Firestore("Firestore\naperture-db")
        registry  = ContainerRegistry("Artifact Registry\naperture-repo")
        secrets   = Iam("Secret Manager\n6 secrets")

        with Cluster("Cloud Run"):
            aperture  = Run("aperture\n(FastAPI)")
            dashboard = Run("aperture-dashboard\n(Streamlit)")

        with Cluster("e2-micro VM"):
            ctrl   = ComputeEngine("vm_control_server\n:9090")
            cfared = ComputeEngine("cloudflared")

    # ── Connections ───────────────────────────────────────────────────────────

    # Gmail push → Pub/Sub → aperture
    gmail  >> Edge(label="push subscription") >> pubsub
    pubsub >> Edge(label="historyId notify")  >> aperture

    # aperture → Gmail API
    aperture >> Edge(label="list history\nlabel · archive", color="#4285F4") >> gmail

    # Telegram
    telegram >> Edge(label="webhook\n(messages · callbacks)")   >> aperture
    aperture >> Edge(label="alerts · replies", color="#229ED9") >> telegram

    # LLM triage
    aperture >> Edge(label="triage request") >> gemini

    # Firestore
    aperture  >> Edge(label="triage log · queues\ncorrections · config") >> firestore
    dashboard >> Edge(label="read metrics · state", color="#888888")      >> firestore

    # Cloud Scheduler
    scheduler >> Edge(label="POST /internal/*") >> aperture

    # Cloud Tasks: dashboard auto-stop
    aperture >> Edge(label="enqueue stop (+1h)")           >> tasks
    tasks    >> Edge(label="POST /internal/dashboard/stop") >> aperture

    # Dashboard lifecycle
    aperture >> Edge(label="scale min-instances") >> dashboard
    aperture >> Edge(label="start / stop")        >> ctrl
    ctrl     >> cfared

    # User → Cloudflare → dashboard
    user >> cf >> dashboard

    # Infrastructure (dashed)
    registry >> Edge(label="image pull",    style="dashed", color="#aaaaaa") >> aperture
    registry >> Edge(label="image pull",    style="dashed", color="#aaaaaa") >> dashboard
    secrets  >> Edge(label="env injection", style="dashed", color="#aaaaaa") >> aperture
    secrets  >> Edge(label="env injection", style="dashed", color="#aaaaaa") >> dashboard
