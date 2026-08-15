#!/usr/bin/env python3
"""
广州车牌摇号结果自动查询脚本
- 从公布栏抓取最新摇号结果PDF链接
- 下载PDF并搜索申请编码判断是否中签
- 更新状态文件并生成月度报告
- 自动git commit & push到GitHub
"""

import json
import os
import re
import ssl
import smtplib
import sys
from datetime import datetime
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ============ 配置 ============
APPLY_CODE = "1187104949388"        # 申请编码
APPLY_CODE_MASKED = "1187****9388"  # 脱敏编码
APPLICANT = "杰哥"
TARGET_HOST = "https://jtzl.jtj.gz.gov.cn"
GBL_PATH = "/index/gbl/"   
# 代理配置：通过 Cloudflare Worker 中转绕过海外 IP 403 限制
# 设置环境变量 PROXY_BASE_URL 即可启用代理（如 https://yaohao-proxy.your-name.workers.dev ）
PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "")
GBL_URL = f"{PROXY_BASE_URL}{GBL_PATH}" if PROXY_BASE_URL else f"{TARGET_HOST}{GBL_PATH}"
REPO_DIR = Path(__file__).parent.resolve()
STATE_FILE = REPO_DIR / "state.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
# ============ 邮件通知配置 ============
SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_CODE = os.environ.get("SMTP_CODE", "")
SMTP_TO = os.environ.get("SMTP_TO", "")
MAIL_FROM_NAME = "摇号查询通知"

REQUEST_TIMEOUT = 60

# 阶梯规则：每12次升一阶，73次及以上直接领牌
LADDER_THRESHOLDS = [(1, 12), (13, 24), (25, 36), (37, 48), (49, 60), (61, 72)]
LADDER_MULT = [1, 2, 3, 4, 5, 6]


def get_ladder_info(total_ballots: int) -> dict:
    """根据累计摇号次数计算阶梯信息"""
    for i, (lo, hi) in enumerate(LADDER_THRESHOLDS):
        if lo <= total_ballots <= hi:
            return {
                "ladder": i + 1,
                "multiplier": LADDER_MULT[i],
                "next_ladder_at": hi + 1,
                "remaining_to_next": hi + 1 - total_ballots,
            }
    if total_ballots >= 73:
        return {"ladder": 7, "multiplier": 0, "next_ladder_at": 0, "remaining_to_next": 0}
    return {"ladder": 1, "multiplier": 1, "next_ladder_at": 13, "remaining_to_next": 13 - total_ballots}


def load_state() -> dict:
    """读取状态文件"""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"code": APPLY_CODE, "total_ballots": 0, "history": []}


def save_state(state: dict):
    """保存状态文件"""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def proxy_url(url: str) -> str:
    """将目标URL转为代理URL"""
    if not PROXY_BASE_URL:
        return url
    # 将 https://jtzl.jtj.gz.gov.cn/xxx 替换为 https://proxy/xxx
    if url.startswith(TARGET_HOST):
        return PROXY_BASE_URL + url[len(TARGET_HOST):]
    return url


