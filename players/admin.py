from django.contrib import admin
from tournaments.admin_users import ClubOwnerAdminMixin

from .models import Player, Team, DivisionCategory

@admin.register(DivisionCategory)
class DivisionCategoryAdmin(ClubOwnerAdminMixin, admin.ModelAdmin):
    """Configure admin list behavior for division categories."""

    list_display = ('name', 'sort_order', 'owner_club')
    list_filter = ('owner',)
    search_fields = ('name',)

@admin.register(Player)
class PlayerAdmin(ClubOwnerAdminMixin, admin.ModelAdmin):
    """Configure admin list behavior for players."""

    list_display = ('name', 'gender', 'age', 'division', 'school', 'owner_club')
    list_filter = ('owner', 'gender', 'division')
    search_fields = ('name', 'school')

@admin.register(Team)
class TeamAdmin(admin.ModelAdmin):
    """Configure admin list behavior for teams."""

    list_display = ('name', 'player1', 'player2')
    search_fields = ('name',)
