import asyncio
import copy
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch


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
sys.modules.pop("main", None)
import main
from webui_config import load_schema


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
        self.assertEqual(status["data"]["version"], "6.5.0")
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
