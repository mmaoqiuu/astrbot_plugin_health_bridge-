"""health_bridge —— 健康数据接入插件（AstrBot v4.25.1）。

两件事：
  模块一：开一个独立的 HTTP 端点，接住手机推来的当天健康数据并存成文件。
  模块二：给主 LLM（角色）挂一个函数工具 check_health，让角色能读最近的身体数据。
  模块三：后台 monitor —— 基于历史基线做异常检测，发现异常时主动推送关怀。

为什么自己用 aiohttp 开监听、而不用 AstrBot 的 register_web_api：
  register_web_api 注册的路由挂在「管理面板」那个 web 服务上（默认 6185 端口），
  且会被面板的登录鉴权（JWT）拦截。手机端不可能带着面板登录令牌来上报，
  把 6185 暴露到公网又等于把整个管理后台暴露出去。所以这里按 HANDOFF 第五节的
  备选方案，自开一个独立端口（默认 8787）的轻量监听，自包含、只暴露这一个端点。
  （已对照 v4.25.1 源码 astrbot/dashboard/server.py 的 auth_middleware 确认。）

纯逻辑（解析/存储/读取/格式化）都在 health_logic.py，便于脱离 AstrBot 本地测。
本文件只负责把那些逻辑接到 AstrBot 的网络监听和工具注册上。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import secrets
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

from . import health_logic
from .monitor import HealthMonitor

# 插件目录名，同时用作数据子目录名。数据存在 AstrBot 的 data 目录下，
# 不放插件自身目录，这样更新/重装插件不丢历史数据。
PLUGIN_DIR_NAME = "astrbot_plugin_health_bridge"

# 接收端点路径。固定值；真正的门锁是 auth_token，不是这个路径。
ENDPOINT_PATH = "/health/report"

# 鉴权请求头名。手机上报时带 X-Auth-Token: <auth_token>。
AUTH_HEADER = "X-Auth-Token"

# 请求体上限（健康数据只有几百字节，给到 64KB 足够，挡掉超大请求）。
MAX_BODY_BYTES = 64 * 1024


@register(PLUGIN_DIR_NAME, "pupotato", "接收手机推送的健康数据并存储，提供一个工具供角色读取最近的身体状态。", "1.3.0")
class HealthBridge(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        # data/plugin_data/astrbot_plugin_health_bridge/<date>.json
        self._data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_DIR_NAME
        # aiohttp 监听相关对象，启动后赋值，便于 terminate 时干净关闭。
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._last_event: AstrMessageEvent | None = None

        # 健康异常检测 + 主动关怀
        self.monitor = HealthMonitor(
            data_dir=self._data_dir,
            config=self.config,
            cooldown_path=self._data_dir / "_cooldowns.json",
        )
        self._monitor_task: asyncio.Task | None = None
        # 运行时开关（/health_monitor on|off 会即时改它，不依赖 config 写回）
        self._monitor_runtime_enabled: bool = bool(
            self.config.get("monitor_enabled", False)
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message_record(self, event: AstrMessageEvent):
        # 缓存最近一次的真实交互事件，拿到 umo、cqhttp 实例和 bot 身份
        if not event.get_sender_id() or event.get_sender_id() == event.get_self_id():
            return
        self._last_event = event

    # -- 生命周期 ---------------------------------------------------------

    async def initialize(self) -> None:
        """插件激活时调用：确保密钥、建目录、清过期、起监听、起 monitor。"""
        self._ensure_auth_token()
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.exception("[health_bridge] 创建数据目录失败")
        self._run_cleanup()
        await self._start_server()

        # 启动主动关怀轮询（用运行时开关判断，/health_monitor on 也能补起）
        if self._monitor_runtime_enabled:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info(
                f"[health_bridge] 主动关怀已启动，间隔 "
                f"{int(self.config.get('check_interval', 900))}s"
            )

    async def terminate(self) -> None:
        """插件停用/重载时调用：关掉监听、取消 monitor，释放端口。"""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("[health_bridge] 停止 monitor 时出错")
            self._monitor_task = None
        await self._stop_server()

    def _ensure_auth_token(self) -> None:
        """没配密钥时，自动生成一串本机专属的随机密钥并写回配置。

        这样别人装上「留空也能用」，且每台机器的密钥各不相同 —— 不是写死一个
        人人相同的默认值（那种一旦插件代码公开就等于没锁）。
        生成值写回配置后在 WebUI 可见；正常情况下不把密钥打进日志。
        """
        if self.config.get("auth_token"):
            return
        new_token = secrets.token_urlsafe(24)
        self.config["auth_token"] = new_token
        try:
            self.config.save_config()
            logger.warning(
                "[health_bridge] 未配置 auth_token，已自动生成一串专属密钥并写入配置。"
                "请在 WebUI 插件配置里查看 auth_token，并把同样的值填进手机上报端。"
            )
        except Exception:
            # 写回失败：放弃这串密钥（不保留、不打印——密钥绝不进日志），
            # 保持失败关闭，让用户手动配置后重载。
            self.config["auth_token"] = ""
            logger.warning(
                "[health_bridge] 自动生成密钥后写入配置失败，已放弃以避免密钥落日志。"
                "接收端在配置前会拒收一切；请在 WebUI 手动填写 auth_token 后重载插件。"
            )

    # -- 模块一：接收端 ---------------------------------------------------

    async def _start_server(self) -> None:
        """启动 aiohttp 监听。失败只记日志、不抛异常，避免连累其他插件。"""
        await self._stop_server()  # 防御：若已在跑，先收掉再起

        try:
            port = int(self.config.get("listen_port", 8787))
        except (TypeError, ValueError):
            port = 8787

        runner = None
        try:
            app = web.Application(client_max_size=MAX_BODY_BYTES)
            app.router.add_post(ENDPOINT_PATH, self._handle_report)
            app.router.add_get("/health/dashboard", self._handle_dashboard)
            app.router.add_get("/health/api/data", self._handle_api_data)
            # 静态资源（图标/装饰图），文件放插件目录下的 assets/
            app.router.add_get("/health/assets/{name}", self._handle_asset)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host="0.0.0.0", port=port)
            await site.start()
            self._app, self._runner, self._site = app, runner, site
            logger.info(
                f"[health_bridge] 接收端已启动：监听 0.0.0.0:{port}{ENDPOINT_PATH}（仅 POST）"
            )
            if not self.config.get("auth_token"):
                logger.warning(
                    "[health_bridge] auth_token 尚未配置，配置前所有上报都会被拒绝（401）。"
                    "请在 WebUI 插件配置里填写。"
                )
        except Exception:
            # 端口被占用/被禁等。不抛出：避免触发 AstrBot「全部插件重载」恢复机制。
            logger.exception(
                f"[health_bridge] 接收端启动失败（端口 {port} 可能被占用或被防火墙拦）。"
                "读取工具仍可用，但暂时收不到新数据。"
            )
            # setup() 成功但 start() 失败时，回收本地 runner，避免资源泄漏。
            if runner is not None:
                try:
                    await runner.cleanup()
                except Exception:
                    logger.exception("[health_bridge] 回收启动失败的监听器时出错")

    async def _stop_server(self) -> None:
        site, runner = self._site, self._runner
        self._site = self._runner = self._app = None
        try:
            if site is not None:
                await site.stop()
        except Exception:
            logger.exception("[health_bridge] 停止监听站点时出错")
        try:
            if runner is not None:
                await runner.cleanup()
        except Exception:
            logger.exception("[health_bridge] 清理监听器时出错")

    async def _handle_report(self, request: web.Request) -> web.Response:
        """处理手机的一次上报。任何异常都兜住，不让插件崩。"""
        try:
            token = self.config.get("auth_token", "") or ""
            provided = request.headers.get(AUTH_HEADER, "")

            # 未配置密钥 = 失败关闭，拒绝一切（不开放空口）。
            if not token:
                logger.warning("[health_bridge] 收到上报但 auth_token 未配置，已拒绝。")
                return web.json_response(
                    {"ok": False, "error": "server not configured"}, status=401
                )
            # 缺失或不匹配 → 401。用 compare_digest 做定长比较，避免时序泄露。
            if not provided or not hmac.compare_digest(provided, token):
                return web.json_response({"ok": False, "error": "unauthorized"}, status=401)

            raw = await request.read()  # 超过 MAX_BODY_BYTES 时 aiohttp 自动抛 413
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return web.json_response({"ok": False, "error": "bad json"}, status=400)

            try:
                # 认两种格式：原生契约 + Health Auto Export，统一成原生记录列表。
                records = health_logic.normalize_payload(data)
            except health_logic.InvalidPayloadError as exc:
                return web.json_response({"ok": False, "error": str(exc)}, status=400)

            saved_days: list[str] = []
            for rec in records:
                try:
                    saved_days.append(health_logic.store_report(self._data_dir, rec).stem)
                except health_logic.InvalidPayloadError as e:
                    logger.warning(f"[health_bridge] 丢弃一条非法记录: {e}")
                    continue
            if not saved_days:
                return web.json_response({"ok": False, "error": "no usable data"}, status=400)

            # 存成功后顺手清理过期数据（文件很少，开销可忽略）。
            self._run_cleanup()
            # 只记日期，绝不记密钥或原始数据内容。
            logger.info(
                f"[health_bridge] 已接收并存储 {len(saved_days)} 天数据：{', '.join(saved_days)}"
            )
            return web.json_response({"ok": True, "saved": saved_days})

        except web.HTTPException:
            raise  # 让 aiohttp 的 413（请求体过大）等正常返回
        except Exception:
            logger.exception("[health_bridge] 处理上报时发生未预期错误")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _trigger_event_wakeup(self, prompt: str) -> None:
        """monitor 主动关怀用：构造一条伪造消息注入 AstrBot 处理流程，
        让角色带着当前会话上下文和人格，自然地说一句关心的话。
        """
        # 优先用配置里显式指定的推送目标（主动关怀即使没人说过话也能送达）
        umo = self.config.get("push_target_umo") or None
        bot_self_id = None
        cq_bot = None

        # 1. 尝试从 self._last_event 获取
        if self._last_event:
            if not umo:
                umo = self._last_event.unified_msg_origin
            bot_self_id = self._last_event.get_self_id()
            if hasattr(self._last_event, "bot"):
                cq_bot = self._last_event.bot

        # 2. 如果没有，从 wakeup 插件获取
        if not umo or not cq_bot or not bot_self_id:
            for star_wrapper in getattr(self.context, "_stars", []) or getattr(self.context, "stars", []) or []:
                s = getattr(star_wrapper, "star_instance", star_wrapper)
                p_name = getattr(s, "plugin_name", "") or getattr(star_wrapper, "name", "")
                if "wakeup" in str(p_name):
                    if not umo:
                        umo = getattr(s, "target_umo", None)
                    if not bot_self_id:
                        bot_self_id = getattr(s, "_bot_qq_id", None)
                    if not cq_bot:
                        cq_bot = getattr(s, "_cqhttp_bot", None)
                    break

        # 3. 寻找 cq_bot
        if cq_bot is None:
            cq_bot = getattr(self.context, "_cqhttp_bot", None)
        if cq_bot is None:
            try:
                for mgr_name in ("platform_manager", "platform_mgr", "_platform_manager"):
                    mgr = getattr(self.context, mgr_name, None)
                    if not mgr:
                        continue
                    for list_name in ("platforms", "_platforms", "adapters", "_adapters"):
                        plist = getattr(mgr, list_name, None)
                        if not plist or not hasattr(plist, "__iter__"):
                            continue
                        for p in plist:
                            bot = getattr(p, "bot", None)
                            if bot and hasattr(bot, "send_private_msg"):
                                cq_bot = bot
                                break
                        if cq_bot:
                            break
                    if cq_bot:
                        break
            except Exception as e:
                logger.debug(f"[health_bridge] 搜索 bot 实例失败: {e}")

        if not bot_self_id and cq_bot and hasattr(cq_bot, "get_login_info"):
            try:
                info = await cq_bot.get_login_info()
                bot_self_id = str(info.get("user_id", ""))
            except Exception:
                pass

        if not umo:
            logger.info("[health_bridge] 尚未记录到最近会话交互，跳过即时唤醒")
            return

        try:
            from aiocqhttp import Event as CQEvent
        except ImportError:
            logger.warning("[health_bridge] 未找到 aiocqhttp，跳过伪造注入")
            return

        parts = umo.rsplit(":", 2)
        if len(parts) < 3:
            return
        session_id = parts[2]
        msg_type_str = parts[1]
        is_group = "Group" in msg_type_str

        if is_group:
            if "_" in session_id:
                uid, gid = session_id.rsplit("_", 1)
            else:
                return
            payload = {
                "post_type": "message",
                "message_type": "group",
                "sub_type": "normal",
                "message_id": int(time.time()) % 2147483647,
                "group_id": int(gid),
                "user_id": int(uid),
                "message": [{"type": "text", "data": {"text": prompt}}],
                "raw_message": prompt,
                "font": 0,
                "sender": {"user_id": int(uid), "nickname": "event_bridge", "card": ""},
                "time": int(time.time()),
                "self_id": int(bot_self_id) if bot_self_id else 0,
            }
        else:
            payload = {
                "post_type": "message",
                "message_type": "private",
                "sub_type": "friend",
                "message_id": int(time.time()) % 2147483647,
                "user_id": int(session_id),
                "message": [{"type": "text", "data": {"text": prompt}}],
                "raw_message": prompt,
                "font": 0,
                "sender": {"user_id": int(session_id), "nickname": "event_bridge", "sex": "unknown", "age": 0},
                "time": int(time.time()),
                "self_id": int(bot_self_id) if bot_self_id else 0,
            }

        fake_event = CQEvent.from_payload(payload)
        if not fake_event:
            return

        if cq_bot:
            handler = getattr(cq_bot, "_handle_event", None) or getattr(cq_bot, "handle_event", None)
            if handler:
                await handler(fake_event)
                logger.info(f"[health_bridge] 🎯 已成功注入消息 | umo={umo}")
            else:
                logger.warning("[health_bridge] cq_bot 没有可用 handle_event 方法")
        else:
            logger.warning("[health_bridge] 未获取到 cq_bot 实例，无法注入")

    # -- 模块三：主动关怀 monitor ------------------------------------------

    async def _monitor_loop(self) -> None:
        """后台轮询：每隔 check_interval 秒扫一次健康数据。"""
        interval = int(self.config.get("check_interval", 900))
        while True:
            try:
                await asyncio.sleep(interval)
                await self._monitor_tick()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("[health_bridge] monitor tick 出错")

    def _in_quiet_hours(self) -> bool:
        """静默时段判断。支持跨天（如 23 → 7）。"""
        now_h = datetime.now().hour
        try:
            start = int(self.config.get("quiet_hours_start", 23))
            end = int(self.config.get("quiet_hours_end", 7))
        except (TypeError, ValueError):
            return False
        if start == end:
            return False
        if start < end:
            return start <= now_h < end
        return now_h >= start or now_h < end

    async def _monitor_tick(self) -> None:
        """一次扫描：检测异常 → 过滤冷却 → 推送。一次 tick 最多推一条。"""
        if not self._monitor_runtime_enabled:
            return
        if self._in_quiet_hours():
            logger.info("[health_bridge] 处于静默时段，跳过本次检测")
            return

        alerts = self.monitor.scan()
        if not alerts:
            return

        for a in alerts:
            key = a.get("cooldown_key") or a.get("type")
            if self.monitor._in_cooldown(key):
                continue
            await self._trigger_event_wakeup(
                f"【系统健康感知】{a['hint']}"
                f"请结合当前对话上下文和你们的关系，用你的人格自然地说一句关心的话，"
                f"不要罗列数据、不要像健康报告。"
            )
            self.monitor._mark(key)
            logger.info(f"[health_bridge] 已推送健康关怀: {a['type']}")
            break  # 一次 tick 只推一条，避免轰炸

    def _run_cleanup(self) -> None:
        try:
            retention = int(self.config.get("retention_days", 30))
        except (TypeError, ValueError):
            retention = 30
        try:
            removed = health_logic.cleanup_old(self._data_dir, retention)
            if removed:
                logger.info(f"[health_bridge] 已清理 {len(removed)} 天过期数据")
        except Exception:
            logger.exception("[health_bridge] 清理过期数据时出错（不影响其他功能）")

    async def _handle_dashboard(self, request: web.Request) -> web.Response:
        """返回 HTML 实时健康看板页面。"""
        try:
            html_path = Path(__file__).parent / "index.html"
            if not html_path.exists():
                return web.Response(text="Dashboard template not found", status=404)
            content = html_path.read_text(encoding="utf-8")
            return web.Response(text=content, content_type="text/html", charset="utf-8")
        except Exception:
            logger.exception("[health_bridge] 加载 dashboard 失败")
            return web.Response(text="Internal Error", status=500)

    async def _handle_api_data(self, request: web.Request) -> web.Response:
        """返回最新的健康数据 JSON，给前端页面实时渲染。"""
        try:
            data = health_logic.load_latest(self._data_dir) or {}
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return web.json_response({"ok": True, "data": data, "server_time": now_str})
        except Exception:
            logger.exception("[health_bridge] API 获取数据失败")
            return web.json_response({"ok": False, "error": "internal error"}, status=500)

    async def _handle_asset(self, request: web.Request) -> web.Response:
        """返回插件 assets/ 目录下的静态图片。防路径穿越。"""
        from urllib.parse import unquote
        name = unquote(request.match_info.get("name", ""))
        # 简单防路径穿越：不包含斜杠、反斜杠和连续两点
        if not name or "/" in name or "\\" in name or ".." in name:
            return web.Response(status=404)
        path = Path(__file__).parent / "assets" / name
        if not path.is_file():
            return web.Response(status=404)
        return web.FileResponse(path)

    # -- 模块二：读取工具（给角色的 LLM 工具）-----------------------------

    # 普通 LLM 工具：角色需要时自己调用。「何时调用」由角色侧的人设决定，不写进这里。
    # 说明只放客观事实；细节（参数）放在工具里。若用户在 AstrBot 开了
    # provider_settings.tool_schema_mode=skills_like，参数会推迟到真正调用时才发，
    # 平时每回合只带「名字 + 这句说明」。本工具很小，不开也只占几十 token。
    @filter.llm_tool(name="check_health")
    async def check_health(self, event: AstrMessageEvent, date: str = "") -> str:
        """读取用户最近的身体数据：睡眠、心率、活动、经期、症状等。
        返回内容里会标明数据是什么时候收到的，请据此判断数据新鲜度，
        不要把过期数据当成当前状态来讲。

        Args:
            date(string): 可选，指定日期 YYYY-MM-DD；留空返回最近一天。
        """
        try:
            if isinstance(date, str) and date.strip():
                return health_logic.format_for_date(self._data_dir, date.strip())
            return health_logic.format_latest(self._data_dir)
        except Exception:
            logger.exception("[health_bridge] 读取身体数据时出错")
            return "读取身体数据时出错了。"

    # -- 调试指令（可选）-------------------------------------------------

    @filter.command("health_monitor")
    async def cmd_health_monitor(self, event: AstrMessageEvent, action: str = ""):
        """/health_monitor [on|off|status|test] 主动关怀开关与调试。"""
        # 隐私隔离：只在私聊响应
        try:
            if not event.is_private_chat():
                return
        except Exception:
            pass

        act = (action or "").lower().strip()

        if act == "on":
            self._monitor_runtime_enabled = True
            if not self._monitor_task or self._monitor_task.done():
                self._monitor_task = asyncio.create_task(self._monitor_loop())
            yield event.plain_result("主动关怀已开启（本次运行有效，重启后按配置为准）")
            return

        if act == "off":
            self._monitor_runtime_enabled = False
            yield event.plain_result("主动关怀已关闭")
            return

        if act == "test":
            alerts = self.monitor.scan()
            baseline = int(self.config.get("baseline_days", 7))
            have = len(self.monitor._load_recent(baseline))
            if not alerts:
                yield event.plain_result(
                    f"本次无异常。\n基线天数: {have}/{baseline}"
                )
                return
            lines = [f"检测到 {len(alerts)} 条候选关怀："]
            for a in alerts:
                key = a.get("cooldown_key") or a.get("type")
                cd = "（冷却中）" if self.monitor._in_cooldown(key) else ""
                lines.append(f"  - [{a['type']}]{cd} {a['hint']}")
            yield event.plain_result("\n".join(lines))
            return

        # status
        baseline = int(self.config.get("baseline_days", 7))
        have = len(self.monitor._load_recent(baseline))
        lines = [
            "[健康主动关怀 状态]",
            f"启用     : {'✅' if self._monitor_runtime_enabled else '❌'}",
            f"轮询间隔 : {int(self.config.get('check_interval', 900))}s",
            f"基线天数 : {have}/{baseline}",
            f"静默时段 : {int(self.config.get('quiet_hours_start', 23))} - "
            f"{int(self.config.get('quiet_hours_end', 7))} 点",
        ]
        yield event.plain_result("\n".join(lines))