import asyncio
import copy
import importlib
import json
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# The real-loopback suite imports this helper by its top-level module name.
# Keep that name bound to this module when tests are imported as a package.
sys.modules.setdefault("test_webui_handlers", sys.modules[__name__])


class FakeResponse(dict):
    pass


class FakeRequest:
    def __init__(self):
        self.payload = {}
        self.uploads = {}
        self.plugin_name = "astrbot_plugin_endworld_img_api"

    async def json(self, default=None):
        return self.payload

    async def files(self):
        return self.uploads


fake_request = FakeRequest()


def json_response(data=None, status_code=200, headers=None):
    return FakeResponse(kind="json", data=data, status=status_code, headers=headers)


def error_response(message, status_code=400):
    return FakeResponse(kind="error", data={"status": "error", "message": message}, status=status_code)


def file_response(path, filename=None, content_type=None):
    return FakeResponse(kind="file", path=Path(path), filename=filename, content_type=content_type)


class PluginUploadFile:
    def __init__(self, payload: bytes, filename="config.json"):
        self.payload = payload
        self.filename = filename
        self.content_type = "application/json"

    async def save(self, path):
        Path(path).write_bytes(self.payload)


class Logger:
    def __getattr__(self, _name):
        return lambda *_args, **_kwargs: None


def install_astrbot_fakes(data_dir):
    if "aiohttp" not in sys.modules:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.ClientSession = type(
            "ClientSession",
            (),
            {"__init__": lambda self, **_kwargs: setattr(self, "closed", False), "close": AsyncMock()},
        )
        aiohttp.TCPConnector = lambda **_kwargs: object()
        sys.modules["aiohttp"] = aiohttp
    if "aiofiles" not in sys.modules:
        aiofiles = types.ModuleType("aiofiles")
        aiofiles.open = None
        sys.modules["aiofiles"] = aiofiles
    if "PIL" not in sys.modules:
        pil = types.ModuleType("PIL")
        pil.Image = types.SimpleNamespace(open=lambda *_args: None)
        sys.modules["PIL"] = pil

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = Logger()
    web = types.ModuleType("astrbot.api.web")
    web.request = fake_request
    web.PluginUploadFile = PluginUploadFile
    web.json_response = json_response
    web.error_response = error_response
    web.file_response = file_response

    components = types.ModuleType("astrbot.api.message_components")
    components.Image = type("Image", (), {"fromFileSystem": staticmethod(lambda path: path)})
    components.Plain = lambda text: text

    event = types.ModuleType("astrbot.api.event")
    event.AstrMessageEvent = object
    event.MessageChain = list
    filter_obj = types.SimpleNamespace(
        EventMessageType=types.SimpleNamespace(ALL="all"),
        event_message_type=lambda *_args, **_kwargs: (lambda fn: fn),
    )
    event.filter = filter_obj

    star = types.ModuleType("astrbot.api.star")
    star.Context = object
    star.Star = type("Star", (), {"__init__": lambda self, context: setattr(self, "context", context)})
    star.StarTools = type("StarTools", (), {"get_data_dir": staticmethod(lambda: Path(data_dir))})
    star.register = lambda *_args, **_kwargs: (lambda cls: cls)

    sys.modules.update(
        {
            "astrbot": astrbot,
            "astrbot.api": api,
            "astrbot.api.web": web,
            "astrbot.api.message_components": components,
            "astrbot.api.event": event,
            "astrbot.api.star": star,
        }
    )


_module_temp = tempfile.TemporaryDirectory()
install_astrbot_fakes(_module_temp.name)
_repository_parent = str(Path(__file__).resolve().parents[2])
if _repository_parent not in sys.path:
    sys.path.insert(0, _repository_parent)
main = importlib.import_module("astrbot_plugin_endworld_img_api.main")
from astrbot_plugin_endworld_img_api.webui_config import load_schema


