"""Tool registry: name -> ToolSpec.

Open/closed by design (mirrors sc-chatbot's ``tools/`` registry pattern in
chatbot-poc-projects): adding a new tool is a new handler module +
``@register(...)`` — nothing here or in ``protocol/router.py`` needs to
change.

Handlers receive a ``ToolContext`` rather than a concrete store: the D365
tools build their own ``CredentialStore`` from ``ctx.session`` and
``check_fee_waiver_status`` builds its own ``CustomApiCredentialStore`` the
same way. Keeping the shared contract store-agnostic is what lets a second
credential kind (the custom fee-waiver API) arrive as a new tool file
without editing the dispatcher in ``protocol/router.py``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from geneco_mcp.store.crm_resolver import CrmCredentialCache
from geneco_mcp.store.ssp_resolver import SspCredentialCache

#: Which backend a tool talks to, and therefore which credential kind it
#: needs and which MCP sub-app serves it. Declared per tool rather than
#: derived from the tool's name: the registered names are bare
#: (``verify_account``, ``check_fee_waiver_status``), and only the *module*
#: names carry a ``crm_``/``api_`` prefix — so a name-based split would be
#: a rule nothing enforces the day someone adds a tool.
ToolKind = Literal["crm", "ssp"]


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Per-call dependencies handed to every tool handler: the request's DB
    session, the encryption key needed to decrypt whichever credential kind
    the tool actually uses, and the process-local caches of vault-fetched
    credentials — one per backend, each empty and unused for tools that
    don't reach that backend."""

    session: AsyncSession
    encryption_key: str
    crm_cache: CrmCredentialCache
    ssp_cache: SspCredentialCache


ToolHandler = Callable[[ToolContext, str, BaseModel], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    kind: ToolKind
    description: str
    input_model: type[BaseModel]
    handler: ToolHandler


_REGISTRY: dict[str, ToolSpec] = {}


def register(
    name: str, *, kind: ToolKind, description: str, input_model: type[BaseModel]
) -> Callable[[ToolHandler], ToolHandler]:
    def decorator(fn: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise ValueError(f"tool {name!r} already registered")
        _REGISTRY[name] = ToolSpec(
            name=name, kind=kind, description=description, input_model=input_model, handler=fn
        )
        return fn

    return decorator


def all_tools() -> tuple[ToolSpec, ...]:
    return tuple(_REGISTRY.values())


def tools_for_kind(kind: ToolKind) -> tuple[ToolSpec, ...]:
    """The tools one MCP sub-app serves. The registry itself stays whole —
    it is populated once at import, and each app filters a view out of it,
    so two apps in one process never contend over registration."""
    return tuple(spec for spec in _REGISTRY.values() if spec.kind == kind)


def get_tool(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)
