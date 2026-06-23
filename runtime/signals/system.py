"""Re-export shim — system-signal publication moved to
runtime.system_status_post (F2 / Step 4). This shim keeps the legacy
runtime.signals.system import path working until the signals package is deleted.
"""

from runtime.system_status_post import (  # noqa: F401
    _post_system_signal_updates,
    _preauthored_system_signal_result,
    _preauthored_system_signal_target,
    _publish_pending_system_signal_updates,
    _system_signal_updates,
)
