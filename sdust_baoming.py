# -*- coding: utf-8 -*-
"""
山东科技大学 第二课堂 自动报名脚本（电脑 / 安卓 Termux / Pydroid 3 均可运行）
运行：python sdust_baoming.py
依赖：pip install requests
"""
import re
import json
import time
import getpass
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

# ==================== 固定配置 ====================
MYHOST = "77726476706e69737468656265737421fdee0f8f23257b44300d8db9d6562d"
EKHOST = "77726476706e69737468656265737421f5fc559d69236c456d1cc7a99c406d3661"
WEBVPN = "https://webvpn.sdust.edu.cn"
CAS = WEBVPN + "/http/" + MYHOST + "/cas"
API = WEBVPN + "/https/" + EKHOST + "/api/app/client/v1"
SCHOOL_CODE = 10424
UA = ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")

# 活动状态码：4=待报名（未开始）、5=报名中、6=待开始、7=进行中、8=已结束
STATUS_NOT_STARTED = 4
STATUS_ENROLLING = 5
# 当前用户对活动的报名状态 applyStatus：2=已报名，3=未报名
APPLY_ENROLLED = 2
SIGN_LIMIT = {0: "不限", 1: "个人报名", 2: "团队报名"}
LIMIT_TYPE = {0: "不限", 1: "必修", 2: "选修"}
GENDER = {0: "不限", 1: "男", 2: "女"}


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def input_password(prompt):
    """输入密码时逐字符显示 *，支持退格；非 Windows 环境自动回退"""
    print(prompt, end="", flush=True)
    try:
        import msvcrt
        pw = ""
        while True:
            ch = msvcrt.getch()
            if ch in (b"\r", b"\n"):
                print()
                return pw
            if ch == b"\x08":  # 退格
                if pw:
                    pw = pw[:-1]
                    print("\b \b", end="", flush=True)
                continue
            if ch in (b"\x00", b"\xe0"):  # 功能键/方向键，丢弃
                msvcrt.getch()
                continue
            if ch == b"\x03":  # Ctrl+C
                raise KeyboardInterrupt
            c = ch.decode("utf-8", "ignore") or ch.decode("latin-1", "ignore")
            pw += c
            print("*", end="", flush=True)
    except ImportError:
        try:
            return getpass.getpass("")
        except Exception:
            return input("请输入统一认证密码：")


def ms_to_str(ms):
    if not ms:
        return "未知"
    return datetime.fromtimestamp(ms / 1000.0).strftime("%Y-%m-%d %H:%M:%S")


