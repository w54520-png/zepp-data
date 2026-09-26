"""
通用解析工具（移植自 ZeppBridge crates/core/src/normalizer/mod.rs）

设计原则（照搬 ZeppBridge 注释）：
  - first_value / first_string / first_number 返回 Option，不臆测。
  - 多个候选字段名按优先级列表查找（不同版本 API 兼容）。
  - 哨兵值（sentinel）逐字段显式过滤。
  - parse_timestamp 同时支持毫秒 / 秒 / compact YYYYMMDD 三种格式。
"""
from __future__ import annotations
import base64
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional


# ===== 数据模型 =====

@dataclass
class MetricSample:
    """一个时间点上的标量读数（HRV/压力/血氧 等）。"""
    metric: str           # "hrv_rmssd" / "spo2" / "stress" / "pai_daily"
    timestamp: datetime   # 必须 tz-aware UTC
    value: float
    unit: str             # "ms" / "score" / "%" / "brpm" / "bpm" / "pai"
    source_scope: str     # "device" / "user_fused" / "unknown"
    device_id: Optional[str] = None
    extra: dict = field(default_factory=dict)


@dataclass
class DailyMetric:
    """按天聚合的指标（PAI/步数/Sport Load 等）。"""
    metric: str           # "pai_total" / "steps" / "sport_load"
    date: str             # YYYY-MM-DD（本地日历日，Asia/Shanghai）
    value: float
    unit: str
    source_scope: str
    device_id: Optional[str] = None


@dataclass
class NormalizedBatch:
    """一个流的解析输出。永远返回 records，可能为空，但 diagnostics 永远记录跳过原因。"""
    records: list = field(default_factory=list)         # MetricSample 或 DailyMetric
    diagnostics: list = field(default_factory=list)     # ["item 0: heart rate 数值无效"]
    capability: str = "verified"     # verified / unverified / unavailable

    @property
    def count(self) -> int:
        return len(self.records)


class DataUnavailable(Exception):
    """报文合法但没有可识别记录（云端「这段时间没数据」）。"""
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


# ===== 数据提取（照搬 extract_items） =====

def extract_items(raw: Any) -> list:
    """
    从各种 envelope 形状中提取 items 数组。
    支持：items / data / data.items / data.summary / records / list / results / 顶层 array。
    空数组抛 DataUnavailable（不是解析失败，是云端明确答「没数据」）。
    """
    if isinstance(raw, list):
        if not raw:
            raise DataUnavailable("响应 items 为空")
        return raw

    if not isinstance(raw, dict):
        raise ValueError(f"响应必须是 object 或 array，得到 {type(raw).__name__}")

    for key in ("items", "records", "results", "list"):
        arr = raw.get(key)
        if isinstance(arr, list):
            if not arr:
                raise DataUnavailable(f"响应 {key} 为空")
            return arr

    data = raw.get("data")
    if isinstance(data, list):
        if not data:
            raise DataUnavailable("响应 data 为空")
        return data

    if isinstance(data, dict):
        for key in ("items", "records", "results", "list", "summary"):
            arr = data.get(key)
            if isinstance(arr, list):
                if not arr:
                    raise DataUnavailable(f"响应 data.{key} 为空")
                return arr

    if isinstance(data, str):
        raise DataUnavailable("响应 data 是编码字符串，无法安全完整解码")

    raise ValueError(
        f"响应缺少 items/data 数组，可用字段: {', '.join(raw.keys())}"
    )


# ===== item_object（自动选 value 子对象）=====

_VALUE_KEYS = ("timestamp", "time", "value", "heartRate", "steps", "date")


def item_object(value: dict) -> dict:
    """
    如果 item.value 是 dict 且含 _VALUE_KEYS 之一，就用 value；否则用原 item。
    照搬 Rust item_object。
    """
    if not isinstance(value, dict):
        return value
    inner = value.get("value")
    if isinstance(inner, dict) and any(k in inner for k in _VALUE_KEYS):
        return inner
    return value


# ===== 通用字段查找 =====

def first_value(obj: dict, names: Iterable[str]):
    """按 names 顺序找第一个非空字段值。None 和空字符串都算空。"""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name not in obj:
            continue
        v = obj[name]
        if v is None:
            continue
        if isinstance(v, str) and v.strip() == "":
            continue
        return v
    return None


def first_value_from(obj: dict, nested: Optional[dict], names: Iterable[str]):
    """先在 obj 找，找不到再去 nested 找。"""
    v = first_value(obj, names)
    if v is not None:
        return v
    if nested:
        return first_value(nested, names)
    return None


