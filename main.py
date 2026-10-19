# -*- coding: utf-8 -*-
"""astrbot_plugin_spending_rank —— API 中转站「全站每日排行榜」播报插件。

功能概览
--------
1. 被动触发：群内发送 /排行榜（别名：消费榜 / 消费排行榜 / 消费排行），
   立即拉取接口数据、渲染当日榜单图并发送到当前会话。
2. 主动定时：每天 cron_time（默认 23:00）自动把当天榜单图推送到 target_group。
3. 榜单口径：接口同时给出 data.ranking（Token 用量榜）与 data.spending_ranking
   （消耗额度榜）时，两个榜并排画进同一张图；只有一套时退化为单榜。
   排名一律按指标自己重排（接口自带的 rank 与该榜的排序字段并不一致，插件不信它）。
4. 数据缓存：cache_ttl 秒内复用同一份数据，既防群友狂点，也避免定时任务与
   手动指令并发把接口刷爆。
5. 图片渲染：默认用内置的本地渲染器直接画图（纯 Pillow，不联网、不依赖
   浏览器）；远端 t2i 服务只作为可选通道，详见下方「渲染说明」。

依赖安装
--------
    pip install -r requirements.txt      # 只有 httpx 与 jinja2

渲染说明
--------
出图有三条通道，由配置项 render_mode 决定：

  local   只用本地渲染器（local_render.py，纯 Pillow 绘制）。不联网、不装
          浏览器，只要系统里有中文字体就能出图。
  remote  只用远端 t2i 服务：把模板原文与数据 POST 过去（User-Agent 必须是
          AstrBot/t2i，否则 403），服务端用无头浏览器 full_page 截图后返回
          图片 id，插件再把它下载回来转 Base64 发进群。
  auto    （默认）先本地，本地不可用再退回远端。

为什么把本地渲染放在第一位：远端 t2i 是公共服务，限流了就是 HTTP 429、
没有可用节点就是 HTTP 503，插件当场发不出图。本地渲染器只依赖 Pillow
（AstrBot 自带）和系统里的中文字体，不受任何外部服务影响。

三条通道全挂时，插件不再只回一句报错，而是把榜单降级成纯文字发出去，
保证群里至少能看到数据。

作者：zjh19    版本：v1.1.0
"""

from __future__ import annotations

import asyncio
import base64
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import httpx
from jinja2 import Environment, select_autoescape

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

PLUGIN_DIR = Path(__file__).resolve().parent
TEMPLATE_FILE = PLUGIN_DIR / "template.html"

# t2i 渲染服务要求的 User-Agent，UA 不对会直接被 403 挡掉
T2I_USER_AGENT = "AstrBot/t2i"

RENDER_HINT = (
    "榜单图片渲染失败。本插件不依赖本机 Chromium，而是调用 t2i 渲染服务出图：\n"
    "  1) 确认 AstrBot 所在机器能访问 t2i_endpoint（默认 https://t2i.soulter.top/text2img）\n"
    "  2) 用的是自建渲染服务时，请在插件配置里改 t2i_endpoint\n"
    "  3) 详细报错见 AstrBot 日志里以 [SpendingRank] 开头的记录"
)

# 配置默认值。真实取值来自 WebUI 写入的插件配置（由 _conf_schema.json 描述），
# 这里只作为兜底，保证配置项缺失时插件依然可用。
DEFAULT_CONFIG: dict[str, Any] = {
    "api_url": "https://worldcodes.online/api/v1/leaderboard/daily",
    "api_token": "",
    "cache_ttl": 300,
    "request_timeout": 10,
    "top_n": 10,
    "cron_enabled": True,
    "cron_time": "23:00",
    "target_group": "",
    "platform_id": "aiocqhttp",
    "show_cost": True,
    "panels": "dual",
    "brand": "WorldCodes",
    "image_width": 880,
    "image_format": "png",
    "image_quality": 92,
    "render_mode": "auto",
    "t2i_endpoint": "https://t2i.soulter.top/text2img",
    "render_timeout": 60,
}


class RenderError(RuntimeError):
    """图片渲染失败（依赖缺失 / 浏览器启动失败 / 模板不可用）。"""


# 本地渲染器按文件路径加载，而不是靠相对导入 —— AstrBot 加载插件的方式不保证
# 插件目录是个 Python 包，用 importlib 直接执行文件最稳。
_LOCAL_RENDERER: Any = None
_LOCAL_RENDERER_ERROR = ""


