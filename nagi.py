#!/usr/bin/env python3
"""
FFLogs (cn.fflogs.com) Report Analyzer
========================================

輸入報告網址 → 列出所有戰鬥 → 選擇要解析的戰鬥(可複選) → 列出每個人的最佳 DPS。

使用官方 GraphQL API v2。DPS 採用 FFLogs 預設的 rDPS(rDPS 傷害 / 該場時長秒數)。

事前準備:
------------------
1. 登入 https://cn.fflogs.com
2. 前往 https://cn.fflogs.com/api/clients/ 建立 Client,拿到 Client ID / Secret。
   (國際服請把所有網址的 cn. 拿掉)

安裝套件:
------------------
pip install requests --break-system-packages

使用方式:
------------------
python3 nagi.py                         # 互動輸入網址
python3 nagi.py <報告網址或代碼>          # 直接帶入
"""

import os
import re
import sys
import json
import requests

# ---- 設定區 ----
# 若是國際服 (fflogs.com) 請把下面 cn. 拿掉
BASE_URL = "https://cn.fflogs.com"
TOKEN_URL = f"{BASE_URL}/oauth/token"
GRAPHQL_URL = f"{BASE_URL}/api/v2/client"

# 建議用環境變數;沒設定時退回下方寫死的值。
CLIENT_ID = os.environ.get("FFLOGS_CLIENT_ID", "019f922f-0ee4-705f-8ca8-726e5a9b089b")
CLIENT_SECRET = os.environ.get("FFLOGS_CLIENT_SECRET", "l8PRwzccWgGV2ra1FahIJwTM4gekeM8RnlT9Fazc")


# ---- API 基礎 ----

