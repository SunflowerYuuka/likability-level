import os
import json
import random
import re
import time
from typing import Dict, Any, Optional, List
from pathlib import Path
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register
from astrbot.api.provider import LLMResponse, ProviderRequest 
from astrbot.api import AstrBotConfig 

try:
    from astrbot.api import logger
except ImportError:  # 兼容不暴露 logger 的旧版本
    import logging

    logger = logging.getLogger("astrbot")

class StreamingCleaner:
    HOLD_BACK = 32
    MAX_TAG_LENGTH = 64
    CLOSERS = {"[": "]", "【": "】"}

    def __init__(self, patterns):
        self.patterns = [re.compile(pattern) for pattern in patterns]
        self.buffer = ""

    def feed(self, text: str) -> str:
        self.buffer += text
        if len(self.buffer) <= self.HOLD_BACK:
            return ""

        cut = len(self.buffer) - self.HOLD_BACK
        hold_from = self._unclosed_opener()
        if hold_from is not None:
            cut = min(cut, hold_from)

        safe = self.buffer[:cut]
        self.buffer = self.buffer[cut:]
        return self._clean(safe)

    def flush(self) -> str:
        # 生成被截断时尾部可能残留半截标记，直接丢弃，避免漏到回复里
        match = re.search(r"[\[【][^\]】]*$", self.buffer)
        if match and len(self.buffer) - match.start() <= self.MAX_TAG_LENGTH:
            self.buffer = self.buffer[: match.start()]
        text = self._clean(self.buffer)
        self.buffer = ""
        return text

    def _unclosed_opener(self) -> Optional[int]:
        """返回最靠后的、尚未闭合且长度未超限的开标记位置

        旧实现只看最后一个开括号：只要它后面还有闭合的标记，就会漏掉更早
        那个未闭合的开标记，导致 `【abc[好感度上升]` 这类内容分片错位。
        """
        for index in range(len(self.buffer) - 1, -1, -1):
            closer = self.CLOSERS.get(self.buffer[index])
            if closer is None:
                continue
            if self.buffer.find(closer, index + 1) >= 0:
                continue
            if len(self.buffer) - index <= self.MAX_TAG_LENGTH:
                return index
            return None
        return None

    def _clean(self, text: str) -> str:
        for pattern in self.patterns:
            text = pattern.sub("", text)
        return text

