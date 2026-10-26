#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 积分加油站 · GitHub Actions 云端运行脚本（自包含，无第三方依赖）

设计目标（对照需求）：
  - 云端 runner 不读本机登录态；refresh token 仅从环境变量（GitHub Secret）读取。
  - 同一次运行内先「刷新令牌 → 签到 → 派猫猫旅行（先领后派）」，再推送微信（PushPlus）。
  - 签到与猫猫状态在微信 markdown 正文中分两节展示，一眼能分清。
  - 猫猫部分失败不影响签到结论；只要签到成功，整体即成功（退出码 0）。
  - token 明文绝不出现在 stdout / 日志 / 推送内容中（仅保留掩码）。
  - 接口路径以本机实测 / 官方返回为准（已核对，非网上流传旧名）：
      * 刷新：POST https://copilot.tencent.com/v2/plugin/auth/token/refresh
      * 签到状态：POST https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status
      * 签到领取：POST https://www.codebuddy.cn/v2/billing/meter/daily-checkin
      * 派猫状态：GET  https://www.workbuddy.cn/activity/growth/buddy/travel/status
      * 派猫配置：GET  https://www.workbuddy.cn/activity/growth/buddy/travel/config
      * 派猫领取：POST https://www.workbuddy.cn/activity/growth/buddy/travel/claim
      * 派猫派遣：POST https://www.workbuddy.cn/activity/growth/buddy/travel/depart
    注意：签到走 codebuddy 域名 + /v2 前缀；派猫走 workbuddy 域名且无 /v2 前缀，写错一律 404。

