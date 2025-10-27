from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_action import BaseAction, ActionActivationType
from src.plugin_system.base.config_types import ConfigField
from src.plugin_system.base.component_types import ComponentInfo
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.apis import generator_api, config_api, database_api
from src.common.database.database_model import PersonInfo
from src.common.logger import get_logger
from typing import Tuple, Optional, Dict, Any, List, Type
import random
import asyncio
import toml
import time
import os

logger = get_logger("anti_poke")

# 使用模块级别的全局变量来保存状态
_POKE_STATE = {
    'poke_count': 0,
    'is_silent': False,
    'silence_start_time': 0,
    'last_poke_time': 0,
    'current_silence_duration': 0,
    'current_poke_threshold': 0,
    'decay_task': None,
    'counter_lock': None,
    'last_poke_back_time': 0,
    'last_poke_received_time': 0,
}

POKE_BACK_COOLDOWN = 10

def _get_or_create_lock():
    """获取或创建异步锁，处理事件循环未就绪的情况"""
    if _POKE_STATE['counter_lock'] is None:
        try:
            _POKE_STATE['counter_lock'] = asyncio.Lock()
        except RuntimeError:
            # 如果没有事件循环，返回None
            return None
    return _POKE_STATE['counter_lock']

@register_plugin
class AntiPokePlugin(BasePlugin):
    """防戳插件
    - 支持配置一些具体参数以更像人
    """

    # 插件基本信息
    plugin_name = "anti_poke_plugin"
    enable_plugin = True
    config_file_name = "config.toml"
    dependencies = []
    python_dependencies = []

    # 配置节描述
    config_section_descriptions = {
        "plugin": "插件基本配置",
        "components": "组件启用控制",
        "poke_value":"防戳机制的参数配置（支持热重载）",
        "logging": "日志记录配置",
    }

    # 配置Schema定义
    config_schema = {
        "plugin": {
            "config_version": ConfigField(type=str, default="1.4.6", description="插件配置文件版本号"),
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
        },
        "components": {
            "enable_anti_poke": ConfigField(type=bool, default=True, description="是否启用防戳插件本体组件"),
            "enable_may_poke": ConfigField(type=bool, default=True, description="是否启用较为主动的戳人（实验性功能）"),
        },
        "poke_value": {
            "min_silence_time": ConfigField(type=int, default = 120, description="戳一戳沉默的最短时间，单位为秒，整数"),
            "max_silence_time": ConfigField(type=int, default = 300, description="戳一戳沉默的最长时间，单位为秒，整数"),
            "min_silence_counts": ConfigField(type=int, default = 5, description="沉默需要被戳的最小次数"),
            "max_silence_counts": ConfigField(type=int, default = 9, description="沉默需要被戳的最大次数"),
            "counts_decay_interval": ConfigField(type=int, default = 150, description="被戳次数的递减间隔，单位为秒"),
            "reflect_probability": ConfigField(type=float, default = 0.4, description="戳回去的概率，取值0到1之间任意小数。注意，不反戳就会正常回复"),
            "follow_probability": ConfigField(type=float, default = 0.3, description="跟戳的概率，取值0到1之间任意小数。"),
            "insensitivity_duration": ConfigField(type=float, default = 4, description="钝感时长，该设置决定了麦麦的戳一戳钝感时间（无敌帧），整数"),
        },
        "logging": {
            "level": ConfigField(
                type=str, default="INFO", description="日志级别", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
            ),
            "prefix": ConfigField(type=str, default="[Anti_Poke]", description="日志前缀"),
        },
    }


    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """返回插件包含的组件列表"""

        components = []

        if self.get_config("components.enable_may_poke", True):
            components.append((AntiPokeAction.get_action_info(), AntiPokeAction))

        if self.get_config("components.enable_anti_poke", True):
            components.append((AntiPokeCommand.get_command_info(), AntiPokeCommand))

        return components
    