def get_access_token() -> str:
    """用 Client Credentials Flow 取得 access token"""
    if not CLIENT_ID or not CLIENT_SECRET:
        sys.exit("錯誤: 請設定 FFLOGS_CLIENT_ID 和 FFLOGS_CLIENT_SECRET")
    resp = requests.post(
        TOKEN_URL,
        data={"grant_type": "client_credentials"},
        auth=(CLIENT_ID, CLIENT_SECRET),
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def graphql_query(token: str, query: str, variables: dict) -> dict:
    resp = requests.post(
        GRAPHQL_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"query": query, "variables": variables},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "errors" in data:
        raise RuntimeError(json.dumps(data["errors"], ensure_ascii=False, indent=2))
    return data["data"]


# ---- GraphQL 查詢 ----

REPORT_OVERVIEW_QUERY = """
query ReportOverview($code: String!) {
  reportData {
    report(code: $code) {
      title
      zone { name }
      fights {
        id
        name
        difficulty
        kill
        bossPercentage
        startTime
        endTime
        combatTime
      }
    }
  }
}
"""

DAMAGE_DONE_QUERY = """
query DamageDone($code: String!, $fightIDs: [Int!]) {
  reportData {
    report(code: $code) {
      table(fightIDs: $fightIDs, dataType: DamageDone)
    }
  }
}
"""


# ---- 工具函式 ----

def parse_report_code(text: str) -> str:
    """從網址或原始代碼取出報告代碼。

    支援:
      https://cn.fflogs.com/reports/VFQHXP16nyj9bgG8#fight=4
      https://www.fflogs.com/reports/VFQHXP16nyj9bgG8
      VFQHXP16nyj9bgG8
    """
    text = text.strip()
    m = re.search(r"/reports/([a-zA-Z0-9]+)", text)
    if m:
        return m.group(1)
    # 沒有 /reports/ 就當作使用者直接貼了代碼
    return text.split("#")[0].split("?")[0].strip("/")


def ms_to_clock(ms: int) -> str:
    total_seconds = int(ms) // 1000
    m, s = divmod(total_seconds, 60)
    return f"{m:02d}:{s:02d}"


def parse_selection(raw: str, count: int) -> list:
    """把使用者輸入(如 '1,3,5-7' 或 'all')解析成 1-based 選項索引清單。"""
    raw = raw.strip().lower()
    if raw in ("all", "a", "*", "全部"):
        return list(range(1, count + 1))
    picked = set()
    for part in re.split(r"[,\s]+", raw):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            if a.isdigit() and b.isdigit():
                for i in range(int(a), int(b) + 1):
                    if 1 <= i <= count:
                        picked.add(i)
        elif part.isdigit():
            i = int(part)
            if 1 <= i <= count:
                picked.add(i)
    return sorted(picked)


# ---- 主要流程 ----

def print_fight_list(fights: list):
    print("\n【戰鬥列表】 (共 %d 場)" % len(fights))
    print("  序號  戰鬥                            難度   耗時    結果")
    print("  " + "-" * 62)
    for idx, f in enumerate(fights, start=1):
        duration = ms_to_clock(f["endTime"] - f["startTime"])
        if f.get("kill"):
            status = "擊殺 ✅"
        else:
            bp = f.get("bossPercentage")
            status = f"未擊殺 (剩 {bp}%)" if bp is not None else "未擊殺"
        diff = f.get("difficulty")
        diff_s = str(diff) if diff is not None else "-"
        print(f"  [{idx:>2}]  {f['name'][:28]:<28}  {diff_s:>4}   {duration}   {status}")


def build_fight_link(report_code: str, fight_id: int, source_id) -> str:
    """組出可以直接跳到某玩家、某場戰鬥輸出頁面的 FFLogs 超連結。"""
    link = f"{BASE_URL}/reports/{report_code}#fight={fight_id}&type=damage-done"
    if source_id is not None:
        link += f"&source={source_id}"
    return link


def collect_fight_records(token: str, report_code: str, fights: list) -> list:
    """逐場查詢輸出,回傳每人每場一筆的原始紀錄清單(不在這裡做跨場彙總,
    彙總/統計交給 viewer.html 處理)。"""
    records = []
    for f in fights:
        fid = f["id"]
        print(f"  查詢中: #{fid} {f['name']} ...", flush=True)
        data = graphql_query(
            token, DAMAGE_DONE_QUERY,
            {"code": report_code, "fightIDs": [fid]},
        )
        table = data["reportData"]["report"]["table"]["data"]
        # combatTime 已扣掉機制無敵/中場等停手時間,是 FFLogs 網站算 DPS 實際用的分母;
        # 若該場沒有 combatTime(例如非戰鬥/雜項紀錄),才退回用 totalTime。
        ms = f.get("combatTime") or table.get("totalTime", 0)
        seconds = ms / 1000
        if seconds <= 0:
            continue
        for e in table.get("entries", []):
            source_id = e.get("id")
            records.append({
                "fight_id": fid,
                "fight_name": f["name"],
                "name": e.get("name", "?"),
                "job": e.get("type", "?"),
                "source_id": source_id,
                "rdps": round(e.get("totalRDPS", 0) / seconds, 1),
                "dps": round(e.get("total", 0) / seconds, 1),
                "link": build_fight_link(report_code, fid, source_id),
            })
    return records


def summarize_best(records: list) -> dict:
    """從逐場紀錄中取每人 rDPS 最高的一筆,只用於終端機摘要輸出。"""
    best = {}
    for r in records:
        cur = best.get(r["name"])
        if cur is None or r["rdps"] > cur["rdps"]:
            best[r["name"]] = r
    return best


def print_best_dps(best: dict):
    print("\n" + "=" * 70)
    print("【每人最佳 DPS】(rDPS = FFLogs 預設指標,取跨所選戰鬥的單場最佳)")
    print("=" * 70)
    if not best:
        print("  (無資料)")
        return
    rows = sorted(best.items(), key=lambda kv: kv[1]["rdps"], reverse=True)
    print(f"  {'#':>2}  {'玩家':<14}{'職業':<14}{'rDPS':>10}{'DPS':>10}   最佳場次")
    print("  " + "-" * 66)
    for rank, (name, info) in enumerate(rows, start=1):
        print(f"  {rank:>2}  {name[:12]:<14}{info['job']:<14}"
              f"{info['rdps']:>10,.0f}{info['dps']:>10,.0f}"
              f"   #{info['fight_id']} {info['fight_name'][:16]}")


def sanitize_filename_part(text: str) -> str:
    """把字串轉成檔名安全的片段: 去除路徑分隔字元,空白改底線。"""
    text = text.strip()
    text = re.sub(r'[\\/:*?"<>|]', "", text)
    text = re.sub(r"\s+", "_", text)
    return text or "unknown"


def build_instance_label(selected: list) -> str:
    """用所選戰鬥的副本/首領名稱組出檔名片段(不重複、依選擇順序)。"""
    seen = []
    for f in selected:
        name = f.get("name") or "unknown"
        if name not in seen:
            seen.append(name)
    return "_".join(sanitize_filename_part(n) for n in seen)


def save_records_json(report_code: str, report: dict, selected: list, records: list) -> str:
    """把每場每人的原始 DPS 紀錄存成 best_dps_{報告代碼}_fight_{副本名稱}.json,回傳存檔路徑。"""
    instance_label = build_instance_label(selected)
    out_path = f"best_dps_{report_code}_fight_{instance_label}.json"

    payload = {
        "report_code": report_code,
        "report_title": report.get("title"),
        "zone_name": report["zone"]["name"] if report.get("zone") else None,
        "fights": [
            {
                "id": f["id"],
                "name": f["name"],
                "difficulty": f.get("difficulty"),
                "kill": f.get("kill"),
                "boss_percentage": f.get("bossPercentage"),
                "combat_time_ms": f.get("combatTime") or (f["endTime"] - f["startTime"]),
            }
            for f in selected
        ],
        "entries": records,
    }

    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return out_path


def main():
    # 1. 取得報告代碼
    if len(sys.argv) > 1:
        raw = sys.argv[1]
    else:
        raw = input("請貼上 FFLogs 報告網址或代碼: ").strip()
    if not raw:
        sys.exit("未輸入網址。")
    report_code = parse_report_code(raw)

    token = get_access_token()

    # 2. 抓報告總覽 + 戰鬥列表
    overview = graphql_query(token, REPORT_OVERVIEW_QUERY, {"code": report_code})
    report = overview["reportData"]["report"]
    if report is None:
        sys.exit(f"找不到報告: {report_code} (確認網址正確、且報告非私人/未過期)")

    print("=" * 70)
    print(f"報告: {report.get('title')}   |   區域: "
          f"{report['zone']['name'] if report.get('zone') else '未知'}")
    print(f"代碼: {report_code}")
    print("=" * 70)

    fights = report.get("fights", [])
    if not fights:
        sys.exit("這份報告沒有任何戰鬥紀錄。")
    print_fight_list(fights)

    # 3. 讓使用者選擇戰鬥(可複選)
    sel_raw = input("\n選擇要解析的戰鬥序號 (例: 1,3,5-7,或 all): ").strip()
    picked_idx = parse_selection(sel_raw, len(fights))
    if not picked_idx:
        sys.exit("沒有選到任何戰鬥。")
    selected = [fights[i - 1] for i in picked_idx]
    print(f"\n已選 {len(selected)} 場: " +
          ", ".join(f"#{f['id']} {f['name']}" for f in selected))

    # 4. 逐場算 DPS
    print("\n開始查詢輸出資料...")
    records = collect_fight_records(token, report_code, selected)
    print_best_dps(summarize_best(records))

    # 5. 存成 JSON 供 viewer.html 顯示(存原始逐場數據,統計交給 viewer 處理)
    out_path = save_records_json(report_code, report, selected, records)
    print(f"\n已存成: {out_path}")


if __name__ == "__main__":
    main()
