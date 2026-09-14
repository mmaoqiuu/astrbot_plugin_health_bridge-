# health_bridge/monitor.py
"""基于按天 JSON 的健康异常检测 + 冷却控制。

时区原则跟 health_logic.py 一致：不读服务器时钟，
一律以「已存数据里最新的那天」为基准往前数。
例外：实时熬夜判定用 current_time（接收端写入的 HH:MM），
只用于「此刻是不是深夜」，不参与日期/过期判断。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from astrbot.api import logger

from . import health_logic


class HealthMonitor:
    def __init__(self, data_dir: Path, config, cooldown_path: Path):
        self.data_dir = data_dir
        self.config = config
        self.cooldown_path = cooldown_path
        self._cooldowns: dict[str, float] = self._load_cooldowns()

    # ---------- 冷却持久化 ----------
    def _load_cooldowns(self) -> dict:
        try:
            return json.loads(self.cooldown_path.read_text("utf-8"))
        except Exception:
            return {}

    def _save_cooldowns(self) -> None:
        try:
            self.cooldown_path.write_text(
                json.dumps(self._cooldowns), encoding="utf-8"
            )
        except Exception:
            logger.exception("[health_bridge] 写冷却状态失败")

    def _in_cooldown(self, key: str) -> bool:
        hours = float(self.config.get(f"cooldown_hours_{key}", 8))
        last = self._cooldowns.get(key, 0)
        return (datetime.now().timestamp() - last) < hours * 3600

    def _mark(self, key: str) -> None:
        self._cooldowns[key] = datetime.now().timestamp()
        self._save_cooldowns()

    # ---------- 读数据 ----------
    def _load_by_date(self, date_str: str) -> dict | None:
        return health_logic.load_by_date(self.data_dir, date_str)

    def _latest_date(self) -> str | None:
        dates = health_logic.list_dates(self.data_dir)
        return dates[-1] if dates else None

    def _load_today(self) -> dict | None:
        """取「最新一天」的数据（不读服务器时钟）。"""
        d = self._latest_date()
        return self._load_by_date(d) if d else None

    def _load_recent(self, days: int) -> list[dict]:
        """最新一天往前数 N 天（不含最新那天）的历史，用于算基线。"""
        dates = health_logic.list_dates(self.data_dir)
        if len(dates) < 2:
            return []
        newest = datetime.strptime(dates[-1], "%Y-%m-%d").date()
        out = []
        for i in range(1, days + 1):
            d = (newest - timedelta(days=i)).isoformat()
            rec = self._load_by_date(d)
            if rec:
                out.append(rec)
        return out

    # ---------- 统计 ----------
    @staticmethod
    def _zscore(value, series: list[float]) -> float | None:
        if value is None or len(series) < 3:
            return None
        mean = sum(series) / len(series)
        var = sum((x - mean) ** 2 for x in series) / len(series)
        std = var ** 0.5
        if std == 0:
            return None
        return (value - mean) / std

    @staticmethod
    def _parse_hhmm(s: str) -> int | None:
        try:
            h, m = s.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return None

    @staticmethod
    def _parse_time_to_minutes(val, default_min: int) -> int:
        if val is None:
            return default_min
        if isinstance(val, (int, float)):
            return int(val)
        if isinstance(val, str):
            val = val.strip()
            if ":" in val:
                m = HealthMonitor._parse_hhmm(val)
                return m if m is not None else default_min
            try:
                return int(val)
            except ValueError:
                return default_min
        return default_min

    # ---------- 检测 ----------
    def scan(self) -> list[dict]:
        alerts: list[dict] = []
        today = self._load_today()
        if not today:
            return alerts

        history = self._load_recent(int(self.config.get("baseline_days", 7)))
        if len(history) < 3:
            logger.info(
                f"[health_bridge] 基线天数不足({len(history)}/3)，本次跳过检测"
            )
            return alerts

        if self.config.get("rule_resting_hr_high", True):
            self._check_resting_hr(today, history, alerts)
        if self.config.get("rule_spo2_low", True):
            self._check_spo2(today, alerts)
        if self.config.get("rule_sleep_low", True):
            self._check_sleep(today, history, alerts)
        if self.config.get("rule_late_night", True):
            self._check_late_night(today, alerts)
        if self.config.get("rule_late_night_realtime", True):
            self._check_late_night_realtime(today, alerts)
        if self.config.get("rule_hrv_low", False):
            self._check_hrv(today, history, alerts)

        return alerts

    def _check_resting_hr(self, today, history, alerts):
        val = today.get("resting_hr")
        series = [r.get("resting_hr") for r in history if r.get("resting_hr")]
        z = self._zscore(val, series)
        if z is not None and z >= float(self.config.get("resting_hr_z", 2.0)):
            mean = sum(series) / len(series)
            alerts.append({
                "type": "resting_hr_high",
                "cooldown_key": "resting_hr_high",
                "hint": (
                    f"她今天的静息心率 {val:.0f} bpm，"
                    f"比最近 {len(series)} 天平均（{mean:.0f} bpm）偏高不少。"
                ),
            })

    def _check_spo2(self, today, alerts):
        val = today.get("blood_oxygen_pct")
        if val is None:
            return
        threshold = float(self.config.get("spo2_threshold", 95))
        if val < threshold:
            alerts.append({
                "type": "spo2_low",
                "cooldown_key": "spo2_low",
                "hint": f"她今天的血氧只有 {val:.0f}%，低于平时，可能有点累或没休息好。",
            })

    def _check_sleep(self, today, history, alerts):
        sleep = today.get("sleep") or {}
        dur = sleep.get("duration_min")
        if not dur:
            return
        dur = int(dur)
        series = [
            int(r["sleep"]["duration_min"])
            for r in history
            if (r.get("sleep") or {}).get("duration_min")
        ]
        if len(series) < 3:
            return
        mean = sum(series) / len(series)
        if mean - dur >= float(self.config.get("sleep_short_min", 90)):
            alerts.append({
                "type": "sleep_short",
                "cooldown_key": "sleep_short",
                "hint": (
                    f"她昨晚只睡了 {dur // 60} 小时 {dur % 60} 分，"
                    f"比平时少了大约 {(mean - dur) / 60:.1f} 小时。"
                ),
            })

    def _check_late_night(self, today, alerts):
        """基于 HAE 睡眠汇总的入睡时间判定（次日复盘用）。"""
        sleep = today.get("sleep") or {}
        bedtime = sleep.get("bedtime")
        if not bedtime:
            return
        minutes = self._parse_hhmm(bedtime)
        if minutes is None:
            return
        threshold = int(self.config.get("late_night_bedtime_min", 30))
        # 覆盖两段：凌晨 00:00~05:00（晚于阈值）、深夜 23:00~23:59
        is_late = (minutes < 300 and minutes > threshold) or (minutes >= 23 * 60)
        if is_late:
            hh, mm = divmod(minutes, 60)
            alerts.append({
                "type": "late_night",
                "cooldown_key": "late_night",
                "hint": f"她昨晚 {hh:02d}:{mm:02d} 才睡，熬得有点晚。",
            })

    def _check_late_night_realtime(self, today, alerts):
        """实时熬夜判定：看「接收端写入的当前时刻」是否在深夜窗口，且心率偏高。

        - current_time：接收端每次收到数据时写入的 HH:MM（服务器时钟）。
        - latest_hr：当天最后一次收到的心率读数。
        - resting_hr：当天静息心率（做对照）。
        两个条件都满足才推：在深夜窗口 + 心率高于静息一定比例。
        """
        cur = today.get("current_time")
        latest = today.get("latest_hr")
        resting = today.get("resting_hr")
        if not cur or latest is None or not resting:
            return

        minutes = self._parse_hhmm(cur)
        if minutes is None:
            return

        # 深夜窗口：默认 23:00 ~ 05:00（支持跨天，支持直接填 HH:MM 或分钟数）。
        start_val = self.config.get("late_night_start_time", self.config.get("late_night_start_min", "23:00"))
        end_val = self.config.get("late_night_end_time", self.config.get("late_night_end_min", "05:00"))
        start = self._parse_time_to_minutes(start_val, 23 * 60)
        end = self._parse_time_to_minutes(end_val, 5 * 60)
        if start <= end:
            in_window = start <= minutes < end
        else:
            in_window = minutes >= start or minutes < end
        if not in_window:
            return

        # 心率是否高于静息（说明人还醒着）。默认高 15% 以上。
        ratio_cfg = float(self.config.get("late_night_hr_ratio", 1.15))
        try:
            awake = float(latest) > float(resting) * ratio_cfg
        except (TypeError, ValueError):
            return
        if not awake:
            return

        hh, mm = divmod(minutes, 60)
        alerts.append({
            "type": "late_night_realtime",
            "cooldown_key": "late_night_realtime",
            "hint": (
                f"现在是 {hh:02d}:{mm:02d}，她还没睡，"
                f"心率 {int(latest)} bpm（静息 {int(resting)}），人还醒着。"
            ),
        })

    def _check_hrv(self, today, history, alerts):
        val = today.get("hrv_ms")
        series = [r.get("hrv_ms") for r in history if r.get("hrv_ms")]
        z = self._zscore(val, series)
        if z is not None and z <= -float(self.config.get("hrv_z", 1.5)):
            alerts.append({
                "type": "hrv_low",
                "cooldown_key": "hrv_low",
                "hint": (
                    f"她今天的 HRV 只有 {val:.0f} ms，"
                    f"比最近平均偏低，身体可能有点疲劳。"
                ),
            })