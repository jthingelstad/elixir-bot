import sys

from runtime import app as _app

if __name__ == "__main__":
    _app.main()
else:
    sys.modules[__name__] = _app
