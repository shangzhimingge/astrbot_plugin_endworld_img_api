import os
import time
import uuid
import json
import asyncio
import copy
import inspect
import aiohttp
import aiofiles
import ipaddress
import ssl
import socket
import mimetypes
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from io import BytesIO
from urllib.parse import urlparse, urljoin
from pathlib import Path 
from PIL import Image as PILImage 

from astrbot.api.message_components import Image, Plain
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger 
from astrbot.api.web import PluginUploadFile, error_response, file_response, json_response, request

from .webui_config import ConfigValidationError, load_schema, summarize_changes, validate_and_normalize


PLUGIN_NAME = "astrbot_plugin_endworld_img_api"
PLUGIN_VERSION = "6.5.1"
IMPORT_LIMIT_BYTES = 256 * 1024


@dataclass(slots=True)
class FetchResult:
    ok: bool
    status: int | None
    content_type: str
    final_url: str
    body: bytes
    error: str | None

@register("mccloud_img", "随机图片", "支持批量并发获取、轮询抽卡防拦截、双重撤回与直链兜底的高性能版。", PLUGIN_VERSION)
class SetuPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cfg = config 
        
        self.cooldowns: dict[str, float] = {}
        self.cache_dir = StarTools.get_data_dir() / "temp_images"
        if not self.cache_dir.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            
        self._session: aiohttp.ClientSession | None = None
        self._ssl_context: ssl.SSLContext | None = None
        self._session_users: dict[int, int] = {}
        self._retired_sessions: dict[int, aiohttp.ClientSession] = {}
        self._config_lock = asyncio.Lock()
        self._session_lock = asyncio.Lock()
        self._last_saved_at: str | None = None
        self._schema = load_schema()
        
        # 优化 2: 维护后台任务的强引用，防止被 GC 意外回收
        self._background_tasks = set()

        routes = (
            ("config", self.page_config, ["GET"], "Read Page configuration"),
            ("config/save", self.page_save_config, ["POST"], "Save Page configuration"),
            ("api/test", self.page_test_api, ["POST"], "Test an image API URL"),
            ("status", self.page_status, ["GET"], "Read plugin runtime status"),
            ("config/export", self.page_export_config, ["GET"], "Export Page configuration"),
            ("config/import", self.page_import_config, ["POST"], "Preview or confirm configuration import"),
        )
        for suffix, handler, methods, description in routes:
            context.register_web_api(f"/{PLUGIN_NAME}/{suffix}", handler, methods, description)

    async def _close_retired_session_locked(self, session_key: int) -> None:
        session = self._retired_sessions.pop(session_key, None)
        self._session_users.pop(session_key, None)
        if session and not session.closed:
            try:
                await session.close()
            except Exception as exc:
                logger.warning(f"[随机图片] 关闭旧网络会话失败: {exc}")

    async def _retire_current_session_locked(self) -> None:
        session = self._session
        self._session = None
        self._ssl_context = None
        if session is None:
            return
        session_key = id(session)
        self._retired_sessions[session_key] = session
        if self._session_users.get(session_key, 0) == 0:
            await self._close_retired_session_locked(session_key)

    async def _close_session(self) -> None:
        async with self._config_lock:
            async with self._session_lock:
                await self._retire_current_session_locked()

    async def _apply_config(self, candidate: object) -> tuple[list[dict], dict]:
        normalized = validate_and_normalize(candidate, self._schema)
        async with self._config_lock:
            snapshot = copy.deepcopy(dict(self.cfg))
            verify_ssl_changed = normalized.get("verify_ssl") != snapshot.get("verify_ssl")
            changes = summarize_changes(snapshot, normalized)
            self.cfg.clear()
            self.cfg.update(copy.deepcopy(normalized))
            try:
                persisted = self.cfg.save_config()
                if inspect.isawaitable(persisted):
                    await persisted
            except Exception:
                self.cfg.clear()
                self.cfg.update(snapshot)
                raise
            self._last_saved_at = datetime.now(timezone.utc).isoformat()
            if verify_ssl_changed:
                async with self._session_lock:
                    await self._retire_current_session_locked()
        return changes, copy.deepcopy(normalized)

    @staticmethod
    def _validation_response(exc: ConfigValidationError):
        return json_response(
            {"saved": False, "message": "配置校验失败", "errors": exc.errors},
        )

    async def page_config(self):
        async with self._config_lock:
            current = copy.deepcopy(dict(self.cfg))
            try:
                current = validate_and_normalize(current, self._schema)
            except ConfigValidationError:
                # Keep known invalid values visible so the editor can show and repair them.
                pass
            payload = {"config": current, "schema": copy.deepcopy(self._schema)}
        return json_response(payload)

    async def page_save_config(self):
        payload = await request.json(default={})
        try:
            changes, normalized = await self._apply_config(payload)
            return json_response(
                {"saved": True, "changes": changes, "config": normalized, "saved_at": self._last_saved_at}
            )
        except ConfigValidationError as exc:
            return self._validation_response(exc)
        except Exception:
            logger.exception("[随机图片] WebUI 配置保存失败")
            return error_response("配置保存失败，原配置已恢复", status_code=500)

    async def page_status(self):
        session_state = "inactive"
        if self._session is not None:
            session_state = "closed" if self._session.closed else "active"
        return json_response(
            {
                "version": PLUGIN_VERSION,
                "source_count": len(self.cfg.get("sources", [])),
                "cooldown_count": len(self.cooldowns),
                "session": session_state,
                "last_saved_at": self._last_saved_at,
            }
        )

    async def page_export_config(self):
        async with self._config_lock:
            body = json.dumps(dict(self.cfg), ensure_ascii=False, indent=2).encode("utf-8")
        export_dir = self.cache_dir / "webui"
        export_dir.mkdir(parents=True, exist_ok=True)
        target = export_dir / "endworld-img-config.json"
        temporary = export_dir / f".{uuid.uuid4().hex}.tmp"
        temporary.write_bytes(body)
        os.replace(temporary, target)
        return file_response(target, filename="endworld-img-config.json", content_type="application/json")

    async def page_import_config(self):
        files = await request.files()
        upload = files.get("file") if files else None
        if isinstance(upload, PluginUploadFile):
            declared_size = getattr(upload, "size", None)
            if isinstance(declared_size, int) and declared_size > IMPORT_LIMIT_BYTES:
                return error_response("导入文件超过 256 KiB", status_code=400)
            import_dir = self.cache_dir / "webui" / "imports"
            import_dir.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix="import-", suffix=".json", dir=import_dir)
            os.close(fd)
            temp_path = Path(temp_name)
            try:
                await upload.save(temp_path)
                if temp_path.stat().st_size > IMPORT_LIMIT_BYTES:
                    return error_response("导入文件超过 256 KiB", status_code=400)
                try:
                    candidate = json.loads(temp_path.read_text(encoding="utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return error_response("导入文件不是有效的 UTF-8 JSON", status_code=400)
                try:
                    normalized = validate_and_normalize(candidate, self._schema)
                except ConfigValidationError as exc:
                    return self._validation_response(exc)
                async with self._config_lock:
                    changes = summarize_changes(dict(self.cfg), normalized)
                return json_response({"config": normalized, "changes": changes})
            finally:
                temp_path.unlink(missing_ok=True)

        payload = await request.json(default={})
        if not isinstance(payload, dict) or payload.get("confirm") is not True or "config" not in payload:
            return error_response("缺少导入文件或确认数据", status_code=400)
        try:
            changes, normalized = await self._apply_config(payload["config"])
            return json_response(
                {"saved": True, "changes": changes, "config": normalized, "saved_at": self._last_saved_at}
            )
        except ConfigValidationError as exc:
            return self._validation_response(exc)
        except Exception:
            logger.exception("[随机图片] WebUI 导入配置保存失败")
            return error_response("导入保存失败，原配置已恢复", status_code=500)

    async def page_test_api(self):
        payload = await request.json(default={})
        raw_url = payload.get("url") if isinstance(payload, dict) else None
        if not isinstance(raw_url, str) or not raw_url.strip():
            return error_response("请输入 API 地址", status_code=400)
        started = time.monotonic()
        async with self._session_scope() as session:
            result = await self._fetch_url(session, raw_url.strip(), max_size_mb=2, redirects=3, timeout=10)
        return json_response(
            {
                "ok": result.ok,
                "status": result.status,
                "content_type": result.content_type,
                "final_url": result.final_url,
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "size": len(result.body),
                "error": result.error,
            }
        )

    # 优化 3: 提供生命周期终止钩子，优雅释放 ClientSession 资源
    def terminate(self):
        asyncio.create_task(self._close_session())

    def _get_or_create_session_locked(self, use_ssl: bool) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._ssl_context = ssl.create_default_context()
            if not use_ssl:
                self._ssl_context.check_hostname = False
                self._ssl_context.verify_mode = ssl.CERT_NONE

            connector = aiohttp.TCPConnector(ssl=self._ssl_context)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._config_lock:
            use_ssl = self.cfg.get("verify_ssl", True)
            async with self._session_lock:
                return self._get_or_create_session_locked(use_ssl)

    @asynccontextmanager
    async def _session_scope(self):
        async with self._config_lock:
            use_ssl = self.cfg.get("verify_ssl", True)
            async with self._session_lock:
                session = self._get_or_create_session_locked(use_ssl)
                session_key = id(session)
                self._session_users[session_key] = self._session_users.get(session_key, 0) + 1
        try:
            yield session
        finally:
            async with self._session_lock:
                remaining = self._session_users.get(session_key, 1) - 1
                if remaining > 0:
                    self._session_users[session_key] = remaining
                else:
                    self._session_users.pop(session_key, None)
                    if session_key in self._retired_sessions:
                        await self._close_retired_session_locked(session_key)

    def _text(self, base_text: str) -> str:
        if self.cfg.get("catgirl_enable", False):
            suffix = self.cfg.get("catgirl_suffix", "喵~")
            return f"{base_text}{suffix}"
        return base_text

    def _clean_cooldowns(self):
        now = time.time()
        cooldown_time = self.cfg.get("cooldown", 10)
        self.cooldowns = {uid: t for uid, t in self.cooldowns.items() if now - t < cooldown_time}

    def _check_cooldown(self, user_id: str) -> float:
        now = time.time()
        if len(self.cooldowns) > 50:
            self._clean_cooldowns()
            
        cooldown_time = self.cfg.get("cooldown", 10)
        if user_id in self.cooldowns:
            elapsed = now - self.cooldowns[user_id]
            if elapsed < cooldown_time:
                return cooldown_time - elapsed
        return 0

    # 优化 1 & 5: 将 IP 校验与 DNS 解析解耦
    def _is_private_ip(self, ip_str: str | int) -> bool:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
            return (
                ip_obj.is_private
                or ip_obj.is_loopback
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            )
        except ValueError:
            return False

    async def _resolve_and_check_domain(self, hostname: str) -> bool:
        try:
            loop = asyncio.get_running_loop()
            # 优化 5: 增加超时控制，防止慢速 DNS 放大攻击阻塞主协程
            infos = await asyncio.wait_for(
                loop.getaddrinfo(hostname, 80, family=0, type=socket.SOCK_STREAM),
                timeout=5.0
            )
            for info in infos:
                resolved_ip = info[4][0]
                
                # 放行 Clash/Surge 等产生的 Fake-IP 网段 (198.18.x.x)
                if str(resolved_ip).startswith('198.18.'):
                    continue
                    
                if self._is_private_ip(resolved_ip):
                    logger.warning(f"[随机图片] SSRF拦截: 域名 {hostname} 解析出危险内部 IP: {resolved_ip}")
                    return False
            return True
        except asyncio.TimeoutError:
            logger.warning(f"[随机图片] 域名解析超时: {hostname}")
            return False # 出于安全考虑，超时默认拦截
        except Exception as e:
            logger.warning(f"[随机图片] 域名解析失败并已拦截: {e}")
            return False

    async def _is_safe_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
                return False
            hostname = parsed.hostname
            if not hostname: 
                return False
                
            forbidden_hosts = ['localhost', '::1', '0.0.0.0']
            if hostname in forbidden_hosts:
                return False

            # 判断是否直接为 IP（兼容十进制/十六进制）
            try:
                ip_to_check = int(hostname) if hostname.isdigit() else hostname
                ipaddress.ip_address(ip_to_check)
                return not self._is_private_ip(ip_to_check)
            except ValueError:
                pass 
                
            # 执行 DNS 解析校验
            return await self._resolve_and_check_domain(hostname)
        except Exception as e:
            logger.debug(f"[随机图片] URL 安全校验异常: {e}")
            return False

    def _extract_url_from_json(self, data: dict | list) -> str:
        if isinstance(data, list):
            for item in data:
                res = self._extract_url_from_json(item)
                if res: 
                    return res
        elif isinstance(data, dict):
            for key in ["original", "url_original", "url", "img", "image", "src", "link"]:
                if key in data and isinstance(data[key], str) and data[key].startswith("http"):
                    return data[key]
            for value in data.values():
                res = self._extract_url_from_json(value)
                if res: 
                    return res
        return ""

    def _compress_image(self, image_data: bytes) -> bytes:
        if not self.cfg.get("compress_enable", True):
            return image_data
            
        threshold_mb = self.cfg.get("compress_threshold", 5)
        if len(image_data) <= threshold_mb * 1024 * 1024:
            return image_data
            
        try:
            img = PILImage.open(BytesIO(image_data))
            if img.mode != 'RGB':
                img = img.convert('RGB')
            quality = self.cfg.get("compress_quality", 85)
            output_buffer = BytesIO()
            img.save(output_buffer, format='JPEG', quality=quality)
            return output_buffer.getvalue()
        except Exception as e:
            logger.warning(f"[随机图片] 压缩失败，回退使用原图: {e}")
            return image_data 

    async def _fetch_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        max_size_mb: int = 20,
        redirects: int = 3,
        timeout: float = 20,
    ) -> FetchResult:
        if redirects < 0:
            return FetchResult(False, None, "", url, b"", "重定向次数超过限制")
        if not await self._is_safe_url(url):
            logger.warning(f"[随机图片] 拦截针对非法地址的请求: {url}")
            return FetchResult(False, None, "", url, b"", "地址未通过安全检查")

        separator = "&" if "?" in url else "?"
        no_cache_url = f"{url}{separator}_t={int(time.time() * 1000)}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        try:
            async with session.get(
                no_cache_url,
                headers=headers,
                allow_redirects=False,
                timeout=timeout,
            ) as response:
                final_url = str(response.url)
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        return FetchResult(False, response.status, "", final_url, b"", "重定向缺少目标地址")
                    new_url = urljoin(final_url, location)
                    if redirects == 0:
                        return FetchResult(False, response.status, "", final_url, b"", "重定向次数超过限制")
                    return await self._fetch_url(session, new_url, max_size_mb, redirects - 1, timeout)

                content_type = response.headers.get("Content-Type", "").lower()
                if response.status != 200:
                    return FetchResult(False, response.status, content_type, final_url, b"", f"HTTP {response.status}")

                max_bytes = max_size_mb * 1024 * 1024
                content_length = response.headers.get("Content-Length")
                if content_length:
                    try:
                        if int(content_length) > max_bytes:
                            return FetchResult(False, response.status, content_type, final_url, b"", "响应大小超过限制")
                    except ValueError:
                        pass
                chunks = bytearray()
                while True:
                    chunk = await response.content.read(8192)
                    if not chunk:
                        break
                    chunks.extend(chunk)
                    if len(chunks) > max_bytes:
                        logger.warning(f"[随机图片] 数据流大小超出安全限制 ({max_size_mb}MB)")
                        return FetchResult(False, response.status, content_type, final_url, b"", "响应大小超过限制")
                return FetchResult(True, response.status, content_type, final_url, bytes(chunks), None)
        except (asyncio.TimeoutError, TimeoutError):
            return FetchResult(False, None, "", url, b"", "请求超时")
        except Exception as exc:
            logger.debug(f"[随机图片] Fetch 网络请求异常: {exc}")
            return FetchResult(False, None, "", url, b"", "网络请求失败")

    async def _safe_fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        max_size_mb: int = 20,
        redirects: int = 3,
    ) -> tuple[bytes, str, str]:
        result = await self._fetch_url(session, url, max_size_mb, redirects)
        if not result.ok:
            return b"", "", result.final_url
        return result.body, result.content_type, result.final_url

    # 优化 4: 解耦 OneBot 协议的防风控逻辑，净化主发送接口
    async def _try_onebot_forward(self, event: AstrMessageEvent, obmsg: list, use_forward: bool) -> bool | None:
        client = event.bot
        group_id = getattr(event.message_obj, "group_id", None)
        user_id = event.get_sender_id()
        bot_id = str(getattr(client, "self_id", user_id))

        if use_forward and obmsg:
            obmsg_node = [{"type": "node", "data": {"name": "虚断", "uin": bot_id, "content": obmsg}}]
            if group_id and hasattr(client, "send_group_forward_msg"):
                return await client.send_group_forward_msg(group_id=int(group_id), messages=obmsg_node)
            elif hasattr(client, "send_private_forward_msg"):
                return await client.send_private_forward_msg(user_id=int(user_id), messages=obmsg_node)
        elif obmsg:
            if group_id and hasattr(client, "send_group_msg"):
                return await client.send_group_msg(group_id=int(group_id), message=obmsg)
            elif hasattr(client, "send_private_msg"):
                return await client.send_private_msg(user_id=int(user_id), message=obmsg)
        return None

    async def _send_advanced(self, event: AstrMessageEvent, obmsg: list, fallback_chain: MessageChain, use_forward: bool):
        try:
            onebot_ret = await self._try_onebot_forward(event, obmsg, use_forward)
            if onebot_ret is not None:
                return onebot_ret if onebot_ret else True
        except Exception as e:
            logger.warning(f"[随机图片] 特定协议原生 API 调用失败，转为通用发送: {e}")

        try:
            ret = await event.send(fallback_chain)
            return ret if ret else True
        except Exception as e:
            logger.error(f"[随机图片] 通用发送彻底失败: {e}")
            return False

    async def _recall_msgs(self, event: AstrMessageEvent, rets: list, delay: int):
        logger.info(f"[随机图片] 撤回倒计时开始: {delay} 秒")
        await asyncio.sleep(delay)
        client = event.bot
        for send_ret in rets:
            if send_ret is True or not send_ret:
                continue
            try:
                if hasattr(send_ret, "recall"): 
                    await send_ret.recall()
                    continue
                    
                msg_id = None
                if isinstance(send_ret, dict): 
                    msg_id = send_ret.get("message_id")
                elif hasattr(send_ret, "message_id"): 
                    msg_id = getattr(send_ret, "message_id")
                    
                if not msg_id: 
                    continue
                
                if hasattr(client, "delete_msg"): 
                    await client.delete_msg(message_id=int(msg_id))
                elif hasattr(client, "api") and hasattr(client.api, "call_action"): 
                    await client.api.call_action("delete_msg", message_id=int(msg_id))
                elif hasattr(client, "recall"):
                    await client.recall(msg_id)
            except Exception as e: 
                logger.debug(f"[随机图片] 消息撤回失败: {e}")

    # 优化 2: 严格管理强引用
    def _create_safe_task(self, coro):
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        
        def _task_done_callback(t: asyncio.Task):
            self._background_tasks.discard(t)
            if not t.cancelled() and t.exception():
                logger.error(f"[随机图片] 异步任务执行异常: {t.exception()}")
                
        task.add_done_callback(_task_done_callback)
        return task

    async def _delayed_delete(self, path: str):
        await asyncio.sleep(30)
        try: 
            if os.path.exists(path):
                os.remove(path)
        except Exception as e: 
            logger.debug(f"[随机图片] 延迟清理缓存失败: {e}")

    def _match_source(self, msg_text: str) -> tuple[dict | None, int]:
        sources = self.cfg.get("sources", [])
        for source in sources:
            if not isinstance(source, dict): 
                continue
            keywords = source.get("keywords", [])
            for kw in keywords:
                kw = str(kw).strip()
                if not kw: 
                    continue
                if msg_text == kw:
                    return source, 1
                elif msg_text.startswith(kw + " "):
                    rest = msg_text[len(kw):].strip()
                    if rest.isdigit():
                        return source, int(rest)
        return None, 1

    async def _validate_request(self, event: AstrMessageEvent, source: dict, count: int) -> tuple[bool, int]:
        group_id = getattr(event.message_obj, "group_id", None)
        if group_id:
            group_id_str = str(group_id)
            list_mode = source.get("list_mode", "无限制")
            group_list = [str(x) for x in source.get("group_list", []) if x]
            if list_mode == "白名单" and group_id_str not in group_list: 
                return False, count
            elif list_mode == "黑名单" and group_id_str in group_list: 
                return False, count

        user_id = event.get_sender_id()
        remaining = self._check_cooldown(user_id)
        if remaining > 0:
            await event.send(MessageChain([Plain(self._text(f"冲太快了！请休息 {int(remaining)} 秒再试"))]))
            return False, count
            
        target_apis = source.get("apis", [])
        if not target_apis:
            await event.send(MessageChain([Plain(self._text(f"图源 [{source.get('name')}] 未配置 API 地址"))]))
            return False, count

        max_count = self.cfg.get("batch_max_count", 10)
        if count > max_count:
            count = max_count
            await event.send(MessageChain([Plain(self._text(f"最多只能同时请求 {max_count} 张哦，已为您调整~"))]))
        elif count <= 0:
            count = 1
            
        return True, count

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg_text = event.message_str.strip()
        matched_source, count = self._match_source(msg_text)
        
        if not matched_source: 
            return 
            
        event.stop_event()
        
        is_valid, count = await self._validate_request(event, matched_source, count)
        if not is_valid:
            return

        target_apis = matched_source.get("apis", [])
        source_use_forward = matched_source.get("use_forward", False)
        force_forward = self.cfg.get("batch_force_forward", False)
        threshold = self.cfg.get("batch_forward_threshold", 3)
        final_use_forward = source_use_forward or force_forward or (count >= threshold)

        success = await self._process_and_send(event, target_apis, matched_source, count, final_use_forward)
        if success: 
            self.cooldowns[event.get_sender_id()] = time.time()

    async def _try_download_single(self, session: aiohttp.ClientSession, api_url: str) -> tuple[str, str]:
        body, ctype, final_url = await self._safe_fetch(session, api_url)
        if not body: 
            return "", ""

        if "application/json" in ctype:
            try:
                decoded_body = body.decode('utf-8', errors='ignore')
                data = json.loads(decoded_body)
                real_img_url = self._extract_url_from_json(data)
                if real_img_url:
                    body, ctype, final_url = await self._safe_fetch(session, real_img_url)
            except json.JSONDecodeError as e:
                logger.debug(f"[随机图片] 解析图源 JSON 失败: {e}")
            except Exception as e:
                logger.debug(f"[随机图片] 处理 JSON 图源时发生未预期错误: {e}")
                
        if not body: 
            return "", ""

        if "text" in ctype and len(body) < 2000 and body.startswith(b"http"):
            try:
                real_url = body.decode('utf-8', errors='ignore').strip()
                body, ctype, final_url = await self._safe_fetch(session, real_url)
            except Exception as e:
                logger.debug(f"[随机图片] 解析纯文本 URL 时发生异常: {e}")
        
        if not body: 
            return "", ""

        body = await asyncio.to_thread(self._compress_image, body)
        
        # 优化 2: 采用标准库精确提取扩展名
        ext = mimetypes.guess_extension(ctype.split(';')[0]) or ".jpg"
        if ext == '.jpe': 
            ext = '.jpg'
            
        filename = f"{uuid.uuid4()}{ext}"
        temp_file_path = str(self.cache_dir / filename)

        async with aiofiles.open(temp_file_path, "wb") as f:
            await f.write(body)

        return temp_file_path, final_url

    async def _download_image_with_retry(self, session: aiohttp.ClientSession, api_list: list[str], max_retries: int) -> tuple[str, str]:
        valid_apis = [url.strip() for url in api_list if url.strip()]
        if not valid_apis:
            return "", ""
            
        total_attempts = max_retries + 1
        for attempt in range(total_attempts):
            api_url = valid_apis[attempt % len(valid_apis)]
            try:
                path, final_url = await self._try_download_single(session, api_url)
                if path:
                    return path, final_url
            except Exception as e:
                logger.debug(f"[随机图片] 接口请求失效 第 {attempt + 1} 次尝试 ({api_url}): {e}")
            
            if attempt < total_attempts - 1:
                if len(valid_apis) == 1:
                    await asyncio.sleep(2) 
                else:
                    await asyncio.sleep(0.5)
                
        return "", ""

    async def _process_and_send(self, event: AstrMessageEvent, api_list: list[str], source_cfg: dict, count: int, use_forward: bool) -> bool:
        max_retries = self.cfg.get("send_retries", 3)
        recall_delay = int(source_cfg.get("recall_delay", 0)) if source_cfg.get("recall_delay") else 0
        
        async with self._session_scope() as session:
            if use_forward:
                return await self._handle_batch_forward(session, event, api_list, count, max_retries, recall_delay)
            return await self._handle_single_send(session, event, api_list, count, max_retries, recall_delay)

    async def _handle_batch_forward(self, session: aiohttp.ClientSession, event: AstrMessageEvent, api_list: list[str], count: int, max_retries: int, recall_delay: int) -> bool:
        temp_files = []
        urls = []
        rets_to_recall = []
        
        tasks = [self._download_image_with_retry(session, api_list, max_retries) for _ in range(count)]
        results = await asyncio.gather(*tasks)
        
        for path, url in results:
            if path:
                temp_files.append(path)
                urls.append(url)
                
        if not temp_files:
            await event.send(MessageChain([Plain(self._text("所有图源均无法连接，或已被屏蔽"))]))
            return False
            
        last_final_url = urls[-1] if urls else ""
        
        obmsg_batch = []
        fallback_chains = []
        for path in temp_files:
            file_uri = Path(path).absolute().as_uri()
            obmsg_batch.append({'type': 'image', 'data': {'file': file_uri}})
            fallback_chains.append(Image.fromFileSystem(path))
            
        fallback_chain = MessageChain(fallback_chains)
        
        try:
            send_ret = await self._send_advanced(event, obmsg_batch, fallback_chain, use_forward=True)
            if send_ret:
                if recall_delay > 0:
                    rets_to_recall.append(send_ret)
            else:
                raise Exception("合并转发和通用发送均无返回值")
        except Exception as e:
            logger.warning(f"[随机图片] 发送失败，触发直链兜底: {e}")
            fallback_msg = self._text(f"图片批量发送均被拦截，为您提供最后一张图的直链：\n{last_final_url}")
            await self._send_advanced(event, [{'type': 'text', 'data': {'text': fallback_msg}}], MessageChain([Plain(fallback_msg)]), use_forward=True)
            self._cleanup_files(temp_files)
            return True 
            
        self._cleanup_files(temp_files)
        await self._schedule_recall(event, rets_to_recall, recall_delay)
        return True

    async def _handle_single_send(self, session: aiohttp.ClientSession, event: AstrMessageEvent, api_list: list[str], count: int, max_retries: int, recall_delay: int) -> bool:
        success_count = 0
        last_final_url = ""
        rets_to_recall = []
        
        tasks = [self._download_image_with_retry(session, api_list, max_retries) for _ in range(count)]
        results = await asyncio.gather(*tasks)
        
        for path, url in results:
            if not path:
                continue
                
            last_final_url = url
            file_uri = Path(path).absolute().as_uri()
            obmsg_img = [{'type': 'image', 'data': {'file': file_uri}}]
            fallback_chain_img = MessageChain([Image.fromFileSystem(path)])
            
            try:
                send_ret = await self._send_advanced(event, obmsg_img, fallback_chain_img, use_forward=False)
                if send_ret: 
                    success_count += 1
                    if recall_delay > 0:
                        rets_to_recall.append(send_ret)
            except Exception as e:
                logger.warning(f"[随机图片] 图片单次请求拦截或发送失败: {e}")
                
            self._cleanup_files([path])
            
        if success_count == 0:
            if last_final_url:
                fallback_msg = self._text(f"图片多次发送均被拦截（已尝试重新抽卡），为您提供最后一张图的直链：\n{last_final_url}")
                await self._send_advanced(event, [{'type': 'text', 'data': {'text': fallback_msg}}], MessageChain([Plain(fallback_msg)]), use_forward=True)
                return True
            else:
                await event.send(MessageChain([Plain(self._text("所有图源均无法连接，或已被屏蔽"))]))
                return False

        await self._schedule_recall(event, rets_to_recall, recall_delay)
        return success_count > 0

    def _cleanup_files(self, paths: list[str]):
        for path in paths:
            if path:
                self._create_safe_task(self._delayed_delete(path))

    async def _schedule_recall(self, event: AstrMessageEvent, rets_to_recall: list, recall_delay: int):
        if rets_to_recall and recall_delay > 0:
            notice_text = self._text(f"发送的内容将在 {recall_delay} 秒后自动撤回")
            try:
                notice_ret = await self._send_advanced(event, [{'type': 'text', 'data': {'text': notice_text}}], MessageChain([Plain(notice_text)]), use_forward=False)
                if notice_ret:
                    rets_to_recall.append(notice_ret)
            except Exception as e:
                logger.debug(f"[随机图片] 撤回提示附带发送失败: {e}")
                
            self._create_safe_task(self._recall_msgs(event, rets_to_recall, recall_delay))
