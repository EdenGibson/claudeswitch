"""The reverse proxy that lets a running session change backend.

Claude Code points at ``http://127.0.0.1:8318``. Every request reads the mode
file and goes to whichever backend it names, so a flip reaches a session that
is already running, at its next request, with no restart.

Two rules keep this safe:

* Claude mode is verbatim. The request is forwarded to ``api.anthropic.com``
  with its own ``Authorization`` header untouched, so the saved claude.ai
  login keeps working and cswap's credential switching is unaffected.
* Codex mode never falls back to Anthropic. Falling back would spend the
  Claude quota the switch exists to protect, so an unreachable CLIProxyAPI is
  a 503.

Needs aiohttp, which the ``cswap[router]`` extra installs. Nothing else in
cswap imports this module.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from claude_swap.router.mode import (
    ANTHROPIC_UPSTREAM,
    CLIPROXY_PORT,
    DEFAULT_PORT,
    ModeWatcher,
)
from claude_swap.router.modelmap import ModelMap, read_map, reconcile

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

_logger = logging.getLogger(__name__)

#: Never copied between the two connections. Each hop owns these itself.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
        "content-length",
        "host",
    }
)
# Note: content-encoding is deliberately NOT here. The client session runs
# with auto_decompress off, so a gzip body is forwarded still compressed —
# dropping the header that says so leaves the caller unable to read it.

#: Health endpoint, under a cswap-only prefix so it can never shadow a real
#: Anthropic path.
HEALTH_PATH = "/_cswap/health"


def _forwardable(headers) -> dict[str, str]:
    """Copy headers minus the ones that describe this hop's connection."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP}