退出码：签到成功 0 / 签到失败 1（猫猫失败不计入）。
"""

import json
import os
import sys
import time
import random
import urllib.request
import urllib.error
import urllib.parse

# ----------------------------------------------------------------------------
# 真实接口常量（已在本机核对，非网上流传旧名）
# ----------------------------------------------------------------------------
PLUGIN_API = "https://copilot.tencent.com"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

BILLING_BASE = "https://www.codebuddy.cn/v2"
STATUS_PATH = "/billing/meter/checkin-activity-status"
CHECKIN_PATH = "/billing/meter/daily-checkin"

TRAVEL_BASE = "https://www.workbuddy.cn"
TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
TRAVEL_CONFIG_PATH = "/activity/growth/buddy/travel/config"
TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"
TRAVEL_DEPART_PATH = "/activity/growth/buddy/travel/depart"

TRAVEL_STATE_TEXT = {
    "idle": "空闲（可派遣）",
    "traveling": "旅行中",
    "arrived": "已到达，待领取",
}

HTTP_TIMEOUT = 15
UA = "WorkBuddy-Score-GHAction/1.0"


# ----------------------------------------------------------------------------
# 工具
# ----------------------------------------------------------------------------
def log(msg):
    # 仅输出运行进度与结论，绝不写入 token
    sys.stdout.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def mask_token(t):
    if not t:
        return "<empty>"
    return t[:6] + "..." + t[-4:]


def _fmt_duration(seconds):
    if not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return "%d 小时 %d 分" % (h, m)
    if m:
        return "%d 分" % m
    return "%d 秒" % seconds


def api_call(base, path, token, payload=None, method="POST"):
    """标准库 HTTP 调用。GET 且无 payload 时不带请求体。返回 (http_status, body)。"""
    url = base + path
    if method == "GET" and payload is None:
        data = None
    else:
        data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8", "replace")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, {"raw": body}
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(body)
            except json.JSONDecodeError:
                return e.code, {"raw": body}
        except Exception:
            return e.code, {"raw": ""}
    except Exception as e:
        return -1, {"error": str(e)[:200]}


def refresh_token(rt):
    """用长效 refresh token 换取新的接口令牌。返回 (access_token, new_refresh_token)。"""
    req = urllib.request.Request(PLUGIN_API + REFRESH_PATH, data=b"{}", method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Refresh-Token", rt)
    req.add_header("X-Auth-Refresh-Source", "plugin")
    req.add_header("X-Domain", "copilot.tencent.com")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            body = {"code": -1, "msg": "HTTP %s" % e.code}
    except Exception as e:
        body = {"code": -1, "msg": str(e)[:200]}
    if body.get("code") != 0 or not isinstance(body.get("data"), dict):
        raise RuntimeError("刷新令牌失败 code=%s %s" % (body.get("code"), body.get("msg") or ""))
    return body["data"].get("accessToken") or "", body["data"].get("refreshToken") or ""


# ----------------------------------------------------------------------------
# 签到
# ----------------------------------------------------------------------------
def _extract_balance(*bodies):
    candidates = ("total_credits", "total_credit", "total_credit_balance",
                  "total_points", "points_balance", "credit_balance",
                  "balance", "remain_credit", "data.total_credits",
                  "data.balance", "data.total")
    for b in bodies:
        if not isinstance(b, dict):
            continue
        for key in candidates:
            node = b
            ok = True
            for part in key.split("."):
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    ok = False
                    break
            if ok and isinstance(node, (int, float)):
                return node
    return None


def do_checkin(token):
    """查询状态 → 必要时领取。返回结构化结果。"""
    res = {"ok": False, "action": None, "points": None,
           "balance": None, "msg": "", "detail": {}}

    st_code, st_body = api_call(BILLING_BASE, STATUS_PATH, token)
    res["detail"]["status_http"] = st_code
    res["detail"]["status_resp"] = st_body  # 已脱敏：不含 token
    res["balance"] = _extract_balance(st_body)
    res["detail"]["balance"] = res["balance"]

    today_signed = False
    if isinstance(st_body, dict):
        if st_body.get("today_checked_in") is True:
            today_signed = True
        elif isinstance(st_body.get("data"), dict) and st_body["data"].get("today_checked_in") is True:
            today_signed = True
        elif str(st_body.get("code")) == "10001":
            today_signed = True

    if today_signed:
        bal = ("，当前积分余额 %s" % res["balance"]) if res.get("balance") is not None else ""
        res.update(ok=True, action="skip_already_signed",
                   msg="今日已签到，无需重复领取" + bal)
        return res

    ck_code, ck_body = api_call(BILLING_BASE, CHECKIN_PATH, token)
    res["detail"]["checkin_http"] = ck_code
    res["detail"]["checkin_resp"] = ck_body

    if not isinstance(ck_body, dict):
        res.update(action="failed", msg="领取接口返回非 JSON: %s" % ck_body.get("raw", "")[:200])
        return res

    code = str(ck_body.get("code", ""))
    msg = ck_body.get("msg") or ck_body.get("message") or ""
    data = ck_body.get("data") if isinstance(ck_body.get("data"), dict) else {}
    if code == "10001" or "已签到" in msg or "今天已签到" in msg:
        res.update(ok=True, action="skip_already_signed", msg="今日已签到（接口返回 code=10001）")
        return res
    success_code = code in ("", "0", "200")
    if 200 <= ck_code < 300 and success_code:
        credit = (ck_body.get("credit") or data.get("credit")
                  or data.get("daily_credit") or data.get("today_credit"))
        streak = ck_body.get("streak_days") or data.get("streak_days")
        bbal = _extract_balance(ck_body)
        if bbal is not None:
            res["balance"] = bbal
            res["detail"]["balance"] = bbal
        bal = ("，当前积分余额 %s" % res["balance"]) if res.get("balance") is not None else ""
        streak_txt = ("，连续第 %s 天" % streak) if streak else ""
        res.update(ok=True, action="clicked", points=credit,
                   msg="领取成功" + (("，+%s 积分" % credit) if credit else "") + streak_txt + bal)
        res["detail"]["streak_days"] = streak
        return res

    res.update(action="failed", msg=msg or ("HTTP %s（业务码 %s）" % (ck_code, code)))
    return res


# ----------------------------------------------------------------------------
# 派猫猫旅行（先领后派；失败不影响签到结论）
# ----------------------------------------------------------------------------
def _travel_get(token, path):
    try:
        st, body = api_call(TRAVEL_BASE, path, token, method="GET")
        if st != 200 or not isinstance(body, dict) or body.get("code") != 0:
            return None
        return body.get("data")
    except Exception:
        return None


def _parse_travel(status, config=None):
    if not isinstance(status, dict):
        return {"available": False}
    locations = []
    if isinstance(config, dict) and isinstance(config.get("locations"), list):
        for loc in config["locations"]:
            if isinstance(loc, dict):
                locations.append({"id": loc.get("id"),
                                  "name": loc.get("name") or "未命名地点"})
    state = status.get("state")
    remaining = None
    if state == "traveling":
        arrive, now = status.get("arrive_at"), status.get("server_now")
        if isinstance(arrive, int) and isinstance(now, int):
            remaining = max(0, arrive - now)
    loc = status.get("location")
    return {
        "available": True,
        "state": state,
        "state_text": TRAVEL_STATE_TEXT.get(state, state),
        "location_name": loc.get("name") if isinstance(loc, dict) else None,
        "reward_credit": status.get("reward_credit"),
        "remaining_seconds": remaining,
        "remaining_text": _fmt_duration(remaining) if remaining is not None else None,
        "daily_limit_reached": status.get("daily_limit_reached"),
        "locations": locations,
    }


def _travel_claim(token):
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_CLAIM_PATH, token, payload={})
    except Exception as e:
        return {"action": "failed", "success": False, "message": str(e)}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        credit = (body.get("data") or {}).get("reward_credit")
        return {"action": "claimed", "success": True, "reward_credit": credit,
                "message": "已领取旅行奖励 +%s 积分" % (credit if credit is not None else "?")}
    return {"action": "failed", "success": False,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)}


def _travel_depart(token, location_id=None, locations=None):
    chosen = location_id
    if chosen is None:
        ids = [loc.get("id") for loc in (locations or []) if loc.get("id") is not None]
        if not ids:
            return {"action": "failed", "success": False, "message": "未能获取可选地点列表，已跳过派遣"}
        chosen = random.choice(ids)
    try:
        st, body = api_call(TRAVEL_BASE, TRAVEL_DEPART_PATH, token,
                            payload={"location_id": chosen})
    except Exception as e:
        return {"action": "failed", "success": False, "location_id": chosen, "message": str(e)}
    if st == 200 and isinstance(body, dict) and body.get("code") == 0:
        loc = (body.get("data") or {}).get("location") or {}
        return {"action": "departed", "success": True, "location_id": chosen,
                "location_name": loc.get("name"),
                "message": "已派出 Buddy 前往【%s】" % (loc.get("name") or ("地点 %s" % chosen))}
    return {"action": "failed", "success": False, "location_id": chosen,
            "message": (body.get("msg") if isinstance(body, dict) else None) or ("HTTP %s" % st)}


def run_travel(token, location_id=None):
    """全自动闭环：先领取已到达的，再在空闲且未达上限时派遣。失败安全降级。"""
    out = {"available": False, "ok": True, "log": [], "status": None}
    status = _travel_get(token, TRAVEL_STATUS_PATH)
    if status is None:
        out["ok"] = False
        out["log"].append("查询旅行状态失败（接口不可用/网络异常），已跳过猫猫部分")
        return out

    if status.get("state") == "arrived":
        r = _travel_claim(token)
        out["log"].append(r.get("message") or r.get("action"))
        if r.get("action") == "claimed":
            fresh = _travel_get(token, TRAVEL_STATUS_PATH)
            if fresh is not None:
                status = fresh

    if status.get("daily_limit_reached"):
        out["log"].append("今日派遣次数已用完，跳过派遣")
    elif status.get("state") in (None, "", "idle"):
        config = _travel_get(token, TRAVEL_CONFIG_PATH) or {}
        r = _travel_depart(token, location_id, config.get("locations") or [])
        out["log"].append(r.get("message") or r.get("action"))
    else:
        out["log"].append("Buddy 正在旅行中，无需派遣")

    final = _travel_get(token, TRAVEL_STATUS_PATH) or status
    parsed = _parse_travel(final, _travel_get(token, TRAVEL_CONFIG_PATH))
    out["status"] = parsed
    out["available"] = parsed.get("available", False)
    return out


# ----------------------------------------------------------------------------
# 微信推送（PushPlus：经公众号下发到个人微信；markdown 模板，签到与猫猫分两节展示）
# ----------------------------------------------------------------------------
PUSHPLUS_API = "https://www.pushplus.plus/send"


def build_markdown(checkin, travel, run_at):
    """构建微信 markdown 正文：签到、猫猫两节，用分隔线分开，一眼能分清。"""
    ck = checkin
    ck_head = "✅ 签到成功" if ck["ok"] else "❌ 签到失败"
    ck_lines = [
        "## %s" % ck_head,
        "- **结果**：%s" % (ck.get("msg") or "—"),
    ]
    if ck.get("points") is not None:
        ck_lines.append("- **本次积分**：+%s" % ck["points"])
    if ck.get("balance") is not None:
        ck_lines.append("- **积分余额**：%s" % ck["balance"])
    ck_block = "\n".join(ck_lines)

    if travel.get("available"):
        st = travel["status"] or {}
        t_state = st.get("state_text") or "未知"
        t_loc = st.get("location_name") or "—"
        t_extra = ""
        if st.get("state") == "traveling" and st.get("remaining_text"):
            t_extra = "（还需 %s）" % st["remaining_text"]
        if st.get("state") == "arrived" and st.get("reward_credit") is not None:
            t_extra = "（可领 %s 积分）" % st["reward_credit"]
        t_lines = [
            "## 🐱 猫猫状态",
            "- **状态**：%s" % t_state,
            "- **当前地点**：%s %s" % (t_loc, t_extra),
            "- **本次操作**：%s" % ("；".join(travel.get("log") or ["无"]) or "无"),
        ]
        t_block = "\n".join(t_lines)
    else:
        t_block = ("## 🐱 猫猫状态\n"
                   "- 本次未执行 / 接口不可用（不影响签到结论）\n"
                   "- 说明：%s" % ("；".join(travel.get("log") or ["—"]) or "—"))

    return "%s\n\n---\n\n%s\n\n> 运行时间：%s ｜ 签到成功即整体成功，猫猫失败不影响结论" % (
        ck_block, t_block, run_at)


def push_pushplus(token, content):
    """PushPlus 发送：仅需 token，无加签。成功判定 code==200 或 msg=='请求成功'。"""
    if not token:
        return {"status": "unconfigured", "detail": "未配置 PUSHPLUS_TOKEN"}
    payload = json.dumps({
        "token": token,
        "title": "WorkBuddy 积分加油站 · 每日播报",
        "content": content,
        "template": "markdown",
    }).encode("utf-8")
    req = urllib.request.Request(PUSHPLUS_API, data=payload, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", UA)
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = json.loads(resp.read().decode("utf-8", "replace"))
        ok = body.get("code") in (200, 0) or body.get("msg") in ("请求成功", "success")
        return {"status": "ok" if ok else "failed", "detail": body}
    except Exception as e:
        return {"status": "failed", "detail": str(e)[:200]}


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def main():
    rt = os.environ.get("WORKBUDDY_REFRESH_TOKEN", "").strip()
    location_id = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--location" and i + 1 < len(sys.argv[1:]):
            try:
                location_id = int(sys.argv[1:][i + 1])
            except ValueError:
                pass

    run_at = time.strftime("%Y-%m-%d %H:%M:%S (UTC+8)",
                           time.localtime(time.time() + 8 * 3600))

    if not rt:
        log("缺少 WORKBUDDY_REFRESH_TOKEN 环境变量（请在仓库 Secrets 中配置）。")
        # 仍尝试推送失败的提示（若微信已配置）
        push_pushplus(os.environ.get("PUSHPLUS_TOKEN", ""),
                      build_markdown({"ok": False, "action": None, "points": None,
                                      "balance": None, "msg": "缺少 Refresh Token 配置", "detail": {}},
                                     {"available": False, "log": []}, run_at))
        return 1

    try:
        at, new_rt = refresh_token(rt)
    except Exception as e:
        log("令牌刷新失败：%s" % e)
        push_pushplus(os.environ.get("PUSHPLUS_TOKEN", ""),
                      build_markdown({"ok": False, "action": None, "points": None,
                                      "balance": None, "msg": "令牌刷新失败：%s" % e, "detail": {}},
                                     {"available": False, "log": []}, run_at))
        return 1

    if not at:
        log("刷新应答缺少接口令牌。")
        return 1
    log("令牌刷新成功，接口令牌=%s（已掩码，不输出明文）" % mask_token(at))

    # 1) 签到
    checkin = do_checkin(at)
    log("签到结论：%s | %s" % (checkin["action"], checkin["msg"]))

    # 2) 派猫猫旅行（失败不影响签到结论）
    travel = run_travel(at, location_id=location_id)
    if travel.get("available"):
        log("猫猫状态：%s | %s" % (travel["status"].get("state_text"),
                                   "；".join(travel.get("log") or [])))
    else:
        log("猫猫部分：%s" % ("；".join(travel.get("log") or ["接口不可用"])))

    # 3) 推送微信（PushPlus；签到与猫猫分两节展示）
    md = build_markdown(checkin, travel, run_at)
    push_result = push_pushplus(os.environ.get("PUSHPLUS_TOKEN", ""), md)
    log("微信推送：%s %s" % (push_result.get("status"), push_result.get("detail")))

    # 整体成功与否仅由签到决定
    return 0 if checkin["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