def fetch_gbl_page() -> str:
    """获取公布栏首页HTML"""
    print(f"  使用{'代理' if PROXY_BASE_URL else '直连'}: {GBL_URL}")
    for attempt in range(3):
        try:
            resp = requests.get(GBL_URL, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            # 用content让BS4自动检测编码（HTML meta标签中声明了charset=utf-8）
            soup = BeautifulSoup(resp.content, "html.parser")
            print(f"  HTTP {resp.status_code}, len={len(resp.content)}, encoding={resp.encoding}")
            if resp.status_code == 403:
                print(f"  403 Forbidden! 可能是海外IP被限制，请配置 PROXY_BASE_URL")
                if attempt < 2:
                    print(f"  等待10秒后重试...")
                    import time; time.sleep(10)
                    continue
                raise Exception("公布栏返回403，需要配置代理")
            return str(soup)
        except Exception as e:
            print(f"  请求失败(尝试{attempt+1}/3): {e}")
            if attempt == 2:
                raise


def find_yaohao_links(html: str) -> list:
    """
    从公布栏页面找出所有"摇号配置结果"链接
    返回 [(title, url), ...]
    """
    soup = BeautifulSoup(html, "html.parser")
    results = []
    all_links = soup.find_all("a", href=True)
    print(f"  页面共 {len(all_links)} 个链接")
    yaohao_count = 0
    for link in all_links:
        text = link.get_text(strip=True)
        href = link["href"]
        if "摇号" in text:
            yaohao_count += 1
            print(f"  摇号链接: text=[{text[:50]}] href=[{href[:80]}]")
            # 匹配 "个人、单位普通车（摇号）配置结果" 的链接
            if "配置结果" in text and "单位" in text:
                if not href.startswith("http"):
                    href = "https://jtzl.jtj.gz.gov.cn" + href
                results.append((text, href))
                print(f"    >>> 匹配成功!")
    print(f"  含'摇号'的链接: {yaohao_count}个, 精确匹配: {len(results)}个")
    return results


def find_pdf_url(detail_url: str) -> str | None:
    """从摇号结果详情页提取个人摇号结果PDF链接"""
    # 将详情页URL转为代理URL
    detail_url = proxy_url(detail_url)
    for attempt in range(3):
        try:
            resp = requests.get(detail_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            soup = BeautifulSoup(resp.content, "html.parser")
            print(f"  详情页 HTTP {resp.status_code}, links={len(soup.find_all('a', href=True))}")
            for link in soup.find_all("a", href=True):
                text = link.get_text(strip=True)
                href = link["href"]
                if "个人" in text and "摇号" in text and href.endswith(".pdf"):
                    if not href.startswith("http"):
                        href = TARGET_HOST + href
                    print(f"  PDF链接: [{text}] -> [{href}]")
                    return href
            print(f"  未找到个人摇号PDF链接")
            return None
        except Exception as e:
            print(f"  详情页请求失败(尝试{attempt+1}/3): {e}")
            if attempt == 2:
                return None


def check_code_in_pdf(pdf_url: str, code: str) -> bool:
    """下载PDF并搜索申请编码，返回是否中签"""
    # 将PDF URL也转为代理URL
    pdf_url = proxy_url(pdf_url)
    print(f"  下载PDF: {pdf_url}")
    resp = requests.get(pdf_url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    content = resp.content
    # PDF中文字以文本流形式存储，直接搜索编码字符串
    # 尝试解码为文本
    try:
        text = content.decode("latin-1")
    except Exception:
        text = content.decode("utf-8", errors="replace")
    return code in text


def extract_month_from_title(title: str) -> str | None:
    """从标题中提取月份，如'2026年7月' -> '2026-07'"""
    m = re.search(r"(\d{4})年(\d{1,2})月", title)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    return None


def generate_report(state: dict, month: str, joined: bool, won: bool) -> str:
    """生成月度报告Markdown"""
    total = state["total_ballots"]
    ladder_info = get_ladder_info(total)

    if ladder_info["ladder"] == 7:
        ladder_desc = "直接领牌"
        mult_desc = "直领"
        remaining_desc = "已达标"
    else:
        ladder_desc = f"第 {ladder_info['ladder']} 阶梯"
        mult_desc = f"{ladder_info['multiplier']}x"
        remaining_desc = f"还需 {ladder_info['remaining_to_next']} 次（第 {ladder_info['next_ladder_at']} 次升级）"

    direct_remaining = max(0, 73 - total)

    # 格式化月份
    year, mon = month.split("-")
    month_cn = f"{year}年{int(mon)}月"

    lines = [
        f"# 广州车牌摇号月度报告",
        f"",
        f"> 本期：{month_cn} | 申请人：{APPLICANT} | 申请编码：{APPLY_CODE}",
        f"",
        f"---",
        f"",
        f"## 本期结果",
        f"",
        f"| 项目 | 内容 |",
        f"|------|------|",
        f"| 摇号月份 | {month_cn} |",
        f"| 是否参与 | {'是' if joined else '否'} |",
        f"| 是否中签 | {'**是** :tada:' if won else '否'} |",
        f"",
        f"---",
        f"",
        f"## 进度追踪",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| 累计摇号次数 | {total} 次 |",
        f"| 当前阶梯 | {ladder_desc} |",
        f"| 当前倍率 | {mult_desc} |",
        f"| 距离下一阶梯 | {remaining_desc} |",
        f"| 距离直接领牌 | 还需 {direct_remaining} 次（第 73 次） |",
        f"",
        f"---",
        f"",
        f"## 阶梯规则表",
        f"",
        f"| 阶梯 | 摇号次数 | 倍率 | 说明 |",
        f"|------|----------|------|------|",
        f"| 1 | 1 ~ 12 次 | 1x | 基准倍率 |",
        f"| 2 | 13 ~ 24 次 | 2x | 中签率翻倍 |",
        f"| 3 | 25 ~ 36 次 | 3x | |",
        f"| 4 | 37 ~ 48 次 | 4x | |",
        f"| 5 | 49 ~ 60 次 | 5x | |",
        f"| 6 | 61 ~ 72 次 | 6x | |",
        f"| 直接领牌 | 73 次及以上 | - | 直领指标 |",
        f"",
        f"> 依据：《广州市小客车指标调控管理办法》穗府办规〔2024〕9号",
        f"",
        f"---",
        f"",
        f"## 历史记录",
        f"",
        f"| 月份 | 是否参与 | 是否中签 | 累计次数 | 阶梯 |",
        f"|------|----------|----------|----------|------|",
    ]

    for entry in state["history"]:
        m = entry["month"]
        j = "是" if entry["joined"] else "否"
        w = "**是**" if entry["won"] else "否"
        # 计算当时的累计次数和阶梯
        idx = state["history"].index(entry) + 1
        running_total = idx  # 简化：按顺序递增
        li = get_ladder_info(running_total)
        if li["ladder"] == 7:
            ldesc = "领牌"
        else:
            ldesc = str(li["ladder"])
        lines.append(f"| {m} | {j} | {w} | {running_total} | {ldesc} |")

    lines.extend([
        f"",
        f"---",
        f"",
        f"## 备注",
        f"",
        f"- 本报告由 GitHub Actions 自动生成并推送",
        f"- 每月 26/27 日自动查询最新摇号结果",
        f"- 数据来源：广州市中小客车指标调控管理信息系统（公布栏PDF）",
        f"- 查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ])

    return "\n".join(lines)


def generate_readme(state: dict) -> str:
    """生成 README.md"""
    total = state["total_ballots"]
    lines = [
        "# yaohao-report",
        "",
        "广州车牌摇号月度报告 - 自动推送",
        "",
        "## 项目说明",
        "",
        "每月自动查询广州车牌摇号结果，生成月度报告并推送到本仓库。",
        "",
        f"- 申请人：{APPLICANT}",
        f"- 申请编码：{APPLY_CODE}",
        "- 自动查询时间：每月 26/27 日（GitHub Actions 云端执行，无需本地电脑开机）",
        "- 数据来源：广州市中小客车指标调控管理信息系统",
        "",
        "## 历史月报",
        "",
    ]

    # 按时间正序列出
    for entry in sorted(state["history"], key=lambda x: x["month"]):
        y, m = entry["month"].split("-")
        filename = f"{entry['month']}.md"
        lines.append(f"- [{y}年{int(m)}月](./{filename})")

    lines.extend([
        "",
        "## 阶梯规则",
        "",
        "每 12 次摇号升一阶，倍率递增；72 次以上可直接领牌。",
        "",
        "| 阶梯 | 次数 | 倍率 |",
        "|------|------|------|",
        "| 1 | 1-12 | 1x |",
        "| 2 | 13-24 | 2x |",
        "| 3 | 25-36 | 3x |",
        "| 4 | 37-48 | 4x |",
        "| 5 | 49-60 | 5x |",
        "| 6 | 61-72 | 6x |",
        "| 领牌 | 73+ | 直领 |",
    ])

    return "\n".join(lines)





def send_mail(subject: str, body_html: str) -> bool:
    """通过QQ邮箱SMTP发送邮件通知"""
    if not (SMTP_USER and SMTP_CODE and SMTP_TO):
        print("[WARN] SMTP 配置缺失，跳过邮件通知")
        return False
    try:
        msg = MIMEText(body_html, "html", "utf-8")
        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(MAIL_FROM_NAME, "utf-8")), SMTP_USER))
        msg["To"] = SMTP_TO
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=30) as server:
            server.login(SMTP_USER, SMTP_CODE)
            server.sendmail(SMTP_USER, [SMTP_TO], msg.as_string())
        print(f"  邮件已发送 -> {SMTP_TO}")
        return True
    except Exception as e:
        print(f"[WARN] 邮件发送失败: {e}")
        return False


def build_mail_html(state: dict, new_results: list) -> str:
    """构建邮件HTML内容"""
    total = state["total_ballots"]
    ladder_info = get_ladder_info(total)

    if ladder_info["ladder"] == 7:
        ladder_desc = "直接领牌"
        mult_desc = "直领"
    else:
        ladder_desc = f"第 {ladder_info['ladder']} 阶梯"
        mult_desc = f"{ladder_info['multiplier']}x"

    # 构建新结果部分
    if new_results:
        result_rows = ""
        for month, joined, won in new_results:
            y, m = month.split("-")
            month_cn = f"{y}年{int(m)}月"
            status = '<span style="color:green;font-weight:bold">中签!</span>' if won else '<span style="color:#999">未中签</span>'
            result_rows += f"<tr><td>{month_cn}</td><td>{'是' if joined else '否'}</td><td>{status}</td></tr>"
        new_section = f"""
        <h3>本期结果</h3>
        <table style="border-collapse:collapse;width:100%">
          <tr style="background:#f0f0f0"><th style="border:1px solid #ddd;padding:8px;text-align:left">月份</th><th style="border:1px solid #ddd;padding:8px;text-align:left">参与</th><th style="border:1px solid #ddd;padding:8px;text-align:left">结果</th></tr>
          {result_rows}
        </table>"""
    else:
        new_section = '<p style="color:#666">本期暂无新的摇号结果公布。</p>'

    # 历史记录最近5条
    recent = state["history"][-5:]
    history_rows = ""
    for entry in recent:
        y, m = entry["month"].split("-")
        month_cn = f"{y}年{int(m)}月"
        w = '<span style="color:green">中签</span>' if entry["won"] else "未中签"
        history_rows += f"<tr><td>{month_cn}</td><td>{w}</td></tr>"

    html = f"""
    <div style="max-width:600px;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
      <h2 style="color:#333;border-bottom:2px solid #4CAF50;padding-bottom:8px">广州车牌摇号查询报告</h2>
      {new_section}
      <h3>进度概览</h3>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f0f0f0"><th style="border:1px solid #ddd;padding:8px;text-align:left">指标</th><th style="border:1px solid #ddd;padding:8px;text-align:left">数值</th></tr>
        <tr><td style="border:1px solid #ddd;padding:8px">累计摇号</td><td style="border:1px solid #ddd;padding:8px">{total} 次</td></tr>
        <tr><td style="border:1px solid #ddd;padding:8px">当前阶梯</td><td style="border:1px solid #ddd;padding:8px">{ladder_desc}</td></tr>
        <tr><td style="border:1px solid #ddd;padding:8px">当前倍率</td><td style="border:1px solid #ddd;padding:8px">{mult_desc}</td></tr>
      </table>
      <h3>近期记录</h3>
      <table style="border-collapse:collapse;width:100%">
        <tr style="background:#f0f0f0"><th style="border:1px solid #ddd;padding:8px;text-align:left">月份</th><th style="border:1px solid #ddd;padding:8px;text-align:left">结果</th></tr>
        {history_rows}
      </table>
      <p style="color:#999;font-size:12px;margin-top:16px">查询时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 申请人：{APPLICANT} | 编码：{APPLY_CODE}</p>
    </div>"""
    return html


def main():
    print("=" * 50)
    print("广州车牌摇号自动查询")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"申请编码: {APPLY_CODE}")
    print("=" * 50)

    # 1. 读取状态
    state = load_state()
    existing_months = {h["month"] for h in state["history"]}
    print(f"已记录月份数: {len(existing_months)}")
    print(f"累计摇号次数: {state['total_ballots']}")

    # 2. 抓取公布栏
    print("\n[1/4] 抓取公布栏...")
    try:
        gbl_html = fetch_gbl_page()
    except Exception as e:
        print(f"  错误: 无法访问公布栏 - {e}")
        sys.exit(1)

    yaohao_links = find_yaohao_links(gbl_html)
    print(f"  找到 {len(yaohao_links)} 个摇号结果链接")

    # 3. 逐月检查未记录的结果
    new_results = []  # [(month, joined, won)]
    for title, url in yaohao_links:
        month = extract_month_from_title(title)
        if not month:
            continue
        if month in existing_months:
            continue

        print(f"\n[2/4] 检查 {month} 摇号结果...")
        print(f"  标题: {title}")

        # 获取个人摇号PDF链接
        pdf_url = find_pdf_url(url)
        if not pdf_url:
            print(f"  未找到个人摇号结果PDF，跳过")
            continue

        # 下载PDF搜索编码
        try:
            won = check_code_in_pdf(pdf_url, APPLY_CODE)
            print(f"  申请编码 {APPLY_CODE}: {'中签!' if won else '未中签'}")
            new_results.append((month, True, won))
        except Exception as e:
            print(f"  PDF下载/解析错误: {e}")
            new_results.append((month, True, False))

    # 4. 更新状态
    if new_results:
        print(f"\n[3/4] 更新状态文件...")
        for month, joined, won in new_results:
            state["history"].append({
                "month": month,
                "joined": joined,
                "won": won,
            })
            if joined:
                state["total_ballots"] += 1
        # 按月份排序
        state["history"].sort(key=lambda x: x["month"])
        save_state(state)
        print(f"  已更新，当前累计: {state['total_ballots']} 次")
    else:
        print(f"\n[3/4] 没有新的摇号结果")

    # 5. 生成月报
    print(f"\n[4/4] 生成报告...")
    # 为每个月报生成 Markdown
    for month, joined, won in new_results:
        report = generate_report(state, month, joined, won)
        report_file = REPO_DIR / f"{month}.md"
        with open(report_file, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"  已生成: {report_file.name}")

    # 更新 README
    readme = generate_readme(state)
    with open(REPO_DIR / "README.md", "w", encoding="utf-8") as f:
        f.write(readme)
    print(f"  已更新: README.md")

    # git push 由 GitHub Actions 的后续 step 处理
    # 脚本只负责生成文件

    # 输出最终状态
    print("\n" + "=" * 50)
    print("查询完成!")
    print(f"累计摇号: {state['total_ballots']} 次")
    ladder = get_ladder_info(state["total_ballots"])
    print(f"当前阶梯: 第{ladder['ladder']}阶梯 | 倍率: {ladder['multiplier']}x")
    if new_results:
        for m, j, w in new_results:
            status = "中签!" if w else "未中签"
            print(f"  {m}: {status}")
    else:
        print("本期暂无新结果")
    print("=" * 50)

    # 发送邮件通知
    print("\n[邮件] 发送通知...")
    if new_results:
        has_won = any(w for _, _, w in new_results)
        if has_won:
            mail_subject = "广州摇号结果：中签!"
        else:
            months_str = "、".join(m for m, _, _ in new_results)
            mail_subject = f"广州摇号结果：{months_str} 未中签"
    else:
        mail_subject = "广州摇号查询：本期暂无新结果"
    mail_body = build_mail_html(state, new_results)
    send_mail(mail_subject, mail_body)


if __name__ == "__main__":
    main()