class PackageImportTests(unittest.TestCase):
    def test_main_loads_from_astrbot_plugin_package(self):
        plugin_root = Path(__file__).resolve().parents[1]
        package_name = "astrbot_plugin_endworld_img_api_package_test"
        module_name = f"{package_name}.main"
        package = types.ModuleType(package_name)
        package.__path__ = [str(plugin_root)]

        original_path = sys.path[:]
        original_webui_config = sys.modules.pop("webui_config", None)
        sys.modules[package_name] = package
        try:
            sys.path = [
                entry
                for entry in sys.path
                if Path(entry or ".").resolve() != plugin_root
            ]
            with tempfile.TemporaryDirectory() as data_dir:
                install_astrbot_fakes(data_dir)
                spec = importlib.util.spec_from_file_location(module_name, plugin_root / "main.py")
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
            self.assertTrue(hasattr(module, "SetuPlugin"))
        finally:
            sys.path = original_path
            sys.modules.pop(module_name, None)
            sys.modules.pop(package_name, None)
            if original_webui_config is not None:
                sys.modules["webui_config"] = original_webui_config


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, route, handler, methods, description):
        self.routes.append((route, handler.__name__, methods, description))


class SaveConfig(dict):
    def __init__(self, *args, fail=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls = 0
        self.fail = fail

    def save_config(self):
        self.calls += 1
        if self.fail:
            raise OSError("disk full")


def default_config():
    return {key: copy.deepcopy(rule["default"]) for key, rule in load_schema().items()}


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fake_request.payload = {}
        fake_request.uploads = {}
        self.context = FakeContext()
        self.config = SaveConfig(default_config())
        self.plugin = main.SetuPlugin(self.context, self.config)

    async def asyncTearDown(self):
        await self.plugin._close_session()

    def test_terminate_is_an_async_lifecycle_hook(self):
        self.assertTrue(asyncio.iscoroutinefunction(type(self.plugin).terminate))

    async def test_terminate_waits_for_session_cleanup(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        before = set(asyncio.all_tasks())

        async def blocked_cleanup():
            entered.set()
            await release.wait()

        with patch.object(self.plugin, "_close_session", side_effect=blocked_cleanup) as cleanup:
            try:
                result = self.plugin.terminate()
                self.assertTrue(asyncio.iscoroutine(result))
                task = asyncio.create_task(result)
                await entered.wait()
                self.assertFalse(task.done())
                release.set()
                await task
                cleanup.assert_awaited_once()
            finally:
                release.set()
                # The old fire-and-forget hook can leave a scheduled cleanup task.
                await asyncio.sleep(0)
                pending = [task for task in asyncio.all_tasks() - before if task is not asyncio.current_task()]
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

    async def test_terminate_closes_idle_session_and_is_idempotent(self):
        session = types.SimpleNamespace(closed=False)

        async def close_session():
            session.closed = True

        session.close = AsyncMock(side_effect=close_session)
        self.plugin._session = session
        self.plugin._ssl_context = object()

        await self.plugin.terminate()

        session.close.assert_awaited_once()
        self.assertIsNone(self.plugin._session)
        self.assertIsNone(self.plugin._ssl_context)
        self.assertEqual(self.plugin._retired_sessions, {})
        self.assertEqual(self.plugin._session_users, {})
        await self.plugin.terminate()

    async def test_terminate_retires_in_flight_session_until_scope_exits(self):
        session = types.SimpleNamespace(closed=False)

        async def close_session():
            session.closed = True

        session.close = AsyncMock(side_effect=close_session)
        self.plugin._session = session
        with patch.object(self.plugin, "_get_or_create_session_locked", return_value=session):
            scope = self.plugin._session_scope()
            await scope.__aenter__()
            try:
                await self.plugin.terminate()
                self.assertIsNone(self.plugin._session)
                self.assertIn(id(session), self.plugin._retired_sessions)
                session.close.assert_not_awaited()
            finally:
                await scope.__aexit__(None, None, None)
        session.close.assert_awaited_once()
        self.assertTrue(session.closed)

    def test_exact_routes_are_registered(self):
        actual = {(route, tuple(methods)) for route, _handler, methods, _desc in self.context.routes}
        prefix = "/astrbot_plugin_endworld_img_api/"
        self.assertEqual(
            actual,
            {
                (prefix + "config", ("GET",)),
                (prefix + "config/save", ("POST",)),
                (prefix + "api/test", ("POST",)),
                (prefix + "status", ("GET",)),
                (prefix + "config/export", ("GET",)),
                (prefix + "config/import", ("POST",)),
            },
        )

    async def test_config_read_is_deep_copy(self):
        response = await self.plugin.page_config()
        response["data"]["config"]["sources"][0]["name"] = "changed"
        self.assertNotEqual(self.config["sources"][0]["name"], "changed")
        self.assertIn("schema", response["data"])

    async def test_config_read_cleans_legacy_keys_and_fills_missing_defaults(self):
        self.config["legacy_field"] = "old"
        self.config.pop("cooldown")
        self.config.pop("allowed_private_ips")
        self.config["sources"][0]["legacy_source_field"] = "old"
        self.config["sources"][0].pop("group_list")

        response = await self.plugin.page_config()

        loaded = response["data"]["config"]
        self.assertNotIn("legacy_field", loaded)
        self.assertNotIn("legacy_source_field", loaded["sources"][0])
        self.assertEqual(loaded["cooldown"], load_schema()["cooldown"]["default"])
        self.assertEqual(loaded["allowed_private_ips"], [])
        self.assertEqual(loaded["sources"][0]["group_list"], [])

    async def test_save_preserves_identity_and_persists_once(self):
        candidate = default_config()
        candidate["cooldown"] = 42
        identity = id(self.config)
        fake_request.payload = candidate
        response = await self.plugin.page_save_config()
        self.assertEqual(response["status"], 200)
        self.assertEqual(id(self.plugin.cfg), identity)
        self.assertEqual(self.config["cooldown"], 42)
        self.assertEqual(self.config.calls, 1)
        self.assertTrue(response["data"]["changes"])

    async def test_save_returns_the_normalized_configuration(self):
        candidate = default_config()
        candidate["legacy_field"] = True
        candidate.pop("cooldown")
        fake_request.payload = candidate

        response = await self.plugin.page_save_config()

        self.assertNotIn("legacy_field", response["data"]["config"])
        self.assertEqual(response["data"]["config"]["cooldown"], load_schema()["cooldown"]["default"])

    async def test_allowlist_save_and_import_round_trip_and_invalid_save_preserves_runtime(self):
        candidate = default_config()
        candidate["allowed_private_ips"] = [" 127.0.0.1 ", "192.168.1.10/24"]
        fake_request.payload = candidate
        saved = await self.plugin.page_save_config()
        self.assertEqual(saved["data"]["config"]["allowed_private_ips"], ["127.0.0.1/32", "192.168.1.0/24"])

        export = await self.plugin.page_export_config()
        self.assertEqual(
            json.loads(export["path"].read_text(encoding="utf-8"))["allowed_private_ips"],
            ["127.0.0.1/32", "192.168.1.0/24"],
        )
        imported = default_config()
        imported["allowed_private_ips"] = ["10.1.2.3/8"]
        fake_request.payload = {"confirm": True, "config": imported}
        applied = await self.plugin.page_import_config()
        self.assertEqual(applied["data"]["config"]["allowed_private_ips"], ["10.0.0.0/8"])

        before = copy.deepcopy(self.plugin.cfg)
        invalid = default_config()
        invalid["allowed_private_ips"] = ["localhost"]
        fake_request.payload = invalid
        rejected = await self.plugin.page_save_config()
        self.assertFalse(rejected["data"]["saved"])
        self.assertIn("allowed_private_ips[0]", rejected["data"]["errors"])
        self.assertEqual(dict(self.plugin.cfg), before)

    async def test_save_rolls_back_when_persistence_fails(self):
        failing = SaveConfig(default_config(), fail=True)
        plugin = main.SetuPlugin(FakeContext(), failing)
        before = copy.deepcopy(failing)
        candidate = default_config()
        candidate["cooldown"] = 99
        fake_request.payload = candidate
        response = await plugin.page_save_config()
        self.assertEqual(response["status"], 500)
        self.assertEqual(dict(failing), before)
        self.assertEqual(failing.calls, 1)

    async def test_validation_errors_are_a_bridge_readable_business_response(self):
        before = copy.deepcopy(self.config)
        candidate = default_config()
        candidate["cooldown"] = -1
        fake_request.payload = candidate

        response = await self.plugin.page_save_config()

        self.assertEqual(response["status"], 200)
        self.assertFalse(response["data"]["saved"])
        self.assertEqual(response["data"]["errors"]["cooldown"], "不得小于 0")
        self.assertEqual(dict(self.config), before)
        self.assertEqual(self.config.calls, 0)

    async def test_import_preview_does_not_apply_and_confirm_does(self):
        candidate = default_config()
        candidate["cooldown"] = 77
        fake_request.uploads = {"file": PluginUploadFile(json.dumps(candidate).encode())}
        preview = await self.plugin.page_import_config()
        self.assertEqual(self.config["cooldown"], default_config()["cooldown"])
        self.assertEqual(preview["data"]["config"]["cooldown"], 77)
        self.assertTrue(preview["data"]["changes"])

        fake_request.uploads = {}
        fake_request.payload = {"config": candidate, "confirm": True}
        confirmed = await self.plugin.page_import_config()
        self.assertTrue(confirmed["data"]["saved"])
        self.assertEqual(self.config["cooldown"], 77)

    async def test_import_limit_and_invalid_json_preserve_config(self):
        before = copy.deepcopy(self.config)
        for payload in (b"x" * (256 * 1024 + 1), b"not-json"):
            fake_request.uploads = {"file": PluginUploadFile(payload)}
            response = await self.plugin.page_import_config()
            self.assertEqual(response["status"], 400)
            self.assertEqual(dict(self.config), before)

    async def test_export_round_trip_and_status(self):
        exported = await self.plugin.page_export_config()
        self.assertEqual(exported["filename"], "endworld-img-config.json")
        self.assertEqual(json.loads(exported["path"].read_text(encoding="utf-8")), dict(self.config))
        self.plugin.cooldowns["u"] = 1.0
        status = await self.plugin.page_status()
        self.assertEqual(status["data"]["version"], "6.6.1")
        self.assertEqual(status["data"]["source_count"], len(self.config["sources"]))
        self.assertEqual(status["data"]["cooldown_count"], 1)

    async def test_ssl_change_closes_only_existing_session(self):
        session = types.SimpleNamespace(closed=False, close=AsyncMock())
        self.plugin._session = session
        candidate = default_config()
        candidate["cooldown"] += 1
        fake_request.payload = candidate
        await self.plugin.page_save_config()
        session.close.assert_not_awaited()

        candidate["verify_ssl"] = not candidate["verify_ssl"]
        fake_request.payload = candidate
        await self.plugin.page_save_config()
        session.close.assert_awaited_once()
        self.assertIsNone(self.plugin._session)

    async def test_ssl_change_retires_but_does_not_interrupt_inflight_session(self):
        old_session = types.SimpleNamespace(closed=False, close=AsyncMock())
        new_session = types.SimpleNamespace(closed=False, close=AsyncMock())
        self.plugin._session = old_session
        fetch_started = asyncio.Event()
        release_fetch = asyncio.Event()

        async def slow_fetch(session, *_args, **_kwargs):
            self.assertIs(session, old_session)
            fetch_started.set()
            await release_fetch.wait()
            old_session.close.assert_not_awaited()
            return main.FetchResult(True, 200, "image/png", "https://example.com/a", b"ok", None)

        fake_request.payload = {"url": "https://example.com/unsaved"}
        with patch.object(self.plugin, "_fetch_url", side_effect=slow_fetch):
            inflight = asyncio.create_task(self.plugin.page_test_api())
            await asyncio.wait_for(fetch_started.wait(), timeout=1)

            candidate = default_config()
            candidate["verify_ssl"] = not candidate["verify_ssl"]
            fake_request.payload = candidate
            saved = await self.plugin.page_save_config()
            self.assertTrue(saved["data"]["saved"])
            old_session.close.assert_not_awaited()
            self.assertIsNone(self.plugin._session)

            with patch.object(main.aiohttp, "ClientSession", return_value=new_session):
                acquired = await self.plugin._get_session()
            self.assertIs(acquired, new_session)

            release_fetch.set()
            response = await asyncio.wait_for(inflight, timeout=1)

        self.assertTrue(response["data"]["ok"])
        old_session.close.assert_awaited_once()
        self.assertIs(self.plugin._session, new_session)

    async def test_api_test_uses_supplied_value_without_persistence(self):
        fake_request.payload = {"url": "https://example.com/unsaved"}
        result = main.FetchResult(True, 200, "image/png", "https://cdn.example/a.png", b"ok", None)
        with patch.object(self.plugin, "_fetch_url", AsyncMock(return_value=result)) as fetch:
            response = await self.plugin.page_test_api()
        fetch.assert_awaited_once()
        self.assertEqual(fetch.await_args.args[1], "https://example.com/unsaved")
        self.assertEqual(response["data"]["status"], 200)
        self.assertEqual(self.config.calls, 0)


class FakeContent:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _size):
        return self.chunks.pop(0) if self.chunks else b""


class FakeHTTPResponse:
    def __init__(self, status=200, headers=None, url="https://example.com/a", chunks=(b"ok",)):
        self.status = status
        self.headers = headers or {"Content-Type": "image/png"}
        self.url = url
        self.content = FakeContent(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


class FetchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.plugin = main.SetuPlugin(FakeContext(), SaveConfig(default_config()))

    async def test_private_allowlist_selectively_permits_literals_and_mapped_ipv6(self):
        self.assertFalse(await self.plugin._is_safe_url("http://127.0.0.1/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://192.168.2.10/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://[::1]/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://[fd00::1]/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://198.18.0.1/image"))
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.0/8", "192.168.1.4/24"]
        self.assertTrue(await self.plugin._is_safe_url("http://127.0.0.1/image"))
        self.assertTrue(await self.plugin._is_safe_url("http://192.168.1.99/image"))
        self.assertTrue(await self.plugin._is_safe_url("http://[::ffff:127.0.0.1]/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://192.168.2.10/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://[::1]/image"))
        self.plugin.cfg["allowed_private_ips"].append("fd00::/8")
        self.assertTrue(await self.plugin._is_safe_url("http://[fd00::1]/image"))
        self.assertFalse(await self.plugin._is_safe_url("http://[fc00::1]/image"))

    async def test_private_allowlist_preserves_hard_denies_and_fails_closed_on_bad_runtime_config(self):
        self.plugin.cfg["allowed_private_ips"] = ["0.0.0.0/0", "::/0"]
        for url in (
            "http://0.0.0.0/", "http://224.0.0.1/", "http://[ff02::1]/",
            "http://[::ffff:224.0.0.1]/", "http://[::ffff:0.0.0.0]/",
            "http://[fe80::1%25eth0]/", "http://user@127.0.0.1/", "ftp://127.0.0.1/",
            "http://127.0.0.1:invalid/",
        ):
            with self.subTest(url=url):
                self.assertFalse(await self.plugin._is_safe_url(url))
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.0/8", "example.com"]
        self.assertFalse(await self.plugin._is_safe_url("http://127.0.0.1/"))

    async def test_dns_requires_every_answer_to_be_permitted(self):
        loop = asyncio.get_running_loop()
        make_info = lambda address: (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 80))
        self.plugin.cfg["allowed_private_ips"] = []
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("127.0.0.1")])):
            self.assertFalse(await self.plugin._is_safe_url("http://localhost/image"))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("93.184.216.34")])):
            self.assertTrue(await self.plugin._resolve_and_check_domain("example.test"))
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.1"]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("127.0.0.1")])):
            self.assertTrue(await self.plugin._resolve_and_check_domain("localhost"))
        make_v6_info = lambda address: (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 80, 0, 0))
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.1", "::1"]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("127.0.0.1"), make_v6_info("::1")])):
            self.assertTrue(await self.plugin._resolve_and_check_domain("localhost"))
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.1"]
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("127.0.0.1"), make_v6_info("::1")])):
            self.assertFalse(await self.plugin._resolve_and_check_domain("localhost"))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("198.18.1.1")])):
            self.assertTrue(await self.plugin._resolve_and_check_domain("proxy.test"))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[make_info("198.19.1.1")])):
            self.assertFalse(await self.plugin._resolve_and_check_domain("proxy.test"))
        for answers in (
            [make_info("93.184.216.34"), make_info("192.168.1.5")],
            [make_info("127.0.0.1"), make_info("127.0.0.2")],
            [],
        ):
            with patch.object(loop, "getaddrinfo", AsyncMock(return_value=answers)):
                self.assertFalse(await self.plugin._resolve_and_check_domain("example.test"))
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=OSError("DNS error"))):
            self.assertFalse(await self.plugin._resolve_and_check_domain("example.test"))
        with patch.object(loop, "getaddrinfo", AsyncMock(side_effect=asyncio.TimeoutError)):
            self.assertFalse(await self.plugin._resolve_and_check_domain("example.test"))

    async def test_redirect_rechecks_local_allowlist_and_blocks_other_local_targets(self):
        self.plugin.cfg["allowed_private_ips"] = ["127.0.0.1"]
        session = FakeSession([
            FakeHTTPResponse(302, {"Location": "http://127.0.0.2/private"}, url="http://127.0.0.1/start")
        ])
        result = await self.plugin._fetch_url(session, "http://127.0.0.1/start")
        self.assertFalse(result.ok)
        self.assertEqual(len(session.calls), 1)

    async def test_scheme_private_and_redirect_are_revalidated(self):
        session = FakeSession([])
        result = await self.plugin._fetch_url(session, "file:///etc/passwd")
        self.assertFalse(result.ok)
        self.assertEqual(session.calls, [])

        with patch.object(self.plugin, "_is_safe_url", AsyncMock(side_effect=[True, False])) as safe:
            session = FakeSession([FakeHTTPResponse(302, {"Location": "http://127.0.0.1/x"})])
            result = await self.plugin._fetch_url(session, "https://example.com/start")
        self.assertFalse(result.ok)
        self.assertEqual(safe.await_count, 2)

    async def test_fetch_diagnostics_and_tuple_compatibility(self):
        with patch.object(self.plugin, "_is_safe_url", AsyncMock(return_value=True)):
            session = FakeSession([FakeHTTPResponse()])
            result = await self.plugin._fetch_url(session, "https://example.com/a")
        self.assertTrue(result.ok)
        self.assertEqual((result.status, result.content_type, result.final_url), (200, "image/png", "https://example.com/a"))

        with patch.object(self.plugin, "_is_safe_url", AsyncMock(return_value=True)):
            session = FakeSession([FakeHTTPResponse()])
            value = await self.plugin._safe_fetch(session, "https://example.com/a")
        self.assertEqual(value, (b"ok", "image/png", "https://example.com/a"))

    async def test_size_and_timeout_are_bounded(self):
        with patch.object(self.plugin, "_is_safe_url", AsyncMock(return_value=True)):
            session = FakeSession([FakeHTTPResponse(chunks=(b"12345",))])
            result = await self.plugin._fetch_url(session, "https://example.com/a", max_size_mb=0)
        self.assertFalse(result.ok)
        self.assertIn("大小", result.error)

        class TimeoutSession:
            def get(self, *_args, **_kwargs):
                raise asyncio.TimeoutError()

        with patch.object(self.plugin, "_is_safe_url", AsyncMock(return_value=True)):
            result = await self.plugin._fetch_url(TimeoutSession(), "https://example.com/a", timeout=0.01)
        self.assertFalse(result.ok)
        self.assertIn("超时", result.error)


if __name__ == "__main__":
    unittest.main()
