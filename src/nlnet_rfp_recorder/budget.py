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

    def __str__(self) -> str:
        line = f"Budget {self.used:.0f}€/{self.total:.0f}€"
        if self.rate is None:
            return line
        remaining = format_duration_hours(self.remaining / self.rate)
        if remaining == "0:00":
            return f"{line} - DONE"
        return f"{line} - {remaining} left"