# ==================== 核心客户端 ====================
class SdustClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.token = None
        self.user_id = None
        self.college_name = None
        self.college_id = None
        self.grade = None
        self.time_offset = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA})
        self._relogin_lock = threading.Lock()
        self._relogining = False

    def _encrypt_pwd(self, pub_n, pub_e):
        raw = int.from_bytes(self.password[::-1].encode("latin-1"), "little")
        return format(pow(raw, pub_e, pub_n), "x")

    def login(self, max_retry=5):
        for attempt in range(1, max_retry + 1):
            try:
                self._do_login()
                self._calibrate_time()
                return
            except Exception as ex:
                log("第 %d 次登录失败：%s" % (attempt, ex))
                self.session = requests.Session()
                self.session.headers.update({"User-Agent": UA})
                time.sleep(1.5)
        raise RuntimeError("多次登录仍失败，请检查账号密码或网络")

    def _do_login(self):
        s = self.session
        service = WEBVPN + "/login?cas_login=true"
        r = s.get(CAS + "/login", params={"service": service}, timeout=25)
        m = re.search(r'name="execution"[^>]*value="([^"]+)"', r.text)
        if not m:
            raise RuntimeError("未取到 execution")
        execution = m.group(1)

        pk = s.get(CAS + "/v2/getPubKey", timeout=25).json()
        enc = self._encrypt_pwd(int(pk["modulus"], 16),
                                int(pk["exponent"], 16))
        r2 = s.post(CAS + "/login", params={"service": service},
                    data={"username": self.username, "password": enc,
                          "execution": execution, "_eventId": "submit"},
                    allow_redirects=False, timeout=25)
        url = r2.headers.get("Location")
        if not url:
            raise RuntimeError("CAS 未跳转（服务端抖动）")

        for _ in range(8):
            full = url if url.startswith("http") else WEBVPN + url
            rr = s.get(full, allow_redirects=False, timeout=25)
            url = rr.headers.get("Location")
            if not url:
                break

        ekt_service = "https://ekta.sdust.edu.cn/sdustlogin"
        cas_url = CAS + "/login?service=" + requests.utils.quote(
            ekt_service, safe="")
        rr = s.get(cas_url, allow_redirects=False, timeout=25)
        loc = rr.headers.get("Location", "")
        tm = re.search(r"ticket=([^&]+)", loc)
        if not tm:
            raise RuntimeError("未取到第二课堂 ticket")
        ticket = tm.group(1)

        path_all = (WEBVPN + "/https/" + EKHOST +
                    "/sdustlogin?ticket=" + ticket)
        j3 = self._get("user/sdustSSOAuth", {
            "targetUrl": None, "ticket": ticket,
            "routeUrl": "/sdustlogin", "pathAll": path_all}).json()
        d3 = j3["data"]
        j4 = self._get("third/token", {
            "schoolCode": SCHOOL_CODE, "code": d3["code"],
            "timestamp": d3["timestamp"], "sign": d3["sign"]}).json()
        self.token = j4["data"]["access_token"]

        info = self._get("student/user/my-info", {},
                         self.token).json()["data"]
        self.user_id = info["id"]
        self.college_name = info.get("collegeName")
        self.grade = info.get("grade")
        log("登录成功：%s（%s）学号 %s" % (
            info.get("name"), info.get("className"), info.get("code")))

    def _calibrate_time(self):
        local_before = time.time()
        r = self._get("dict/school/system-para/list", {}, self.token)
        local_after = time.time()
        date = r.headers.get("Date")
        if date:
            server = datetime.strptime(
                date, "%a, %d %b %Y %H:%M:%S %Z").timestamp()
            self.time_offset = server - (local_before + local_after) / 2
        try:
            ts = r.json().get("timestamp")
            if ts:
                self.time_offset = ts / 1000.0 - local_after
        except Exception:
            pass
        log("时间校准完成，与服务器偏差 %.2f 秒" % self.time_offset)

    def server_time(self):
        return time.time() + self.time_offset

    def _get(self, path, params, token=None):
        body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        headers = {"Authorization": token} if token else {}
        return self.session.get(API + "/" + path.lstrip("/"),
                                params={"params": body},
                                headers=headers, timeout=25)

    def _post(self, path, params, token):
        body = json.dumps(params, separators=(",", ":"), ensure_ascii=False)
        return self.session.post(
            API + "/" + path.lstrip("/"), data={"params": body},
            headers={"Authorization": token,
                     "Content-Type": "application/x-www-form-urlencoded"},
            timeout=25)

    def _ensure_token(self):
        with self._relogin_lock:
            if self._relogining:
                while self._relogining:
                    time.sleep(0.05)
                return
            self._relogining = True
        try:
            log("Token 失效，重新登录…")
            self.login()
        finally:
            self._relogining = False

    def biz_post(self, path, params):
        r = self._post(path, params, self.token)
        j = r.json()
        if j.get("code") == 10001:
            self._ensure_token()
            j = self._post(path, params, self.token).json()
        return j

    def biz_get(self, path, params):
        r = self._get(path, params, self.token)
        j = r.json()
        if j.get("code") == 10001:
            self._ensure_token()
            j = self._get(path, params, self.token).json()
        return j


# ==================== 模块与活动读取 ====================
def fetch_modules(client):
    j = client.biz_get("dict/activity/classify/list", {"parentId": 0})
    return j.get("data") or []


def activity_state_text(a):
    """活动当前可报状态文案"""
    st = a.get("activityStatus")
    if st == STATUS_NOT_STARTED:
        return "未开始报名"
    if st == STATUS_ENROLLING:
        return "报名中（可报）"
    return "其他"


def is_available(a):
    """未开始报名，或报名中且人数没满"""
    st = a.get("activityStatus")
    if st == STATUS_NOT_STARTED:
        return True
    if st == STATUS_ENROLLING:
        limit = a.get("peopleLimit") or 0
        if not limit:  # 不限人数
            return True
        return (a.get("enrollCount") or 0) < limit
    return False