def _get_local_renderer():
    """取本地渲染函数；拿不到返回 None（并记下原因，只警告一次）。"""
    global _LOCAL_RENDERER, _LOCAL_RENDERER_ERROR

    if _LOCAL_RENDERER is not None or _LOCAL_RENDERER_ERROR:
        return _LOCAL_RENDERER

    path = PLUGIN_DIR / "local_render.py"
    if not path.exists():
        _LOCAL_RENDERER_ERROR = f"找不到 {path.name}"
        logger.warning(f"[SpendingRank] 本地渲染器缺失，只能用远端渲染：{_LOCAL_RENDERER_ERROR}")
        return None

    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location("spending_rank_local_render", path)
        if spec is None or spec.loader is None:
            raise RuntimeError("无法为 local_render.py 创建模块规格")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not hasattr(module, "render_panels"):
            raise RuntimeError("local_render.py 缺少 render_panels（版本过旧）")
        _LOCAL_RENDERER = module
        logger.info("[SpendingRank] 本地渲染器已就绪（纯 Pillow，不依赖网络，支持双榜）")
    except Exception as exc:
        _LOCAL_RENDERER_ERROR = str(exc)
        logger.warning(f"[SpendingRank] 本地渲染器加载失败，只能用远端渲染：{exc}")

    return _LOCAL_RENDERER


# ---------------------------------------------------------------------------
# 纯函数工具：字段兼容、数值格式化
# ---------------------------------------------------------------------------
def _pick(item: dict, keys: tuple, default: Any = None) -> Any:
    """按候选字段名依次取值，兼容接口字段改名的各种情况。"""
    for key in keys:
        if key in item:
            value = item.get(key)
            if value is not None and value != "":
                return value
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return default


def _fmt_int(value: int) -> str:
    return "{:,}".format(value)


def _fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000_000:
        return "{:.2f}B".format(tokens / 1_000_000_000)
    if tokens >= 1_000_000:
        return "{:.2f}M".format(tokens / 1_000_000)
    if tokens >= 1_000:
        return "{:.1f}K".format(tokens / 1_000)
    return str(tokens)


def _fmt_cost(cost: float) -> str:
    """金额显示：1000 以上带千分位保留 2 位，其余保留 4 位小数（与中转站后台计费口径一致）。"""
    if cost >= 1000:
        return "{:,.2f}".format(cost)
    return "{:.4f}".format(cost)


# 榜单列表可能藏在这些键里（不同中转站接口命名习惯不同）
_LIST_KEYS = (
    "data",
    "ranking",
    "list",
    "items",
    "rows",
    "result",
    "records",
    "leaderboard",
    "entries",
    "rank_list",
    "rankList",
    "users",
)


def _extract_list(payload: Any) -> list:
    """从各种可能的接口返回结构里挖出榜单列表。

    真实接口形如 {"code": 0, "data": {"date": "...", "ranking": [...]}}，
    即列表嵌套在 data.ranking 里，所以这里做递归下钻。
    """

    def _dig(node: Any, depth: int = 0) -> list:
        if isinstance(node, list):
            return node
        if isinstance(node, dict) and depth < 3:
            for key in _LIST_KEYS:
                found = _dig(node.get(key), depth + 1)
                if found:
                    return found
        return []

    return _dig(payload)


