from django.db import models


class Url(models.Model):
    # id (BigAutoField, 64-bit) is the source of uniqueness.
    # short_code is base62(id), stored for direct lookup so the redirect
    # path can SELECT by short_code without decoding.
    long_url = models.TextField()
    short_code = models.CharField(max_length=12, unique=True, null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'{self.short_code} -> {self.long_url}'
