import sys

from runtime import app as _app

if __name__ == "__main__":
    _app.main()
else:
    # If runtime submodules imported runtime.app before this compatibility
    # shim was loaded, app initialization may have seen a partially imported
    # runtime.jobs package. Refresh the job exports here so `import elixir`
    # remains stable regardless of import order.
    from runtime import jobs as _jobs_module

    _app.__export_public(_jobs_module)
    _app.__all__ = [name for name in vars(_app) if not name.startswith("__")]
    sys.modules[__name__] = _app
