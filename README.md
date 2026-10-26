# WorkBuddy 积分加油站 · GitHub Actions 自动化方案

每天定时一次，在 GitHub 免费云端 runner 上**同一次运行**完成「签到 + 派猫猫旅行」，并把结果推送微信（PushPlus，经公众号下发到个人微信）。无需本机常开、无需本地登录态。

---

## 一、它解决了什么

| 痛点 | 本方案做法 |
|---|---|
| 云端没有本机登录态 | 云端用 `refresh token` 经官方接口换取短效 `access token`，**不读本机登录态** |
| token 不能落仓库 / 进日志 | token 仅存于仓库 **Secrets**；脚本全程输出**掩码**，绝不打印明文 |
| 不想硬编码解释器版本 | 工作流用 `command -v python3 || command -v python` 动态解析 |
| 接口名以实际为准 | 全部端点**已在本机实测核对**，非网上流传旧名（见第三节） |
| 猫猫失败别连累签到 | 退出码**只由签到决定**；猫猫失败整体仍成功 |

---

## 二、仓库结构

```
.
├── .github/
│   └── workflows/
│       └── daily.yml            # 定时工作流（schedule + workflow_dispatch）
├── scripts/
│   ├── run.py                   # 云端运行脚本（自包含，无第三方依赖）
│   └── export_token.py          # 本机刷新脚本：读取登录态导出 refresh token
└── README.md
```

数据流：

```
本机(已登录) ──export_token.py──▶ GitHub Secret: WORKBUDDY_REFRESH_TOKEN
                                          │
GitHub Actions (ubuntu, 免费)             │
   └─ daily.yml ──▶ run.py：              │
        1. 读 Secret 的 refresh token      │
        2. POST /v2/plugin/auth/token/refresh → access token
        3. 签到（先查状态，必要时领取，幂等）
        4. 派猫猫（先领已到家的，再判断今日能否派新的一趟）
        5. 微信(PushPlus) markdown 推送（签到 / 猫猫分两节展示）
```

---

## 三、真实接口清单（已实测，非旧名）

| 用途 | 方法 | 真实路径 |
|---|---|---|
| 刷新令牌 | POST | `https://copilot.tencent.com/v2/plugin/auth/token/refresh`（头 `X-Refresh-Token`） |
| 签到状态 | POST | `https://www.codebuddy.cn/v2/billing/meter/checkin-activity-status` |
| 签到领取 | POST | `https://www.codebuddy.cn/v2/billing/meter/daily-checkin` |
| 派猫状态 | GET  | `https://www.workbuddy.cn/activity/growth/buddy/travel/status` |
| 派猫配置 | GET  | `https://www.workbuddy.cn/activity/growth/buddy/travel/config` |
| 派猫领取 | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/claim` |
| 派猫派遣 | POST | `https://www.workbuddy.cn/activity/growth/buddy/travel/depart` |

> 关键差异：**签到走 `codebuddy` 域名 + `/v2` 前缀**；**派猫走 `workbuddy` 域名且无 `/v2` 前缀**——写错一律 404。

---

## 四、配置步骤（一次性）

### 1. 准备 refresh token（本机，仅一次）
在 **已登录 WorkBuddy 的电脑** 上，于仓库根目录运行：

```bash
# 推荐：经 gh 直接无痕写入仓库 Secret（明文不落盘、不进命令行参数）
python scripts/export_token.py --gh --repo <owner>/<repo>

# 或仅打印明文，自行到 GitHub → Settings → Secrets → Actions 添加
# 名称：WORKBUDDY_REFRESH_TOKEN
python scripts/export_token.py
```

> 说明：refresh token 等同账号钥匙，仅保存在仓库 Secret（加密存储）。本脚本只读取、解密本地登录态，**绝不修改/外传**你的 accessToken。