def fetch_not_started(client, classify_id):
    """读取该模块可报活动：未开始报名(状态4)或报名中未满(状态5)"""
    rows, page = [], 1
    while True:
        j = client.biz_get("page/activity/list", {
            "search": "", "pageNum": page, "pageSize": 50, "sortType": 1,
            "classifyId": classify_id, "status": "4,5,6,7", "level": 0,
            "organizationName": "", "fieldType": "activity_iseErollment",
            "keyWord": "switch"})
        d = j.get("data") or {}
        part = d.get("list") or []
        rows += part
        if not part or d.get("lastPage"):
            break
        page += 1

    result = [a for a in rows if is_available(a)]
    result.sort(key=lambda a: (a.get("enrollStartTime") or 0))
    return result


def enrich_sign_limit(client, activities):
    """并发查详情，补充报名方式 signLimit（列表接口不返回该字段）"""
    cache = {}

    def one(a):
        aid = str(a.get("id"))
        try:
            d = client.biz_get("activity/detail/participant",
                               {"id": aid}).get("data") or {}
            cache[aid] = d.get("signLimit")
        except Exception:
            cache[aid] = None

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(ex.map(one, activities))
    return cache


def parse_ids(text):
    out = []
    for p in re.split(r"[;；,，\s]+", text.strip()):
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def resolve_college_id(client, college_name):
    """根据学院名查学院 id；匹配不到返回 None"""
    if not college_name:
        return None
    try:
        j = client.biz_get("dict/college/list", {})
        for c in j.get("data") or []:
            if c.get("name") == college_name:
                return c.get("id")
    except Exception:
        pass
    return None


def match_user(a, college_id, grade):
    """活动是否符合当前用户的学院与年级限制；返回 False 表示应剔除"""
    cl = str(a.get("collegeLimit") or "0")
    if cl not in ("0", "") and college_id is not None:
        allow = {x for x in cl.split(",") if x}
        if str(college_id) not in allow:
            return False
    gl = str(a.get("gradeLimit") or "0")
    if gl not in ("0", "") and grade is not None:
        allow = {x for x in gl.split(",") if x}
        if str(grade) not in allow:
            return False
    return True


def _vlen(s):
    """字符串显示宽度（中文算 2，其余算 1）"""
    return sum(2 if ord(ch) > 127 else 1 for ch in str(s))


def _fit(s, width):
    """截断/补齐到指定显示宽度"""
    s = str(s)
    if _vlen(s) > width:
        out, w = "", 0
        for ch in s:
            cw = 2 if ord(ch) > 127 else 1
            if w + cw > width - 1:
                out += "…"
                break
            out += ch
            w += cw
        s = out
    return s + " " * (width - _vlen(s))


def print_activity_table(rows, sign_cache):
    """三线表打印活动列表"""
    headers = [("活动ID", 8), ("活动名称", 28), ("报名开始时间", 21),
               ("报名结束时间", 21), ("报名限制", 22), ("已报名人数", 10)]
    head_line = "| " + " | ".join(
        _fit(h, w) for h, w in headers) + " |"
    bar = "=" * len(head_line)
    sep = "-" * len(head_line)
    print(bar)
    print(head_line)
    print(sep)
    for a in rows:
        aid = str(a.get("id"))
        sign = SIGN_LIMIT.get(sign_cache.get(aid), "未知")
        rest = limit_desc(a)
        limit_cell = sign if rest == "不限" else "%s·%s" % (sign, rest)
        cells = [
            aid,
            a.get("name") or "",
            ms_to_str(a.get("enrollStartTime")),
            ms_to_str(a.get("enrollEndTime")),
            limit_cell,
            str(a.get("enrollCount") or 0),
        ]
        print("| " + " | ".join(
            _fit(c, w) for c, (_, w) in zip(cells, headers)) + " |")
    print(bar)


def get_detail(client, activity_id):
    return client.biz_get("activity/detail/participant", {"id": activity_id})


def submit_enroll(client, activity_id):
    # 真实个人报名接口（抓包确认）：POST activity/enroll/person
    # 参数为活动 id 与报名材料（无额外材料时为空数组），无需 personId
    return client.biz_post("activity/enroll/person", {
        "id": str(activity_id),
        "personMaterial": []})


