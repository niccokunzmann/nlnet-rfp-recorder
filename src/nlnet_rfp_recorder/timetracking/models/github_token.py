from __future__ import annotations

from django.db import models


class GitHubToken(models.Model):
    token = models.CharField(max_length=255)

    @classmethod
    def set(cls, token: str) -> GitHubToken:
        cls.objects.all().delete()
        return cls.objects.create(token=token)

    @classmethod
    def get(cls) -> str | None:
        obj = cls.objects.first()
        return obj.token if obj else None
