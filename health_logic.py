"""health_bridge 的纯逻辑核心。

这个模块刻意不导入任何 AstrBot 的东西，也不开网络监听。
所有「解析 / 存储 / 读取 / 格式化」都在这里，可以脱离 AstrBot 在本地用
普通 Python 直接测（见 tests/test_health_logic.py）。

时区原则（见 HANDOFF 第九节）：服务器全程不碰时区。
日期一律以手机发来的 `date` 为准；连数据保留的「过期」判断也以
已存数据里最新的那天为基准来算，绝不读服务器时钟。

例外：`_received_at` 由接收端在「收到数据那一刻」写入（用服务器时钟），
仅用于向 LLM 标注数据新鲜度，不参与任何日期/过期判断。
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

# 手机发来的、以及读取工具接受的日期，必须严格是 YYYY-MM-DD。
# 这条正则同时充当安全闸：日期会被当成文件名用，严格匹配可挡掉
# 形如 "../../etc/passwd" 的路径穿越输入。
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class InvalidPayloadError(ValueError):
    """手机发来的数据不合规（缺 date 或 date 格式错）时抛出。"""


def is_valid_date(date_str: object) -> bool:
    """是否为合法的 YYYY-MM-DD 字符串，且确实是真实存在的日历日期。"""
    if not isinstance(date_str, str) or not DATE_RE.match(date_str):
        return False
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def extract_date(data: dict) -> str:
    """从手机发来的数据里取出并校验主键 date。

    若未提供 date 或格式不符，自动取当前日期 YYYY-MM-DD，不抛出异常。
    """
    if not isinstance(data, dict):
        raise InvalidPayloadError("请求体不是一个 JSON 对象")
    date_str = data.get("date")
    if not is_valid_date(date_str):
        date_str = datetime.now().strftime("%Y-%m-%d")
        data["date"] = date_str
    return date_str


# ---------------------------------------------------------------------------
# 多格式归一化：把上报数据统一转成「本机原生记录」列表（每条带合法 date）。
# 认两种来源：
#   1) 原生契约：顶层带 date 的单条记录（手机快捷指令可直接照这个发）。
#   2) Health Auto Export App：顶层 {"data": {"metrics": [...], "workouts": [...]}}。
# HAE 路是「尽力映射」：其字段名 / 单位随 App 版本和地区设置可能不同，故用
# 别名容错 + 未知字段忽略 + 单位启发式；README 已注明可能需用真实样本校准。
# 建议把 HAE 导出聚合设为「按天」，则每个指标每天一条值，映射最干净。
# ---------------------------------------------------------------------------

# HAE 指标名（小写）→ 原生字段（"a.b" 表示写进嵌套 dict）。带别名挡名字小差异。
_HAE_METRIC_MAP = {
    "resting_heart_rate": "resting_hr",
    "heart_rate_variability": "hrv_ms",
    "heart_rate_variability_sdnn": "hrv_ms",
    "respiratory_rate": "respiratory_rate",
    "blood_oxygen_saturation": "blood_oxygen_pct",
    "oxygen_saturation": "blood_oxygen_pct",
    "apple_sleeping_wrist_temperature": "wrist_temp_c",
    "wrist_temperature": "wrist_temp_c",
    "step_count": "steps",
    "steps": "steps",
    "dietary_water": "water_ml",
    "water": "water_ml",
    "active_energy": "activity.move_kcal",
    "active_energy_burned": "activity.move_kcal",
    "apple_exercise_time": "activity.exercise_min",
    "exercise_time": "activity.exercise_min",
    # Apple 站立小时：HAE 实际发的是带 s 的 apple_stand_hours，两个都收。
    "apple_stand_hour": "activity.stand_hr",
    "apple_stand_hours": "activity.stand_hr",
    "stand_hour": "activity.stand_hr",
    "stand_hours": "activity.stand_hr",
    # 心率区间：min / max / avg 三个读数（速率型，同天取最后一条）。
    "heart_rate_min": "heart_rate_min",
    "heart_rate_max": "heart_rate_max",
    "heart_rate_average": "heart_rate_avg",
    # 以下按真实导出样本校准新增：体重 / 距离 / 爬楼 / 步行心率 / 营养。
    "weight_body_mass": "weight_kg",
    "body_mass": "weight_kg",
    "walking_running_distance": "distance_km",
    "flights_climbed": "flights",
    "walking_heart_rate_average": "walking_hr",
    "carbohydrates": "nutrition.carbs_g",
    "protein": "nutrition.protein_g",
    "total_fat": "nutrition.fat_g",
    "dietary_energy": "nutrition.energy_kcal",
}

# 同一天多条样本的归并方式（非按天聚合时才会出现多条）：
#   累计型（步数 / 喝水 / 能量 / 锻炼 / 站立）求「当天总和」；
#   其余速率型（心率 / HRV / 血氧 / 呼吸 / 体温）取「当天最后一条」——
#   即最近一次读数，反映当下状态，符合本插件「体察近况」的用途（而非日均统计）。
# 按天聚合时每指标每天仅一条，两种归并的结果都 = 该值。
_HAE_CUMULATIVE = {
    "steps",
    "water_ml",
    "distance_km",
    "flights",
    "activity.move_kcal",
    "activity.exercise_min",
    "activity.stand_hr",
    "nutrition.carbs_g",
    "nutrition.protein_g",
    "nutrition.fat_g",
    "nutrition.energy_kcal",
}


def _hae_day(date_str: object) -> str | None:
    """从 HAE 时间串取 YYYY-MM-DD（兼容 '... HH:mm:ss Z' 和 ISO 'T' 两种）。"""
    if not isinstance(date_str, str):
        return None
    m = re.match(r"(\d{4}-\d{2}-\d{2})", date_str)
    return m.group(1) if m else None


def _hae_time(date_str: object) -> str | None:
    """从 HAE 时间串取 HH:MM。"""
    if not isinstance(date_str, str):
        return None
    m = re.search(r"\d{4}-\d{2}-\d{2}[ T](\d{2}:\d{2})", date_str)
    return m.group(1) if m else None


def _hae_value(sample: dict) -> float | int | None:
    """从一条 HAE 样本取数值：优先 qty，其次 Avg/avg，再 value，最后 Min/min。"""
    for key in ("qty", "Avg", "avg", "value", "Min", "min"):
        v = sample.get(key)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v
    return None


def _assign(rec: dict, target: str, value) -> None:
    if "." in target:
        top, sub = target.split(".", 1)
        rec.setdefault(top, {})[sub] = value
    else:
        rec[target] = value


def _get(rec: dict, target: str):
    if "." in target:
        top, sub = target.split(".", 1)
        return rec.get(top, {}).get(sub)
    return rec.get(target)


def _accumulate(rec: dict, target: str, value) -> None:
    """累计型字段同日多样本求和；其余取最后一条覆盖（最近读数）。"""
    if target in _HAE_CUMULATIVE:
        prev = _get(rec, target)
        if isinstance(prev, (int, float)) and not isinstance(prev, bool):
            value = prev + value
    _assign(rec, target, value)


def _hae_convert(field: str, value, units: object):
    """对单位随地区变化的指标换算到原生字段单位；未知单位原样返回。

    HAE 的喝水 / 体温 / 活动能量会随地区设置发出不同单位，必须按 units 换算，
    否则会出现「站立 60 小时」「喝水 0.9ml」这类错值。
    """
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    u = str(units or "").strip().lower()
    if field == "water_ml":
        if "fl" in u or "oz" in u:
            return round(value * 29.5735)  # 液量盎司 -> 毫升
        if u in ("l", "liter", "litre", "liters", "litres"):
            return round(value * 1000)  # 升 -> 毫升
        return value  # 毫升 / 未知
    if field == "wrist_temp_c":
        if "f" in u and "c" not in u:
            return round((value - 32) * 5 / 9, 1)  # 华氏 -> 摄氏
        return value
    if field in ("activity.move_kcal", "nutrition.energy_kcal"):
        if "kj" in u:
            return round(value / 4.184)  # 千焦 -> 千卡
        return value
    if field == "distance_km":
        if "mi" in u:
            return round(value * 1.60934, 2)  # 英里 -> 公里
        return round(value, 2) if isinstance(value, float) else value
    if field == "weight_kg":
        if "lb" in u:
            return round(value * 0.453592, 1)  # 磅 -> 公斤
        return round(value, 1) if isinstance(value, float) else value
    return value


def _apply_hae_sleep(rec: dict, s: dict) -> None:
    """把一条 HAE sleep_analysis 并入记录的 sleep 字段。

    同一天可能有多条睡眠样本（碎片化睡眠，或未按天聚合）：时长累加；
    入睡时间保留最早一条、醒来时间用最晚一条（HAE 样本按时间先后排列，
    故「首条的入睡」即最早、「末条的醒来」即最晚——避免跨午夜时比较 HH:MM 出错）。
    同时收睡眠分期：深睡 / 快速动眼 / 核心 / 清醒（HAE 用小时计，转成分钟累加）。
    """
    dur = None
    # 时长优先级：实际睡眠 totalSleep > 在床 inBed > 旧字段 asleep。
    # 只用 iPhone「在床」记录的那几晚 totalSleep/asleep 为 0、仅 inBed 有值，
    # 故必须跳过 0、回退到 inBed，否则会显示「睡了 0 分」。
    for k in ("totalSleep", "inBed", "asleep"):
        v = s.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            # HAE 睡眠多以小时计；启发式兼容万一是分钟（<=24 视为小时）。
            dur = int(round(v * 60)) if v <= 24 else int(round(v))
            break
    bt = _hae_time(s.get("sleepStart") or s.get("startDate"))
    wt = _hae_time(s.get("sleepEnd") or s.get("endDate"))

    # 睡眠分期：HAE 用小时计，转成分钟累加。
    # 兼容大小写与常见别名（deep/Deep、rem/REM、core、awake）。
    stages = {
        "deep_min": ("deep", "Deep", "deepSleep"),
        "rem_min": ("rem", "REM", "remSleep", "快速动眼期"),
        "core_min": ("core", "Core", "核心"),
        "awake_min": ("awake", "Awake", "清醒"),
    }
    stage_vals: dict[str, int] = {}
    for target, keys in stages.items():
        for k in keys:
            v = s.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                stage_vals[target] = int(round(v * 60))
                break

    if dur is None and not bt and not wt and not stage_vals:
        return

    sleep = rec.setdefault("sleep", {})
    if dur is not None:
        sleep["duration_min"] = sleep.get("duration_min", 0) + dur
    for target, mins in stage_vals.items():
        sleep[target] = sleep.get(target, 0) + mins

    # 入睡时间为「00:00」多是仅在床记录时的占位（没戴表测不到真实入睡），跳过免误导。
    if bt and bt != "00:00" and "bedtime" not in sleep:  # 保留最早（首条）的入睡时间
        sleep["bedtime"] = bt
    if wt:  # 醒来时间用最晚（后到）的一条
        sleep["wake_time"] = wt


# 经期：判定「在经期」只看「月经流量」记录有没有真实流量值；
# 「月经间期出血」(spotting) 不算在经期。名称按中英两版兼容。
_EMPTY_FLOW_VALUES = {"", "无", "没有", "none", "未指定", "unspecified"}


def _is_menstrual_flow(name: object) -> bool:
    n = str(name).strip().lower()
    return "月经流量" in n or "menstrual flow" in n or "menstrualflow" in n


def _normalize_hae(data: dict) -> list[dict]:
    records: dict[str, dict] = {}

    def rec_for(day: str) -> dict:
        return records.setdefault(day, {"date": day})

    for metric in data.get("metrics") or []:
        if not isinstance(metric, dict):
            continue
        name = str(metric.get("name", "")).strip().lower()
        samples = metric.get("data")
        if not isinstance(samples, list):
            continue
        if name == "sleep_analysis":
            for s in samples:
                if not isinstance(s, dict):
                    continue
                day = _hae_day(s.get("sleepEnd") or s.get("endDate") or s.get("date"))
                if day:
                    _apply_hae_sleep(rec_for(day), s)
            continue
        field = _HAE_METRIC_MAP.get(name)
        if not field:
            continue  # 未知指标，忽略（保持健壮，不报错）
        units = metric.get("units")
        for s in samples:
            if not isinstance(s, dict):
                continue
            day = _hae_day(s.get("date"))
            val = _hae_value(s)
            if day is None or val is None:
                continue
            if field == "blood_oxygen_pct":
                # 血氧可能是 0~1 的小数，也可能已是百分数；<=1.5 视为小数转百分比。
                if val <= 1.5:
                    val = round(val * 100, 1)
            else:
                val = _hae_convert(field, val, units)  # 单位换算（喝水/体温/能量）
            _accumulate(rec_for(day), field, val)

    for w in data.get("workouts") or []:
        if not isinstance(w, dict):
            continue
        day = _hae_day(w.get("start") or w.get("end"))
        if day is None:
            continue
        item: dict = {}
        wtype = w.get("name") or w.get("type")
        if isinstance(wtype, str) and wtype:
            item["type"] = wtype
        dur = w.get("duration")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool):
            item["duration_min"] = int(
                round(dur / 60)
            )  # HAE 文档：workout duration 单位为秒
        if item:
            rec_for(day).setdefault("workouts", []).append(item)

    # 经期跟踪：把有真实流量的「月经流量」那天标记为在经期，并记下流量等级。
    for c in data.get("cycleTracking") or []:
        if not isinstance(c, dict) or not _is_menstrual_flow(c.get("name")):
            continue
        value = str(c.get("value", "")).strip()
        if not value or value.lower() in _EMPTY_FLOW_VALUES:
            continue
        day = _hae_day(c.get("start") or c.get("date"))
        if day:
            r = rec_for(day)
            r["in_period"] = True
            r.setdefault("period_flow", value)

    # 症状：当天出现的症状名（去重）。被错归到症状里的「月经流量」跳过（已由经期处理）。
    for sym in data.get("symptoms") or []:
        if not isinstance(sym, dict):
            continue
        name = str(sym.get("name", "")).strip()
        if not name or _is_menstrual_flow(name):
            continue
        day = _hae_day(sym.get("start") or sym.get("date"))
        if not day:
            continue
        lst = rec_for(day).setdefault("symptoms", [])
        if name not in lst:
            lst.append(name)

    return [records[d] for d in sorted(records)]


def normalize_payload(payload: object) -> list[dict]:
    """把上报数据归一化成原生记录列表（每条带合法 date）。

    认两种格式：原生契约（顶层 date）、Health Auto Export（顶层 data.metrics）。
    都不合规则抛 InvalidPayloadError。
    """
    if not isinstance(payload, dict):
        raise InvalidPayloadError("请求体不是一个 JSON 对象")
    data = payload.get("data")
    if isinstance(data, dict) and any(
        k in data for k in ("metrics", "workouts", "cycleTracking", "symptoms")
    ):
        records = [r for r in _normalize_hae(data) if is_valid_date(r.get("date"))]
        if not records:
            raise InvalidPayloadError("Health Auto Export 数据里没有可用的日期/指标")
        return records
    # 原生契约
    extract_date(payload)  # 校验 date，不合法即抛
    return [payload]


# ---------------------------------------------------------------------------
# 存储：一天一个文件，<data_dir>/<date>.json，UTF-8 无 BOM，同一天覆盖。
# ---------------------------------------------------------------------------


def store_report(data_dir: Path, data: dict) -> Path:
    """把一条当天数据写成文件。返回写入的文件路径。

    支持增量合并同一天的数据，保留事件记录等。
    每次存储都会把 `_received_at` 刷成服务器当前时间，用于向 LLM 标注数据新鲜度。
    """
    date_str = extract_date(data)
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / f"{date_str}.json"

    merged = {}
    if target.is_file():
        try:
            merged = json.loads(target.read_text(encoding="utf-8"))
        except Exception:
            merged = {}

    for k, v in data.items():
        if k == "events" and isinstance(v, list):
            merged.setdefault("events", []).extend(v)
        elif k == "event" and "event" in data:
            ev_list = merged.setdefault("events", [])
            ev_item = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "event": data.get("event"),
                "app_name": data.get("app_name"),
                "source": data.get("source")
            }
            ev_list.append(ev_item)
        elif k in ("app_name", "source") and "event" in data:
            continue
        elif isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        elif isinstance(v, list) and isinstance(merged.get(k), list):
            for item in v:
                if item not in merged[k]:
                    merged[k].append(item)
        else:
            merged[k] = v

    # 接收端统一记录「收到数据的时刻」，覆盖上报端可能写死的旧值。
    # 这样 LLM 读到的 _received_at 永远反映最近一次收到数据的时间。
    # 注意：这里读了一次服务器时钟，仅用于标注新鲜度，不参与日期/过期判断。
    merged["_received_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    text = json.dumps(merged, ensure_ascii=False, indent=2)
    tmp = data_dir / f".{date_str}.json.tmp"
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return target


def list_dates(data_dir: Path) -> list[str]:
    """列出已存数据的日期，升序。只认文件名是合法日期的 .json。"""
    if not data_dir.is_dir():
        return []
    dates = [p.stem for p in data_dir.glob("*.json") if is_valid_date(p.stem)]
    return sorted(dates)


def load_by_date(data_dir: Path, date_str: str) -> dict | None:
    """读指定日期的数据。日期非法或文件不存在/读不出来返回 None。"""
    if not is_valid_date(date_str):
        return None
    target = data_dir / f"{date_str}.json"
    if not target.is_file():
        return None
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    # 兜底：万一文件里没存 date，用文件名补上，方便下游显示。
    loaded.setdefault("date", date_str)
    return loaded


def load_latest(data_dir: Path) -> dict | None:
    """读「最近一天」的数据 = 已存文件里日期最大的那条（不看服务器时钟）。"""
    dates = list_dates(data_dir)
    if not dates:
        return None
    return load_by_date(data_dir, dates[-1])


def cleanup_old(data_dir: Path, retention_days: int) -> list[str]:
    """清理过期旧文件，返回被删掉的日期列表。

    过期判断以「已存数据里最新的那天」为基准，往前数 retention_days 天，
    更早的删掉。这样完全不依赖服务器时钟（见 HANDOFF 第九节）：
    没有新数据进来时最新日期不动，也就不会误删。
    """
    if not isinstance(retention_days, int) or retention_days <= 0:
        return []
    dates = list_dates(data_dir)
    if not dates:
        return []
    newest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
    cutoff = newest - timedelta(days=retention_days)
    removed: list[str] = []
    for d in dates:
        if datetime.strptime(d, "%Y-%m-%d").date() < cutoff:
            try:
                (data_dir / f"{d}.json").unlink()
                removed.append(d)
            except OSError:
                pass
    return removed


# ---------------------------------------------------------------------------
# 格式化：把一条数据整理成分组、带单位的自然文字，回传给模型。
# 只摆事实、不做判断（不写「偏低」「睡得不好」这类解读，那是角色的事）。
# 任何字段都可能缺失：有则显示，没有就跳过，绝不报错。
# ---------------------------------------------------------------------------


def _fmt_number(value: object) -> str | None:
    """把数值转成干净字符串：整数就不带小数点；非数值返回 None。"""
    if isinstance(value, bool):  # bool 是 int 的子类，单独挡掉
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer():
            return str(int(value))
        return str(round(value, 1))  # 收掉浮点长尾，避免 HRV 38.216238… 这种噪音
    return None


def _fmt_duration(minutes: object) -> str | None:
    """把分钟数转成「X 小时 Y 分」。非法值返回 None。"""
    if isinstance(minutes, bool):
        return None
    if not isinstance(minutes, (int, float)):
        return None
    total = int(minutes)
    if total < 0:
        return None
    hours, mins = divmod(total, 60)
    if hours and mins:
        return f"{hours} 小时 {mins} 分钟"
    if hours:
        return f"{hours} 小时"
    return f"{mins} 分钟"


# 月经流量等级翻译：把各来源/各地区的怪词归一成自然说法给角色。
# 「中等」是锚点；明亮(bright)/剧烈(intense) 按 Apple 经量「轻<中<重」三档对应。
_FLOW_PHRASE = {
    "明亮": "偏少",
    "轻": "偏少",
    "少量": "偏少",
    "点滴": "偏少",
    "淡": "偏少",
    "light": "偏少",
    "spotting": "偏少",
    "low": "偏少",
    "中等": "适中",
    "中": "适中",
    "中量": "适中",
    "适中": "适中",
    "medium": "适中",
    "剧烈": "偏多",
    "重": "偏多",
    "大": "偏多",
    "大量": "偏多",
    "heavy": "偏多",
    "high": "偏多",
}


def _flow_phrase(value: object) -> str | None:
    """把月经流量值翻译成「偏少/适中/偏多」；未知值返回 None（只说在经期）。"""
    if value is None:
        return None
    return _FLOW_PHRASE.get(str(value).strip().lower())


def _hhmm_to_min(t: object) -> int | None:
    if not isinstance(t, str):
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})$", t.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    return h * 60 + mi if h <= 23 and mi <= 59 else None


def _sleep_times_plausible(bedtime: object, wake: object) -> bool:
    """入睡→醒的在床跨度是否合理（<=16h）。源数据偶有错乱，跨度会荒诞（如 22 小时）。"""
    b, w = _hhmm_to_min(bedtime), _hhmm_to_min(wake)
    if b is None or w is None:
        return True  # 缺一个就不判断
    window = w - b
    if window <= 0:
        window += 1440  # 跨午夜
    return window <= 16 * 60


def _night_fragments(data: dict) -> list[str]:
    """「昨晚」组：睡眠 + 静息生理指标（这些多在睡眠/静息时测得）。"""
    frags: list[str] = []

    sleep = data.get("sleep")
    if isinstance(sleep, dict):
        dur = _fmt_duration(sleep.get("duration_min"))
        if dur:
            frags.append(f"睡了 {dur}")
        # 睡眠分期：有则显示（深睡 / 快速动眼 / 核心 / 清醒）。
        stage_parts = []
        for key, label in (
            ("deep_min", "深睡"),
            ("rem_min", "快速动眼"),
            ("core_min", "核心"),
            ("awake_min", "清醒"),
        ):
            mins = _fmt_duration(sleep.get(key))
            if mins:
                stage_parts.append(f"{label} {mins}")
        if stage_parts:
            frags.append("、".join(stage_parts))
        bedtime = sleep.get("bedtime")
        wake = sleep.get("wake_time")
        # 入睡/醒时间偶有源数据错乱（在床跨度荒诞），那种只留时长、不显示时间。
        if isinstance(bedtime, str) and isinstance(wake, str):
            if _sleep_times_plausible(bedtime, wake):
                frags.append(f"{bedtime} 入睡，{wake} 醒")
        elif isinstance(bedtime, str):
            frags.append(f"{bedtime} 入睡")
        elif isinstance(wake, str):
            frags.append(f"{wake} 醒")

    vitals_specs = [
        ("resting_hr", "静息心率 {}"),
        ("hrv_ms", "HRV {}ms"),
        ("blood_oxygen_pct", "血氧 {}%"),
        ("respiratory_rate", "呼吸 {} 次/分"),
        ("wrist_temp_c", "手腕温度 {}°C"),
    ]
    for key, template in vitals_specs:
        num = _fmt_number(data.get(key))
        if num is not None:
            frags.append(template.format(num))

    return frags


def _day_fragments(data: dict) -> list[str]:
    """「今天」组：步数、距离、爬楼、喝水、活动环、运动、步行心率、体重。"""
    frags: list[str] = []

    steps = _fmt_number(data.get("steps"))
    if steps is not None:
        frags.append(f"{steps} 步")

    distance = _fmt_number(data.get("distance_km"))
    if distance is not None:
        frags.append(f"走/跑 {distance}km")

    flights = _fmt_number(data.get("flights"))
    if flights is not None:
        frags.append(f"爬楼 {flights} 层")

    water = _fmt_number(data.get("water_ml"))
    if water is not None:
        frags.append(f"喝水 {water}ml")

    activity = data.get("activity")
    if isinstance(activity, dict):
        ring: list[str] = []
        move = _fmt_number(activity.get("move_kcal"))
        if move is not None:
            ring.append(f"移动 {move}kcal")
        exercise = _fmt_number(activity.get("exercise_min"))
        if exercise is not None:
            ring.append(f"锻炼 {exercise} 分钟")
        stand = _fmt_number(activity.get("stand_hr"))
        if stand is not None:
            ring.append(f"站立 {stand} 小时")
        if ring:
            frags.append("活动环 " + "、".join(ring))

    workouts = data.get("workouts")
    if isinstance(workouts, list) and workouts:
        items: list[str] = []
        for w in workouts:
            if not isinstance(w, dict):
                continue
            wtype = w.get("type")
            if not isinstance(wtype, str) or not wtype:
                continue
            dur = _fmt_duration(w.get("duration_min"))
            items.append(f"{wtype} {dur}" if dur else wtype)
        if items:
            frags.append("运动 " + "、".join(items))

    walking_hr = _fmt_number(data.get("walking_hr"))
    if walking_hr is not None:
        frags.append(f"步行心率 {walking_hr}")

    weight = _fmt_number(data.get("weight_kg"))
    if weight is not None:
        frags.append(f"体重 {weight}kg")

    events = data.get("events")
    if isinstance(events, list) and events:
        ev_strs = []
        for ev in events:
            if isinstance(ev, dict):
                t = ev.get("time", "")
                name = ev.get("app_name") or ev.get("event") or "事件"
                ev_strs.append(f"{t} 打开了 {name}" if t else f"打开了 {name}")
        if ev_strs:
            frags.append("最近动态：" + "；".join(ev_strs[-3:]))

    return frags


def _nutrition_fragments(data: dict) -> list[str]:
    """「营养」组：饮食热量 + 三大营养素（多来自饮食记录类 App）。"""
    n = data.get("nutrition")
    if not isinstance(n, dict):
        return []
    frags: list[str] = []
    for key, template in (
        ("energy_kcal", "热量 {}kcal"),
        ("carbs_g", "碳水 {}g"),
        ("protein_g", "蛋白 {}g"),
        ("fat_g", "脂肪 {}g"),
    ):
        num = _fmt_number(n.get(key))
        if num is not None:
            frags.append(template.format(num))
    return frags


def format_report(data: dict | None) -> str:
    """把一条数据整理成分组的自然文字。data 为 None 表示没有任何数据。"""
    if not data:
        return "目前还没有任何身体数据。"

    date_str = data.get("date", "未知日期")
    lines = [f"{date_str} 的身体数据。"]

    # 数据接收时间：让 LLM 知道这条数据是什么时候收到的，据此判断新鲜度。
    recv = data.get("_received_at")
    if isinstance(recv, str) and recv.strip():
        lines.append(f"（数据接收于 {recv}，请据此判断是否为过期数据。）")

    night = _night_fragments(data)
    if night:
        lines.append("昨晚：" + "；".join(night) + "。")

    day = _day_fragments(data)
    if day:
        lines.append("今天：" + "；".join(day) + "。")

    nutrition = _nutrition_fragments(data)
    if nutrition:
        lines.append("营养：" + "、".join(nutrition) + "。")

    in_period = data.get("in_period")
    if isinstance(in_period, bool):
        if in_period:
            phrase = _flow_phrase(data.get("period_flow"))
            lines.append(
                f"经期：在经期，经量{phrase}。" if phrase else "经期：在经期。"
            )
        else:
            lines.append("经期：不在经期。")

    symptoms = data.get("symptoms")
    if isinstance(symptoms, list):
        names = [s for s in symptoms if isinstance(s, str) and s.strip()]
        if names:
            lines.append("症状：" + "、".join(names) + "。")

    events = data.get("events")
    if isinstance(events, list) and events:
        ev_strs = []
        for ev in events:
            if isinstance(ev, dict):
                t = ev.get("time", "")
                name = ev.get("app_name") or ev.get("event") or ""
                ev_strs.append(f"{t} {name}".strip())
        if ev_strs:
            lines.append("事件：" + "、".join(ev_strs) + "。")

    # 只有日期行、没有任何指标：明说一句，免得下游以为有内容。
    if len(lines) == 1:
        lines.append("这一天没有记录到具体指标。")

    return "\n".join(lines)


def format_for_date(data_dir: Path, date_str: str) -> str:
    """读取工具用：取指定日期并格式化；该日无数据时明确说出来。"""
    if not is_valid_date(date_str):
        return "日期格式应为 YYYY-MM-DD。"
    data = load_by_date(data_dir, date_str)
    if data is None:
        return f"{date_str} 这一天还没有数据。"
    return format_report(data)


def format_latest(data_dir: Path) -> str:
    """读取工具用：取最近一天并格式化。"""
    return format_report(load_latest(data_dir))