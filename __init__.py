"""Directory-loader entry for Talaria (plugin id t3code).

Hermes' directory scanner and ``hermes plugins doctor .`` import this file as
the plugin module (``__package__`` set, ``__path__`` = plugin dir) and call
``register()``. Pip installs load the ``talaria`` package via the
``hermes_agent.plugins`` entry point instead.

Relative import is required for doctor: it copies the repo into a temp
plugins dir that is not on ``sys.path``. Absolute import is the fallback
when this file is loaded as a loose module (pytest discovering a root
``__init__.py``).
"""

if __package__:
    from .talaria import register
else:
    from talaria import register

__all__ = ["register"]