def confirm_enrolled(client, activity_id):
    # 详情接口不返回用户 id，应通过 applyStatus 判断当前用户是否已报名：
    # applyStatus=2 表示已报名（含待审核/已通过），3 表示未报名
    try:
        j = get_detail(client, activity_id)
    except Exception:
        return False
    d = j.get("data") or {}
    return d.get("applyStatus") == APPLY_ENROLLED


# ==================== 抢报引擎 ====================
def wait_until(client, fire_ts, activity_id):
    while True:
        remain = fire_ts - client.server_time()
        if remain <= 0:
            return
        if remain > 120:
            log("[%s] 距开抢还有 %.0f 秒，等待中…" % (activity_id, remain))
            time.sleep(min(30, remain - 90))
        elif remain > 5:
            with client._relogin_lock:
                client.login()
            log("[%s] 临近开抢已刷新登录，倒计时 %.1f 秒" % (
                activity_id, remain))
            time.sleep(remain - 5)
        else:
            while client.server_time() < fire_ts:
                pass
            return


def grab(client, activity_id, workers=8, duration=60):
    stop = threading.Event()
    result = {"ok": False, "msg": "", "count": 0}
    counter_lock = threading.Lock()

    def worker():
        while not stop.is_set():
            try:
                j = submit_enroll(client, activity_id)
                with counter_lock:
                    result["count"] += 1
                already = j.get("code") == 0 or "已报名" in (j.get("msg") or "")
                if already:
                    if confirm_enrolled(client, activity_id):
                        result["ok"] = True
                        result["msg"] = j.get("msg", "ok")
                        stop.set()
                        return
                    if j.get("code") == 0:
                        time.sleep(0.05)
                else:
                    result["msg"] = "%s %s" % (j.get("code"), j.get("msg", ""))
            except Exception as ex:
                result["msg"] = str(ex)
                time.sleep(0.1)

    log("[%s] 开抢！%d 线程高频提交，持续 %d 秒…" % (
        activity_id, workers, duration))
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(worker) for _ in range(workers)]
        while time.time() - start < duration and not stop.is_set():
            time.sleep(0.2)
        stop.set()
        for f in futs:
            f.cancel()
    log("[%s] 共提交 %d 次，最后状态：%s" % (
        activity_id, result["count"], result["msg"]))
    return result["ok"]


class GrabTask(threading.Thread):
    def __init__(self, client, activity_id, fire_ts, workers):
        super().__init__(daemon=True)
        self.client = client
        self.activity_id = str(activity_id)
        self.fire_ts = fire_ts
        self.workers = workers
        self.ok = False
        self.msg = ""

    def run(self):
        try:
            wait_until(self.client, self.fire_ts, self.activity_id)
            self.ok = grab(self.client, self.activity_id,
                           workers=self.workers)
        except Exception as ex:
            self.msg = str(ex)


# ==================== 交互主流程 ====================
def parse_time(s):
    return datetime.strptime(s.strip(), "%Y-%m-%d %H:%M:%S").timestamp()


def pick_module_letters(modules):
    """显示模块菜单并解析选择，支持多模块（分号隔开）"""
    print("\n===== 活动模块 =====")
    letters = []
    for i, mobj in enumerate(modules):
        letter = chr(65 + i)
        letters.append(letter)
        print("%s. %s" % (letter, mobj.get("name")))
    raw = input("请选择模块字母（可多选，用分号隔开，如 A;C）：").strip().upper()
    picked = []
    for token in re.split(r"[;；,，\s]+", raw):
        if token in letters and token not in picked:
            picked.append(token)
    return picked, letters


def limit_desc(a):
    """汇总报名限制条件"""
    parts = []
    lt = a.get("limitType")
    if lt in (1, 2):
        parts.append(LIMIT_TYPE.get(lt))
    g = a.get("genderLimit")
    if g in (1, 2):
        parts.append("限" + str(GENDER.get(g)))
    pl = a.get("peopleLimit")
    if pl:
        parts.append("上限%d人" % pl)
    for field, label in [("collegeLimit", "限学院"),
                         ("gradeLimit", "限年级"),
                         ("organizationLimit", "限组织")]:
        v = a.get(field)
        if v and str(v) != "0":
            parts.append(label)
    return "、".join(parts) if parts else "不限"


