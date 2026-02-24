"""
Aperture Dashboard — Streamlit monitoring app.

Run locally:
    streamlit run dashboard/app.py

Tabs:
  Overview        — daily/weekly metrics and action breakdown
  Recent Activity — full triage log with filters
  Silent Actions  — emails processed without a Telegram alert
  Digest Queue    — emails pending the next 07:30 / 17:30 digest
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.data import (
    get_control_state,
    get_db,
    get_subscription_state,
    get_summary_queue,
    get_triage_log,
    get_watch_state,
    pause_subscription,
    resume_subscription,
)

# ── Page config ───────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Aperture",
    page_icon="🔭",
    layout="wide",
    initial_sidebar_state="expanded",
)

ACTION_EMOJI = {
    "ALERT":       "🚨",
    "SUMMARY":     "📋",
    "INBOX":       "📥",
    "ARCHIVE":     "📦",
    "UNSUBSCRIBE": "🧹",
    "TRASH":       "🗑️",
}

SILENT_ACTIONS = {"INBOX", "ARCHIVE", "UNSUBSCRIBE", "TRASH"}

db = get_db()

# ── Sidebar ───────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🔭 Aperture")
    st.divider()

    # System status
    st.subheader("System Status")

    sub_state = get_subscription_state()
    if sub_state == "RUNNING":
        st.success("● Running", icon="✅")
    elif sub_state == "PAUSED":
        st.warning("● Paused", icon="⏸️")
    else:
        st.info("● Unknown", icon="❓")

    # Watch expiry
    watch = get_watch_state(db)
    if watch.get("expiration_iso"):
        expiry = datetime.fromisoformat(watch["expiration_iso"])
        now = datetime.now(timezone.utc)
        days_left = (expiry - now).days
        if days_left <= 1:
            st.error(f"Watch expires in {days_left}d", icon="⚠️")
        elif days_left <= 3:
            st.warning(f"Watch expires in {days_left}d")
        else:
            st.caption(f"Watch expires in {days_left}d")

    st.divider()

    # Quick actions
    st.subheader("Quick Actions")

    col1, col2 = st.columns(2)
    with col1:
        if st.button("⏸ Pause", use_container_width=True, disabled=(sub_state == "PAUSED")):
            with st.spinner("Pausing…"):
                pause_subscription()
            st.success("Paused.")
            st.rerun()
    with col2:
        if st.button("▶ Resume", use_container_width=True, disabled=(sub_state == "RUNNING")):
            with st.spinner("Resuming…"):
                resume_subscription()
            st.success("Resumed.")
            st.rerun()

    if st.button("📬 Send Digest Now", use_container_width=True):
        from notifications.telegram import TelegramNotifier
        from scheduler.digest import send_digest
        with st.spinner("Sending digest…"):
            count = asyncio.run(send_digest(db, TelegramNotifier()))
        if count:
            st.success(f"Sent {count} items.")
        else:
            st.info("Queue is empty.")

    st.divider()
    if st.button("🔄 Refresh", use_container_width=True):
        st.cache_data.clear()
        st.rerun()


# ── Main area ─────────────────────────────────────────────────────────────────

st.header("Aperture Dashboard")

log = get_triage_log(db)
queue = get_summary_queue(db)

tab1, tab2, tab3, tab4 = st.tabs(
    ["📊 Overview", "📜 Recent Activity", "🔕 Silent Actions", "📋 Digest Queue"]
)

# ── Tab 1: Overview ───────────────────────────────────────────────────────────

with tab1:
    if not log:
        st.info("No triage data yet. Emails will appear here as they are processed.")
    else:
        df = pd.DataFrame(log)
        now = datetime.now(timezone.utc)

        today_df    = df[df["processed_at"] >= now.replace(hour=0, minute=0, second=0)]
        week_df     = df[df["processed_at"] >= now - timedelta(days=7)]

        # Top metrics
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Today",      len(today_df))
        c2.metric("This Week",  len(week_df))
        c3.metric("Total Logged", len(df))
        c4.metric("Digest Queue", len(queue))

        st.divider()

        # Action breakdown
        st.subheader("Action Breakdown — Last 7 Days")
        if not week_df.empty:
            action_counts = (
                week_df["action"]
                .value_counts()
                .reset_index()
                .rename(columns={"index": "action", "count": "count"})
            )
            action_counts["label"] = action_counts["action"].map(
                lambda a: f"{ACTION_EMOJI.get(a, '')} {a}"
            )
            st.bar_chart(action_counts.set_index("label")["count"])

        st.divider()

        # Category breakdown
        st.subheader("Category Breakdown — Last 7 Days")
        if not week_df.empty:
            cat_counts = (
                week_df.groupby(["category", "category_name"])
                .size()
                .reset_index(name="count")
                .sort_values("category")
            )
            cat_counts["label"] = cat_counts.apply(
                lambda r: f"[{r['category']}] {r['category_name']}", axis=1
            )
            st.bar_chart(cat_counts.set_index("label")["count"])


# ── Tab 2: Recent Activity ────────────────────────────────────────────────────

with tab2:
    if not log:
        st.info("No activity yet.")
    else:
        df = pd.DataFrame(log)

        # Filters
        col1, col2 = st.columns([2, 3])
        with col1:
            action_filter = st.multiselect(
                "Filter by action",
                options=list(ACTION_EMOJI.keys()),
                default=list(ACTION_EMOJI.keys()),
                format_func=lambda a: f"{ACTION_EMOJI[a]} {a}",
            )
        with col2:
            date_range = st.radio(
                "Date range",
                ["Last 24h", "Last 7d", "Last 30d", "All"],
                horizontal=True,
                index=1,
            )

        now = datetime.now(timezone.utc)
        cutoffs = {
            "Last 24h": now - timedelta(hours=24),
            "Last 7d":  now - timedelta(days=7),
            "Last 30d": now - timedelta(days=30),
            "All":      datetime.min.replace(tzinfo=timezone.utc),
        }
        filtered = df[
            (df["action"].isin(action_filter)) &
            (df["processed_at"] >= cutoffs[date_range])
        ].copy()

        st.caption(f"{len(filtered)} emails")

        if not filtered.empty:
            filtered["Time"] = filtered["processed_at"].dt.strftime("%m/%d %H:%M")
            filtered["Action"] = filtered["action"].map(
                lambda a: f"{ACTION_EMOJI.get(a, '')} {a}"
            )
            filtered["Category"] = filtered.apply(
                lambda r: f"[{r['category']}] {r['category_name']}", axis=1
            )
            filtered["Sender"] = filtered["sender"].str[:45]
            filtered["Subject"] = filtered["subject"].str[:60]

            st.dataframe(
                filtered[["Time", "Sender", "Subject", "Category", "Action", "summary"]],
                column_config={
                    "summary": st.column_config.TextColumn("Summary", width="large"),
                },
                use_container_width=True,
                hide_index=True,
            )


# ── Tab 3: Silent Actions ─────────────────────────────────────────────────────

with tab3:
    st.caption(
        "Emails processed without a Telegram alert — "
        "archived, trashed, unsubscribed, or left in inbox quietly."
    )
    if not log:
        st.info("No activity yet.")
    else:
        df = pd.DataFrame(log)
        silent = df[df["action"].isin(SILENT_ACTIONS)].copy()

        col1, col2 = st.columns([2, 3])
        with col1:
            action_filter = st.multiselect(
                "Filter by action ",  # trailing space avoids key collision with tab2
                options=list(SILENT_ACTIONS),
                default=list(SILENT_ACTIONS),
                format_func=lambda a: f"{ACTION_EMOJI[a]} {a}",
            )
        with col2:
            date_range = st.radio(
                "Date range ",
                ["Last 24h", "Last 7d", "Last 30d", "All"],
                horizontal=True,
                index=1,
            )

        now = datetime.now(timezone.utc)
        cutoffs = {
            "Last 24h": now - timedelta(hours=24),
            "Last 7d":  now - timedelta(days=7),
            "Last 30d": now - timedelta(days=30),
            "All":      datetime.min.replace(tzinfo=timezone.utc),
        }
        filtered = silent[
            (silent["action"].isin(action_filter)) &
            (silent["processed_at"] >= cutoffs[date_range])
        ].copy()

        st.caption(f"{len(filtered)} emails")

        if not filtered.empty:
            filtered["Time"] = filtered["processed_at"].dt.strftime("%m/%d %H:%M")
            filtered["Action"] = filtered["action"].map(
                lambda a: f"{ACTION_EMOJI.get(a, '')} {a}"
            )
            filtered["Category"] = filtered.apply(
                lambda r: f"[{r['category']}] {r['category_name']}", axis=1
            )
            filtered["Sender"] = filtered["sender"].str[:45]
            filtered["Subject"] = filtered["subject"].str[:60]

            st.dataframe(
                filtered[["Time", "Sender", "Subject", "Category", "Action", "reasoning"]],
                column_config={
                    "reasoning": st.column_config.TextColumn("Reasoning", width="large"),
                },
                use_container_width=True,
                hide_index=True,
            )


# ── Tab 4: Digest Queue ───────────────────────────────────────────────────────

with tab4:
    if not queue:
        st.info("Digest queue is empty — nothing pending for the next send.")
    else:
        st.caption(f"{len(queue)} email{'s' if len(queue) != 1 else ''} pending")

        q_df = pd.DataFrame(queue)
        q_df["Enqueued"] = pd.to_datetime(q_df["enqueued_at"]).dt.strftime("%m/%d %H:%M")
        q_df["Category"] = q_df.apply(
            lambda r: f"[{r['category']}] {r['category_name']}", axis=1
        )
        q_df["Sender"] = q_df["sender"].str[:45]
        q_df["Subject"] = q_df["subject"].str[:60]

        st.dataframe(
            q_df[["Enqueued", "Sender", "Subject", "Category", "summary"]],
            column_config={
                "summary": st.column_config.TextColumn("Summary", width="large"),
            },
            use_container_width=True,
            hide_index=True,
        )
