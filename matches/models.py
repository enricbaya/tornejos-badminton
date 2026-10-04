from django.db import models
from django.utils.translation import gettext_lazy as _
from players.models import Player
from tournaments.models import Tournament

# Create your models here.

class Match(models.Model):
    """Store a simple head-to-head match between two players."""

    player1 = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='player1_matches')
    player2 = models.ForeignKey(Player, on_delete=models.CASCADE, related_name='player2_matches')
    tournament = models.ForeignKey(Tournament, on_delete=models.CASCADE)
    score = models.CharField(max_length=50, blank=True, null=True)

    class Meta:
        verbose_name = _("Legacy match")
        verbose_name_plural = _("Legacy matches")

    def __str__(self):
        """Return a human-readable match label.

        Returns:
            str: Match label containing players and tournament.
        """
        return f"{self.player1} vs {self.player2} in {self.tournament}"
