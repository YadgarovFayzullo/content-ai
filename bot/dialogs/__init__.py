from bot.dialogs.channel_admin import (
    add_channel_dialog,
    assign_client_dialog,
    publish_dialog,
    post_all_dialog,
    remove_channel_dialog,
)
from bot.dialogs.channel_admin import entry_router as channel_admin_entry_router
from bot.dialogs.settings import entry_router as settings_entry_router
from bot.dialogs.settings import settings_dialog

__all__ = [
    "add_channel_dialog",
    "assign_client_dialog",
    "publish_dialog",
    "post_all_dialog",
    "remove_channel_dialog",
    "channel_admin_entry_router",
    "settings_dialog",
    "settings_entry_router",
]
