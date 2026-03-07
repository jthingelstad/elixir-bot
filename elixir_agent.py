import sys

from agent import app as _app

sys.modules[__name__] = _app
