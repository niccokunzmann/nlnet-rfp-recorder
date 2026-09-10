from dataclasses import dataclass


def format_duration_hours(hours: float) -> str:
    """Format a duration given in (fractional) hours as 'H:MM'.

    This is the single canonical rounding rule (floor to the whole minute)
    used everywhere a duration is shown, so different displays never
    disagree with each other over the same underlying time.
    """
    total_minutes = int(hours * 60)
    hh, mm = divmod(total_minutes, 60)
    return f"{hh}:{mm:02d}"


@dataclass
class BudgetLine:
    used: float
    total: float
    rate: float | None = None  # euros per hour; None if unknown

    @property
    def remaining(self) -> float:
        return self.total - self.used

    @property
    def money(self) -> str:
        return f"{self.used:.0f}€/{self.total:.0f}€"

    @property
    def time_left(self) -> str | None:
        """Raw 'H:MM' remaining, "DONE" if fully used, or None if the
        rate (euros/hour) isn't known.
        """
        if self.rate is None:
            return None
        remaining = format_duration_hours(self.remaining / self.rate)
        return "DONE" if remaining == "0:00" else remaining

    def __str__(self) -> str:
        time_left = self.time_left
        if time_left is None:
            return self.money
        if time_left == "DONE":
            return f"{self.money} {time_left}"
        return f"{self.money} {time_left} left"
