"""Real aiohttp loopback integration tests for the opt-in local API policy."""

import tempfile
import importlib
import sys
import unittest
from pathlib import Path


def _load_real_module(name):
    module = sys.modules.get(name)
    if module is not None and not getattr(module, "__file__", None):
        for key in list(sys.modules):
            if key == name or key.startswith(f"{name}."):
                del sys.modules[key]
    return importlib.import_module(name)


class RealLoopbackTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        tests_dir = str(Path(__file__).resolve().parent)
        if tests_dir not in sys.path:
            sys.path.insert(0, tests_dir)
        aiohttp = _load_real_module("aiohttp")
        aiofiles = _load_real_module("aiofiles")
        aiohttp.web = importlib.import_module("aiohttp.web")
        _load_real_module("PIL")
        PILImage = _load_real_module("PIL.Image")
        helper = importlib.import_module("test_webui_handlers")
        main = helper.main
        # If another suite imported the test fakes first, rebind the plugin module
        # to genuine libraries before this integration exercise.
        main.aiohttp = aiohttp
        main.aiofiles = aiofiles
        main.PILImage = PILImage
        cls.aiohttp = aiohttp
        cls.aiofiles = aiofiles
        cls.web = aiohttp.web
        cls.PILImage = PILImage
        cls.FakeContext = helper.FakeContext
        cls.SaveConfig = helper.SaveConfig
        cls.default_config = staticmethod(helper.default_config)
        cls.fake_request = helper.fake_request
        cls.main = main

    async def asyncSetUp(self):
        self.hits = []
        app = self.web.Application()

        async def pixel(request):
            self.hits.append(request.path)
            return self.web.Response(body=b"loopback-image-bytes", content_type="image/png")

        async def redirect(request):
            self.hits.append(request.path)
            raise self.web.HTTPFound(location="/pixel")

        async def api_json(request):
            self.hits.append(request.path)
            return self.web.json_response({"url": f"http://127.0.0.1:{self.port}/pixel"})

        async def api_json_unlisted(request):
            self.hits.append(request.path)
            return self.web.json_response({"url": f"http://127.0.0.2:{self.port}/pixel"})

        app.add_routes([
            self.web.get("/pixel", pixel), self.web.get("/redirect", redirect),
            self.web.get("/json", api_json), self.web.get("/json-unlisted", api_json_unlisted),
        ])
        self.runner = self.web.AppRunner(app)
        await self.runner.setup()
        self.site = self.web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        self.port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{self.port}"

        self.temp_dir = tempfile.TemporaryDirectory()
        config = self.default_config()
        config["compress_enable"] = False
        config["allowed_private_ips"] = ["127.0.0.1"]
        self.plugin = self.main.SetuPlugin(self.FakeContext(), self.SaveConfig(config))
        self.plugin.cache_dir = Path(self.temp_dir.name)
        self.assertIs(self.main.aiohttp, self.aiohttp)
        self.assertIs(self.main.aiofiles, self.aiofiles)
        self.assertIs(self.main.PILImage, self.PILImage)

    async def asyncTearDown(self):
        await self.plugin._close_session()
        await self.runner.cleanup()
        self.temp_dir.cleanup()

    async def test_real_loopback_default_block_whitelist_redirect_json_and_page_test(self):
        self.plugin.cfg["allowed_private_ips"] = []
        async with self.plugin._session_scope() as session:
            blocked = await self.plugin._fetch_url(session, f"{self.base}/pixel")
        self.assertFalse(blocked.ok)
        self.assertEqual(self.hits, [])
        self.fake_request.payload = {"url": f"{self.base}/pixel", "allowed_private_ips": ["127.0.0.0/8"]}
        untrusted_diagnostic = await self.plugin.page_test_api()
        self.assertFalse(untrusted_diagnostic["data"]["ok"])
        self.assertEqual(self.hits, [])

        await self.plugin._apply_config({**self.plugin.cfg, "allowed_private_ips": ["127.0.0.1/32"]})
        async with self.plugin._session_scope() as session:
            direct = await self.plugin._fetch_url(session, f"{self.base}/pixel")
            redirected = await self.plugin._fetch_url(session, f"{self.base}/redirect")
            image_path, final_url = await self.plugin._try_download_single(session, f"{self.base}/json")
        self.assertTrue(direct.ok)
        self.assertEqual(direct.body, b"loopback-image-bytes")
        self.assertTrue(redirected.ok)
        self.assertEqual(redirected.body, b"loopback-image-bytes")
        self.assertTrue(image_path)
        self.assertEqual(Path(image_path).read_bytes(), b"loopback-image-bytes")
        self.assertTrue(final_url.startswith(f"{self.base}/pixel"))
        async with self.plugin._session_scope() as session:
            unlisted_json = await self.plugin._try_download_single(session, f"{self.base}/json-unlisted")
        self.assertEqual(unlisted_json, ("", ""))
        self.assertNotIn("/pixel", self.hits[self.hits.index("/json-unlisted") + 1:])

        self.fake_request.payload = {"url": f"{self.base}/pixel"}
        diagnostic = await self.plugin.page_test_api()
        self.assertTrue(diagnostic["data"]["ok"])
        self.assertGreaterEqual(self.hits.count("/pixel"), 4)

        async with self.plugin._session_scope() as session:
            unlisted = await self.plugin._fetch_url(session, f"http://127.0.0.2:{self.port}/pixel")
        self.assertFalse(unlisted.ok)
        self.assertEqual(self.hits.count("/pixel"), 4)


if __name__ == "__main__":
    unittest.main()
