from django.contrib import admin
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from .models import User


class UserAdmin(admin.ModelAdmin):
    list_display = ("id", "email", "phone")


admin.site.register(User, UserAdmin)

admin.site.register(Permission)
admin.site.register(ContentType)
