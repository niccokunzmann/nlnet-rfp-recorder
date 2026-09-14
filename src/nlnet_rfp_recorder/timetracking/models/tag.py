from __future__ import annotations

from django.db import models

TAG_NAMES = ("implementation", "review")


class Tag(models.Model):
    name = models.CharField(
        max_length=32,
        unique=True,
        choices=[(name, name) for name in TAG_NAMES],
        help_text="The tag name: 'implementation' or 'review'.",
    )

    def __str__(self) -> str:
        return self.name
