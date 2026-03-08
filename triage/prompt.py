"""
Prompt utilities for the Aperture triage pipeline.

The system prompt (core + learned) is loaded from Firestore at runtime —
see triage/llm_client.py and scripts/sync_prompt.py.
"""


def build_user_message(sender: str, subject: str, snippet: str, date: str = "") -> str:
    return (
        f"**From**: {sender}\n"
        f"**Date**: {date}\n"
        f"**Subject**: {subject}\n"
        f"**Body/Snippet**: {snippet}\n\n"
        "Categorize this email."
    )