class FavorManager:
    """好感度管理系统"""
    DATA_PATH = Path("data/FavorSystem")
    MAINTENANCE_INTERVAL = 60  # 秒：过期清理与计数器衰减的最短执行间隔

    def __init__(self, config: AstrBotConfig): 
        self._init_path()
        self._init_config(config)
        self._init_data()
        self._maintenance(force=True)

    def _init_path(self):
        """初始化数据目录"""
        self.DATA_PATH.mkdir(parents=True, exist_ok=True)

    def _init_config(self, config: AstrBotConfig):
        """初始化配置"""
        self.config = config
        # 基础配置
        self.black_threshold = config.get("black_threshold", 3)
        self.min_favor_value = config.get("min_favor_value", -30)
        self.max_favor_value = config.get("max_favor_value", 149)
        self.black_favor_limit = config.get("black_favor_limit", -20)
        self.clean_patterns = config.get("clean_patterns", [r"【.*?】", r"\[好感度.*?\]"])
        # 自动移除配置
        self.auto_remove_enabled = config.get("auto_blacklist_clean", True)
        self.auto_remove_hours = config.get("auto_blacklist_time", 24)
        # 会话独立好感度配置
        self.session_based_favor = config.get("session_based_favor", False)
        # 会话独立黑名单配置
        self.session_based_blacklist = config.get("session_based_blacklist", False)
        # 会话独立计数器配置
        self.session_based_counter = config.get("session_based_counter", False)
        # 低好感计数器自动减少配置
        self.auto_decrease_enabled = config.get("auto_decrease_counter", True)
        self.auto_decrease_hours = config.get("auto_decrease_counter_hours", 24)
        self.auto_decrease_amount = config.get("auto_decrease_counter_amount", 1)

    def _init_data(self):
        """初始化数据"""
        self.favor_data = {}
        self.session_favor_data = {}  # 新增：会话好感度数据
        self.blacklist = {}
        self.session_blacklist = {}  # 新增：会话黑名单数据
        self.whitelist = {}
        self.low_counter = {}
        self.session_low_counter = {}  # 新增：会话计数器数据
        self.last_decrease_time = {}  # 新增：记录上次减少时间
        self._last_maintenance = 0.0
        self._load_all_data()

    def _load_all_data(self):
        """从磁盘加载所有数据（仅在插件加载时执行一次，之后常驻内存）"""
        self.favor_data = self._load_data("favor_data.json")
        self.session_favor_data = self._load_data("session_favor_data.json")  # 新增：加载会话好感度数据
        self.blacklist = self._load_data("blacklist.json")
        self.session_blacklist = self._load_data("session_blacklist.json")  # 新增：加载会话黑名单数据
        self.whitelist = self._load_data("whitelist.json")
        self.low_counter = self._load_data("low_counter.json")
        self.session_low_counter = self._load_data("session_low_counter.json")  # 新增：加载会话计数器数据
        self.last_decrease_time = self._load_data("last_decrease_time.json")  # 新增：加载上次减少时间

    def _maintenance(self, force: bool = False):
        """按间隔执行过期清理与计数器衰减，避免每条消息都做全表扫描与写盘"""
        now = time.time()
        if not force and now - self._last_maintenance < self.MAINTENANCE_INTERVAL:
            return
        self._last_maintenance = now
        self._check_auto_removal()
        self._check_auto_decrease()

    def _load_data(self, filename: str) -> Dict[str, Any]:
        """加载指定文件的数据；文件损坏时备份原文件并告警，避免数据被静默清空"""
        path = self.DATA_PATH / filename
        if not path.exists():
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise TypeError("数据根节点不是 JSON 对象")
            return {str(k): v for k, v in raw.items()}
        except Exception as e:  # JSONDecodeError / OSError / UnicodeDecodeError / TypeError
            backup = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
            logger.error(f"[FavorSystem] 读取 {filename} 失败（{e}），已备份到 {backup.name}")
            try:
                os.replace(path, backup)
            except OSError:
                logger.error(f"[FavorSystem] 备份 {filename} 失败，后续写入可能覆盖原数据")
            return {}

    def _save_data(self, data: Dict, filename: str):
        """原子保存数据：先写临时文件再替换，避免中断产生半截 JSON"""
        path = self.DATA_PATH / filename
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump({str(k): v for k, v in data.items()}, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        except OSError as e:
            logger.error(f"[FavorSystem] 保存 {filename} 失败: {e}")

    def _reset_user_state(self, user_id: str, session_id: str = None):
        """把用户在对应作用域下的好感度归零、计数器清空

        session_id 为 None 表示全局黑名单：全局与所有会话都重置；
        否则只重置该会话（未开启会话模式时退回全局）。
        """
        user_id = str(user_id)
        touched_favor = touched_counter = touched_time = False

        if session_id is not None and self.session_based_favor:
            favor_targets = [self.session_favor_data.get(session_id)]
        elif session_id is not None:
            favor_targets = [self.favor_data]
        else:
            favor_targets = [self.favor_data, *self.session_favor_data.values()]

        for users in favor_targets:
            if users is not None and user_id in users:
                users[user_id] = 0
                touched_favor = True

        if session_id is not None and self.session_based_counter:
            counter_targets = [self.session_low_counter.get(session_id, {})]
        elif session_id is not None:
            counter_targets = [self.low_counter]
        else:
            counter_targets = [self.low_counter, *self.session_low_counter.values()]

        for users in counter_targets:
            if user_id in users:
                del users[user_id]
                touched_counter = True

        keys = [user_id]
        if session_id is not None:
            keys.append(f"{session_id}_{user_id}")
        else:
            keys.extend(f"{sid}_{user_id}" for sid in self.session_low_counter)
        for key in keys:
            if self.last_decrease_time.pop(key, None) is not None:
                touched_time = True

        if touched_favor:
            self._save_data(self.favor_data, "favor_data.json")
            self._save_data(self.session_favor_data, "session_favor_data.json")
        if touched_counter:
            self._save_data(self.low_counter, "low_counter.json")
            self._save_data(self.session_low_counter, "session_low_counter.json")
        if touched_time:
            self._save_data(self.last_decrease_time, "last_decrease_time.json")

    def _check_auto_removal(self):
        """检查并处理需要自动移除的黑名单用户"""
        if not self.auto_remove_enabled:
            return

        now = time.time()
        ttl = max(0.0, float(self.auto_remove_hours or 0)) * 3600

        def expired(entry) -> bool:
            return (
                isinstance(entry, dict)
                and entry.get("auto_added", False)
                and now - float(entry.get("timestamp", 0)) >= ttl
            )

        # 处理全局黑名单
        removed = [uid for uid, entry in self.blacklist.items() if expired(entry)]
        if removed:
            for user_id in removed:
                del self.blacklist[user_id]
                self._reset_user_state(user_id)
            self._save_data(self.blacklist, "blacklist.json")

        # 处理会话黑名单
        if not self.session_based_blacklist:
            return
        for session_id, users in list(self.session_blacklist.items()):
            removed = [uid for uid, entry in users.items() if expired(entry)]
            if not removed:
                continue
            for user_id in removed:
                del users[user_id]
                self._reset_user_state(user_id, session_id)
            self._save_data(self.session_blacklist, "session_blacklist.json")

    def is_blacklisted(self, user_id: str, session_id: str = None) -> bool:
        """检查用户是否在黑名单中"""
        user_id = str(user_id)
        if self.session_based_blacklist and session_id:
            return user_id in self.session_blacklist.get(session_id, {})
        return user_id in self.blacklist

    def add_to_blacklist(self, user_id: str, session_id: str = None, auto_added: bool = False):
        """将用户添加到黑名单"""
        user_id = str(user_id)
        if self.session_based_blacklist and session_id:
            if session_id not in self.session_blacklist:
                self.session_blacklist[session_id] = {}
            self.session_blacklist[session_id][user_id] = {
                "timestamp": time.time(),
                "auto_added": auto_added
            }
            self._save_data(self.session_blacklist, "session_blacklist.json")
        else:
            self.blacklist[user_id] = {
                "timestamp": time.time(),
                "auto_added": auto_added
            }
            self._save_data(self.blacklist, "blacklist.json")

    def remove_from_blacklist(self, user_id: str, session_id: str = None):
        """将用户从黑名单中移除"""
        user_id = str(user_id)
        if self.session_based_blacklist and session_id:
            if session_id in self.session_blacklist and user_id in self.session_blacklist[session_id]:
                del self.session_blacklist[session_id][user_id]
                self._save_data(self.session_blacklist, "session_blacklist.json")
        else:
            if user_id in self.blacklist:
                del self.blacklist[user_id]
                self._save_data(self.blacklist, "blacklist.json")

    def get_low_counter(self, user_id: str, session_id: str = None) -> int:
        """获取用户的低好感度计数器值"""
        user_id = str(user_id)
        if self.session_based_counter and session_id:
            return self.session_low_counter.get(session_id, {}).get(user_id, 0)
        return self.low_counter.get(user_id, 0)

    def increment_low_counter(self, user_id: str, session_id: str = None):
        """增加用户的低好感度计数器值"""
        user_id = str(user_id)
        if self.session_based_counter and session_id:
            if session_id not in self.session_low_counter:
                self.session_low_counter[session_id] = {}
            self.session_low_counter[session_id][user_id] = self.session_low_counter[session_id].get(user_id, 0) + 1
            self._save_data(self.session_low_counter, "session_low_counter.json")
        else:
            self.low_counter[user_id] = self.low_counter.get(user_id, 0) + 1
            self._save_data(self.low_counter, "low_counter.json")

    def reset_low_counter(self, user_id: str, session_id: str = None):
        """重置用户的低好感度计数器值"""
        user_id = str(user_id)
        if self.session_based_counter and session_id:
            if session_id in self.session_low_counter and user_id in self.session_low_counter[session_id]:
                del self.session_low_counter[session_id][user_id]
                self._save_data(self.session_low_counter, "session_low_counter.json")
        else:
            if user_id in self.low_counter:
                del self.low_counter[user_id]
                self._save_data(self.low_counter, "low_counter.json")

    def _check_blacklist_condition(self, user_id: str, current: int, session_id: str = None):
        """检查是否需要加入黑名单"""
        if current <= self.black_favor_limit and self.get_low_counter(user_id, session_id) >= self.black_threshold:
            if not self.is_blacklisted(user_id, session_id):
                self.add_to_blacklist(user_id, session_id, auto_added=True)

    def update_favor(self, user_id: str, change: str, session_id: str = None):
        """更新好感度"""
        user_id = str(user_id)
        self._maintenance()

        if user_id in self.whitelist:
            return

        # 根据配置决定使用哪种好感度数据
        if self.session_based_favor and session_id:
            current = self.session_favor_data.get(session_id, {}).get(user_id, 0)
            delta = self._calculate_favor_delta(change)
            
            if delta is not None:
                current = self._apply_favor_change(current, delta, user_id, session_id)
                # 如果是好感度下降，且当前好感度已经达到或低于阈值，更新计数器
                if delta < 0 and current <= self.black_favor_limit:
                    self.increment_low_counter(user_id, session_id)
                self._check_blacklist_condition(user_id, current, session_id)
        else:
            current = self.favor_data.get(user_id, 0)
            delta = self._calculate_favor_delta(change)
            
            if delta is not None:
                current = self._apply_favor_change(current, delta, user_id)
                # 如果是好感度下降，且当前好感度已经达到或低于阈值，更新计数器
                if delta < 0 and current <= self.black_favor_limit:
                    self.increment_low_counter(user_id, session_id)
                self._check_blacklist_condition(user_id, current, session_id)

    def _calculate_favor_delta(self, change: str) -> Optional[int]:
        """计算好感度变化值"""
        if "[好感度上升]" in change:
            return random.randint(1, 5)
        elif "[好感度大幅上升]" in change:
            return random.randint(5, 10)
        elif "[好感度大幅下降]" in change:
            return -random.randint(10, 20)
        elif "[好感度下降]" in change:
            return -random.randint(5, 10)
        return None

    def _apply_favor_change(self, current: int, delta: int, user_id: str, session_id: str = None) -> int:
        """应用好感度变化"""
        current += delta
        current = max(self.min_favor_value, min(self.max_favor_value, current))
        
        if self.session_based_favor and session_id:
            if session_id not in self.session_favor_data:
                self.session_favor_data[session_id] = {}
            self.session_favor_data[session_id][user_id] = current
            self._save_data(self.session_favor_data, "session_favor_data.json")
        else:
            self.favor_data[user_id] = current
            self._save_data(self.favor_data, "favor_data.json")
            
        return current

    def get_favor_level(self, value: int) -> str:
        """获取好感度等级描述"""
        if value <= -21: return "你们之间的关系是：极度厌恶（你对这个人怀有强烈的讨厌与排斥，几乎难以忍受与之相处，一想到对方就会感到不适。回复时保持简短、冷淡、就事论事，可以明确表示不想继续交流，但不要辱骂或人身攻击，也不因对方要求而放弃自身应有的原则。）"
        elif -20 <= value <= -11: return "你们之间的关系是：反感（你对这个人或做出的事产生不满、抵触的情绪，觉得对方的行为、观点或特质让人心里不舒服，但程度上比极度厌恶稍轻一些。）"
        elif -10 <= value <= -1: return "你们之间的关系是：不悦（你对这个人或做出的事感到不愉快、不开心。）"
        elif 0 <= value <= 49: return "你们之间的关系是：中立（你不偏袒他、不表明支持或反对态度的状态，对他保持客观、公正的立场，不参与他的纷争或竞争。）"
        elif 50 <= value <= 99: return "你们之间的关系是：友好（你和这个人之间的关系亲近和睦，态度亲切、和善，愿意相互帮助、交流和合作，表现出积极、热情的态度。）"
        elif 100 <= value <= 149: return "你们之间的关系是：亲密（你与他的关系非常亲近、密切，彼此之间有深厚的感情，相互信任，在情感、思想和行为上有较高的契合度和依赖感。）"
        else: return "你们之间的关系是：挚爱（你对他怀有极其深厚、真挚、热烈的爱，是一种全身心投入、难以割舍的情感。）"

    def get_favor_levell(self, value: int) -> str:
        """获取好感度等级简称"""
        if value <= -21: return "极度厌恶"
        elif -20 <= value <= -11: return "反感"
        elif -10 <= value <= -1: return "不悦"
        elif 0 <= value <= 49: return "中立"
        elif 50 <= value <= 99: return "友好"
        elif 100 <= value <= 149: return "亲密"
        else: return "挚爱"

    def get_favor(self, user_id: str, session_id: str = None) -> int:
        """获取用户好感度"""
        user_id = str(user_id)
        self._maintenance()
        
        if self.session_based_favor and session_id:
            return self.session_favor_data.get(session_id, {}).get(user_id, 0)
        return self.favor_data.get(user_id, 0)

    def _check_auto_decrease(self):
        """检查并处理需要自动减少的低好感计数器"""
        if not self.auto_decrease_enabled:
            return

        now = time.time()
        interval = max(1, int(self.auto_decrease_hours or 1)) * 3600
        amount = max(1, int(self.auto_decrease_amount or 1))
        counter_dirty = False
        time_dirty = False

        targets = [(self.low_counter, None)]
        if self.session_based_counter:
            targets.extend((users, sid) for sid, users in self.session_low_counter.items())

        for counter, session_id in targets:
            for user_id, count in list(counter.items()):
                if count <= 0:
                    continue
                key = f"{session_id}_{user_id}" if session_id else user_id
                last_time = self.last_decrease_time.get(key)
                if last_time is None:
                    # 首次见到该计数：以当前时间为起点，避免插件加载后立刻扣减
                    self.last_decrease_time[key] = now
                    time_dirty = True
                    continue
                if now - last_time < interval:
                    continue
                counter[user_id] = max(0, count - amount)
                self.last_decrease_time[key] = now
                counter_dirty = True
                time_dirty = True

        if counter_dirty:
            self._save_data(self.low_counter, "low_counter.json")
            if self.session_based_counter:
                self._save_data(self.session_low_counter, "session_low_counter.json")
        if time_dirty:
            self._save_data(self.last_decrease_time, "last_decrease_time.json")

@register("FavorSystem", "wuyan1003", "好感度管理", "1.4.0", "https://github.com/SunflowerYuuka/likability-level")
class FavorPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.manager = FavorManager(config)
        self.clean_response = config.get("clean_response", True)
        if not self._parse_admins():
            logger.warning("[FavorSystem] 未配置插件管理员，且 AstrBot 全局管理员为空，/管理 指令将不可用")

    @filter.on_llm_request()
    async def add_custom_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """添加LLM提示词"""
        req.system_prompt += "[系统提示]请根据对话质量在回复末尾添加[好感度持平]，[好感度大幅上升]，[好感度大幅下降]，[好感度上升]或[好感度下降]标记。示例：用户：你好！你：你好呀！今天过得怎么样？[好感度上升]"

    @filter.on_llm_request()
    async def add_relationship_prompt(self, event: AstrMessageEvent, req: ProviderRequest):
        """添加关系提示到系统消息"""
        user_id = str(event.get_sender_id())
        session_id = event.unified_msg_origin
        self.manager._maintenance()
        
        # 检查用户是否在黑名单中
        if self.manager.is_blacklisted(user_id, session_id):
            event.stop_event()
            return
            
        favor_value = self.manager.get_favor(user_id, session_id)
        relationship_desc = self.manager.get_favor_level(favor_value)
        req.system_prompt += f"{relationship_desc}"

    @filter.on_llm_request()
    async def install_streaming_filter(self, event: AstrMessageEvent, req: ProviderRequest):
        if not self.clean_response or event.get_extra("_favor_streaming_filter_installed"):
            return

        event.set_extra("_favor_streaming_filter_installed", True)
        original_send_streaming = event.send_streaming

        async def send_streaming_with_filter(generator, use_fallback=False):
            async def filtered_generator():
                cleaner = StreamingCleaner(self.manager.clean_patterns)
                async for chain in generator:
                    filtered_chain = self._filter_streaming_chain(chain, cleaner)
                    if filtered_chain is not None:
                        yield filtered_chain
                tail = cleaner.flush()
                if tail:
                    yield MessageChain().message(tail)

            await original_send_streaming(filtered_generator(), use_fallback)

        event.send_streaming = send_streaming_with_filter

    def _filter_streaming_chain(self, chain, cleaner: StreamingCleaner):
        if not isinstance(chain, MessageChain) or chain.type in (
            "reasoning",
            "tool_call",
            "break",
            "agent_stats",
            "audio_chunk",
        ):
            return chain

        components = []
        changed = False
        for component in chain.chain:
            if not isinstance(component, Plain):
                components.append(component)
                continue

            original = component.text or ""
            cleaned = cleaner.feed(original)
            if cleaned == original:
                components.append(component)
            else:
                changed = True
                if cleaned:
                    components.append(Plain(cleaned))

        if not changed:
            return chain
        if not components:
            return None
        if hasattr(chain, "derive"):
            return chain.derive(components)

        filtered_chain = MessageChain(chain=components, type=chain.type)
        filtered_chain.use_t2i_ = getattr(chain, "use_t2i_", None)
        if hasattr(chain, "use_markdown_"):
            filtered_chain.use_markdown_ = chain.use_markdown_
        return filtered_chain

    @filter.on_llm_response()
    async def on_llm_resp(self, event: AstrMessageEvent, resp: LLMResponse):
        """处理LLM响应"""
        user_id = str(event.get_sender_id())
        session_id = event.unified_msg_origin
        self.manager._maintenance()

        original_text = resp.completion_text
        self.manager.update_favor(user_id, original_text, session_id)

        if self.clean_response:
            cleaned_text = original_text
            for pattern in self.manager.clean_patterns:
                cleaned_text = re.sub(pattern, '', cleaned_text)
            resp.completion_text = cleaned_text.strip()

    @filter.command("好感度")
    async def query_favor(self, event: AstrMessageEvent):
        """查询好感度"""
        user_id = str(event.get_sender_id())
        session_id = event.unified_msg_origin
        self.manager._maintenance()

        if self.manager.is_blacklisted(user_id, session_id):
            yield event.plain_result("你已被列入黑名单")
            return

        favor = self.manager.get_favor(user_id, session_id)
        level = self.manager.get_favor_levell(favor)
        counter = self.manager.get_low_counter(user_id, session_id)
        yield event.plain_result(f"当前好感度：{favor} ({level})\n低好感度计数：{counter}")

    @filter.command("管理")
    async def admin_control(self, event: AstrMessageEvent, cmd: str, target: str = None, value: int = None):
        """管理员控制命令"""
        admins = self._parse_admins()
        if str(event.get_sender_id()) not in admins:
            yield event.plain_result("⚠️ 你没有权限执行此操作")
            event.stop_event()
            return

        target = str(target).strip() if target else None
        session_id = event.unified_msg_origin
        self.manager._maintenance()

        try:
            if cmd == "好感度":
                if target and value is not None:
                    low = int(self.manager.min_favor_value)
                    high = int(self.manager.max_favor_value)
                    clamped_value = max(low, min(high, int(value)))
                    if self.manager.session_based_favor:
                        if session_id not in self.manager.session_favor_data:
                            self.manager.session_favor_data[session_id] = {}
                        self.manager.session_favor_data[session_id][target] = clamped_value
                        self.manager._save_data(self.manager.session_favor_data, "session_favor_data.json")
                    else:
                        self.manager.favor_data[target] = clamped_value
                        self.manager._save_data(self.manager.favor_data, "favor_data.json")
                    tip = "" if clamped_value == int(value) else f"（已按配置限制在 {low} ~ {high}）"
                    yield event.plain_result(f"✅ 用户 {target} 好感度已设为 {clamped_value}{tip}")
                elif target:
                    favor = self.manager.get_favor(target, session_id)
                    level = self.manager.get_favor_levell(favor)
                    yield event.plain_result(f"用户 {target} 当前好感度：{favor}（{level}）")
                else:
                    yield event.plain_result(self._favor_summary(session_id))
            elif cmd == "黑名单":
                if not target:
                    if self.manager.session_based_blacklist:
                        data = json.dumps(self.manager.session_blacklist.get(session_id, {}), indent=2, ensure_ascii=False)
                        yield event.plain_result(f"当前会话黑名单用户：\n{data}")
                    else:
                        data = json.dumps(self.manager.blacklist, indent=2, ensure_ascii=False)
                        yield event.plain_result(f"黑名单用户：\n{data}")
                else:
                    if self.manager.is_blacklisted(target, session_id):
                        yield event.plain_result("⚠️ 该用户已在黑名单中")
                    else:
                        self.manager.add_to_blacklist(target, session_id)
                        yield event.plain_result(f"⛔ 用户 {target} 已加入黑名单")
            elif cmd == "移出黑名单":
                if not target:
                    yield event.plain_result("⚠️ 请指定要移出黑名单的用户")
                else:
                    if not self.manager.is_blacklisted(target, session_id):
                        yield event.plain_result("⚠️ 该用户不在黑名单中")
                    else:
                        # 移除黑名单，并重置该作用域下的好感度和计数器
                        self.manager.remove_from_blacklist(target, session_id)
                        self.manager._reset_user_state(target, session_id)
                        
                        yield event.plain_result(f"✅ 用户 {target} 已移出黑名单，并重置好感度和计数器")
            elif cmd == "白名单":
                if not target:
                    data = json.dumps(self.manager.whitelist, indent=2, ensure_ascii=False)
                    yield event.plain_result(f"白名单用户：\n{data}")
                else:
                    if target in self.manager.whitelist:
                        yield event.plain_result("⚠️ 该用户已在白名单中")
                    else:
                        self.manager.whitelist[target] = True
                        self.manager._save_data(self.manager.whitelist, "whitelist.json")
                        yield event.plain_result(f"✅ 用户 {target} 已加入白名单")
            elif cmd == "移出白名单":
                if not target:
                    yield event.plain_result("⚠️ 请指定要移出白名单的用户")
                else:
                    if target not in self.manager.whitelist:
                        yield event.plain_result("⚠️ 该用户不在白名单中")
                    else:
                        del self.manager.whitelist[target]
                        self.manager._save_data(self.manager.whitelist, "whitelist.json")
                        yield event.plain_result(f"✅ 用户 {target} 已移出白名单")
            elif cmd == "计数器":
                if not target:
                    yield event.plain_result(f"当前计数器设置：\n自动减少：{'开启' if self.manager.auto_decrease_enabled else '关闭'}\n减少间隔：{self.manager.auto_decrease_hours}小时\n减少数量：{self.manager.auto_decrease_amount}")
                else:
                    if target == "开启":
                        self.manager.auto_decrease_enabled = True
                        await self._persist("auto_decrease_counter", True)
                        yield event.plain_result("✅ 已开启计数器自动减少功能")
                    elif target == "关闭":
                        self.manager.auto_decrease_enabled = False
                        await self._persist("auto_decrease_counter", False)
                        yield event.plain_result("✅ 已关闭计数器自动减少功能")
                    elif target == "间隔" and value is not None:
                        if value <= 0:
                            yield event.plain_result("⚠️ 间隔时间必须大于0")
                        else:
                            self.manager.auto_decrease_hours = value
                            await self._persist("auto_decrease_counter_hours", value)
                            yield event.plain_result(f"✅ 已设置计数器减少间隔为 {value} 小时")
                    elif target == "数量" and value is not None:
                        if value <= 0:
                            yield event.plain_result("⚠️ 减少数量必须大于0")
                        else:
                            self.manager.auto_decrease_amount = value
                            await self._persist("auto_decrease_counter_amount", value)
                            yield event.plain_result(f"✅ 已设置计数器每次减少数量为 {value}")
                    else:
                        yield event.plain_result("❌ 无效的参数，可用参数：开启/关闭/间隔/数量")
            else:
                yield event.plain_result("❌ 无效指令，可用命令：好感度/黑名单/移出黑名单/白名单/移出白名单/计数器")
        except ValueError:
            yield event.plain_result("❌ 数值参数必须为整数")
        except Exception as e:
            yield event.plain_result(f"⚠️ 操作失败：{str(e)}")

    def _favor_summary(self, session_id: str = None) -> str:
        """好感度统计摘要：只给聚合数据，避免把全量用户 ID 发到群里"""
        if self.manager.session_based_favor:
            data = self.manager.session_favor_data.get(session_id, {})
            scope = "当前会话"
        else:
            data = self.manager.favor_data
            scope = "全局"

        values = [v for v in data.values() if isinstance(v, (int, float))]
        if not values:
            return f"{scope}暂无好感度记录"
        average = sum(values) / len(values)
        return (
            f"{scope}好感度统计：共 {len(values)} 人，平均 {average:.1f}，"
            f"最高 {max(values)}，最低 {min(values)}（查询个人请用 /管理 好感度 <QQ号>）"
        )

    async def _persist(self, key: str, value) -> None:
        """把运行期修改写回插件配置，避免重启后设置丢失"""
        try:
            self.config[key] = value
            save_async = getattr(self.config, "save_config_async", None)
            if save_async is not None:
                await save_async()
            elif hasattr(self.config, "save_config"):
                self.config.save_config()
        except Exception as e:
            logger.warning(f"[FavorSystem] 保存配置 {key} 失败: {e}")

    def _parse_admins(self) -> List[str]:
        """解析管理员 ID：优先插件配置，未配置时回退到 AstrBot 全局管理员"""
        admins = self.config.get("admins_id", [])
        if isinstance(admins, str):
            admins = admins.split(",")
        admins = [str(x).strip() for x in (admins or []) if str(x).strip()]
        if admins:
            return admins

        try:
            global_admins = self.context.get_config().get("admins_id", [])
        except Exception as e:
            logger.warning(f"[FavorSystem] 读取全局管理员失败: {e}")
            global_admins = []
        if isinstance(global_admins, str):
            global_admins = global_admins.split(",")
        return [str(x).strip() for x in (global_admins or []) if str(x).strip()]

    async def terminate(self):
        """插件终止时保存数据"""
        self.manager._save_data(self.manager.favor_data, "favor_data.json")
        self.manager._save_data(self.manager.session_favor_data, "session_favor_data.json")
        self.manager._save_data(self.manager.blacklist, "blacklist.json")
        self.manager._save_data(self.manager.session_blacklist, "session_blacklist.json")
        self.manager._save_data(self.manager.whitelist, "whitelist.json")
        self.manager._save_data(self.manager.low_counter, "low_counter.json")
        self.manager._save_data(self.manager.session_low_counter, "session_low_counter.json")
        self.manager._save_data(self.manager.last_decrease_time, "last_decrease_time.json")  # 新增：保存上次减少时间