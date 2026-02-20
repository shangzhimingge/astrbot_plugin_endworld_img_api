import os
import time
import uuid
import asyncio
import aiohttp
import aiofiles
from io import BytesIO
from typing import Union, List
from PIL import Image as PILImage 

from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.star import Context, Star, register

@register("endworld_img_api", "随机图片", "发送重试机制+原图优先+多指令分流。", "4.9")
class SetuPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.cfg = config 
        
        self.cooldowns = {} 
        self.cache_dir = os.path.join(os.getcwd(), "data", "temp_images")
        if not os.path.exists(self.cache_dir):
            os.makedirs(self.cache_dir, exist_ok=True)

    def _check_cooldown(self, user_id: str) -> float:
        current_time = time.time()
        cooldown_time = self.cfg.get("cooldown", 10)
        if user_id in self.cooldowns:
            elapsed = current_time - self.cooldowns[user_id]
            if elapsed < cooldown_time:
                return cooldown_time - elapsed
        return 0

    def _extract_url_from_json(self, data: Union[dict, list]) -> str:
        if isinstance(data, list):
            for item in data:
                res = self._extract_url_from_json(item)
                if res: return res
        elif isinstance(data, dict):
            # 优先查找 original 键
            for key in ["original", "url_original", "url", "img", "image", "src", "link"]:
                if key in data and isinstance(data[key], str) and data[key].startswith("http"):
                    return data[key]
            for value in data.values():
                res = self._extract_url_from_json(value)
                if res: return res
        return ""

    def _compress_image(self, image_data: bytes) -> bytes:
        """根据配置进行图片压缩"""
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
        except Exception:
            return image_data 

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        msg_text = event.message_str.strip()
        
        sources = self.cfg.get("sources", [])
        target_apis = []
        matched_source_name = ""
        is_matched = False

        for source in sources:
            if not isinstance(source, dict): continue
            keywords = source.get("keywords", [])
            for kw in keywords:
                kw = str(kw).strip()
                if not kw: continue
                if msg_text == kw or msg_text.startswith(kw + " "):
                    target_apis = source.get("apis", [])
                    matched_source_name = source.get("name", "未命名图源")
                    is_matched = True
                    break
            if is_matched: break
        
        if not is_matched: return 

        event.stop_event()

        user_id = event.get_sender_id()
        remaining = self._check_cooldown(user_id)
        if remaining > 0:
            yield event.plain_result(f"冲太快了！请休息 {int(remaining)} 秒再试。")
            return
        
        self.cooldowns[user_id] = time.time()
        
        if not target_apis:
            yield event.plain_result(f"⚠️ 图源 [{matched_source_name}] 未配置 API 地址。")
            return

        await self._process_and_send(event, target_apis)

    async def _process_and_send(self, event: AstrMessageEvent, api_list: List[str]):
        ssl_context = aiohttp.TCPConnector(verify_ssl=self.cfg.get("verify_ssl", False))
        async with aiohttp.ClientSession(connector=ssl_context) as session:
            for idx, api_url in enumerate(api_list):
                api_url = str(api_url).strip()
                if not api_url: continue

                temp_file_path = None
                final_img_url = api_url 

                try:
                    # 下载超时稍微给多点，防止下载就断了
                    async with session.get(api_url, allow_redirects=True, timeout=30) as response:
                        content_type = response.headers.get("Content-Type", "").lower()
                        image_bytes = None

                        if str(response.url) != api_url:
                            final_img_url = str(response.url)

                        if "application/json" in content_type:
                            try:
                                data = await response.json()
                                real_img_url = self._extract_url_from_json(data)
                                if real_img_url:
                                    final_img_url = real_img_url
                                    async with session.get(real_img_url, timeout=30) as img_resp:
                                        image_bytes = await img_resp.read()
                            except: pass 
                        elif "image" in content_type:
                            image_bytes = await response.read()
                        else:
                            text = await response.text()
                            if text.startswith("http"):
                                final_img_url = text.strip()
                                async with session.get(final_img_url, timeout=30) as img_resp:
                                    image_bytes = await img_resp.read()

                        if not image_bytes: continue

                        # 压缩处理
                        image_bytes = self._compress_image(image_bytes)

                        file_ext = "jpg" 
                        if image_bytes[0:4] == b'\x89PNG': file_ext = "png"
                        elif image_bytes[0:3] == b'GIF': file_ext = "gif"
                        
                        filename = f"{uuid.uuid4()}.{file_ext}"
                        temp_file_path = os.path.join(self.cache_dir, filename)

                        async with aiofiles.open(temp_file_path, "wb") as f:
                            await f.write(image_bytes)

                        # --- 发送逻辑 (带重试) ---
                        img_component = Image.fromFileSystem(temp_file_path)
                        message_chain = MessageChain([img_component])
                        
                        # 获取配置的重试次数，默认3次
                        max_retries = self.cfg.get("send_retries", 3)
                        send_success = False

                        for attempt in range(max_retries + 1):
                            try:
                                await event.send(message_chain)
                                send_success = True
                                break # 发送成功，退出重试循环
                            except Exception:
                                # 发送失败
                                if attempt < max_retries:
                                    # 如果还有重试机会，等待1秒后重试
                                    await asyncio.sleep(1)
                                else:
                                    # 次数用尽，保持 send_success = False
                                    pass

                        if send_success:
                            # 发送成功，跳出外层API循环
                            break
                        else:
                            # 彻底失败，转为发送直链
                            fallback_msg = f"⚠️ 发送失败(重试{max_retries}次)，原图直链：\n{final_img_url}"
                            await event.send(MessageChain([Plain(fallback_msg)]))
                            return # 结束处理，不再尝试下一个API

                except Exception:
                    continue
                finally:
                    # 无论成功失败，最后都清理垃圾文件
                    if temp_file_path and os.path.exists(temp_file_path):
                        async def delayed_delete(path):
                            await asyncio.sleep(15) 
                            try: os.remove(path)
                            except: pass
                        asyncio.create_task(delayed_delete(temp_file_path))
            else:
                await event.send(MessageChain([Plain("图片获取失败喵")]))
