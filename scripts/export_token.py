#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 积分加油站 · 本机令牌导出脚本（刷新脚本，仅在已登录的电脑上运行）

用途（对应需求 #5 / #6）：
  云端 runner 没有本机登录态，无法读取本地 accessToken。
  本脚本在本机（WorkBuddy 已登录）读取登录态文件，解密出长效 refresh token，
  导出后由你手动写入仓库 Secrets（或经 `gh` 直接写入，明文不落仓库/不进日志）。

安全约定：
  - 仅在本机读取并解密，绝不修改/外传你的 accessToken。
  - 默认只把 refresh token 打印到「标准输出」（你自行保管/录入 Secret）；
    若要直接写入 GitHub Secret，使用 --gh 参数（经 stdin 管道，不留痕于文件/日志）。
  - 任何情况下都不要把 token 写进仓库文件或提交。

运行（先在仓库根目录）：
  python scripts/export_token.py                  # 打印 refresh token + 配置指引
  python scripts/export_token.py --gh             # 直接经 gh 写入仓库 Secret（推荐，无痕）
  python scripts/export_token.py --gh --repo owner/name
"""

import os
import sys
import json
import shutil
import subprocess

SKILL_NAME = "totorosir-workbuddy-score"


def find_skill_scripts_dir():
    """定位技能脚本目录（含 workbuddy_checkin.py）。"""
    candidates = []
    env_dir = os.environ.get("SKILL_SCRIPTS_DIR")
    if env_dir:
        candidates.append(env_dir)
    home = os.path.expanduser("~")
    candidates.append(os.path.join(
        home, ".workbuddy", "skills", SKILL_NAME, "scripts"))
    # Windows 常见布局兜底
    candidates.append(os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "workbuddy", "skills",
        SKILL_NAME, "scripts"))
    for d in candidates:
        if d and os.path.isfile(os.path.join(d, "workbuddy_checkin.py")):
            return d
    return None


def load_wc():
    d = find_skill_scripts_dir()
    if not d:
        sys.stderr.write(
            "未找到技能脚本目录（期望含 workbuddy_checkin.py）。\n"
            "请确认 WorkBuddy 技能 '%s' 已安装；或通过环境变量 "
            "SKILL_SCRIPTS_DIR 指定其 scripts 目录。\n" % SKILL_NAME)
        return None
    if d not in sys.path:
        sys.path.insert(0, d)
    try:
        import workbuddy_checkin as wc
        return wc
    except Exception as e:
        sys.stderr.write("导入技能脚本失败：%s\n" % e)
        return None


def extract_refresh_token(wc):
    """复用技能的本地读取能力，解密 refreshToken。返回 (token, path)。"""
    errors = []
    for p in wc._auth_candidates():
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                auth = json.load(f).get("auth", {})
            raw = auth.get("refreshToken")
            if raw is None:
                errors.append((p, "缺少 refreshToken 字段"))
                continue
            return wc.decrypt_access_token_field(raw), p
        except Exception as e:
            errors.append((p, str(e)[:160]))
    raise RuntimeError(
        "所有候选登录态均无法读取 refreshToken（共 %d 个）：\n" % len(errors)
        + "\n".join("  - %s: %s" % (pp, ee) for pp, ee in errors))


def mask_token(t):
    if not t:
        return "<empty>"
    return t[:6] + "..." + t[-4:]


def set_gh_secret(token, repo):
    gh = shutil.which("gh")
    if not gh:
        sys.stderr.write("未检测到 gh CLI，无法自动写入。请手动复制上面的 token 到仓库 Secrets。\n")
        return False
    cmd = [gh, "secret", "set", "WORKBUDDY_REFRESH_TOKEN"]
    if repo:
        cmd += ["--repo", repo]
    # 经 stdin 管道传入，明文不落入命令行参数/日志文件
    try:
        subprocess.run(cmd, input=token.encode("utf-8"),
                       check=True, capture_output=True)
        sys.stderr.write("[ok] 已通过 gh 写入仓库 Secret WORKBUDDY_REFRESH_TOKEN（明文未落盘/未进参数）。\n")
        return True
    except subprocess.CalledProcessError as e:
        sys.stderr.write("gh 写入失败：%s\n%s\n" % (e, e.stderr.decode("utf-8", "replace")))
        return False


def main():
    argv = sys.argv[1:]
    use_gh = "--gh" in argv
    repo = None
    if "--repo" in argv:
        try:
            repo = argv[argv.index("--repo") + 1]
        except (IndexError, ValueError):
            pass

    wc = load_wc()
    if wc is None:
        return 1

    try:
        token, path = extract_refresh_token(wc)
    except Exception as e:
        sys.stderr.write("读取本机登录态失败：%s\n" % e)
        sys.stderr.write("请确认 WorkBuddy 客户端已在本机登录且未退出。\n")
        return 1

    if use_gh:
        ok = set_gh_secret(token, repo)
        if not ok:
            # 退回：打印明文供手动配置
            sys.stdout.write(token + "\n")
        return 0 if ok else 1

    # 默认：打印明文 token + 指引（用户自行录入 Secret）
    sys.stderr.write(
        "[提示] 以下为你的长效 refresh token 明文（等同账号钥匙），请妥善保管，勿分享。\n"
        "请将其写入仓库 Secrets：名称 WORKBUDDY_REFRESH_TOKEN（Settings → Secrets → Actions）。\n"
        "或使用 `python scripts/export_token.py --gh` 直接无痕写入。\n"
        "脱敏预览：%s\n"
        "---- 以下为 token 明文，请复制后即刻清除终端回滚记录 ----\n" % mask_token(token))
    sys.stdout.write(token + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