def first_string(obj: dict, names: Iterable[str]) -> Optional[str]:
    """字段值转字符串。Number 也接受（device_id 经常是数字）。"""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name not in obj:
            continue
        v = obj[name]
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, (int, float)) and v == v:  # not NaN
            return str(v)
    return None


def first_number(obj: dict, names: Iterable[str]) -> Optional[float]:
    """字段值转 float。接受数字 / 数字字符串。"""
    if not isinstance(obj, dict):
        return None
    for name in names:
        if name not in obj:
            continue
        v = obj[name]
        n = parse_number(v)
        if n is not None:
            return n
    return None


def first_number_from(obj: dict, nested: Optional[dict], names: Iterable[str]) -> Optional[float]:
    """先在 obj 找数字，找不到再去 nested。"""
    n = first_number(obj, names)
    if n is not None:
        return n
    if nested:
        return first_number(nested, names)
    return None


def parse_number(v: Any) -> Optional[float]:
    """严格 parse 数字。NaN/Inf 返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v) if v == v and abs(v) != float("inf") else None
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        try:
            n = float(s)
            return n if n == n and abs(n) != float("inf") else None
        except ValueError:
            return None
    return None


# ===== 时间解析 =====

def parse_timestamp(v: Any) -> Optional[datetime]:
    """
    支持 3 种格式：
      ① compact YYYYMMDD（19000101..=21001231） → 当 UTC 零点
      ② 毫秒（|value| >= 10^10）
      ③ 秒
    """
    n = parse_number(v)
    if n is None:
        return None

    compact = int(n)
    if 19000101 <= compact <= 21001231:
        try:
            d = datetime.strptime(str(compact), "%Y%m%d")
            return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
        except ValueError:
            return None

    if abs(n) >= 10_000_000_000:
        ts = compact / 1000.0
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    else:
        return datetime.fromtimestamp(compact, tz=timezone.utc)


def parse_timestamp_or_date(v: Any) -> Optional[datetime]:
    """parse_timestamp + ISO 日期字符串回退。"""
    ts = parse_timestamp(v)
    if ts is not None:
        return ts
    if isinstance(v, str):
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S.%fZ"):
            try:
                d = datetime.strptime(v.strip(), fmt)
                return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d
            except ValueError:
                continue
    return None


def parse_date(v: Any) -> Optional[str]:
    """解析成 YYYY-MM-DD 字符串。"""
    if isinstance(v, str):
        s = v.strip()
        # 直接 YYYY-MM-DD
        try:
            datetime.strptime(s, "%Y-%m-%d")
            return s
        except ValueError:
            pass
        # RFC3339
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return d.strftime("%Y-%m-%d")
        except ValueError:
            pass
    if isinstance(v, (int, float)):
        n = parse_number(v)
        if n is None:
            return None
        compact = int(n)
        if 19000101 <= compact <= 21001231:
            try:
                return datetime.strptime(str(compact), "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                return None
        ts = parse_timestamp(v)
        if ts:
            return ts.strftime("%Y-%m-%d")
    return None


def parse_date_with_zone(v: Any, offset_secs: Optional[int]) -> Optional[str]:
    """带时区切日的日期解析。"""
    if isinstance(v, str):
        return parse_date(v)
    n = parse_number(v)
    if n is None:
        return parse_date(v)
    compact = int(n)
    if 19000101 <= compact <= 21001231:
        return parse_date(v)
    ts = parse_timestamp(v)
    if ts is None:
        return None
    if offset_secs is not None:
        local = ts + timedelta(seconds=offset_secs)
        return local.strftime("%Y-%m-%d")
    return ts.strftime("%Y-%m-%d")


def add_milliseconds(base: datetime, ms: int) -> Optional[datetime]:
    """checked 加法（防止 overflow panic）。

    注意：本函数只防 `OverflowError`（如 ±2^31 ms 即 ±24.8 天的边界），
    不防「语义越界」（如偏移 7 天以上的伪值）。M1 1.4 起 HRV normalizer
    在调用前用 _MAX_HRV_OFFSET_MS 显式判定，后者是业务规则，本函数
    只保证 Python datetime 算术不抛。
    """
    try:
        return base + timedelta(milliseconds=ms)
    except (OverflowError, ValueError):
        return None


# ===== 时区解析 =====

# 时区字符串 → 偏移秒
_iana_fixed = {
    "Asia/Shanghai": 8 * 3600, "Asia/Hong_Kong": 8 * 3600,
    "Asia/Taipei": 8 * 3600, "Asia/Chongqing": 8 * 3600,
    "Asia/Harbin": 8 * 3600, "PRC": 8 * 3600, "Hongkong": 8 * 3600,
    "Asia/Tokyo": 9 * 3600, "Asia/Seoul": 9 * 3600, "Japan": 9 * 3600,
    "UTC": 0, "Etc/UTC": 0, "Etc/GMT": 0, "GMT": 0, "Zulu": 0,
}


def parse_timezone_text(text: str) -> Optional[int]:
    """接受 "Asia/Shanghai" / "32" / "28800" / "GMT+8" / "+08:00" 等。"""
    text = (text or "").strip()
    if not text:
        return None
    if "," in text:
        left, right = text.split(",", 1)
        return parse_timezone_text(left) or parse_timezone_text(right)

    # 纯数字
    try:
        n = float(text)
        return offset_from_number(n)
    except ValueError:
        pass

    # GMT/UTC 前缀
    clock = text
    for prefix in ("GMT", "Utc", "UTC", "gmt"):
        if clock.startswith(prefix):
            clock = clock[len(prefix):]
            break

    parsed = parse_offset_clock(clock) or _iana_fixed.get(text)
    return parsed


def parse_offset_clock(text: str) -> Optional[int]:
    """解析 '+08:00' / '+0800' / '+8' 形式的偏移（秒）。"""
    text = text.strip()
    if not text:
        return None
    sign = 1
    if text.startswith("+"):
        sign = 1
        text = text[1:]
    elif text.startswith("-"):
        sign = -1
        text = text[1:]

    hours = minutes = 0
    if ":" in text:
        h, m = text.split(":", 1)
        try:
            hours = int(h.strip())
            minutes = int(m.strip())
        except ValueError:
            return None
    elif len(text) == 4 and text.isdigit():
        hours = int(text[:2])
        minutes = int(text[2:])
    else:
        try:
            hours = int(text.strip())
        except ValueError:
            return None

    if not (0 <= hours <= 18) or not (0 <= minutes <= 60):
        return None
    secs = sign * (hours * 3600 + minutes * 60)
    # 15 分钟倍数校验
    if secs % 900 != 0:
        return None
    return secs


def offset_from_number(value: float) -> Optional[int]:
    """Zepp 有时给设备时区序号（"32"），有时给秒（28800）。

    "32" 这种设备 tz 序号**不能**当成 32 秒——会变成无效偏移。
    真实偏移是 15 分钟的倍数，区间 ±18h。
    """
    if value != value or abs(value) == float("inf"):
        return None
    raw = int(round(value))
    # |value| > 18h 说明是毫秒或小时单位，需要转换
    if abs(raw) > 18 * 3600:
        if raw % 1000 != 0:
            return None
        raw = raw // 1000
    if raw % 900 != 0:
        return None
    if -18 * 3600 <= raw <= 18 * 3600:
        return raw
    return None


def timezone_offset_from(obj: dict, nested: Optional[dict] = None) -> Optional[int]:
    """从 obj 或 nested 找时区字段。"""
    for scope in (obj, nested) if nested else (obj,):
        if not isinstance(scope, dict):
            continue
        v = first_value(scope, ("timeZone", "time_zone", "tz"))
        if v is None:
            continue
        if isinstance(v, (int, float)):
            return offset_from_number(float(v))
        if isinstance(v, str):
            return parse_timezone_text(v)
    return None


# ===== summary_date + sort =====

def summary_date(obj: dict, nested: Optional[dict] = None) -> Optional[str]:
    """提取 YYYY-MM-DD 本地日历日。"""
    v = first_value_from(obj, nested, ("date", "day", "dayId", "dateString", "localDate"))
    if v is not None:
        d = parse_date(v)
        if d:
            return d
    offset = timezone_offset_from(obj, nested)
    v = first_value_from(obj, nested, ("timestamp", "time", "startTime"))
    if v is not None:
        return parse_date_with_zone(v, offset)
    return None


def daily_summary_sort_key(item: Any) -> tuple:
    """
    canonical 排序：带日历日的优先于 epoch 回退。
    (有日历日=1, 0; 只有 epoch=0, ts)
    """
    if not isinstance(item, dict):
        return (0, 0)
    nested = item.get("value") if isinstance(item.get("value"), dict) else None
    calendar = ("date", "day", "dayId", "dateString", "localDate")
    explicit = first_value_from(item, nested, calendar) is not None
    if not explicit and nested:
        samples = nested.get("samples")
        if isinstance(samples, list):
            for s in samples:
                if isinstance(s, dict) and first_value(s, calendar) is not None:
                    explicit = True
                    break
    ts = first_number_from(item, nested, ("timestamp", "time", "startTime")) or 0
    return (1 if explicit else 0, int(ts))


# ===== device_id + source_scope =====

_MIN_DEVICE_ID_LEN = 8
_DEVICE_ID_RE = re.compile(r"^[a-zA-Z0-9]+$")
_USER_FUSED_EVENT_TYPES = {"DailyHealth", "Charge"}

# Zepp Cloud 在用户没戴手表时给 PAI 等流填的占位 deviceId。
# 这些不是真实设备，提取的 resting_hr / max_hr 等是云端推算值（不是实测），
# 不应该当真实测量数据入库。
_PLACEHOLDER_DEVICE_IDS = frozenset({
    "single-device-firmware",
    "unknown-device",
    "no-device",
    "firmware-default",
})


def device_id(obj: dict) -> Optional[str]:
    """
    提取真实 device_id。Zepp 经常给逗号分隔的 "1,app" 这种 bookkeeping——
    取最长那段，且必须 ≥8 字符 + 全 alphanumeric + 不在占位黑名单里。
    "1," / "1" / "1,-1" / "1440,app" → None（不通过）
    "3,D85403FFFEE4D576" → "D85403FFFEE4D576"（恢复真实 id）
    "single-device-firmware" → None（Zepp 占位标记，表示当天没戴手表）
    """
    if not isinstance(obj, dict):
        return None
    raw = first_string(obj, ("device_id", "deviceId", "deviceid", "sourceDeviceId"))
    if not raw:
        return None
    candidates = []
    for seg in raw.split(","):
        seg = seg.strip()
        if (len(seg) >= _MIN_DEVICE_ID_LEN
                and _DEVICE_ID_RE.match(seg)
                and seg not in _PLACEHOLDER_DEVICE_IDS):
            candidates.append(seg)
    if not candidates:
        return None
    return max(candidates, key=len)


def source_scope(obj: Optional[dict], dev_id: Optional[str]) -> str:
    """
    决定 source_scope：
      - is_fused / isFused / sourceScope=user_fused → UserFused
      - eventType 是 DailyHealth/Charge → UserFused（即使 deviceId 是记账值）
      - 有合法 device_id → Device
      - 都没有 → Unknown
    """
    if isinstance(obj, dict):
        is_fused = obj.get("is_fused") or obj.get("isFused")
        if is_fused is True:
            return "user_fused"
        sc = first_string(obj, ("source_scope", "sourceScope"))
        if sc and sc.lower() == "user_fused":
            return "user_fused"
        et = first_string(obj, ("eventType",))
        if et and et in _USER_FUSED_EVENT_TYPES:
            return "user_fused"
    if dev_id:
        return "device"
    return "unknown"


# ===== Base64 解码 =====

def decode_base64(encoded: str) -> Optional[bytes]:
    """标准 base64 解码。失败返回 None。"""
    try:
        return base64.b64decode(encoded.strip(), validate=False)
    except Exception:
        return None


def decode_base64_json(encoded: str) -> Optional[Any]:
    """base64 + JSON（Zepp sleep summary 格式）。"""
    raw = decode_base64(encoded)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ===== duration_to_minutes =====

def duration_to_minutes(value: float) -> int:
    """
    Zepp variants：有时给分钟，有时给秒。
    > 24h × 60 = 1440 时当作秒。
    """
    if value > 24 * 60:
        return int(round(value / 60.0))
    return int(round(value))


# ===== parse_heart_range（运动心率区间）=====

def parse_heart_range(text: Optional[str]) -> list:
    """
    解析 `1882,113;3486,141;...` 这种 `秒数,区间上限` 格式。
    全 0 的返回空（不是每个区间 0 秒，是「这次没心率数据」）。
    """
    if not text:
        return []
    out = []
    for i, part in enumerate(text.split(";")):
        bits = part.split(",")
        if len(bits) < 2:
            continue
        try:
            secs = int(bits[0].strip())
            upper = int(bits[1].strip())
        except ValueError:
            continue
        if secs < 0 or upper <= 0:
            continue
        out.append({"index": i, "upper_bound_bpm": upper, "seconds": secs})
    if not out:
        return []
    if all(b["seconds"] == 0 for b in out):
        return []
    return out


# ===== CLI 工具 =====

def to_local_date(ts: datetime, tz_offset_secs: int = 8 * 3600) -> str:
    """UTC datetime → 本地日历日 YYYY-MM-DD（默认 Asia/Shanghai）"""
    local = ts.astimezone(timezone(timedelta(seconds=tz_offset_secs)))
    return local.strftime("%Y-%m-%d")


def in_range(value: float, lo: float, hi: float) -> bool:
    """范围内（闭区间）。"""
    return lo <= value <= hi