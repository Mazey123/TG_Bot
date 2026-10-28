"""Обратная совместимость: контекстный middleware вынесен в context.py."""

from middlewares.context import AppContextMiddleware

__all__ = ["AppContextMiddleware"]
