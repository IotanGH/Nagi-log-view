#!/usr/bin/env python3
"""
FFLogs (cn.fflogs.com) Report Analyzer
========================================

輸入報告網址 → 列出所有戰鬥 → 選擇要解析的戰鬥(可複選) → 現撈資料直接產生報告並開瀏覽器。

使用官方 GraphQL API v2。DPS 採用 FFLogs 預設的 rDPS(rDPS 傷害 / 該場時長秒數)。
資料會直接內嵌進 viewer.html,同時也會把原始資料另存一份 .json,兩者都存在
result/ 資料夾底下並自動開啟 html。

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
import webbrowser
from datetime import datetime
import requests

# ---- 設定區 ----
# 若是國際服 (fflogs.com) 請把下面 cn. 拿掉
BASE_URL = "https://cn.fflogs.com"
TOKEN_URL = f"{BASE_URL}/oauth/token"
GRAPHQL_URL = f"{BASE_URL}/api/v2/client"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
VIEWER_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "viewer.html")
INDEX_TEMPLATE_PATH = os.path.join(SCRIPT_DIR, "index_template.html")
INDEX_HTML_PATH = os.path.join(SCRIPT_DIR, "index.html")
COMBINE_HTML_PATH = os.path.join(SCRIPT_DIR, "combine.html")
RESULT_DIR = os.path.join(SCRIPT_DIR, "result")

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
      startTime
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


def build_payload(report_code: str, report: dict, selected: list, records: list) -> dict:
    """把每場每人的原始 DPS 紀錄整理成 viewer.html 看得懂的資料結構。"""
    return {
        "report_code": report_code,
        "report_title": report.get("title"),
        "report_start_time": report.get("startTime"),
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


def result_basename(payload: dict) -> str:
    """組出 result/ 底下 html 和 json 共用的檔名(不含副檔名):
    log 日期(yyyymmdd) + 副本/首領名稱。"""
    instance_label = build_instance_label(payload["fights"])
    start_ms = payload.get("report_start_time")
    if start_ms:
        date_str = datetime.fromtimestamp(start_ms / 1000).strftime("%Y%m%d")
    else:
        date_str = "unknown"
    return f"{date_str}_{instance_label}"


def generate_report_html(payload: dict) -> str:
    """把資料直接內嵌進 viewer.html 產生一份現撈報告,存到執行資料夾底下的
    result/ 子資料夾(不存在就建立),回傳存檔路徑。"""
    if not os.path.exists(VIEWER_TEMPLATE_PATH):
        sys.exit(f"找不到 viewer.html 樣板: {VIEWER_TEMPLATE_PATH}")

    with open(VIEWER_TEMPLATE_PATH, "r", encoding="utf-8") as fp:
        template = fp.read()

    data_script = (
        "<script>window.__NAGI_DATA__ = "
        + json.dumps(payload, ensure_ascii=False)
        + ";</script>\n</head>"
    )
    html = template.replace("</head>", data_script, 1)

    os.makedirs(RESULT_DIR, exist_ok=True)
    out_path = os.path.join(RESULT_DIR, result_basename(payload) + ".html")
    with open(out_path, "w", encoding="utf-8") as fp:
        fp.write(html)
    return out_path


def save_report_json(payload: dict) -> str:
    """把原始資料另存一份 .json 到同一個 result/ 子資料夾,回傳存檔路徑。"""
    os.makedirs(RESULT_DIR, exist_ok=True)
    out_path = os.path.join(RESULT_DIR, result_basename(payload) + ".json")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(payload, fp, ensure_ascii=False, indent=2)
    return out_path


def scan_result_dir() -> list:
    """掃描 result/ 底下所有 .json,整理成給 index.html 用的報告清單。"""
    if not os.path.isdir(RESULT_DIR):
        return []

    items = []
    for fname in sorted(os.listdir(RESULT_DIR)):
        if not fname.endswith(".json"):
            continue
        json_path = os.path.join(RESULT_DIR, fname)
        basename = fname[:-len(".json")]
        html_path = os.path.join(RESULT_DIR, basename + ".html")
        if not os.path.exists(html_path):
            continue

        try:
            with open(json_path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
        except (OSError, json.JSONDecodeError):
            continue

        date_prefix = re.match(r"^(\d{8})_(.+)$", basename)
        start_ms = payload.get("report_start_time")
        if start_ms:
            dt = datetime.fromtimestamp(start_ms / 1000)
        elif date_prefix:
            try:
                dt = datetime.strptime(date_prefix.group(1), "%Y%m%d")
            except ValueError:
                dt = datetime.fromtimestamp(os.path.getmtime(json_path))
        else:
            dt = datetime.fromtimestamp(os.path.getmtime(json_path))

        instance_label = build_instance_label(payload.get("fights", []))
        if not instance_label:
            instance_label = date_prefix.group(2) if date_prefix else basename
        instance_label = instance_label.replace("_", " ")

        fights = payload.get("fights", [])
        entries = payload.get("entries", [])
        players = {e.get("name") for e in entries if e.get("name")}

        items.append({
            "html": "result/" + basename + ".html",
            "instance": instance_label,
            "zone": payload.get("zone_name") or "",
            "title": payload.get("report_title") or instance_label,
            "report_code": payload.get("report_code") or "",
            "date_ts": dt.timestamp(),
            "date_display": dt.strftime("%Y-%m-%d"),
            "fight_count": len(fights),
            "kill_count": sum(1 for f in fights if f.get("kill")),
            "player_count": len(players),
            # fights/entries 只有 generate_combine_html() 用得到(合併統計要算跨報告
            # 的每人數據),generate_index_html() 會把這兩個欄位拿掉,總覽頁不必背這包資料。
            "fights": fights,
            "entries": entries,
        })
    return items


def generate_index_html() -> str:
    """掃描 result/ 資料夾,重新產生根目錄的 index.html 總覽頁。"""
    if not os.path.exists(INDEX_TEMPLATE_PATH):
        sys.exit(f"找不到 index_template.html 樣板: {INDEX_TEMPLATE_PATH}")

    with open(INDEX_TEMPLATE_PATH, "r", encoding="utf-8") as fp:
        template = fp.read()

    items = [
        {k: v for k, v in item.items() if k not in ("fights", "entries")}
        for item in scan_result_dir()
    ]
    data_script = (
        "<script>window.__NAGI_INDEX_DATA__ = "
        + json.dumps(items, ensure_ascii=False)
        + ";</script>\n</head>"
    )
    html = template.replace("</head>", data_script, 1)

    with open(INDEX_HTML_PATH, "w", encoding="utf-8") as fp:
        fp.write(html)
    return INDEX_HTML_PATH


def generate_combine_html() -> str:
    """掃描 result/ 資料夾,重新產生根目錄的 combine.html:
    讓使用者挑選同一副本、不同日期的報告,合併計算每人跨場次的整體表現。
    直接沿用 viewer.html 樣板(內建的合併統計面板只有在偵測到
    window.__NAGI_COMBINE_DATA__ 時才會顯示),這樣核心的表格/排序/圖表渲染邏輯
    只需要維護一份。"""
    if not os.path.exists(VIEWER_TEMPLATE_PATH):
        sys.exit(f"找不到 viewer.html 樣板: {VIEWER_TEMPLATE_PATH}")

    with open(VIEWER_TEMPLATE_PATH, "r", encoding="utf-8") as fp:
        template = fp.read()

    items = scan_result_dir()
    data_script = (
        "<script>window.__NAGI_COMBINE_DATA__ = "
        + json.dumps(items, ensure_ascii=False)
        + ";</script>\n</head>"
    )
    html = template.replace("</head>", data_script, 1)

    with open(COMBINE_HTML_PATH, "w", encoding="utf-8") as fp:
        fp.write(html)
    return COMBINE_HTML_PATH


def main():
    # 支援 `python nagi.py --index` 只重新掃描 result/ 重建總覽頁/合併統計頁,
    # 不解析新報告。
    if len(sys.argv) > 1 and sys.argv[1] in ("--index", "-i", "index"):
        out_path = generate_index_html()
        combine_path = generate_combine_html()
        print(f"已重新產生報告總覽: {out_path}")
        print(f"已重新產生合併統計頁: {combine_path}")
        return

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

    # 5. 現撈直接產生報告(資料內嵌進 HTML,並另存一份原始 .json),自動開瀏覽器
    payload = build_payload(report_code, report, selected, records)
    out_path = generate_report_html(payload)
    json_path = save_report_json(payload)
    index_path = generate_index_html()
    combine_path = generate_combine_html()
    print(f"\n報告已產生: {out_path}")
    print(f"原始資料已存: {json_path}")
    print(f"總覽頁已更新: {index_path}")
    print(f"合併統計頁已更新: {combine_path}")
    if not webbrowser.open(f"file://{out_path}"):
        print("(無法自動開啟瀏覽器,請手動打開上面的路徑)")


if __name__ == "__main__":
    main()
