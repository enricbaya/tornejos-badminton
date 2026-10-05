from django.contrib import admin
from .admin_users import ClubOwnerAdminMixin, install as _install_club_user_admin
from .models import Tournament, Division, Match, UserProfile

_install_club_user_admin()

@admin.register(UserProfile)
class UserProfileAdmin(admin.ModelAdmin):
    """Configure admin list behavior for user profiles."""

    list_display = ('user', 'language', 'tier')
    list_editable = ('language', 'tier')

@admin.register(Tournament)
class TournamentAdmin(ClubOwnerAdminMixin, admin.ModelAdmin):
    """Configure admin list behavior for tournaments."""

    list_display = ('name', 'date', 'owner_club', 'scoring_model')
    list_filter = ('owner', 'scoring_model')
    search_fields = ('name',)

@admin.register(Division)
class DivisionAdmin(admin.ModelAdmin):
    """Configure admin list behavior for divisions."""

    list_display = ('name', 'tournament', 'team_count')
    list_filter = ('tournament',)
    search_fields = ('name',)
    filter_horizontal = ('teams',)

    def team_count(self, obj):
        """Return the number of teams assigned to a division.

        Args:
            obj: Division instance.

        Returns:
            int: Number of teams in the division.
        """
        return obj.teams.count()
    team_count.short_description = 'Hold'

@admin.register(Match)
class MatchAdmin(admin.ModelAdmin):
    """Configure admin list behavior for matches."""

    list_display = ('division', 'match_round', 'team1', 'team2', 'score', 'winner', 'status')
    list_filter = ('division', 'status')
    search_fields = ('team1__name', 'team2__name')
    list_editable = ('score', 'winner', 'status')
