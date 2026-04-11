"""Shared formatting helpers that avoid circular imports in the storage layer.

Functions here use late imports to access identity lookups, providing a single
shared wrapper instead of duplicated late-import wrappers in each storage module.
"""


def format_member_reference(*args, **kwargs):
    """Format a member reference — delegates to storage.identity."""
    from storage.identity import format_member_reference as _impl
    return _impl(*args, **kwargs)
