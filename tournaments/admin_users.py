"""Admin tweaks so every user reads as a club: a "Club name" field when creating
users, the club name in the user list, and in every "Club user" dropdown."""
from django import forms
from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserCreationForm
from django.contrib.auth.models import User
from django.utils.translation import gettext_lazy as _


def club_label(user):
    """'Club Bàdminton Igualada (club)' or just the username when no club name is set."""
    if user is None:
        return ''
    return f'{user.first_name} ({user.username})' if user.first_name else user.username


class ClubUserCreationForm(UserCreationForm):
    first_name = forms.CharField(
        label=_("Club name"), max_length=150, required=False,
        help_text=_("Shown on the public start page and in the club lists, e.g. Club Bàdminton Igualada."))

    class Meta(UserCreationForm.Meta):
        model = User
        fields = ('username', 'first_name')


class ClubUserAdmin(UserAdmin):
    add_form = ClubUserCreationForm
    add_fieldsets = (
        (None, {
            'classes': ('wide',),
            'fields': ('username', 'first_name', 'password1', 'password2'),
        }),
    )
    list_display = ('username', 'club_name', 'is_staff', 'is_superuser', 'last_login')
    search_fields = ('username', 'first_name', 'last_name', 'email')

    @admin.display(description=_("Club name"), ordering='first_name')
    def club_name(self, obj):
        return obj.first_name

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        if 'first_name' in form.base_fields:
            form.base_fields['first_name'].label = _("Club name")
            form.base_fields['first_name'].help_text = _(
                "Shown on the public start page and in the club lists, e.g. Club Bàdminton Igualada.")
        return form


class ClubOwnerAdminMixin:
    """Show 'Club name (username)' in the owner dropdown and the list."""

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        field = super().formfield_for_foreignkey(db_field, request, **kwargs)
        if db_field.name == 'owner' and field is not None:
            field.label_from_instance = club_label
        return field

    @admin.display(description=_("Club"), ordering='owner__first_name')
    def owner_club(self, obj):
        return club_label(obj.owner)


def install():
    if admin.site.is_registered(User):
        admin.site.unregister(User)
    admin.site.register(User, ClubUserAdmin)