class AntiPokeAction(BaseAction):
    action_name = "may_poke"

    # 一些必要的初始化
    activation_type = ActionActivationType.ALWAYS
    activation_keywords = ["戳一戳", "戳戳","戳一下","戳我","戳了戳"]
    keyword_case_sensitive = False

     # 模式和并行控制
    parallel_action = False

    action_description = "根据当前聊天内容戳一戳别人" # action描述
    action_parameters = {
        "case": "让你决定执行这个动作的情况，必填，只能填一个参数。如果你是被对方请求戳一戳他本人，填'request'；如果你是因为想活跃气氛，开个玩笑戳他，填'joke'；如果你真的不清楚该填什么，请什么都不要填！",
        "target_name": "要戳的人的名字，必填"
    }
    action_require = [
        "当用户明确请求你戳一戳他的时候，看心情使用（这意味着你可以根据自己的心情拒绝执行这个动作）",
        "当聊天环境整体显得轻松趣味时，你可以开玩笑地使用，但最好在当前聊天话题提到戳一戳等字眼时使用",
        "如果用户想让你去戳并非用户本人的其他人的时候，绝对不要使用！！！",
        "使用过该动作一次后，尽量避免再度使用这个动作！！！"
    ]

    associated_types = ["text","emoji","image"] #该插件会发送的消息类型

    def __init__(self,
    action_data: dict,
    action_reasoning: str,
    cycle_timers: dict,
    thinking_id: str,
    global_config: Optional[dict] = None,
    **kwargs,
    ):
        # 显式调用父类初始化
        super().__init__(
        action_data=action_data,
        action_reasoning=action_reasoning,
        cycle_timers=cycle_timers,
        thinking_id=thinking_id,
        global_config=global_config,
        **kwargs
    )

    async def execute(self) -> Tuple[bool, str]:

        if not self.user_id:
            target_name = self.action_data.get("target_name", "")
            personinfo = await database_api.db_get(
            PersonInfo,
            filters={"person_name": f"{target_name}"},
            limit=1
            )
            if personinfo:
                self.user_id = personinfo['user_id']
                self.user_nickname = personinfo['nickname']
            if not self.user_id:
                return False, "无法获取被戳用户的ID"

        current_time = time.time()
        case = self.action_data.get("case", "joke") 

        if _POKE_STATE['last_poke_back_time'] > 0:
                time_since_last_poke_back = current_time - _POKE_STATE['last_poke_back_time']
                if time_since_last_poke_back < POKE_BACK_COOLDOWN:
                    return True, "戳一戳还在冷却"

        _POKE_STATE['last_poke_time'] = current_time

        if case == "request":
            await asyncio.sleep(3)
            await self.send_command("SEND_POKE",{"qq_id": self.user_id},f"（戳了{self.user_nickname}一下）")
            await self.store_info(case)
            return True, "应对方的要求戳了戳对方"
        
        elif case == "joke":
            if random.random() < 0.4:
                await asyncio.sleep(3)
                await self.send_command("SEND_POKE",{"qq_id": self.user_id},f"（开玩笑地戳了{self.user_nickname}一下）")
                await self.store_info(case)
                return True, "开玩笑地戳了一下"
            else:
                return True, "决定不开玩笑"
        else:
            return False, "参数不对"
        
    async def store_info(self, case):
        # 记录动作信息
        if case == "request":
            suffix = f"已经回应{self.user_nickname}的要求戳了一下TA"
        else:
            suffix = f"跟{self.user_nickname}开了一个玩笑，戳了一下TA"
        await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=suffix,
                action_done=True
                )

