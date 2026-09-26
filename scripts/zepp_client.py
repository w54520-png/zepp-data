#!/usr/bin/env python3
"""
Zepp Cloud API client (国服/国际服通用)
基于 ZeppBridge 项目逆向的 API 端点和协议规范实现。

参考：
  - https://github.com/lingcang728/ZeppBridge (Rust 实现)
  - https://github.com/m4ary/zepp-health-cli (独立逆向)
  - https://github.com/Thejuampi/icu (独立逆向)

认证流程（不走 HAR / 桌面 WebView）：
  1. 浏览器打开 https://user.huami.com/privacy2/index.html (国服)
     或 https://watchface.zepp.com/ (国际服)
  2. 点登录 → 跳到 universalLogin → email+password 或手机号+密码
  3. 登录成功后浏览器 set cookie:
     - apptoken (裸 token)
     - userid (用户数字 ID)
     - hm-user-login-info (JSON，含 ttl/region/cname 等)
  4. 从 cookie 抓 apptoken/userid/region_host 存到 secrets/token.json
  5. 本客户端读取后直接用

端点列表（11 个，按 ZeppBridge connectors/zepp.rs 整理）：
  /users/{id}/devices
  /users/{id}/heartRate            [毫秒时间]
  /users/{id}/members
  /users/{id}/members/{member}/weightRecords   [秒时间]
  /v1/data/band_data.json
  /v1/sport/{sport}/history.json
  /v1/sport/run/detail.json
  /v2/watch/users/{id}/WatchSportStatistics/{SPORT_LOAD|VO2_MAX}
  /v2/users/me/events              [毫秒，v2 events]
  /users/{id}/events               [毫秒，user events]
  /users/{id}/events/dateString    [ISO + IANA TZ]
  /users/me/fileInfo/events        [毫秒]

事件类型表（v2/user events / dateString / fileInfo 三种 surface）：
  Continuous（自动持续）：
    hrv_sdnn / real_data
    Charge / stress_data            ← 压力藏在这里
    Charge / insight_data           ← 体电荷 / Readiness
    all_day_stress (user_events, 无 subType)
    blood_oxygen (user_events, 无 subType)
    blood_oxygen / odi | osa_event (user_events_date_string)
    RespiratoryRate / real_data
    HRVRMSSD / real_data
    PaiHealthInfo (user_events, 无 subType)
    second_heart_rate / real_data (v2_events 或 fileInfo)

  Episodic（手动/偶发）：
    blood_pressure / real_data
    LactateThreshold / summary
    Emotion / real_data
    weight (走 weightRecords，不在 events 里)
    Food (v2_events, 无 subType，国际服才有)
"""
import json
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import ssl

# ===== 常量 =====

# token 路径：按优先级查找，跨 session 持久优先
#   1. ZEPP_SECRETS_PATH 环境变量（最优先，给 cron 任务明确指定）
#   2. ~/.zepp-data/.secrets/token.json（用户主目录下，跨 session 持久）
#   3. 旧硬编码路径 /var/minis/workspace/zepp_data/.secrets/token.json（向后兼容）
#       如果该路径不存在但 ~/.zepp-data/.secrets/token.json 存在，自动建 symlink
import os
from pathlib import Path as _P


def _resolve_secrets_path() -> str:
    """Resolve token.json path with fallback chain."""
    env_path = os.environ.get("ZEPP_SECRETS_PATH")
    if env_path and _P(env_path).exists():
        return env_path

    home_path = _P.home() / ".zepp-data" / ".secrets" / "token.json"
    if home_path.exists():
        legacy = _P("/var/minis/workspace/zepp_data/.secrets/token.json")
        if not legacy.exists():
            # 自动建 symlink，让硬编码路径也能工作
            legacy.parent.mkdir(parents=True, exist_ok=True)
            try:
                legacy.symlink_to(home_path)
            except OSError:
                pass
        return str(home_path)

    # 最后兜底返回硬编码旧路径（即使不存在，让上层报错给用户看）
    return "/var/minis/workspace/zepp_data/.secrets/token.json"


DEFAULT_SECRETS_PATH = _resolve_secrets_path()

# 通用请求头，按 ZeppBridge build_headers 复刻
DEFAULT_HEADERS = {
    "appname": "com.huami.midong",
    "appplatform": "ios_phone",  # 伪装 iOS（Zepp App 默认身份）
    "accept": "*/*",
    "v": "2.0",
    "vn": "10.2.5",
    "cv": "1722_10.2.5",
    "vb": "202604132257",
    "lang": "en",
    "country": "",
    "timezone": "UTC",
}