### 2. 配置 PushPlus（微信推送，需求要求）
注册 [PushPlus](https://www.pushplus.plus/) 并登录，在「一对多消息」或「一对一」页面拿到 **Token**，在仓库 Secrets 增加：
- `PUSHPLUS_TOKEN`

> 消息会经 PushPlus 公众号下发到你的个人微信，无需企业微信，也无需加签。

### 3. 启用定时任务
工作流已内置 `schedule: '30 1 * * *'`（= 北京时间 **09:30** 每天一次）。推送后默认在每次 push / 手动 `workflow_dispatch` 也可触发，便于验收。

> 改时间：`daily.yml` 里的 cron 表达式（UTC）。例如想 08:00 北京时间 → `0 0 * * *`。

---

## 五、需求 / 约束对照表

| 编号 | 要求 | 落地点 |
|---|---|---|
| 1 | 每天定时一次，单次运行内同时完成签到与派猫 | `daily.yml` schedule；`run.py` 同进程顺序执行刷新→签到→派猫→推送 |
| 2 | 派猫先领已到家的，再判断今日能否派新的一趟；当天已派过跳过 | `run.py: run_travel()`：先 `claim`(arrived) → 再 `depart`(idle 且 `daily_limit_reached` 为假)；traveling/已派满则跳过 |
| 3 | 微信推送签到与猫猫分两节展示 | `build_markdown()` 生成 markdown 正文，用 `---` 分隔「签到结果」「猫猫状态」两节 |
| 4 | 猫猫失败不影响签到结论；签到成功即整体成功 | `run.py` 退出码**仅由签到决定**；工作流不 `fail-on-error`，猫猫失败仍绿 |
| 5 | 云端无本机登录态，token 由刷新脚本导出再写 Secret | `export_token.py` 本机读登录态解密 refresh token；`run.py` 仅从环境变量读取并刷新 |
| 6 | token 明文不落仓库、不进日志 | 全程仅输出 `mask_token()` 掩码；导出脚本默认只打印到 stdout，或用 `--gh` 经 stdin 写入，避免落盘 |
| 7 | 解释器路径不硬编码版本号 | 工作流 `PYBIN="$(command -v python3 || command -v python || true)"` |
| 8 | 接口名以实际请求返回为准 | 第三节全部端点已本机实测核对，未沿用旧名 |
| 9 | 完成一次成功运行并附脱敏原始返回 | 见第六节（本次真实运行：签到幂等跳过、猫猫 traveling 且今日已派满，退出码 0） |

---

## 六、验收证据：一次真实成功运行（脱敏）

本地以真实 refresh token 运行 `run.py` 的控制台输出（token 仅掩码）：

```
[12:40:45] 令牌刷新成功，接口令牌=eyJhbG...cBWg（已掩码，不输出明文）
[12:40:46] 签到结论：skip_already_signed | 今日已签到，无需重复领取，当前积分余额 1800
[12:40:47] 猫猫状态：旅行中 | 今日派遣次数已用完，跳过派遣
[12:40:47] 微信推送：unconfigured 未配置 PUSHPLUS_TOKEN
=== run.py exit code: 0 ===
```

> 退出码 **0**：签到成功即整体成功；猫猫今日已派满被跳过、飞书未配置均未影响结论 —— 满足要求 #4。
> （微信 `unconfigured` 仅为当前演示机未配置 Secret，配置后即为真实推送。）

### 脱敏后的原始接口返回（本次实测抓包）

**① 刷新令牌** `POST /v2/plugin/auth/token/refresh` → `HTTP 200`
```json
{ "code": 0, "msg": "OK", "data": {
    "accessToken": "***MASKED***", "expiresIn": 2592000,
    "refreshExpiresIn": 5183999, "refreshToken": "***MASKED***",
    "tokenType": "Bearer", "domain": "copilot.tencent.com" } }
```

**② 签到状态** `POST /v2/billing/meter/checkin-activity-status` → `HTTP 200`
```json
{ "code": 0, "msg": "OK", "data": {
    "active": true, "today_checked_in": true, "streak_days": 18,
    "daily_credit": 100, "today_credit": 100,
    "total_credits": 1800, "theme_name": "Buddy加油站",
    "activity_name": "Buddy应用", "claim_button_text": "立即领取" } }
```

**③ 签到领取（幂等）** `POST /v2/billing/meter/daily-checkin` → `HTTP 400`
```json
{ "code": 10001, "msg": "今天已签到，请明天再来" }
```

**④ 派猫状态** `GET /activity/growth/buddy/travel/status` → `HTTP 200`
```json
{ "code": 0, "msg": "OK", "data": {
    "state": "traveling", "buddy_id": 4145974, "record_id": 30812171,
    "location": { "id": 4, "code": "ancient_town", "name": "古镇客栈", "duration_hours": 4 },
    "daily_limit_reached": true, "reward_credit": 7 } }
```

> 说明：`daily_limit_reached=true` 表示今日派遣已达上限，脚本据此**跳过派遣**（符合需求 #2「当天已经派过就跳过」）；下次运行若变为 `arrived` 会自动先补领再判断派遣。

---

## 七、安全与运维

- **凭据**：仅 `WORKBUDDY_REFRESH_TOKEN / PUSHPLUS_TOKEN` 两项 Secrets，全部加密存储，日志中不可见。
- **轮换**：刷新接口会回传新的 `refreshToken`（有效期约 60 天）；如需长期无人值守，可周期性重跑 `export_token.py --gh` 更新 Secret。
- **失效处理**：若 refresh token 过期，run.py 会让作业标红；重跑 `export_token.py` 重新导出即可。
- **依赖**：纯 Python 3.8+ 标准库，runner 自带，无需 `pip install`。
