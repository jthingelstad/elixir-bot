"""elixir — alias for runtime.app, the bot's runtime module.

`import elixir` and `runtime.app` must be the same module object: the test
suite patches runtime internals through `elixir.X`, and runtime.activities
resolves job functions by name on this module. runtime/app.py declares that
surface with explicit imports; this file only installs the alias.
"""

import sys

from runtime import app as _app

if __name__ == "__main__":
    _app.main()
else:
    sys.modules[__name__] = _app