class SpendingRankPlugin(Star):
    """全站每日消费排行榜播报插件。"""

    def __init__(self, context: Context, config: Any = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # ---- 数据缓存（cache_ttl 秒内复用）----
        self._cache_panels: list[dict] = []
        self._cache_meta: dict = {}
        self._cache_ts: float = 0.0
        self._cache_lock = asyncio.Lock()

        # ---- 渲染串行锁：避免定时任务与手动指令同时打渲染服务 ----
        self._render_lock = asyncio.Lock()

        # ---- 定时任务 ----
        self._scheduler_task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()

        # ---- Jinja2 环境 ----
        self._env = Environment(
            autoescape=select_autoescape(["html", "htm", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
        )
        self._template: Any = None
        self._load_template()

        self._start_scheduler()

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            if hasattr(self.config, "get"):
                value = self.config.get(key, None)
                if value is not None and value != "":
                    return value
        except Exception as exc:  # pragma: no cover
            logger.debug(f"[SpendingRank] 读取配置 {key} 失败：{exc}")
        if default is not None:
            return default
        return DEFAULT_CONFIG.get(key)

    def _cfg_bool(self, key: str, default: bool = True) -> bool:
        value = self._cfg(key, default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

    def _cfg_int(self, key: str, default: int = 0) -> int:
        return _to_int(self._cfg(key, default), default)

    def _cfg_float(self, key: str, default: float = 0.0) -> float:
        return _to_float(self._cfg(key, default), default)

    # ------------------------------------------------------------------
    # 模板
    # ------------------------------------------------------------------
    # 模板语法自检用的占位数据：模板写坏时能在日志里立刻看到，而不是等群里报错
    _TEMPLATE_SAMPLE = {
        "brand": "WorldCodes",
        "date": "1970-01-01",
        "rows": [],
        "updated_at": "1970-01-01 00:00:00",
        "total_users": 0,
        "total_tokens_display": "0",
        "total_cost_display": "0.0000",
        "show_cost": True,
        "image_width": 880,
    }

    def _load_template(self) -> None:
        """读取模板原文（Jinja2 占位符交由 t2i 渲染服务解析）。"""
        try:
            self._template = TEMPLATE_FILE.read_text(encoding="utf-8")
        except Exception as exc:
            self._template = None
            logger.error(f"[SpendingRank] 读取模板失败 {TEMPLATE_FILE}：{exc}")
            return

        try:
            self._env.from_string(self._template).render(**self._TEMPLATE_SAMPLE)
        except Exception as exc:
            logger.warning(
                f"[SpendingRank] 模板 {TEMPLATE_FILE.name} 语法可能有误，渲染可能失败：{exc}"
            )

    # ------------------------------------------------------------------
    # 数据拉取 + 缓存
    # ------------------------------------------------------------------
    @staticmethod
    def _build_client(
        timeout: float, trust_env: Optional[bool] = None
    ) -> httpx.AsyncClient:
        """构造 httpx 客户端。

        某些环境会把 NO_PROXY 写成带方括号的 IPv6（例如 [::1]），httpx 读取代理
        环境变量时会直接在构造函数里抛 Invalid port，导致根本发不出请求。
        这里做一次优雅降级：忽略代理环境变量，改为直连。

        trust_env 显式传入时按传入值构造（渲染服务会先直连、失败再走代理重试）。
        """
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(timeout, connect=min(timeout, 5.0)),
            "follow_redirects": True,
        }
        if trust_env is not None:
            return httpx.AsyncClient(trust_env=trust_env, **kwargs)
        try:
            return httpx.AsyncClient(**kwargs)
        except Exception as exc:
            logger.warning(
                f"[SpendingRank] 读取系统代理设置失败（{exc}），已忽略代理环境变量改为直连"
            )
            return httpx.AsyncClient(trust_env=False, **kwargs)

    async def _request_api(self) -> tuple[Any, Optional[str]]:
        """请求排行榜接口，返回 (payload, error)。"""
        url = str(self._cfg("api_url") or "").strip()
        if not url:
            return None, "插件未配置 api_url，请到 AstrBot 插件配置里填写接口地址"

        token = str(self._cfg("api_token") or "").strip()
        timeout = self._cfg_float("request_timeout", 10)
        if timeout <= 0:
            timeout = 10.0

        headers = {
            "Accept": "application/json",
            "User-Agent": "AstrBot-SpendingRank/1.0",
        }
        if token:
            headers["Authorization"] = "Bearer " + token

        try:
            async with self._build_client(timeout) as client:
                response = await client.get(url, headers=headers)
        except httpx.TimeoutException:
            return None, "请求排行榜接口超时（超过 {:.0f} 秒），请稍后再试".format(timeout)
        except httpx.HTTPError as exc:
            return None, f"网络请求失败：{exc}"
        except Exception as exc:
            return None, f"请求接口时出现异常：{exc}"

        if response.status_code != 200:
            hint = ""
            if response.status_code in (401, 403):
                hint = "（api_token 可能无效或已过期）"
            elif response.status_code == 404:
                hint = "（api_url 可能不正确）"
            return None, "接口返回 HTTP {}{}".format(response.status_code, hint)

        try:
            payload = response.json()
        except Exception:
            text = (response.text or "")[:120].replace("\n", " ")
            return None, f"接口返回内容不是合法 JSON，无法解析。原始响应片段：{text}"

        if isinstance(payload, dict):
            code = payload.get("code")
            if code not in (None, 0, 200, "200", "0", "success", "ok"):
                message = (
                    payload.get("message")
                    or payload.get("msg")
                    or payload.get("error")
                    or "未知错误"
                )
                return None, "接口业务返回异常：code={} message={}".format(code, message)

        return payload, None

    @staticmethod
    def _rows_from_list(raw_list: list, sort_key: str = "tokens") -> tuple[list[dict], dict]:
        """把单条榜单数组归一化成行数据，并算出合计。"""
        rows: list[dict] = []
        total_cost = 0.0
        total_tokens = 0
        total_requests = 0

        for index, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue

            user_id = str(
                _pick(item, ("user_id", "userId", "uid", "id", "user"), "") or ""
            ).strip()

            name = _pick(
                item,
                (
                    "display_name",
                    "displayName",
                    "nickname",
                    "nick_name",
                    "username",
                    "user_name",
                    "name",
                    "email",
                ),
                None,
            )
            name = str(name).strip() if name else ""
            if not name:
                name = f"用户ID {user_id}" if user_id else f"用户 #{index + 1}"

            cost = _to_float(
                _pick(
                    item,
                    ("cost", "actual_cost", "actualCost", "real_cost", "realCost",
                     "total_cost", "totalCost", "amount", "spend", "money", "consumption",
                     "price", "quota", "used_quota"),
                    0.0,
                )
            )
            tokens = _to_int(
                _pick(
                    item,
                    ("total_tokens", "totalTokens", "tokens", "token_count",
                     "tokenCount", "total_token"),
                    0,
                )
            )
            requests = _to_int(
                _pick(
                    item,
                    ("request_count", "requestCount", "requests", "req_count",
                     "reqCount", "request_num", "count", "calls"),
                    0,
                )
            )

            total_cost += cost
            total_tokens += tokens
            total_requests += requests

            rows.append(
                {
                    "user_id": user_id,
                    "name": name,
                    "cost": cost,
                    "cost_display": _fmt_cost(cost),
                    "tokens": tokens,
                    "tokens_display": _fmt_tokens(tokens),
                    "requests": requests,
                    "requests_display": _fmt_int(requests),
                }
            )

        # 两个榜的排序口径不同：Token 榜看 Token 量，消耗榜看实际扣费。
        # 接口给的 rank 字段和这两者都不一定一致，所以一律自己重排。
        if sort_key == "cost":
            rows.sort(key=lambda row: (row["cost"], row["tokens"], row["requests"]), reverse=True)
        else:
            rows.sort(key=lambda row: (row["tokens"], row["cost"], row["requests"]), reverse=True)

        sums = {
            "total_cost": total_cost,
            "total_cost_display": _fmt_cost(total_cost),
            "total_tokens": total_tokens,
            "total_tokens_display": _fmt_tokens(total_tokens),
            "total_requests": total_requests,
        }
        return rows, sums

    @staticmethod
    def _date_text(payload: Any) -> str:
        """从 payload 里取统计日期，取不到就用今天。"""
        if isinstance(payload, dict):
            # 日期可能在顶层，也可能在 data 里（真实接口是 data.date）
            nodes = [payload]
            inner = payload.get("data")
            if isinstance(inner, dict):
                nodes.append(inner)
            for node in nodes:
                value = _pick(
                    node, ("date", "day", "stat_date", "statDate", "today", "biz_date"), ""
                )
                text = str(value).strip() if value else ""
                if text:
                    return text
        return datetime.now().strftime("%Y-%m-%d")

    def _normalize(self, payload: Any) -> tuple[list[dict], dict]:
        """兼容旧口径：只归一化单个榜（优先 data.ranking）。"""
        inner = payload.get("data") if isinstance(payload, dict) else None
        raw_list = inner.get("ranking") if isinstance(inner, dict) else None
        if not isinstance(raw_list, list):
            raw_list = _extract_list(payload)
        rows, sums = self._rows_from_list(raw_list, "tokens")
        meta = {
            "date": self._date_text(payload),
            **sums,
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return rows, meta

    def _panel_mode(self) -> str:
        """panels 配置：dual（两个榜并排）/ tokens（只看 Token 榜）/ cost（只看消耗榜）。"""
        mode = str(self._cfg("panels", DEFAULT_CONFIG["panels"]) or "").strip().lower()
        return mode if mode in ("dual", "tokens", "cost") else "dual"

    def _panels_from_payload(self, payload: Any) -> tuple[list[dict], dict]:
        """把接口 payload 拆成渲染器要的榜单面板。

        真实接口一次返回两个榜：data.ranking（按 Token 排）与
        data.spending_ranking（按扣费排）。只有一个榜时自动退化成单栏。
        """
        inner = payload.get("data") if isinstance(payload, dict) else None
        token_list = inner.get("ranking") if isinstance(inner, dict) else None
        cost_list = inner.get("spending_ranking") if isinstance(inner, dict) else None

        panels: list[dict] = []

        def _add(raw: list, metric: str, title: str, subtitle: str) -> None:
            rows, sums = self._rows_from_list(raw, metric)
            panels.append(
                {
                    "title": title,
                    "metric": metric,
                    "subtitle": subtitle,
                    "rows": rows,
                    "total_rows": len(rows),
                    "_sums": sums,
                }
            )

        if isinstance(token_list, list):
            _add(token_list, "tokens", "Token 用量榜", "按总 Token 排序")
        if isinstance(cost_list, list):
            _add(cost_list, "cost", "消耗额度榜", "按实际扣费排序")

        if not panels:
            _add(_extract_list(payload), "tokens", "Token 用量榜", "按总 Token 排序")

        mode = self._panel_mode()
        if mode != "dual":
            picked = [p for p in panels if p["metric"] == mode]
            panels = picked or panels[:1]

        first = panels[0]
        meta = {
            "date": self._date_text(payload),
            **first["_sums"],
            "total_users": max(panel.get("total_rows", 0) for panel in panels),
            "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        return panels, meta

    async def _fetch_leaderboard(self, force: bool = False) -> tuple[list[dict], dict, Optional[str]]:
        """返回 (榜单面板, 汇总信息, 错误信息)。"""
        ttl = self._cfg_int("cache_ttl", 300)
        if ttl < 0:
            ttl = 0

        def _fresh() -> bool:
            return bool(self._cache_panels) and (time.time() - self._cache_ts) < ttl

        if not force and _fresh():
            return self._cache_panels, self._cache_meta, None

        async with self._cache_lock:
            # 双重检查：等高并发时只有一个协程真正去打接口
            if not force and _fresh():
                return self._cache_panels, self._cache_meta, None

            payload, error = await self._request_api()
            if error:
                return [], {}, error

            panels, meta = self._panels_from_payload(payload)
            for panel in panels:
                panel.pop("_sums", None)

            if not any(panel.get("rows") for panel in panels):
                # 空榜单同样写缓存，避免群里反复点到空数据时把接口刷爆
                self._cache_panels, self._cache_meta = [], meta
                self._cache_ts = time.time()
                return [], meta, None

            top_n = max(1, self._cfg_int("top_n", 10))
            for panel in panels:
                top_rows = panel["rows"][:top_n]
                for rank, row in enumerate(top_rows, start=1):
                    row["rank"] = rank
                    row["rank_class"] = "top%d" % rank if rank <= 3 else ""
                panel["rows"] = top_rows

            self._cache_panels, self._cache_meta = panels, meta
            self._cache_ts = time.time()
            detail = "，".join("%s %d 人" % (p["title"], p.get("total_rows", 0)) for p in panels)
            logger.info(
                f"[SpendingRank] 榜单数据已更新：{meta.get('date')}，{detail}，"
                f"本次每榜展示前 {top_n} 名"
            )
            return panels, meta, None

    # ------------------------------------------------------------------
    # 渲染
    # ------------------------------------------------------------------
    def _build_tmpl_data(self, panels: list[dict], meta: dict) -> dict:
        """构造交给 t2i 模板的数据。

        远端模板只画第一个榜（rows 为兼容旧模板保留），完整双榜数据放在 panels，
        自建模板可以按需渲染。
        """
        first = panels[0] if panels else {"rows": [], "title": "", "metric": "tokens"}
        rows = first.get("rows") or []
        return {
            "brand": str(self._cfg("brand") or DEFAULT_CONFIG["brand"]),
            "date": meta.get("date", ""),
            "rows": rows,
            "panels": panels,
            "updated_at": meta.get("updated_at", ""),
            "total_users": meta.get("total_users", len(rows)),
            "total_tokens_display": meta.get("total_tokens_display", "0"),
            "total_cost_display": meta.get("total_cost_display", "0.0000"),
            "show_cost": self._cfg_bool("show_cost", True),
            "image_width": max(320, self._cfg_int("image_width", 880)),
        }

    def _render_options(self) -> dict:
        """t2i 渲染选项：整页截图，高度随内容自适应。"""
        image_format = str(self._cfg("image_format", "png")).strip().lower()
        if image_format == "jpg":
            image_format = "jpeg"
        if image_format not in ("png", "jpeg"):
            image_format = "png"

        options: dict[str, Any] = {"full_page": True, "type": image_format}
        if image_format == "jpeg":
            quality = self._cfg_int("image_quality", 92)
            options["quality"] = min(max(quality, 30), 100)
        return options

    def _template_text(self) -> str:
        """取模板原文，Jinja2 占位符原样交给渲染服务解析。"""
        if self._template is None:
            self._load_template()
        if not self._template:
            raise RenderError(f"模板文件不可用：{TEMPLATE_FILE}")
        return self._template

    async def _t2i_render_direct(self, tmpl: str, tmpl_data: dict, options: dict) -> str:
        """直连 t2i 渲染服务出图，返回图片地址。

        作为 AstrBot html_render 的兜底通道：先 trust_env=False 直连（本机代理
        配置异常时照样能出图），失败再带上代理环境变量重试一次。
        """
        endpoint = str(self._cfg("t2i_endpoint") or DEFAULT_CONFIG["t2i_endpoint"]).strip()
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("text2img"):
            endpoint = f"{endpoint}/text2img"

        timeout = self._cfg_float("render_timeout", 60) or 60.0
        payload = {"tmpl": tmpl, "json": True, "tmpldata": tmpl_data, "options": options}
        headers = {"Content-Type": "application/json", "User-Agent": T2I_USER_AGENT}

        last_error: Optional[Exception] = None
        for trust_env in (False, True):
            try:
                async with self._build_client(timeout, trust_env) as client:
                    response = await client.post(
                        f"{endpoint}/generate", json=payload, headers=headers
                    )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"HTTP {response.status_code} {(response.text or '')[:120]}"
                    )
                data = response.json()
                image_id = (data.get("data") or {}).get("id") if isinstance(data, dict) else None
                if not image_id:
                    raise RuntimeError(f"返回结构异常：{str(data)[:120]}")
                return f"{endpoint}/{image_id}"
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[SpendingRank] 直连 t2i 服务失败（trust_env={trust_env}）：{exc}"
                )
        raise RuntimeError(f"直连 t2i 服务失败：{last_error}")

    async def _download_image(self, url: str) -> bytes:
        """下载渲染结果，同样先直连、失败再走代理。"""
        timeout = self._cfg_float("render_timeout", 60) or 60.0
        headers = {"User-Agent": T2I_USER_AGENT}
        last_error: Optional[Exception] = None
        for trust_env in (False, True):
            try:
                async with self._build_client(timeout, trust_env) as client:
                    response = await client.get(url, headers=headers)
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                if not response.content:
                    raise RuntimeError("下载到的图片为空")
                return response.content
            except Exception as exc:
                last_error = exc
                logger.warning(
                    f"[SpendingRank] 下载榜单图片失败（trust_env={trust_env}）：{exc}"
                )
        raise RenderError(f"下载榜单图片失败：{last_error}")

    def _render_local(self, panels: list[dict], meta: dict) -> bytes:
        """本地渲染（纯 Pillow，同步阻塞）。"""
        renderer = _get_local_renderer()
        if renderer is None:
            raise RenderError(_LOCAL_RENDERER_ERROR or "本地渲染器不可用")

        options = self._render_options()
        return renderer.render_panels(
            panels,
            meta,
            brand=str(self._cfg("brand") or DEFAULT_CONFIG["brand"]),
            image_width=max(320, self._cfg_int("image_width", 880)),
            show_cost=self._cfg_bool("show_cost", True),
            image_format=options.get("type", "png"),
            image_quality=int(options.get("quality", 92)),
        )

    async def _render_remote(self, panels: list[dict], meta: dict) -> bytes:
        """远端渲染：AstrBot 自带的 html_render，失败再直连 t2i_endpoint。"""
        tmpl = self._template_text()
        tmpl_data = self._build_tmpl_data(panels, meta)
        options = self._render_options()
        timeout = self._cfg_float("render_timeout", 60) or 60.0

        async with self._render_lock:
            image_url = ""
            errors: list[str] = []

            try:
                image_url = await asyncio.wait_for(
                    self.html_render(tmpl, tmpl_data, return_url=True, options=options),
                    timeout=timeout,
                )
                image_url = str(image_url or "").strip()
            except Exception as exc:
                errors.append(f"html_render 失败：{exc}")
                logger.warning(
                    f"[SpendingRank] AstrBot t2i 渲染失败，改用直连 t2i_endpoint 兜底：{exc}"
                )

            if not image_url:
                try:
                    image_url = await asyncio.wait_for(
                        self._t2i_render_direct(tmpl, tmpl_data, options), timeout=timeout
                    )
                except Exception as exc:
                    errors.append(f"直连渲染失败：{exc}")

            if not image_url:
                raise RenderError("；".join(errors) or "t2i 渲染服务不可用")

            try:
                return await asyncio.wait_for(self._download_image(image_url), timeout=timeout)
            except Exception as exc:
                raise RenderError(f"渲染结果下载失败：{exc}") from exc

    async def _render_image(self, panels: list[dict], meta: dict) -> bytes:
        """渲染成图片字节流，通道顺序由 render_mode 决定。

        auto   ：本地优先，本地挂了再走远端
        local  ：只用本地
        remote ：只用远端

        本地这条通道不碰网络，所以「远端 t2i 429/503」不会再让插件出不了图。
        """
        mode = str(self._cfg("render_mode") or DEFAULT_CONFIG["render_mode"]).strip().lower()
        if mode not in ("auto", "local", "remote"):
            logger.warning(f"[SpendingRank] render_mode 取值不合法（{mode!r}），已按 auto 处理")
            mode = "auto"

        errors: list[str] = []

        if mode in ("auto", "local"):
            try:
                # Pillow 绘制是同步阻塞的，丢到线程池里跑，别卡住事件循环
                return await asyncio.to_thread(self._render_local, panels, meta)
            except Exception as exc:
                errors.append(f"本地渲染失败：{exc}")
                logger.warning(f"[SpendingRank] 本地渲染失败：{exc}")
                if mode == "local":
                    raise RenderError("；".join(errors)) from exc

        try:
            return await self._render_remote(panels, meta)
        except Exception as exc:
            errors.append(f"远端渲染失败：{exc}")

        raise RenderError("；".join(errors) or "渲染失败")

    @staticmethod
    def _plain_leaderboard(panels: list[dict], meta: dict, brand: str, show_cost: bool) -> str:
        """出图全线失败时的兜底：纯文字榜单，双榜就上下分段。"""
        lines = [f"🏆 {brand} 今日榜单 · {meta.get('date', '')}"]
        multi = len(panels) > 1

        for panel in panels:
            rows = panel.get("rows") or []
            if multi:
                lines.extend(["", f"【{panel.get('title') or '榜单'}】"])
            if not rows:
                lines.append("  今天还没有人上榜")
                continue
            for index, row in enumerate(rows, start=1):
                rank = row.get("rank") or index
                parts = [
                    f"{rank}. {row.get('name') or '匿名用户'}",
                    f"{row.get('tokens_display') or '0'} tokens",
                    f"{row.get('requests_display') or '0'} 次",
                ]
                if show_cost:
                    parts.append("$" + str(row.get("cost_display") or "0"))
                lines.append("  ·  ".join(parts))

        tail = (
            f"共 {meta.get('total_users', 0)} 人上榜"
            f" · 合计 Token {meta.get('total_tokens_display', '0')}"
        )
        if show_cost:
            tail += " · 合计 $" + str(meta.get("total_cost_display", "0"))
        lines.extend(["", tail, f"数据更新于 {meta.get('updated_at', '')}"])
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 消息发送
    # ------------------------------------------------------------------
    @staticmethod
    def _image_result(event: AstrMessageEvent, png: bytes):
        b64 = base64.b64encode(png).decode("ascii")
        try:
            return event.chain_result([Image.fromBase64(b64)])
        except Exception as exc:
            logger.warning(f"[SpendingRank] chain_result 失败，回退 image_result：{exc}")
            return event.image_result(f"base64://{b64}")

    def _resolve_umo(self) -> str:
        """把 target_group 配置解析成 unified_msg_origin。

        支持两种写法：
          - 123456789                            -> aiocqhttp:GroupMessage:123456789
          - aiocqhttp:GroupMessage:123456789     -> 原样使用
        """
        raw = str(self._cfg("target_group") or "").strip()
        if not raw:
            return ""
        if raw.count(":") >= 2:
            return raw
        platform = str(self._cfg("platform_id") or "aiocqhttp").strip() or "aiocqhttp"
        group = raw.split(":", 1)[1] if raw.startswith("GroupMessage:") else raw
        return f"{platform}:GroupMessage:{group}"

    async def _push_to_target_group(self) -> None:
        """定时任务：主动推送到目标群。"""
        umo = self._resolve_umo()
        if not umo:
            logger.warning("[SpendingRank] 已到播报时间，但 target_group 未配置，跳过本次推送")
            return

        panels, meta, error = await self._fetch_leaderboard(force=True)
        if error:
            logger.error(f"[SpendingRank] 定时播报拉取数据失败：{error}")
            return
        if not any(panel.get("rows") for panel in panels):
            logger.info("[SpendingRank] 今日暂无消费记录，跳过定时播报")
            return

        chain: Any
        try:
            png = await self._render_image(panels, meta)
            b64 = base64.b64encode(png).decode("ascii")
            chain = MessageChain([Image.fromBase64(b64)])
        except Exception as exc:
            logger.error(f"[SpendingRank] 定时播报渲染失败，降级为纯文字：{exc}")
            try:
                chain = MessageChain([
                    Plain(
                        self._plain_leaderboard(
                            panels,
                            meta,
                            str(self._cfg("brand") or DEFAULT_CONFIG["brand"]),
                            self._cfg_bool("show_cost", True),
                        )
                    )
                ])
            except Exception as exc2:
                logger.error(f"[SpendingRank] 构造文字消息链失败：{exc2}")
                return

        try:
            ok = await self.context.send_message(umo, chain)
            if ok:
                logger.info(f"[SpendingRank] 定时播报已发送至 {umo}")
            else:
                logger.warning(
                    f"[SpendingRank] 未找到匹配 {umo} 的平台适配器，消息未发出，"
                    "请检查 platform_id 与 target_group 配置"
                )
        except Exception as exc:
            logger.error(f"[SpendingRank] 定时播报发送失败：{exc}", exc_info=True)

    # ------------------------------------------------------------------
    # 指令：/排行榜
    # ------------------------------------------------------------------
    @filter.command("排行榜", alias={"消费榜", "消费排行榜", "消费排行"})
    async def cmd_spending_rank(self, event: AstrMessageEvent):
        """查看当日全站消费排行榜"""
        try:
            panels, meta, error = await self._fetch_leaderboard()
            if error:
                yield event.plain_result(f"❌ 排行榜获取失败：{error}")
                return
            if not any(panel.get("rows") for panel in panels):
                yield event.plain_result("📭 今日暂无消费记录，榜单还是空的哦～")
                return

            try:
                png = await self._render_image(panels, meta)
            except RenderError as exc:
                # 三条渲染通道全挂也不让群友白等：降级成纯文字榜单
                logger.error(f"[SpendingRank] 渲染失败，降级为纯文字：{exc}")
                yield event.plain_result(
                    self._plain_leaderboard(
                        panels,
                        meta,
                        str(self._cfg("brand") or DEFAULT_CONFIG["brand"]),
                        self._cfg_bool("show_cost", True),
                    )
                )
                return

            yield self._image_result(event, png)
        except Exception as exc:
            logger.error(f"[SpendingRank] /排行榜 执行异常：{exc}", exc_info=True)
            yield event.plain_result(f"❌ 排行榜指令执行异常：{exc}")

    # ------------------------------------------------------------------
    # 定时任务
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_hhmm(value: Any) -> tuple[int, int]:
        text = str(value or "").strip().replace("：", ":")
        try:
            parts = text.split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except (ValueError, IndexError):
            logger.warning(f"[SpendingRank] cron_time 格式不正确（{value}），已回退为 23:00")
            hour, minute = 23, 0
        return min(max(hour, 0), 23), min(max(minute, 0), 59)

    def _seconds_until_next(self, hhmm: Any) -> float:
        hour, minute = self._parse_hhmm(hhmm)
        now = datetime.now()
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    def _start_scheduler(self) -> None:
        if not self._cfg_bool("cron_enabled", True):
            logger.info("[SpendingRank] cron_enabled 为 false，定时播报已关闭")
            return
        try:
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        except RuntimeError as exc:
            logger.warning(f"[SpendingRank] 当前没有运行中的事件循环，定时播报未启动：{exc}")
            return

        cron_time = self._cfg("cron_time", "23:00")
        target = self._resolve_umo() or "（未配置 target_group）"
        logger.info(f"[SpendingRank] 定时播报已启动：每天 {cron_time} -> {target}")

    async def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                delay = self._seconds_until_next(self._cfg("cron_time", "23:00"))
                logger.info(
                    "[SpendingRank] 下一次定时播报将在 {:.1f} 分钟后触发".format(delay / 60)
                )
                try:
                    # 到点前可以被 terminate 的 stop_event 立即唤醒
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass  # 正常到点

                await self._push_to_target_group()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(f"[SpendingRank] 定时任务异常：{exc}", exc_info=True)
                await asyncio.sleep(60)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def terminate(self) -> None:
        """插件被停用 / 重载 / 卸载时调用：必须回收后台任务。"""
        self._stop_event.set()

        task, self._scheduler_task = self._scheduler_task, None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(f"[SpendingRank] 定时任务退出时出现异常：{exc}")

        # 出图走远端 t2i 服务，本机没有常驻浏览器进程需要回收
        logger.info("[SpendingRank] 插件已卸载，定时播报任务已停止")
