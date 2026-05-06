import os
import time
import uuid
import json
import asyncio
import aiohttp
import aiofiles
import ipaddress
import ssl
import socket
from io import BytesIO
from urllib.parse import urlparse, urljoin
from pathlib import Path 
from PIL import Image as PILImage 

from astrbot.api.message_components import Image, Plain
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger 

@register("mccloud_img", "随机图片", "支持批量并发获取、轮询抽卡防拦截、双重撤回与直链兜底的高性能版。", "6.3.0")
class SetuPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cfg = config 
        
        self.cooldowns: dict[str, float] = {}
        self.cache_dir = StarTools.get_data_dir() / "temp_images"
        if not self.cache_dir.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            
        # 优化 3 & 5: 声明全局会话和 SSL 上下文容器，推迟到异步上下文中惰性初始化
        self._session: aiohttp.ClientSession | None = None
        self._ssl_context: ssl.SSLContext | None = None

    # 获取全局复用的 ClientSession，开启 TCP 连接池复用
    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            use_ssl = self.cfg.get("verify_ssl", True)
            self._ssl_context = ssl.create_default_context() if use_ssl else ssl.create_default_context()
            if not use_ssl:
                self._ssl_context.check_hostname = False
                self._ssl_context.verify_mode = ssl.CERT_NONE
            
            connector = aiohttp.TCPConnector(ssl=self._ssl_context)
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

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

    async def _is_safe_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname: 
                return False
                
            forbidden_hosts = ['localhost', '::1', '0.0.0.0']
            if hostname in forbidden_hosts:
                return False

            ip_to_check = hostname
            try:
                if hostname.isdigit():
                    ip_to_check = int(hostname)
                ip_obj = ipaddress.ip_address(ip_to_check)
                return not (ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_multicast or ip_obj.is_reserved)
            except ValueError:
                pass 
                
            try:
                loop = asyncio.get_running_loop()
                infos = await loop.getaddrinfo(hostname, 80, family=0, type=socket.SOCK_STREAM)
                for info in infos:
                    resolved_ip = info[4][0]
                    
                    if str(resolved_ip).startswith('198.18.'):
                        continue
                        
                    ip_obj = ipaddress.ip_address(resolved_ip)
                    if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_multicast or ip_obj.is_reserved:
                        logger.warning(f"[随机图片] SSRF拦截: 域名 {hostname} 解析出危险内部 IP: {resolved_ip}")
                        return False
            except Exception as e:
                logger.debug(f"[随机图片] 域名解析失败，可能是合法的无法解析或内部环境问题: {e}")
                pass
                
            return True
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

    async def _safe_fetch(self, session: aiohttp.ClientSession, url: str, max_size_mb: int = 20, redirects: int = 3) -> tuple[bytes, str, str]:
        if redirects < 0:
            return b"", "", url

        if not await self._is_safe_url(url):
            logger.warning(f"[随机图片] 拦截针对非法地址的请求: {url}")
            return b"", "", url
        
        separator = "&" if "?" in url else "?"
        no_cache_url = f"{url}{separator}_t={int(time.time() * 1000)}"
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache"
        }

        try:
            async with session.get(no_cache_url, headers=headers, allow_redirects=False, timeout=20) as response:
                if response.status in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if location:
                        new_url = urljoin(str(response.url), location)
                        return await self._safe_fetch(session, new_url, max_size_mb, redirects - 1)
                        
                if response.status != 200:
                    return b"", "", url
                    
                content_type = response.headers.get("Content-Type", "").lower()
                final_url = str(response.url)
                body = b""
                max_bytes = max_size_mb * 1024 * 1024
                
                while True:
                    chunk = await response.content.read(8192)
                    if not chunk: 
                        break
                    body += chunk
                    if len(body) > max_bytes:
                        logger.warning(f"[随机图片] 数据流大小超出安全限制 ({max_size_mb}MB)")
                        return b"", "", final_url
                return body, content_type, final_url
        except Exception as e: 
            logger.debug(f"[随机图片] Fetch 网络请求异常: {e}")
            
        return b"", "", url

    async def _send_advanced(self, event: AstrMessageEvent, obmsg: list, fallback_chain: MessageChain, use_forward: bool):
        client = event.bot
        group_id = getattr(event.message_obj, "group_id", None)
        user_id = event.get_sender_id()
        bot_id = str(getattr(client, "self_id", user_id))
        
        if use_forward and obmsg:
            obmsg_node = [{
                "type": "node",
                "data": {"name": "虚断", "uin": bot_id, "content": obmsg}
            }]
            try:
                if group_id and hasattr(client, "send_group_forward_msg"):
                    ret = await client.send_group_forward_msg(group_id=int(group_id), messages=obmsg_node)
                    return ret if ret else True
                elif hasattr(client, "send_private_forward_msg"):
                    ret = await client.send_private_forward_msg(user_id=int(user_id), messages=obmsg_node)
                    return ret if ret else True
            except Exception as e:
                logger.warning(f"[随机图片] 底层合并转发API调用失败，准备降级常规发送: {e}")

        if obmsg:
            try:
                if group_id and hasattr(client, "send_group_msg"):
                    ret = await client.send_group_msg(group_id=int(group_id), message=obmsg)
                    return ret if ret else True
                elif hasattr(client, "send_private_msg"):
                    ret = await client.send_private_msg(user_id=int(user_id), message=obmsg)
                    return ret if ret else True
            except Exception as e:
                logger.debug(f"[随机图片] 原生常规发送失败，准备兜底: {e}")

        try:
            ret = await event.send(fallback_chain)
            return ret if ret else True
        except Exception as e:
            logger.error(f"[随机图片] 兜底通用发送失败: {e}")
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

    def _create_safe_task(self, coro):
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: logger.error(f"异步任务异常: {t.exception()}") if not t.cancelled() and t.exception() else None)
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
            # 优化 4: 修复静默吞噬异常，保留关键排错线索
            except Exception as e:
                logger.debug(f"[随机图片] 解析纯文本 URL 时发生异常: {e}")
        
        if not body: 
            return "", ""

        body = await asyncio.to_thread(self._compress_image, body)
        
        file_ext = "jpg" 
        if body[0:4] == b'\x89PNG': 
            file_ext = "png"
        elif body[0:3] == b'GIF': 
            file_ext = "gif"
        
        filename = f"{uuid.uuid4()}.{file_ext}"
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
        
        # 优化 3 & 5: 使用全局复用的 session
        session = await self._get_session()
        
        if use_forward:
            return await self._handle_batch_forward(session, event, api_list, count, max_retries, recall_delay)
        else:
            return await self._handle_single_send(session, event, api_list, count, max_retries, recall_delay)

    # 优化 1 & 2: 增加类型提示，并引入 asyncio.gather 将下载流程全并发化
    async def _handle_batch_forward(self, session: aiohttp.ClientSession, event: AstrMessageEvent, api_list: list[str], count: int, max_retries: int, recall_delay: int) -> bool:
        temp_files = []
        urls = []
        rets_to_recall = []
        
        # 核心：将按序阻塞下载改为任务并行下载
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

    # 优化 1 & 2: 增加类型提示，并发下载突破 I/O 阻塞
    async def _handle_single_send(self, session: aiohttp.ClientSession, event: AstrMessageEvent, api_list: list[str], count: int, max_retries: int, recall_delay: int) -> bool:
        success_count = 0
        last_final_url = ""
        rets_to_recall = []
        
        # 核心：先并发获取所有图片，然后再有序地排队发送到协议端防风控
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
                # 逐个发送，避免瞬间高并发发送导致底层协议瞬间封控限流
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