# 业务返回信封：code=1 是成功（ZeppBridge 通过数 1075 条报文确认）
SUCCESS_CODE = 1

# 网络参数
DEFAULT_TIMEOUT = 35  # 秒（对齐 ZeppBridge 35s）
MAX_ATTEMPTS = 3
RETRY_BACKOFF_MS = [50, 150]
MAX_REDIRECTS = 5
MAX_BACKOFF_MS = 8_000
MAX_BODY_BYTES = 32 * 1024 * 1024  # 32MB


# ===== 异常类 =====

class ZeppError(Exception):
    pass


class NeedsReauth(ZeppError):
    """需要重新登录（401/403）"""
    pass


class CloudRejected(ZeppError):
    """业务层失败（非 1）"""
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"cloud rejected: code={code} message={message!r}")


class Unavailable(ZeppError):
    """404 / 服务下线"""
    pass


class RetryExhausted(ZeppError):
    pass


class NetworkError(ZeppError):
    pass


class Cancelled(ZeppError):
    pass


# ===== 工具 =====

def validate_region_host(host):
    """校验区域主机名。严格按 ZeppBridge validate_region_host 复刻。"""
    if not host:
        raise ZeppError("host 为空")
    s = host.strip()
    if "://" not in s:
        s = "https://" + s
    u = urlparse(s)
    if u.scheme != "https":
        raise ZeppError(f"只允许 HTTPS: {s}")
    if u.username or u.password:
        raise ZeppError("不允许凭据")
    if u.port:
        raise ZeppError("不允许端口")
    if u.path not in ("", "/"):
        raise ZeppError("不允许路径")
    if u.query or u.fragment:
        raise ZeppError("不允许 query/fragment")
    host_lower = (u.hostname or "").lower()
    ok = (host_lower.startswith("api-mifit") and
          (host_lower.endswith(".zepp.com") or host_lower.endswith(".huami.com")))
    if not ok:
        raise ZeppError(f"仅允许 api-mifit*.zepp.com 或 api-mifit*.huami.com: {host_lower}")
    return f"https://{host_lower}"


def exponential_backoff_ms(attempt, base_ms):
    """带 25% 抖动的指数退避（attempt: 0-based）"""
    base = max(base_ms, 200)
    scaled = min(base * (1 << min(attempt, 5)), MAX_BACKOFF_MS)
    # 抖动 75-100%
    import random
    jitter = 75 + random.randint(0, 25)
    return scaled * jitter // 100


# ===== 客户端 =====

# ===== M4 章节 4.5：区域主机备选表（按优先级）=====
# 应对：OAuth 拿到的 region_host 在边缘场景下不可用（DNS 抖动 / 机房迁移），
# 客户端可显式指定 try_regions 列表，自动 fallback 到下一个可用 host。
# Region suffix 含义（ZeppBridge validate_region_host 文档）：
#   cn3  - 中国大陆（api-mifit-cn3.zepp.com）
#   us3  - 美东（api-mifit-us3.zepp.com）
#   us4  - 美西
#   eu2  - 欧洲（api-mifit-eu2.zepp.com）
#   sg2  - 新加坡
# 自动 fallback 顺序：cn3 → us3 → eu2 → sg2（地理覆盖最广）
REGION_FALLBACK_CHAIN = [
    "api-mifit-cn3.zepp.com",
    "api-mifit-us3.zepp.com",
    "api-mifit-eu2.zepp.com",
    "api-mifit-sg2.zepp.com",
]


def region_host_candidates(primary: str) -> list:
    """
    给一个 region_host（"https://api-mifit-...zepp.com"），返回按优先级排序的候选列表。

    优先级：
      1) primary（OAuth 给的，可能在 cn3/us3/eu2/sg2 任何一个）
      2) REGION_FALLBACK_CHAIN 中其他 host
    """
    # 从 primary 提取 host（去掉 https://）
    p = primary.strip()
    if p.startswith("https://"):
        p = p[len("https://"):]
    elif p.startswith("http://"):
        p = p[len("http://"):]
    p = p.rstrip("/")
    out = []
    if p:
        out.append(p)
    for h in REGION_FALLBACK_CHAIN:
        if h not in out:
            out.append(h)
    return out