def main():
    print("=" * 52)
    print("   山东科技大学 第二课堂 自动报名工具（多活动版）")
    print("=" * 52)
    username = input("请输入学号：").strip()
    password = input_password("请输入统一认证密码（输入时不显示）：")
    if not username or not password:
        print("账号密码不能为空")
        return

    client = SdustClient(username, password)
    client.login()
    client.college_id = resolve_college_id(client, client.college_name)
    log("识别到用户：%s（%s级）%s" % (
        client.college_name, client.grade,
        "，学院ID=%s" % client.college_id if client.college_id else
        "，未能匹配学院ID，将不按学院过滤"))

    # 选择模块（可多选），所选模块均无活动则重新选择
    modules = fetch_modules(client)
    if not modules:
        raise RuntimeError("未读取到活动模块")
    module_map = {chr(65 + i): m for i, m in enumerate(modules)}

    while True:
        # 选择模块（可多选），所选模块均无活动则重新选择
        while True:
            picked, letters = pick_module_letters(modules)
            if not picked:
                print("输入无效，请重新选择。")
                continue
            acts = []
            for letter in picked:
                mobj = module_map[letter]
                part = fetch_not_started(client, mobj.get("id"))
                if not part:
                    print("【%s】该模块没有活动。" % mobj.get("name"))
                    continue
                keep = [a for a in part
                        if match_user(a, client.college_id, client.grade)]
                hidden = len(part) - len(keep)
                if hidden:
                    print("【%s】%d 个活动因学院/年级不符合，已隐藏" % (
                        mobj.get("name"), hidden))
                for a in keep:
                    a["_letter"] = letter
                    acts.append(a)
            if not acts:
                print("所选模块均没有活动，请重新选择模块。")
                continue
            # 先按报名开始时间由近到远，时间相同再按模块标号排序
            acts.sort(key=lambda a: (a.get("enrollStartTime") or 0,
                                     a["_letter"]))
            break

        print("正在补充各活动报名方式…")
        sign_cache = enrich_sign_limit(client, acts)
        id_map = {}
        print("-" * 52)
        for a in acts:
            aid = str(a.get("id"))
            id_map[aid] = a
            sign = SIGN_LIMIT.get(sign_cache.get(aid), "未知")
            print("活动 ID：%s ｜ 模块：%s ｜ 状态：%s" % (
                aid, a["_letter"], activity_state_text(a)))
            print("  名称：%s" % a.get("name"))
            print("  报名开始：%s" % ms_to_str(a.get("enrollStartTime")))
            print("  报名结束：%s" % ms_to_str(a.get("enrollEndTime")))
            print("  报名方式：%s ｜ 报名限制：%s ｜ 已报：%s" % (
                sign, limit_desc(a), a.get("enrollCount")))
            print("-" * 52)

        raw = input("请输入要抢的活动 ID（多个用分号 ; 隔开；"
                    "输入 N 返回重选模块）：").strip()
        if raw.upper() == "N":
            print()
            continue
        target_ids = parse_ids(raw)
        if not target_ids:
            print("未输入有效 ID，请重新输入。")
            continue
        bad = [i for i in target_ids if i not in id_map]
        if bad:
            print("以下 ID 不在列表中：%s，请重新输入。" % "、".join(bad))
            continue
        break
    log("本次目标 %d 个：%s" % (len(target_ids), "、".join(target_ids)))

    tasks_ts = {}
    for aid in target_ids:
        a = id_map.get(aid)
        est = a.get("enrollStartTime") if a else None
        if est:
            tasks_ts[aid] = est / 1000.0
        else:
            t = input("活动 %s 未取到报名时间，请输入开抢时间"
                      "(YYYY-MM-DD HH:MM:SS)：" % aid).strip()
            tasks_ts[aid] = parse_time(t) - client.time_offset

    workers = input("每个活动提交线程数(默认8，回车默认)：").strip()
    workers = int(workers) if workers.isdigit() else 8

    threads = []
    for aid in target_ids:
        t = GrabTask(client, aid, tasks_ts[aid], workers)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    print("\n===== 结果汇总 =====")
    for t in threads:
        if t.ok:
            log("✓ 活动 %s 报名成功" % t.activity_id)
        else:
            log("✗ 活动 %s 未确认成功 %s" % (t.activity_id, t.msg))
    print("建议打开第二课堂 App 再次核对报名状态。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已手动退出。")
    except Exception as e:
        print("运行出错：%s" % e)
