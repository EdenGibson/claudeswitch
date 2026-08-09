"""The proxy itself: passthrough, the model rewrite, and the codex 503.

Each test stands up a fake upstream and the router in one asyncio loop, so
nothing here needs an async pytest plugin.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("aiohttp")

from aiohttp import ClientSession, web  # noqa: E402
from aiohttp.test_utils import TestServer  # noqa: E402

from claude_swap.router.mode import RouterMode, write_mode  # noqa: E402
from claude_swap.router.modelmap import ModelMap  # noqa: E402
from claude_swap.router.server import Router, build_app, serve  # noqa: E402


def _echo_app(seen: list[dict]) -> web.Application:
    """An upstream that reports what reached it."""

    async def handler(request):
        raw = await request.read()
        seen.append(
            {
                "method": request.method,
                "path": str(request.rel_url),
                "headers": dict(request.headers),
                "body": raw.decode("utf-8") if raw else "",
            }
        )
        return web.json_response({"ok": True, "type": "message"})

    async def models(_request):
        return web.json_response({"data": [{"id": "gpt-a"}, {"id": "gpt-a-mini"}]})

    app = web.Application()
    app.router.add_get("/v1/models", models)
    app.router.add_route("*", "/{path:.*}", handler)
    return app


async def _exchange(mode_file: Path, *, request_body: dict | None, models=None):
    """Run one request through the router and return (response, what upstream saw)."""
    seen: list[dict] = []
    upstream = TestServer(_echo_app(seen))
    await upstream.start_server()
    base = str(upstream.make_url("")).rstrip("/")

    router = Router(
        mode_path=mode_file,
        anthropic_upstream=base,
        cliproxy_upstream=base,
        cliproxy_token="test-key",
        models=models,
    )
    proxy = TestServer(build_app(router))
    await proxy.start_server()
    try:
        async with ClientSession() as session:
            async with session.post(
                str(proxy.make_url("/v1/messages")),
                data=json.dumps(request_body) if request_body is not None else None,
                headers={"Authorization": "Bearer sk-user", "Content-Type": "application/json"},
            ) as response:
                payload = await response.json()
                return response.status, payload, seen
    finally:
        await proxy.close()
        await upstream.close()


def test_claude_mode_forwards_the_body_untouched(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="claude"), path)
    body = {"model": "claude-opus-5", "messages": []}

    status, _payload, seen = asyncio.run(_exchange(path, request_body=body))

    assert status == 200
    assert json.loads(seen[0]["body"])["model"] == "claude-opus-5"


def test_claude_mode_keeps_the_clients_own_authorization(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="claude"), path)

    _status, _payload, seen = asyncio.run(
        _exchange(path, request_body={"model": "claude-opus-5"})
    )

    assert seen[0]["headers"]["Authorization"] == "Bearer sk-user"


def test_codex_mode_rewrites_the_model(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex", slot="1"), path)

    _status, _payload, seen = asyncio.run(
        _exchange(
            path,
            request_body={"model": "claude-opus-5", "messages": []},
            models=ModelMap(main="gpt-a", small="gpt-a-mini"),
        )
    )

    body = json.loads(seen[-1]["body"])
    assert body["model"] == "gpt-a"
    assert body["messages"] == []


def test_codex_mode_sends_haiku_to_the_small_model(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    _status, _payload, seen = asyncio.run(
        _exchange(
            path,
            request_body={"model": "claude-haiku-4-5-20251001"},
            models=ModelMap(main="gpt-a", small="gpt-a-mini"),
        )
    )

    assert json.loads(seen[-1]["body"])["model"] == "gpt-a-mini"


def test_codex_mode_replaces_the_authorization_with_its_own_key(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    _status, _payload, seen = asyncio.run(
        _exchange(path, request_body={"model": "claude-opus-5"})
    )

    assert seen[-1]["headers"]["Authorization"] == "Bearer test-key"


def test_a_body_that_is_not_json_passes_through(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    async def go():
        seen: list[dict] = []
        upstream = TestServer(_echo_app(seen))
        await upstream.start_server()
        base = str(upstream.make_url("")).rstrip("/")
        router = Router(
            mode_path=path, cliproxy_upstream=base, anthropic_upstream=base
        )
        proxy = TestServer(build_app(router))
        await proxy.start_server()
        try:
            async with ClientSession() as session:
                async with session.post(
                    str(proxy.make_url("/v1/messages")), data=b"raw bytes"
                ) as response:
                    await response.read()
            return seen
        finally:
            await proxy.close()
            await upstream.close()

    seen = asyncio.run(go())
    assert seen[-1]["body"] == "raw bytes"


def test_an_unreachable_codex_backend_is_a_503(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    async def go():
        router = Router(
            mode_path=path,
            cliproxy_upstream="http://127.0.0.1:1",  # nothing listens here
        )
        proxy = TestServer(build_app(router))
        await proxy.start_server()
        try:
            async with ClientSession() as session:
                async with session.post(
                    str(proxy.make_url("/v1/messages")),
                    json={"model": "claude-opus-5"},
                ) as response:
                    return response.status, await response.json()
        finally:
            await proxy.close()

    status, payload = asyncio.run(go())
    assert status == 503
    assert "cswap router" in payload["error"]["message"]
    assert "anthropic" not in payload["error"]["message"].lower()


def test_the_health_route_reports_the_mode(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="claude"), path)

    async def go():
        router = Router(mode_path=path)
        proxy = TestServer(build_app(router))
        await proxy.start_server()
        try:
            async with ClientSession() as session:
                async with session.get(
                    str(proxy.make_url("/_cswap/health"))
                ) as response:
                    return response.status, await response.json()
        finally:
            await proxy.close()

    status, payload = asyncio.run(go())
    assert status == 200
    assert payload["provider"] == "claude"
    assert payload["upstreamReachable"] is True


def test_the_model_map_is_corrected_against_the_backend(temp_home: Path):
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    _status, _payload, seen = asyncio.run(
        _exchange(
            path,
            request_body={"model": "claude-opus-5"},
            models=ModelMap(main="gone", small="also-gone"),
        )
    )

    # The fake upstream serves gpt-a and gpt-a-mini, so neither configured
    # name survives the check.
    assert json.loads(seen[-1]["body"])["model"] == "gpt-a"


def test_a_gzip_response_still_says_it_is_gzip(temp_home: Path):
    """The body is forwarded compressed, so the header must survive with it."""
    import gzip

    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="claude"), path)
    packed = gzip.compress(b'{"ok": true}')

    async def go():
        async def handler(_request):
            return web.Response(
                body=packed,
                headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
            )

        upstream_app = web.Application()
        upstream_app.router.add_route("*", "/{path:.*}", handler)
        upstream = TestServer(upstream_app)
        await upstream.start_server()
        base = str(upstream.make_url("")).rstrip("/")
        router = Router(mode_path=path, anthropic_upstream=base)
        proxy = TestServer(build_app(router))
        await proxy.start_server()
        try:
            async with ClientSession() as session:
                async with session.get(str(proxy.make_url("/v1/models"))) as response:
                    return await response.read()
        finally:
            await proxy.close()
            await upstream.close()

    assert asyncio.run(go()) == b'{"ok": true}'


def test_serve_refuses_a_public_bind():
    with pytest.raises(ValueError, match="loopback only"):
        serve(host="0.0.0.0")


def test_the_model_list_is_read_again_after_a_failure(temp_home: Path):
    """The backend is commonly still starting when the first request lands."""
    path = temp_home / "mode.json"
    write_mode(RouterMode(provider="codex"), path)

    async def go():
        calls: list[int] = []

        async def models(_request):
            calls.append(1)
            if len(calls) == 1:
                return web.Response(status=503)
            return web.json_response({"data": [{"id": "gpt-a"}]})

        async def handler(_request):
            return web.json_response({"ok": True})

        app = web.Application()
        app.router.add_get("/v1/models", models)
        app.router.add_route("*", "/{path:.*}", handler)
        upstream = TestServer(app)
        await upstream.start_server()
        base = str(upstream.make_url("")).rstrip("/")
        router = Router(
            mode_path=path,
            cliproxy_upstream=base,
            models=ModelMap(main="gone", small="also-gone"),
        )
        proxy = TestServer(build_app(router))
        await proxy.start_server()
        try:
            async with ClientSession() as session:
                for _ in range(2):
                    async with session.post(
                        str(proxy.make_url("/v1/messages")),
                        data=json.dumps({"model": "claude-opus-5"}),
                    ) as response:
                        await response.read()
            return router.models
        finally:
            await proxy.close()
            await upstream.close()

    assert asyncio.run(go()).main == "gpt-a"


def test_an_unmatched_model_is_not_replaced_by_an_arbitrary_one(temp_home: Path):
    """An arbitrary pick can be a Claude model, which spends the saved quota."""
    from claude_swap.router.modelmap import reconcile

    kept = reconcile(ModelMap(main="gone", small="also-gone"), ["claude-opus-5"])

    assert kept.main == "gone"