class ZeppClient:
    """Zepp Cloud API 客户端。

    用法：
        c = ZeppClient()  # 从默认路径读 token
        devs = c.devices()
        hrv = c.v2_events("hrv_sdnn", "real_data", from_ms, to_ms)

    M4 章节 4.5 区域切换：
      - 默认用 token.json 里的 region_host
      - 构造时若 try_regions_fallback=True，主 host 失败时自动切到 REGION_FALLBACK_CHAIN
      - 或显式传 base_url_override="api-mifit-us3.zepp.com" 强制用某个 host
    """

    def __init__(self, secrets_path=DEFAULT_SECRETS_PATH, user_agent="ZeppClient/0.1",
                 try_regions_fallback: bool = False, base_url_override: str = None):
        self.secrets_path = Path(secrets_path)
        with open(self.secrets_path) as f:
            self.auth = json.load(f)
        self.app_token = self.auth["app_token"]
        self.user_id = self.auth["user_id"]
        # region_host 解析
        primary_host = self.auth.get("region_host", "https://api-mifit.zepp.com")
        if base_url_override:
            # 用户显式指定（不带 https:// 也接受）
            h = base_url_override.strip()
            if not h.startswith("http"):
                h = "https://" + h
            self.base_url = validate_region_host(h).rstrip("/")
        else:
            self.base_url = validate_region_host(primary_host).rstrip("/")
        # M4 4.5：fallback 候选
        self.try_regions_fallback = try_regions_fallback
        if try_regions_fallback:
            self._region_candidates = region_host_candidates(self.base_url)
        else:
            # 不启用 fallback → 只包含当前 host
            current = self.base_url.replace("https://", "").replace("http://", "").rstrip("/")
            self._region_candidates = [current]
        self._current_region_idx = 0
        self.headers = {**DEFAULT_HEADERS, "apptoken": self.app_token,
                        "User-Agent": user_agent}
        self._req_seq = 0
        # 不验证书链里的 hostname 严格性（沙箱环境）
        self._ssl_ctx = ssl.create_default_context()

    def _rotate_region(self) -> bool:
        """
        M4 4.5：切到下一个候选 region host。
        返回 False 表示已无候选。
        """
        if not self.try_regions_fallback:
            return False
        if self._current_region_idx + 1 >= len(self._region_candidates):
            return False
        self._current_region_idx += 1
        new_host = self._region_candidates[self._current_region_idx]
        try:
            validated = validate_region_host("https://" + new_host)
        except ZeppError:
            return False
        self.base_url = validated.rstrip("/")
        return True

    @property
    def current_region(self) -> str:
        """返回当前激活的 region host（调试 / 日志用）"""
        return self.base_url.replace("https://", "")

    # ---- 低层 HTTP ----

    def _request_id(self):
        self._req_seq += 1
        return f"ZEPCLI-{self._req_seq:016X}"

    def _get(self, path, params=None):
        """发 GET 请求，返回 JSON（dict/list）或原始 bytes。

        完全对齐 ZeppBridge get_json：
        - 重试预算 3 次，redirect 预算 5 次分开记
        - 401/403 → NeedsReauth
        - 404 → Unavailable
        - 429/500-599 → RetryExhausted
        - code != 1 → CloudRejected

        M4 章节 4.5：try_regions_fallback=True 时，
        整个 _get 走完后若都失败（NetworkError / RetryExhausted），
        切到下一个 region host 重试 1 次。
        """
        if not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ZeppError(f"非法 path: {path}")
        # M4 4.5：region-fallback 外层循环（尝试多个 host 各做一轮 _get 内部重试）
        region_attempts = len(self._region_candidates) if self.try_regions_fallback else 1
        last_region_exception = None
        for region_idx in range(region_attempts):
            if region_idx > 0:
                # 切到下一个 region host
                if not self._rotate_region():
                    break
            url = self.base_url + path
            query = list((params or {}).items())
            last_status = None

            for attempt in range(MAX_ATTEMPTS):
                q = list(query)
                q.append(("r", self._request_id()))
                full = url + "?" + urlencode(q)
                req = Request(full, headers=self.headers)
                try:
                    with urlopen(req, timeout=DEFAULT_TIMEOUT, context=self._ssl_ctx) as resp:
                        body = resp.read(MAX_BODY_BYTES + 1)
                        if len(body) > MAX_BODY_BYTES:
                            raise ZeppError("响应过大")
                        # 业务信封检查
                        try:
                            j = json.loads(body)
                        except json.JSONDecodeError:
                            # 不是 JSON，按 bytes 返回
                            return body
                        if isinstance(j, dict) and "code" in j and "data" in j:
                            code = j.get("code")
                            if code != SUCCESS_CODE:
                                raise CloudRejected(code, j.get("message", "(no message)"))
                            return j.get("data", j)
                        return j
                except HTTPError as e:
                    last_status = e.code
                    if e.code in (401, 403):
                        raise NeedsReauth(f"HTTP {e.code}: {e.reason}")
                    if e.code == 404:
                        raise Unavailable(f"HTTP {e.code}: {e.reason}")
                    if e.code == 429 or 500 <= e.code <= 599:
                        wait_ms = exponential_backoff_ms(attempt, RETRY_BACKOFF_MS[min(attempt, len(RETRY_BACKOFF_MS)-1)])
                        if attempt < MAX_ATTEMPTS - 1:
                            time.sleep(wait_ms / 1000)
                            continue
                        last_region_exception = RetryExhausted(f"HTTP {e.code}: {e.reason}")
                        break  # 跳出内层，去尝试下一个 region
                    raise ZeppError(f"HTTP {e.code}: {e.reason}")
                except (URLError, TimeoutError, OSError) as e:
                    if attempt < MAX_ATTEMPTS - 1:
                        time.sleep(exponential_backoff_ms(attempt, RETRY_BACKOFF_MS[min(attempt, len(RETRY_BACKOFF_MS)-1)]) / 1000)
                        continue
                    last_region_exception = NetworkError(str(e))
                    break  # 跳出内层，去尝试下一个 region

        # 全部 region 都失败
        if last_region_exception:
            raise last_region_exception
        raise RetryExhausted(f"HTTP {last_status or 503}")

    # ---- 设备 ----

    def devices(self):
        """获取账号下所有绑定设备"""
        return self._get(f"/users/{self.user_id}/devices",
                         {"enableMultiDevice": "true", "device_type": "android_phone"})

    def members(self):
        """获取账号成员列表（体脂秤共享的人）"""
        return self._get(f"/users/{self.user_id}/members")

    # ---- 心率 ----

    def heart_rate(self, from_seconds, to_seconds, limit=1000, hr_type=2):
        """心率。type=2 自动测，type=1000 运动中

        参数单位：**秒**。自动检测毫秒传入（|val| >= 10^10）并转换。
        调用方也可以显式传毫秒，老代码兼容。
        """
        f_sec = _to_seconds(from_seconds)
        t_sec = _to_seconds(to_seconds)
        if f_sec is None or t_sec is None:
            raise ZeppError(f"heart_rate 时间参数无效: from={from_seconds}, to={to_seconds}")
        # Zepp API 要求**毫秒**（实测 startTime/endTime 都是 13 位毫秒戳）
        from_ms = int(round(f_sec * 1000))
        to_ms = int(round(t_sec * 1000))
        return self._get(f"/users/{self.user_id}/heartRate",
                         {"startTime": str(from_ms), "endTime": str(to_ms),
                          "limit": str(limit), "type": str(hr_type)})

    # ---- 体重 / 体成分 ----

    def weight_records(self, member_id="-1", from_seconds=None, to_seconds=None, limit=300):
        """体重 / 体成分。注意时间单位是**秒**（其他流都是毫秒）。

        参数单位：**秒**。自动检测毫秒传入并转换（兼容老代码）。
        """
        now = int(time.time())
        if from_seconds is None:
            from_seconds = now - 365 * 86400
        if to_seconds is None:
            to_seconds = now
        f_sec = _to_seconds(from_seconds)
        t_sec = _to_seconds(to_seconds)
        if f_sec is None or t_sec is None:
            raise ZeppError(f"weight_records 时间参数无效: from={from_seconds}, to={to_seconds}")
        # Zepp API 要求**秒**（实测 fromTime/toTime 都是 10 位秒戳）
        from_s = int(round(f_sec))
        to_s = int(round(t_sec))
        return self._get(f"/users/{self.user_id}/members/{member_id}/weightRecords",
                         {"fromTime": str(from_s), "toTime": str(to_s),
                          "limit": str(limit), "isForward": "0"})

    # ---- 手环原始数据 / 睡眠 ----

    def band_data(self, from_date, to_date, query_type="detail", byte_length=8, device_type=0):
        """手环原始数据（可能压缩）。query_type: detail/summary。"""
        return self._get("/v1/data/band_data.json",
                         {"userid": self.user_id, "from_date": from_date,
                          "to_date": to_date, "query_type": query_type,
                          "byteLength": str(byte_length), "device_type": str(device_type)})

    # ---- 运动 ----

    def sport_history(self, sport="run", start_track_id=0, stop_track_id=9999999999,
                      need_sub_data=1):
        """运动历史。sport: run/walking/ride/swimming 等"""
        return self._get(f"/v1/sport/{sport}/history.json",
                         {"userid": self.user_id,
                          "startTrackId": str(start_track_id),
                          "stopTrackId": str(stop_track_id),
                          "need_sub_data": str(need_sub_data),
                          "type": ""})

    def sport_detail(self, track_id, source="gpx"):
        """单次运动详情（GPS/心率/逐秒采样）"""
        return self._get("/v1/sport/run/detail.json",
                         {"trackid": str(track_id), "source": source})

    # ---- 训练统计 ----

    def watch_statistics(self, statistic, start_day, end_day, limit=100, reverse=True):
        """statistic: SPORT_LOAD 或 VO2_MAX"""
        if statistic not in ("SPORT_LOAD", "VO2_MAX"):
            raise ZeppError(f"statistic 必须是 SPORT_LOAD/VO2_MAX，得到 {statistic}")
        return self._get(f"/v2/watch/users/{self.user_id}/WatchSportStatistics/{statistic}",
                         {"startDay": start_day, "endDay": end_day,
                          "limit": str(limit), "isReverse": "true" if reverse else "false"})

    # ---- 事件流（三套）----

    def v2_events(self, event_type, sub_type, from_ms, to_ms, limit=50, reverse=True):
        """v2 events 流: /v2/users/me/events"""
        params = {"eventType": event_type, "from": str(from_ms), "to": str(to_ms),
                  "limit": str(limit), "reverse": "1" if reverse else "0"}
        if sub_type:
            params["subType"] = sub_type
        return self._get("/v2/users/me/events", params)

    def user_events(self, event_type, sub_type, from_ms, to_ms, limit=50, reverse=True):
        """user events 流: /users/{id}/events（PAI/全天压力/SpO2 等）"""
        params = {"eventType": event_type, "from": str(from_ms), "to": str(to_ms),
                  "limit": str(limit), "reverse": "1" if reverse else "0",
                  "userId": self.user_id}
        if sub_type:
            params["subType"] = sub_type
        return self._get(f"/users/{self.user_id}/events", params)

    def user_events_date_string(self, event_type, sub_type, from_iso, to_iso,
                                 time_zone="Asia/Shanghai", limit=50):
        """dateString 流: /users/{id}/events/dateString（夜间 SpO2 odi/osa_event 等）"""
        return self._get(f"/users/{self.user_id}/events/dateString",
                         {"eventType": event_type, "subType": sub_type,
                          "from": from_iso, "to": to_iso, "timeZone": time_zone,
                          "limit": str(limit), "reverse": "0",
                          "userId": self.user_id})

    def file_info_events(self, event_type, sub_type, from_ms, to_ms, limit=50):
        """fileInfo 流: /users/me/fileInfo/events（秒级数据文件索引）"""
        return self._get("/users/me/fileInfo/events",
                         {"eventType": event_type, "subType": sub_type,
                          "from": str(from_ms), "to": str(to_ms),
                          "limit": str(limit)})


# ===== 便捷时间工具 =====

def now_ms():
    return int(time.time() * 1000)


def days_ago_ms(n):
    return now_ms() - n * 86400 * 1000


def _to_seconds(value):
    """
    智能检测毫秒 vs 秒，返回统一单位（秒）。

    判定规则：绝对值 ≥ 10^10 → 毫秒，否则秒。
    2026-09-23 当前毫秒 = 1_758_000_000_000（约 1.7e12），远大于 10^10。
    2026-09-23 当前秒  = 1_758_000_000（约 1.7e9），小于 10^10。

    兼容：
      - int / float
      - 数字字符串（如 "1758000000000"）
      - None / 非数字 → 返回 None（不抛异常，让上层决定 fallback）

    用法：
      ts = _to_seconds(from_seconds_or_ms)
      if ts is None:
          ts = time.time() - 86400
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None  # bool 是 int 子类，但永远不该当时间戳
    if isinstance(value, (int, float)):
        n = float(value)
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        try:
            n = float(s)
        except ValueError:
            return None
    else:
        return None

    if n != n or abs(n) == float("inf"):  # NaN / Inf
        return None
    if abs(n) >= 10_000_000_000:  # >= 10^10 → 毫秒
        return n / 1000.0
    return n  # 秒
