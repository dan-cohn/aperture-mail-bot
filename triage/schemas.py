from pydantic import BaseModel, Field

# Human-readable names for each category
CATEGORY_NAMES = {
    1:  "Urgent Alerts",
    2:  "Direct Personal",
    3:  "Important Group",
    4:  "Near-term Events",
    5:  "Timed Headlines",
    6:  "Active Deals",
    7:  "Short-term Events",
    8:  "Long-term Planning",
    9:  "General Reading",
    10: "Regular Lists",
    11: "Cleanup Needed",
    12: "Pure Trash",
}

# Category → action code
ACTION_MAP = {
    1:  "ALERT",        # Action A
    2:  "ALERT",        # Action A
    3:  "SUMMARY",      # Action B
    4:  "SUMMARY",      # Action B
    5:  "SUMMARY",      # Action B
    6:  "INBOX",        # Action C
    7:  "INBOX",        # Action C
    8:  "ARCHIVE",      # Action D
    9:  "INBOX",        # Action C
    10: "ARCHIVE",      # Action D
    11: "UNSUBSCRIBE",  # Action E
    12: "TRASH",        # Action F
}


class TriageResult(BaseModel):
    category: int = Field(ge=1, le=12)
    is_urgent: bool
    summary: str
    reasoning: str
    suggested_action: str

    @property
    def category_name(self) -> str:
        return CATEGORY_NAMES.get(self.category, "Unknown")

    @property
    def action(self) -> str:
        """Canonical action derived from category (ignores LLM's suggested_action)."""
        return ACTION_MAP.get(self.category, "INBOX")