class AntiPokeCommand(BaseCommand):
    command_name = "anti_poke"
    command_description = "防戳插件"
    command_pattern = r"^(?P<content>[\s\S]+)（这是QQ的一个功能，用于提及某人，但没那么明显）$"
    command_help = "无使用方法，自动触发"
    command_examples = []
    
    def __init__(self, message, plugin_config: dict = None):
        super().__init__(message,plugin_config)
        self.message = message
        self.log_prefix = f"[Command:{self.command_name}]"

    @property
    def SILENCE_DURATION_MIN(self):
        config = self._load_config()
        return config["poke_value"].get("min_silence_time", 120)

    @property
    def SILENCE_DURATION_MAX(self):
        config = self._load_config()
        return config["poke_value"].get("max_silence_time", 300)

    @property
    def POKE_COUNT_MIN(self):
        config = self._load_config()
        return config["poke_value"].get("min_silence_counts", 5)

    @property
    def POKE_COUNT_MAX(self):
        config = self._load_config()
        return config["poke_value"].get("max_silence_counts", 9)

    @property
    def DECAY_INTERVAL(self):
        config = self._load_config()
        return config["poke_value"].get("counts_decay_interval", 180)
    
    @property
    def REFLECT_POKE_PROBABILITY(self):
        config = self._load_config()
        return config["poke_value"].get("reflect_probability", 0.4)
    
    @property
    def FOLLOW_POKE_PROBABILITY(self):
        config = self._load_config()
        return config["poke_value"].get("follow_probability", 0.3)
    
    @property
    def INSENSITIVITY_DURATION(self):
        config = self._load_config()
        return config["poke_value"].get("insensitivity_duration", 4)

    async def execute(self) -> Tuple[bool, Optional[str]]:
        try:
            current_time = time.time()
            if _POKE_STATE['is_silent']: # 沉默截断机制
                if current_time - _POKE_STATE['silence_start_time'] > _POKE_STATE['current_silence_duration']:
                    _POKE_STATE['is_silent'] = False
                    _POKE_STATE['poke_count'] = 0  # 重置计数器（仅当解除沉默时）
                    logger.info(f"沉默期结束，持续了 {_POKE_STATE['current_silence_duration']} 秒")
                else:
                    # 沉默期直接忽略所有通知
                    logger.info("当前处于沉默期，忽略戳一戳")
                    return True,"处于沉默期，直接拦截所有戳一戳消息", True

            if not self.message.message_info.message_id == "notice":
                return True,"非戳一戳消息，无需使用命令", True

            content = self.matched_groups.get("content") 
            target_id = self.message.message_info.user_info.user_id
            poked_id = str(self.message.message_info.additional_config.get("target_id"))
            self_id = config_api.get_global_config("bot.qq_account")
            target_nickname = self.message.message_info.user_info.user_nickname

            # 检查是否可以反戳（新增逻辑）
            can_poke_back = True
            if _POKE_STATE['last_poke_back_time'] > 0:
                time_since_last_poke_back = current_time - _POKE_STATE['last_poke_back_time']
                if time_since_last_poke_back < POKE_BACK_COOLDOWN:
                    can_poke_back = False
                    logger.info(f"戳一戳冷却中，还需等待 {POKE_BACK_COOLDOWN - time_since_last_poke_back:.1f} 秒")

            if not poked_id == self_id: # 如果戳一戳完全与自己无关
                if random.random() < self.FOLLOW_POKE_PROBABILITY:
                    await asyncio.sleep(3)
                    await self.send_command("SEND_POKE",{"qq_id": poked_id},f"（戳了{target_nickname}一下）")
                    _POKE_STATE['last_poke_back_time'] = current_time  # 更新上次戳一戳时间
                    return True,"忍不住跟着戳了一下", True
                else:
                    return True,"不是找自己的，也不打算跟戳", True
                
            if self._check_insensitivity_period(current_time):
                return True, "钝感中，勿扰", True
            
            _POKE_STATE['last_poke_received_time'] = current_time  # 更新上次接收到戳一戳的时间
            
            self.start_decay_task_if_needed()
            counter_lock = _get_or_create_lock()
            if counter_lock:
                async with counter_lock:
                    _POKE_STATE['last_poke_time'] = current_time
                    _POKE_STATE['poke_count'] += 1
            else:
            # 如果无法获取锁，直接更新（降级处理）
                _POKE_STATE['last_poke_time'] = current_time
                _POKE_STATE['poke_count'] += 1

            # === 动态后缀生成 ===
            if _POKE_STATE['current_poke_threshold'] == 0:
                self.generate_random_silence_params()

            if _POKE_STATE['poke_count'] >= _POKE_STATE['current_poke_threshold']:
                suffix = "（请一定要回答类似于“哼，我不理你了”的话语以表示对过多戳一戳的抗议）"

            # 触发沉默机制
                _POKE_STATE['is_silent'] = True
                _POKE_STATE['silence_start_time'] = time.time()
                logger.info(f"触发沉默机制，戳戳次数: {_POKE_STATE['poke_count']}/{_POKE_STATE['current_poke_threshold']}, 沉默时长: {_POKE_STATE['current_silence_duration']}秒")
            # 为下次沉默生成新的随机参数 
                self.generate_random_silence_params()
            else:
                suffix = "（这是QQ的一个功能，用于提及某人，但没那么明显）"

            if random.random() < self.REFLECT_POKE_PROBABILITY and not _POKE_STATE['is_silent'] and can_poke_back:
                await asyncio.sleep(3)
                await self.send_command("SEND_POKE",{"qq_id": target_id},f"（戳了{target_nickname}一下）")
                _POKE_STATE['last_poke_back_time'] = current_time  # 更新上次戳一戳时间
                return True,"反戳一下", True
            else:
                if not can_poke_back and not _POKE_STATE['is_silent']:
                    if random.random() < 0.33:
                        await self.generate_reply(content, suffix, target_nickname)
                        return True,"选择言语回复", True
                    else:
                        return True,"不想回复", True
                else:
                    await self.generate_reply(content, suffix, target_nickname)
                    return True,"选择言语回复", True
            
        except Exception as e:
            logger.error(f"{self.log_prefix} 执行错误: {e}")
            return False, f"执行失败: {str(e)}", True
        
    def _load_config(self) -> Dict[str, Any]:
        """从同级目录的config.toml文件直接加载配置"""
        try:
            # 获取当前文件所在目录
            script_dir = os.path.dirname(os.path.abspath(__file__))
            config_path = os.path.join(script_dir, "config.toml")
            
            # 读取并解析TOML配置文件
            with open(config_path, 'r', encoding='utf-8') as f:
                config_data = toml.load(f)
            
            # 构建配置字典，使用get方法安全访问嵌套值
            config = {
                "poke_value": {
                    "min_silence_time": config_data.get("poke_value", {}).get("min_silence_time", 120),
                    "max_silence_time": config_data.get("poke_value", {}).get("max_silence_time", 300),
                    "min_silence_counts": config_data.get("poke_value", {}).get("min_silence_counts", 5),
                    "max_silence_counts": config_data.get("poke_value", {}).get("max_silence_counts", 9),
                    "counts_decay_interval": config_data.get("poke_value", {}).get("counts_decay_interval", 180),
                    "reflect_probability": config_data.get("poke_value", {}).get("reflect_probability", 0.4),
                    "follow_probability": config_data.get("poke_value", {}).get("follow_probability", 0.3),
                    "insensitivity_duration": config_data.get("poke_value", {}).get("insensitivity_duration", 4),
                }
            }
            return config
        except Exception as e:
            logger.error(f"{self.log_prefix} 加载配置失败: {e}")
            raise

    def generate_random_silence_params(self):
        """
        生成随机的沉默参数
        """
        
        # 随机生成沉默持续时间
        _POKE_STATE['current_silence_duration'] = random.randint(
            self.SILENCE_DURATION_MIN, 
            self.SILENCE_DURATION_MAX
        )
        
        # 随机生成触发沉默所需的戳戳次数
        _POKE_STATE['current_poke_threshold'] = random.randint(
            self.POKE_COUNT_MIN, 
            self.POKE_COUNT_MAX
        )
        
        logger.info(f"生成新的沉默参数 - 持续时间: {_POKE_STATE['current_silence_duration']}秒, 触发阈值: {_POKE_STATE['current_poke_threshold']}次")

    async def poke_count_decay_task(self):
        """
        戳一戳计数器衰减任务：每3分钟检查一次，如果没有新的戳戳则减1
        """
        try:
            while True:
                await asyncio.sleep(self.DECAY_INTERVAL)  
            
                if not _POKE_STATE['is_silent'] and _POKE_STATE['poke_count'] > 0:
                    current_time = time.time()
                    # 如果距离上次被戳超过3分钟，则计数器减1
                    if current_time - _POKE_STATE['last_poke_time'] >= self.DECAY_INTERVAL:
                        counter_lock = _get_or_create_lock()
                        if counter_lock:
                            async with counter_lock:
                                _POKE_STATE['poke_count'] = max(0, _POKE_STATE['poke_count'] - 1)
                                logger.info(f"戳一戳计数器衰减，当前计数: {_POKE_STATE['poke_count']}")
                        else:
                             # 如果无法获取锁，直接更新（降级处理）
                            _POKE_STATE['poke_count'] = max(0, _POKE_STATE['poke_count'] - 1)
                            logger.info(f"戳一戳计数器衰减（无锁），当前计数: {_POKE_STATE['poke_count']}")
        except asyncio.CancelledError:
            logger.info("戳一戳计数器衰减任务被取消")
            raise
        except Exception as e:
            logger.error(f"戳一戳计数器衰减任务异常: {e}")
            # 任务异常退出，重置任务引用
            _POKE_STATE['decay_task'] = None
            raise

    def start_decay_task_if_needed(self):
        """
        在有事件循环时启动衰减任务（只启动一次）
        """
        if _POKE_STATE['decay_task'] is None or _POKE_STATE['decay_task'].done():
            try:
                _POKE_STATE['decay_task'] = asyncio.create_task(self.poke_count_decay_task())
                logger.debug("戳一戳计数器衰减任务已启动")
            except RuntimeError:
                # 如果还没有事件循环，稍后再试
                logger.debug("事件循环未就绪，稍后启动衰减任务")


    async def generate_reply(self, content: str, suffix: str, target_nickname):
        result_status, result_message = await generator_api.generate_reply(
                reply_reason = f"{target_nickname}：{content}{suffix}(有人戳了戳你，可能是在找你，也可能是在搞怪，你需要对此做出简洁的回应)",
                chat_stream= self.message.chat_stream
            )
        if result_status:
            for reply_seg in result_message.reply_set.reply_data:
                data = reply_seg.content
                await self.send_custom(message_type = "text", content = data, typing = True)
                await asyncio.sleep(1.0)

    def _check_insensitivity_period(self, current_time: float) -> bool:
        """
        检查是否在钝感期内（无敌帧）
        
        Args:
            current_time: 当前时间戳
            
        Returns:
            bool: True表示在钝感期内，应该忽略戳一戳；False表示可以响应
        """
        if _POKE_STATE['last_poke_received_time'] > 0:
            time_since_last_poke = current_time - _POKE_STATE['last_poke_received_time']
            if time_since_last_poke < self.INSENSITIVITY_DURATION:
                return True
        return False