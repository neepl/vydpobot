"""Helpers that keep hand-written Telegram fakes honest."""

import functools
import inspect

import telegram


def real_bot_signatures(cls: type) -> type:
    """Class decorator: every async method named like a `telegram.Bot` method first binds its
    arguments against the real PTB signature, so misuse (wrong order, unknown kwargs) raises
    TypeError in tests exactly as it would against the real library."""
    for name, fn in list(vars(cls).items()):
        real = getattr(telegram.Bot, name, None)
        if real is None or not inspect.iscoroutinefunction(fn):
            continue
        setattr(cls, name, _checked(fn, inspect.signature(real)))
    return cls


def _checked(fn, sig: inspect.Signature):
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        sig.bind(self, *args, **kwargs)
        return await fn(self, *args, **kwargs)

    return wrapper
