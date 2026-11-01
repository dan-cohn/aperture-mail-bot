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

# How prominently each action surfaces an email, most prominent first.
# Category number alone is NOT a safe ranking: cat 8 (Planning) archives while
# cat 9 (Reading) stays in the inbox, so comparing numbers would let a reply
# archive a thread that was previously visible.
ACTION_RANK = {
    "ALERT":       0,
    "SUMMARY":     1,
    "INBOX":       2,
    "ARCHIVE":     3,
    "UNSUBSCRIBE": 4,
    "TRASH":       5,
}


def category_rank(category: int) -> tuple[int, int]:
    """
    Sort key for how prominently a category surfaces an email, best first.
    Ranks by action visibility, then by category number to break ties.
    A *smaller* tuple outranks a larger one.
    """
    action = ACTION_MAP.get(category, "INBOX")
    return (ACTION_RANK.get(action, 2), category)


def outranks(a: int, b: int) -> bool:
    """True if category *a* surfaces an email more prominently than category *b*."""
    return category_rank(a) < category_rank(b)


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
