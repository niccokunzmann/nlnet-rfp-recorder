from dataclasses import dataclass


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
        hours, minutes = divmod(int(self.remaining / self.rate * 60), 60)
        if hours == 0 and minutes == 0:
            return f"{line} - DONE"
        return f"{line} - {hours}:{minutes:02d} left"
