import os
import time
import uuid
import json
import asyncio
import aiohttp
import aiofiles
import ipaddress
import ssl
from io import BytesIO
from typing import Union, List, Tuple
from urllib.parse import urlparse, urljoin
from pathlib import Path 
from PIL import Image as PILImage 

from astrbot.api.message_components import Image, Plain
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger 

@register("mccloud_img", "随机图片", "支持批量获取、API轮询、重新抽卡防拦截、双重撤回与直链兜底。", "6.1.0")
class SetuPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cfg = config 
        
        self.cooldowns = {}
        self.cache_dir = StarTools.get_data_dir() / "temp_images"
        if not self.cache_dir.exists():
            self.cache_dir.mkdir(parents=True, exist_ok=True)

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
        # 优化 2: 引入惰性清理，避免高并发下每次请求都遍历字典重建
        if len(self.cooldowns) > 50:
            self._clean_cooldowns()
            
        cooldown_time = self.cfg.get("cooldown", 10)
        if user_id in self.cooldowns:
            elapsed = now - self.cooldowns[user_id]
            if elapsed < cooldown_time:
                return cooldown_time - elapsed
        return 0

# 优化 3: 轻量级 SSRF 防御，去除底层 DNS 强解析，兼容代理与容器网络
    async def _is_safe_url(self, url: str) -> bool:
        try:
            parsed = urlparse(url)
            hostname = parsed.hostname
            if not hostname: 
                return False
                
            forbidden_hosts = ['localhost', '::1', '0.0.0.0', '127.0.0.1']
            if hostname in forbidden_hosts:
                return False

            # 尝试直接解析为 IP 检查（针对十进制、十六进制等异常 IP 格式直接发起的绕过）
            try:
                # 兼容攻击者使用纯十进制 IP 的情况 (例如 2130706433 对应 127.0.0.1)
                ip_to_check = int(hostname) if hostname.isdigit() else hostname
                ip_obj = ipaddress.ip_address(ip_to_check)
                if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_multicast or ip_obj.is_reserved:
                    return False
            except ValueError:
                # 抛出 ValueError 说明它不是个 IP，而是一个正常的字符串域名。
                # 直接放行，交由 aiohttp 处理。若后续出现 302 重定向到内网，
                # _safe_fetch 中的 allow_redirects=False 与递归逻辑依然能将其拦截。
                pass

            return True
        except Exception as e:
            logger.debug(f"[随机图片] URL 检测过程异常: {e}")
            return False

    def _extract_url_from_json(self, data: Union[dict, list]) -> str:
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

    # 优化 1 & 4: 规范 PEP 8 写法，抛弃除了报错外的静默处理
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

    # 优化 3: 限制递归重定向以防 SSRF
    async def _safe_fetch(self, session: aiohttp.ClientSession, url: str, max_size_mb: int = 20, redirects: int = 3) -> Tuple[bytes, str, str]:
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
            # allow_redirects=False 手动接管 30x 状态码，防止被重定向到内网资源
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
        
        if use_forward:
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
                logger.warning(f"[随机图片] 合并转发调用失败，降级常规发送: {e}")

        try:
            if group_id and hasattr(client, "send_group_msg"):
                ret = await client.send_group_msg(group_id=int(group_id), message=obmsg)
                return ret if ret else True
            elif hasattr(client, "send_private_msg"):
                ret = await client.send_private_msg(user_id=int(user_id), message=obmsg)
                return ret if ret else True
        except Exception as e: 
            logger.debug(f"[随机图片] 原生常规发送失败: {e}")
            
        try:
            ret = await event.send(fallback_chain)
            return ret if ret else True
        except Exception as e:
            logger.error(f"[随机图片] 兜底发送失败: {e}")
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
                else:
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
            except Exception as e: 
                logger.debug(f"[随机图片] 消息撤回过程失败: {e}")

    def _create_safe_task(self, coro):
        task = asyncio.create_task(coro)
        task.add_done_callback(lambda t: logger.error(f"异步任务异常: {t.exception()}") if not t.cancelled() and t.exception() else None)
        return task

    async def _delayed_delete(self, path: str):
        await asyncio.sleep(30)
        try: 
            os.remove(path)
        except Exception as e: 
            logger.debug(f"[随机图片] 延迟清理缓存失败: {e}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg_text = event.message_str.strip()
        sources = self.cfg.get("sources", [])
        matched_source = None
        is_matched = False
        count = 1

        for source in sources:
            if not isinstance(source, dict): 
                continue
            keywords = source.get("keywords", [])
            for kw in keywords:
                kw = str(kw).strip()
                if not kw: 
                    continue
                if msg_text == kw:
                    matched_source = source
                    is_matched = True
                    break
                elif msg_text.startswith(kw + " "):
                    rest = msg_text[len(kw):].strip()
                    if rest.isdigit():
                        count = int(rest)
                        matched_source = source
                        is_matched = True
                        break
            if is_matched: 
                break
        
        if not is_matched or not matched_source: 
            return 

        group_id = getattr(event.message_obj, "group_id", None)
        if group_id:
            group_id_str = str(group_id)
            list_mode = matched_source.get("list_mode", "无限制")
            group_list = [str(x) for x in matched_source.get("group_list", []) if x]
            if list_mode == "白名单" and group_id_str not in group_list: 
                return 
            elif list_mode == "黑名单" and group_id_str in group_list: 
                return 

        event.stop_event()

        user_id = event.get_sender_id()
        remaining = self._check_cooldown(user_id)
        if remaining > 0:
            yield event.plain_result(self._text(f"冲太快了！请休息 {int(remaining)} 秒再试"))
            return
        
        target_apis = matched_source.get("apis", [])
        if not target_apis:
            yield event.plain_result(self._text(f"图源 [{matched_source.get('name')}] 未配置 API 地址"))
            return

        max_count = self.cfg.get("batch_max_count", 10)
        if count > max_count:
            count = max_count
            await event.send(MessageChain([Plain(self._text(f"最多只能同时请求 {max_count} 张哦，已为您调整~"))]))
        elif count <= 0:
            count = 1

        source_use_forward = matched_source.get("use_forward", False)
        force_forward = self.cfg.get("batch_force_forward", False)
        threshold = self.cfg.get("batch_forward_threshold", 3)
        final_use_forward = source_use_forward or force_forward or (count >= threshold)

        success = await self._process_and_send(event, target_apis, matched_source, count, final_use_forward)
        if success: 
            self.cooldowns[user_id] = time.time()

    # 优化 5: 使用 to_thread 异步执行压缩，彻底拯救事件循环
    async def _try_download_single(self, session: aiohttp.ClientSession, api_url: str) -> Tuple[str, str]:
        body, ctype, final_url = await self._safe_fetch(session, api_url)
        if not body: 
            return None, None

        if "application/json" in ctype:
            try:
                data = json.loads(body.decode('utf-8'))
                real_img_url = self._extract_url_from_json(data)
                if real_img_url:
                    body, ctype, final_url = await self._safe_fetch(session, real_img_url)
            except Exception as e:
                logger.debug(f"[随机图片] 解析 JSON 图源发生错误: {e}")
                
        if not body: 
            return None, None

        if "text" in ctype and len(body) < 2000 and body.startswith(b"http"):
            real_url = body.decode('utf-8').strip()
            body, ctype, final_url = await self._safe_fetch(session, real_url)
        
        if not body: 
            return None, None

        # 核心性能优化！剥离 CPU 密集型操作到独立线程池中
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

    # 优化 2: 采用轮询池分配算法，去除冗余重试，限制单张图请求最大上限
    async def _download_image_with_retry(self, session: aiohttp.ClientSession, api_list: List[str], max_retries: int) -> Tuple[str, str]:
        valid_apis = [url.strip() for url in api_list if url.strip()]
        if not valid_apis:
            return None, None
            
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
                await asyncio.sleep(1)
                
        return None, None

    # 优化 1: 分解高复杂度函数
    async def _process_and_send(self, event: AstrMessageEvent, api_list: List[str], source_cfg: dict, count: int, use_forward: bool) -> bool:
        use_ssl = self.cfg.get("verify_ssl", True)
        ssl_context = ssl.create_default_context() if use_ssl else False
        if not use_ssl:
            ssl_context = ssl.create_default_context()
            ssl_context.check_hostname = False
            ssl_context.verify_mode = ssl.CERT_NONE
            
        connector = aiohttp.TCPConnector(ssl=ssl_context)
        max_retries = self.cfg.get("send_retries", 3)
        recall_delay = int(source_cfg.get("recall_delay", 0)) if source_cfg.get("recall_delay") else 0
        
        async with aiohttp.ClientSession(connector=connector) as session:
            if use_forward:
                return await self._handle_batch_forward(session, event, api_list, count, max_retries, recall_delay)
            else:
                return await self._handle_single_send(session, event, api_list, count, max_retries, recall_delay)

    async def _handle_batch_forward(self, session, event, api_list, count, max_retries, recall_delay):
        temp_files = []
        urls = []
        rets_to_recall = []
        
        for _ in range(count):
            path, url = await self._download_image_with_retry(session, api_list, max_retries)
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
                raise Exception("框架发送接口无有效返回值")
        except Exception as e:
            logger.warning(f"[随机图片] 合并转发调用失败，触发直链兜底: {e}")
            fallback_msg = self._text(f"图片批量发送均被拦截，为您提供最后一张图的直链：\n{last_final_url}")
            await self._send_advanced(event, [{'type': 'text', 'data': {'text': fallback_msg}}], MessageChain([Plain(fallback_msg)]), use_forward=True)
            self._cleanup_files(temp_files)
            return True 
            
        self._cleanup_files(temp_files)
        await self._schedule_recall(event, rets_to_recall, recall_delay)
        return True

    async def _handle_single_send(self, session, event, api_list, count, max_retries, recall_delay):
        success_count = 0
        last_final_url = ""
        rets_to_recall = []
        
        for _ in range(count):
            path, url = await self._download_image_with_retry(session, api_list, max_retries)
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

    def _cleanup_files(self, paths: List[str]):
        for path in paths:
            self._create_safe_task(self._delayed_delete(path))

    async def _schedule_recall(self, event, rets_to_recall, recall_delay):
        if rets_to_recall and recall_delay > 0:
            notice_text = self._text(f"发送的内容将在 {recall_delay} 秒后自动撤回")
            try:
                notice_ret = await self._send_advanced(event, [{'type': 'text', 'data': {'text': notice_text}}], MessageChain([Plain(notice_text)]), use_forward=False)
                if notice_ret:
                    rets_to_recall.append(notice_ret)
            except Exception as e:
                logger.debug(f"[随机图片] 撤回提示附带发送失败: {e}")
                
            self._create_safe_task(self._recall_msgs(event, rets_to_recall, recall_delay))
