"""Shared FastAPI dependencies.

`CurrentUser` is the one every data-touching route should use. It does two
things: demands a logged-in user, and binds that user for LLM spend attribution
for the life of the request.

It is a yield-dependency so the ContextVar is reset on the way out, and it is
**async** for a non-obvious reason: FastAPI runs a *sync* yield-dependency's
setup and teardown halves as two separate thread-pool submissions, in different
`contextvars` Contexts. Resetting there raises
`ValueError: <Token ...> was created in a different Context`.

An async generator runs entirely in the request's event-loop task, so set and
reset share one context. The value still reaches a sync endpoint because
`run_in_threadpool` copies the current context into the worker thread.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends

from app.auth import require_admin, require_user
from app.models import User
from app.services import usage


async def bound_user(user: User = Depends(require_user)) -> AsyncIterator[User]:
    token = usage.set_current_user(user.id)
    try:
        yield user
    finally:
        usage.reset_current_user(token)


async def bound_admin(user: User = Depends(require_admin)) -> AsyncIterator[User]:
    token = usage.set_current_user(user.id)
    try:
        yield user
    finally:
        usage.reset_current_user(token)


CurrentUser = Annotated[User, Depends(bound_user)]
AdminUser = Annotated[User, Depends(bound_admin)]
