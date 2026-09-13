from django.contrib import admin

from .models import ClickEvent, PageView


@admin.register(ClickEvent)
class ClickEventAdmin(admin.ModelAdmin):
    list_display = (
        "created_at",
        "product",
        "topic",
        "placement",
        "button",
        "position",
        "page_path",
    )
    list_filter = ("placement", "button")
    search_fields = ("product__asin", "product__title", "topic__slug", "page_path", "ascsubtag")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 100
    autocomplete_fields = ("product", "topic")
    readonly_fields = tuple(f.name for f in ClickEvent._meta.fields if f.name != "id")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ("created_at", "path", "topic", "referrer_host", "is_bot")
    list_filter = ("is_bot",)
    search_fields = ("path", "referrer_host")
    date_hierarchy = "created_at"
    ordering = ("-created_at",)
    list_per_page = 100
    autocomplete_fields = ("topic",)
    readonly_fields = tuple(f.name for f in PageView._meta.fields if f.name != "id")

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False
