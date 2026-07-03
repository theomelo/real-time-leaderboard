from django.db import models

class Score(models.Model):
    score = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