class Router:
    """Holds the mode watcher and the shared client session."""

    def __init__(
        self,
        *,
        mode_path: "Path | None" = None,
        anthropic_upstream: str = ANTHROPIC_UPSTREAM,
        cliproxy_upstream: str = f"http://127.0.0.1:{CLIPROXY_PORT}",
        cliproxy_token: str | None = None,
        models: ModelMap | None = None,
    ) -> None:
        self.watcher = ModeWatcher(mode_path)
        self.anthropic_upstream = anthropic_upstream.rstrip("/")
        self.cliproxy_upstream = cliproxy_upstream.rstrip("/")
        self.cliproxy_token = cliproxy_token
        self.models = models if models is not None else read_map()
        self._models_checked = False
        self._session = None

    async def session(self):
        import aiohttp

        if self._session is None or self._session.closed:
            # auto_decompress off: the body is passed through byte for byte,
            # so a gzip response reaches Claude Code exactly as it was sent.
            # No total timeout: an SSE stream is open for as long as the model
            # is answering.
            self._session = aiohttp.ClientSession(
                auto_decompress=False,
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=15),
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    def target(self) -> tuple[str, str, dict[str, str]]:
        """(provider, upstream base, header overrides) for the next request."""
        mode = self.watcher.current()
        if mode.provider == "claude":
            return "claude", self.anthropic_upstream, {}
        overrides = {}
        if self.cliproxy_token:
            overrides["Authorization"] = f"Bearer {self.cliproxy_token}"
        return "codex", self.cliproxy_upstream, overrides

    async def handle_health(self, request):
        from aiohttp import web

        mode = self.watcher.current()
        reachable = await self._upstream_reachable()
        return web.json_response(
            {
                "provider": mode.provider,
                "slot": mode.slot,
                "pinned": mode.pinned,
                "upstream": (
                    self.anthropic_upstream
                    if mode.provider == "claude"
                    else self.cliproxy_upstream
                ),
                "upstreamReachable": reachable,
            },
            status=200 if reachable else 503,
        )

    async def _upstream_reachable(self) -> bool:
        """Whether the current backend answers at all.

        Claude mode is reported reachable without a network call: the upstream
        is Anthropic itself, and a health check that reaches out on every poll
        would be a needless request against the user's own rate limit.
        """
        import aiohttp

        mode = self.watcher.current()
        if mode.provider == "claude":
            return True
        session = await self.session()
        try:
            async with session.get(
                f"{self.cliproxy_upstream}/v1/models",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as response:
                # Any answer proves the process is listening. A 401 from
                # CLIProxyAPI still means the hop is alive.
                return response.status < 500
        except Exception:
            return False

    async def _ensure_models(self) -> None:
        """Correct the model map against what the backend says it serves.

        Runs in codex mode until it succeeds once, then never again. A model
        name the backend does not know is a 404 on every request, and Codex
        model names change often enough that a stale config file is the likely
        cause.
        """
        if self._models_checked:
            return
        import aiohttp

        session = await self.session()
        try:
            async with session.get(
                f"{self.cliproxy_upstream}/v1/models",
                headers=(
                    {"Authorization": f"Bearer {self.cliproxy_token}"}
                    if self.cliproxy_token
                    else {}
                ),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                payload = await response.json(content_type=None)
        except Exception as exc:
            # No latch on failure. The backend is commonly still starting when
            # the first request arrives, and a map left uncorrected for the
            # life of the process 404s every request.
            _logger.warning("could not read the backend model list: %s", exc)
            return
        entries = payload.get("data") if isinstance(payload, dict) else None
        available = [
            str(item.get("id"))
            for item in (entries or [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not available:
            # An error status with an empty body reads as None here rather
            # than raising, so this is the only place a starting backend is
            # told apart from one that answered. Latching on it would leave
            # the map uncorrected for the life of the process.
            _logger.warning("the backend served no model list; will ask again")
            return
        self._models_checked = True
        corrected = reconcile(self.models, available)
        if corrected != self.models:
            _logger.info(
                "model map corrected: main %s -> %s, small %s -> %s",
                self.models.main,
                corrected.main,
                self.models.small,
                corrected.small,
            )
            self.models = corrected

    async def _codex_body(self, request) -> bytes:
        """The request body with its Claude model name replaced.

        A body that is not JSON, or carries no ``model``, is passed through
        byte for byte. The router never invents a field the client did not
        send.
        """
        raw = await request.read()
        if not raw:
            return raw
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw
        if not isinstance(payload, dict) or "model" not in payload:
            return raw
        payload["model"] = self.models.pick(payload.get("model"))
        return json.dumps(payload).encode("utf-8")

    async def handle(self, request):
        import aiohttp
        from aiohttp import web

        provider, upstream, overrides = self.target()
        url = f"{upstream}{request.rel_url}"
        headers = _forwardable(request.headers)
        headers.update(overrides)

        # Claude mode streams the body straight through, untouched. Codex mode
        # has to read it first: the model name in it means nothing upstream.
        if provider == "codex":
            await self._ensure_models()
            body = await self._codex_body(request)
        else:
            body = request.content

        session = await self.session()
        try:
            upstream_response = await session.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
            )
        except Exception as exc:
            if provider == "codex":
                # Never fall back to Anthropic here. The whole point of the
                # codex backend is to stop spending Claude quota.
                _logger.error("codex upstream unreachable: %s", exc)
                return web.json_response(
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": (
                                "cswap router: the codex backend at "
                                f"{self.cliproxy_upstream} is not reachable. "
                                "Start it with 'cswap router start', or return "
                                "to Claude with 'cswap backend claude'."
                            ),
                        },
                    },
                    status=503,
                )
            _logger.error("anthropic upstream unreachable: %s", exc)
            return web.json_response(
                {
                    "type": "error",
                    "error": {"type": "api_error", "message": f"cswap router: {exc}"},
                },
                status=502,
            )

        response = web.StreamResponse(
            status=upstream_response.status,
            headers=_forwardable(upstream_response.headers),
        )
        # Chunked, so a token stream reaches the client as it arrives instead
        # of after the whole answer is buffered.
        response.enable_chunked_encoding()
        await response.prepare(request)
        try:
            async for chunk in upstream_response.content.iter_any():
                await response.write(chunk)
        except (aiohttp.ClientError, ConnectionResetError) as exc:
            _logger.warning("stream interrupted: %s", exc)
        finally:
            upstream_response.release()
        await response.write_eof()
        return response


def build_app(router: Router):
    """An aiohttp application serving the health route and the catch-all."""
    from aiohttp import web

    app = web.Application(client_max_size=0)  # 0 = no cap; requests stream
    app.router.add_get(HEALTH_PATH, router.handle_health)
    app.router.add_route("*", "/{path:.*}", router.handle)

    async def _close(_app):
        await router.close()

    app.on_cleanup.append(_close)
    return app


def serve(
    port: int = DEFAULT_PORT,
    *,
    host: str = "127.0.0.1",
    mode_path: "Path | None" = None,
    cliproxy_token: str | None = None,
) -> None:
    """Run the router until the process is stopped.

    Warning: bind loopback only. This box has a public interface, and the
    router forwards whatever Authorization header it is given.
    """
    from aiohttp import web

    if host not in ("127.0.0.1", "::1", "localhost"):
        raise ValueError(
            f"the router binds loopback only, refusing host {host!r}"
        )
    if cliproxy_token is None:
        from claude_swap.router import cliproxy

        cliproxy_token = cliproxy.api_key(create=False) or None
    router = Router(mode_path=mode_path, cliproxy_token=cliproxy_token)
    web.run_app(build_app(router), host=host, port=port, print=None)
