import datetime
import random
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import pandas as pd
import pytz
import requests
import sys

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15"
]

def get_safe_headers():
    """隨機產生一個模仿真實瀏覽器的 Header，規避 403/406 限制"""
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
        "Origin": "https://mlb.com",
        "Referer": "https://mlb.com/"
    }

def random_delay(min_sec=1, max_sec=3):
    """加入隨機微幅延遲（Jitter），模擬人類點擊行為，避免被視為惡意爬蟲"""
    sleep_time = random.uniform(min_sec, max_sec)
    time.sleep(sleep_time)

def upsert_to_google_sheet_hybrid(spreadsheet_id, sheet_name, df, id_column_name):
    """
    結合試算表 ID 與分頁名稱，以 'Upsert' 邏輯寫入 Google Sheet。
    
    :param spreadsheet_id: 網址 /d/ 後面、/edit 前面的那一串長 ID
    :param sheet_name: 工作表分頁名稱 (例如: '比賽數據' 或 '投手數據')
    :param df: 要寫入的 Pandas DataFrame 資料
    :param id_column_name: 用來比對的唯一 ID 欄位名稱 (例如: 'Game_ID' 或 'PitcherId')
    """
    if df is None or df.empty:
        print(f"⚠️ ({sheet_name}) 沒有新資料需要處理。")
        return
        
    scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    try:
        # 1. 連結 Google Sheets 並用「長 ID」開啟整份試算表 (移除了會衝突的 creds.refresh)
        creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
        client = gspread.authorize(creds)
        spreadsheet = client.open_by_key(spreadsheet_id)
        
        # 2. 用「分頁名稱」鎖定工作表
        try:
            worksheet = spreadsheet.worksheet(sheet_name)
            # 讀取現有雲端資料
            existing_data = worksheet.get_all_records()
            df_existing = pd.DataFrame(existing_data)
        except gspread.exceptions.WorksheetNotFound:
            # 如果這個名字的分頁完全不存在，自動幫您建立一個新的
            worksheet = spreadsheet.add_worksheet(title=sheet_name, rows="1000", cols="20")
            df_filled = df.fillna("")
            data_to_upload = [df_filled.columns.values.tolist()] + df_filled.values.tolist()
            worksheet.update(data_to_upload)
            print(f"✨ 發現全新分頁，已直接建立並寫入 {len(df)} 筆資料到 ({sheet_name})。")
            return

        # 3. 如果雲端是空的（只有欄位名稱或完全沒資料），直接整批追加
        if df_existing.empty or id_column_name not in df_existing.columns:
            df_filled = df.fillna("")
            if worksheet.row_count == 0 or not worksheet.row_values(1):
                data_to_upload = [df_filled.columns.values.tolist()] + df_filled.values.tolist()
                worksheet.update(data_to_upload)
            else:
                worksheet.append_rows(df_filled.values.tolist())
            print(f"📝 雲端表格為空，已直接寫入 {len(df)} 筆新資料到 ({sheet_name})。")
            return

        # 4. 確保 ID 欄位均轉為字串以利比對
        df[id_column_name] = df[id_column_name].astype(str)
        df_existing[id_column_name] = df_existing[id_column_name].astype(str)

        # 建立 A1 範圍映射表（雲端資料從第 2 列開始）
        id_to_row_map = {str(id_val): index + 2 for index, id_val in enumerate(df_existing[id_column_name])}

        new_rows_to_append = []
        update_count = 0
        insert_count = 0
        df_filled_new = df.fillna("")

        # 5. 開始逐筆比對
        for _, row in df_filled_new.iterrows():
            current_id = str(row[id_column_name])
            row_list = row.values.tolist()

            if current_id in id_to_row_map:
                # 🎯 【存在就更新】：找到相同 ID，直接覆蓋該 Row
                target_row_number = id_to_row_map[current_id]
                end_column_letter = chr(64 + len(row_list))  # 自動計算欄位英文字母範圍
                cell_range = f"A{target_row_number}:{end_column_letter}{target_row_number}"
                
                worksheet.update([row_list], cell_range)
                update_count += 1
            else:
                # ➕ 【不存在就新增】：先收集起來，最後 batch 追加
                new_rows_to_append.append(row_list)
                insert_count += 1

        # 批次追加新資料（效率最高）
        if new_rows_to_append:
            worksheet.append_rows(new_rows_to_append)

        print(f"📊 ({sheet_name}) 同步完成！共更新 {update_count} 筆現有資料，新增 {insert_count} 筆新資料。")

    except Exception as e:
        print(f"❌ 同步到 Google Sheet ({sheet_name}) 失敗: {e}")


def get_pitcher_stats(pitcher_id, season=2023):
    """
    輸入投手 ID，取得該賽季的生涯/年度進階數據統計。
    """
    domain = "statsapi.mlb.com"
    path = f"/api/v1/people/{pitcher_id}/stats"
    url = f"https://{domain}{path}"
    
    params = {
        "stats": "seasonAdvanced",
        "group": "pitching",
        "season": season
    }
    
    try:
        random_delay(1.5, 3)
        adv_response = requests.get(url, params=params, headers=get_safe_headers(), timeout=10)
        random_delay(0.5, 1.5)
        params["stats"] = "statsSingleSeason"
        std_response = requests.get(url, params=params, headers=get_safe_headers(), timeout=10)
    except requests.exceptions.RequestException as e:
        print(f"❌ 網路連線失敗: {e}")
        raise e
        
    if adv_response.status_code != 200 or std_response.status_code != 200:
        print(f"❌ 無法取得投手數據。狀態碼: {adv_response.status_code}")
        raise e
        
    try:
        adv_json = adv_response.json()
        std_json = std_response.json()
        
        # 安全檢查：確認 stats 存在且有內容
        if "stats" not in std_json or not std_json["stats"] or not std_json["stats"][0].get("splits"):
            print(f"⚠️ 找不到該投手在 {season} 賽季的數據")
            return None
            
        # 🔥 修正點：從第一層 stats 陣列的第一個元素中，抓取 splits 陣列的第一筆數據
        std_splits = std_json["stats"][0]["splits"][0]
        std_stats = std_splits.get("stat", {})
        player_info = std_splits.get("player", {})
        
        # 安全抓取進階數據的對應層級
        adv_stats = {}
        if "stats" in adv_json and adv_json["stats"] and adv_json["stats"][0].get("splits"):
            adv_stats = adv_json["stats"][0]["splits"][0].get("stat", {})
        
        # 1. 處理投球慣用手
        pitch_hand = player_info.get("pitchHand", {}).get("code")
        if not pitch_hand:
            try:
                p_url = f"https://{domain}/api/v1/people/{pitcher_id}"
                p_res = requests.get(p_url, timeout=5).json()
                pitch_hand = p_res.get("people", [{}])[0].get("pitchHand", {}).get("code", "R")
            except:
                pitch_hand = "R"

        # 2. 直接由標準數據計算 K% 與 BB%，避開 API 欄位改名的風險
        bf = std_stats.get("battersFaced", 0)
        so = std_stats.get("strikeOuts", 0)
        bb = std_stats.get("baseOnBalls", 0)
        
        if bf > 0:
            k_rate = f"{(so / bf) * 100:.1f}%"
            bb_rate = f"{(bb / bf) * 100:.1f}%"
        else:
            k_rate = "0.0%"
            bb_rate = "0.0%"

        # 3. 處理 BABIP (若進階數據抓不到，使用標準數據公式計算)
        babip_val = adv_stats.get("babip")
        if not babip_val:
            h = std_stats.get("hits", 0)
            hr = std_stats.get("homeRuns", 0)
            ab = std_stats.get("atBats", 0)
            sf = std_stats.get("sacFlies", 0)
            denom = (ab - so - hr + sf)
            babip_val = f"{(h - hr) / denom:.3f}" if denom > 0 else ".000"

        pitcher_dict = {
            "PitcherId": pitcher_id,
            "PitcherName": player_info.get("fullName", "Unknown Player"),
            "TeamId": std_splits.get("team", {}).get("id"),
            "TeamName": std_splits.get("team", {}).get("name"),
            "pitchHand": pitch_hand,
            "GamesStarted": std_stats.get("gamesStarted"),
            "GamesPlayed": std_stats.get("gamesPlayed"),
            "InningsPitched": std_stats.get("inningsPitched"),
            "TotalBattersFaced": bf,
            "K%": k_rate,
            "BB%": bb_rate,
            "WHIP": std_stats.get("whip"),
            "BABIP": babip_val
        }
        return pd.DataFrame([pitcher_dict])
    except Exception as e:
        print(f"⚠️ 投手數據欄位解析失敗: {e}")
        return None

def get_games_by_date(game_date):
    """
    輸入日期 (格式: 'YYYY-MM-DD')，取得當天所有 MLB 比賽數據。
    """
    domain = "statsapi.mlb.com"
    # 🌟 步驟 1：只抓基本賽程，完全不帶任何 hydrate 參數，確保 100% 不噴 406
    schedule_url = f"https://{domain}/api/v1/schedule"
    schedule_params = {
        "sportId": 1,
        "date": game_date
    }
    
    try:
        # 這裡會以極輕量的方式安全通過大聯盟防火牆
        response = requests.get(schedule_url, params=schedule_params, headers=get_safe_headers(), timeout=12)
        if response.status_code != 200:
            print(f"❌ 賽程基本 API 請求失敗，狀態碼: {response.status_code}")
            return None
        schedule_data = response.json()
    except Exception as e:
        print(f"❌ 抓取基本賽程時發生異常: {e}")
        return None
        
    dates = schedule_data.get("dates", [])
    if not dates:
        return pd.DataFrame() # 當天無賽事
        
    games_list = []
    base_games = dates[0].get("games", [])
    
    print(f"   [Docker 備援防禦啟動] 成功獲取基本賽程，開始穿透 {len(base_games)} 場比賽的 Boxscore 數據...")
    
    # 🌟 步驟 2：疊代每場比賽，呼叫獨立的 Boxscore 端點抓取先發投手
    for idx, base_game in enumerate(base_games):
        game_id = base_game.get("gamePk")
        teams = base_game.get("teams", {})
        away_team = teams.get("away", {})
        home_team = teams.get("home", {})
        
        # 預設先發投手資訊
        away_pitcher_name, away_pitcher_id = "TBD", 0
        home_pitcher_name, home_pitcher_id = "TBD", 0
        
        # 呼叫單場 boxscore 輕量端點，這個端點無防護，Docker 可直接讀取
        boxscore_url = f"https://{domain}/api/v1/game/{game_id}/boxscore"
        
        try:
            # 這裡給予極短的隨機微幅等待（0.2 ~ 0.5 秒），確保極速穿透又不會過於密集
            time.sleep(random.uniform(0.2, 0.5))
            
            box_res = requests.get(boxscore_url, headers=get_safe_headers(), timeout=10)
            if box_res.status_code == 200:
                box_data = box_res.json()
                box_teams = box_data.get("teams", {})
                
                # 穿透客隊先發資訊
                away_box = box_teams.get("away", {})
                away_pitchers_list = away_box.get("pitchers", [])
                if away_pitchers_list:
                    # 陣列中的第一個 ID 就是這場比賽當天實際登板的先發投手！
                    first_away_pitcher_id = away_pitchers_list[0]
                    player_info = away_box.get("players", {}).get(f"ID{first_away_pitcher_id}", {}).get("person", {})
                    away_pitcher_id = first_away_pitcher_id
                    away_pitcher_name = player_info.get("fullName", "TBD")
                    
                # 穿透主隊先發資訊
                home_box = box_teams.get("home", {})
                home_pitchers_list = home_box.get("pitchers", [])
                if home_pitchers_list:
                    first_home_pitcher_id = home_pitchers_list[0]
                    player_info = home_box.get("players", {}).get(f"ID{first_home_pitcher_id}", {}).get("person", {})
                    home_pitcher_id = first_home_pitcher_id
                    home_pitcher_name = player_info.get("fullName", "TBD")
        except Exception as e:
            # 若單場 boxscore 卡頓，優雅降級跳過先發投手名字，確保整張大表不崩潰
            pass

        # 組合出完全符合您 Google Sheet 格式的 13 個完整欄位
        games_list.append({
            "Game_Date": game_date,
            "Game_ID": game_id,
            "Away_Team_Name": away_team.get("team", {}).get("name"),
            "Home_Team_Name": home_team.get("team", {}).get("name"),
            "Away_Started_Pitcher_Name": away_pitcher_name,
            "Away_Started_Pitcher_ID": int(away_pitcher_id),
            "Home_Started_Pitcher_Name": home_pitcher_name,
            "Home_Started_Pitcher_ID": int(home_pitcher_id),
            "Away_Score": away_team.get("score", 0),
            "Home_Score": home_team.get("score", 0),
            "Game_State": base_game.get("status", {}).get("detailedState"),
            "Venue_Name": base_game.get("venue", {}).get("name"),
            "Start_Time_UTC": base_game.get("gameDate")
        })
        
    return pd.DataFrame(games_list)


# ==============================================================================
# 🎮 測試執行
# ==============================================================================
if __name__ == "__TEST__":

    MY_SPREADSHEET_ID = "1_Bo0g95XIoxFO8s2A8de9o4ZHyEYu149gN0x24vbIBk" 
    
    target_date = "2026-07-08"
    print(f"\n=== 🔍 正在查詢 {target_date} 的比賽數據 ===")
    df_games = get_games_by_date(target_date)
    if df_games is not None:
        # 傳入長 ID、分頁名稱、DataFrame、唯一辨識欄位名稱
        upsert_to_google_sheet_hybrid(
            spreadsheet_id=MY_SPREADSHEET_ID, 
            sheet_name="team_stats", 
            df=df_games, 
            id_column_name="Game_ID"
        )

    pitcher_id = 670871
    print(f"\n=== 🔍 正在查詢投手 ID {pitcher_id} 的 2026 賽季數據 ===")
    df_pitcher = get_pitcher_stats(pitcher_id, season=2026)
    if df_pitcher is not None:
        # 傳入長 ID、分頁名稱、DataFrame、唯一辨識欄位名稱
        upsert_to_google_sheet_hybrid(
            spreadsheet_id=MY_SPREADSHEET_ID, 
            sheet_name="pitcher_record", 
            df=df_pitcher, 
            id_column_name="PitcherId"
        )

if __name__ == "__main__":
    # 🔥 1. 請在此處填入您 Google Sheet 的長串 Spreadsheet ID
    TARGET_SPREADSHEET_ID = "1_Bo0g95XIoxFO8s2A8de9o4ZHyEYu149gN0x24vbIBk" 
    
    print("🎬 === 開始執行 MLB 每日數據自動化同步流水線 ===")
    
    if len(sys.argv) > 1:
        us_today_date = sys.argv[1]
        try:
            # 依據輸入的日期字串，自動切出年份作為目標賽季，防止年度對不上的狀況
            current_season = int(us_today_date.split("-")[0])
            print(f"命令列參數偵測成功！")
        except Exception:
            print("❌ 輸入的日期格式有誤，請確保格式為 YYYY-MM-DD (例如: 2024-04-15)")
            sys.exit(1)
    else:
        # 預設機制：自動轉換為美國美東時間日期 (US/Eastern)
        tz_us_eastern = pytz.timezone("US/Eastern")
        current_us_time = datetime.datetime.now(tz_us_eastern)
        us_today_date = current_us_time.strftime("%Y-%m-%d")
        current_season = current_us_time.year
        print(f"未偵測到日期參數，自動啟用今日即時同步機制。")

    print(f"📅 鎖定查詢之美國日期為: {us_today_date}，目標賽季: {current_season}")
    
    print(f"\n📡 正在從 MLB 官方伺服器抓取 {us_today_date} 的比賽數據...")
    df_games = get_games_by_date(us_today_date)
    
    if df_games is None or df_games.empty:
        print(f"📅 日期 {us_today_date} 當天大聯盟沒有安排常規賽事。主流程提前結束。")
    else:
        print(f"✅ 成功獲取比賽資料，當日共計 {len(df_games)} 場對決。")
        
        pitcher_ids_set = set()
        for _, row in df_games.iterrows():
            away_pid = row["Away_Started_Pitcher_ID"]
            home_pid = row["Home_Started_Pitcher_ID"]
            if away_pid and int(away_pid) != 0: pitcher_ids_set.add(int(away_pid))
            if home_pid and int(home_pid) != 0: pitcher_ids_set.add(int(home_pid))
            
        print(f"🎯 經交叉比對，當日共需穿透抓取 {len(pitcher_ids_set)} 位先發投手的賽季數據...")
        
        pitchers_data_list = []
        processed_count = 0
        
        for p_id in pitcher_ids_set:
            processed_count += 1
            print(f"   [進度 {processed_count}/{len(pitcher_ids_set)}] 正在獲取投手 ID: {p_id} 的賽季統計...")
            
            p_stats = None
            max_retries = 3  
            
            for attempt in range(max_retries):
                try:
                    if attempt > 0:
                        # 🚀 遇到逾時或被擋時，主流程主動發動強力備援，拉長等待時間突破防線
                        wait_time = random.uniform(5.0, 10.0)
                        print(f"      ⚠️ 偵測到大聯盟限速或卡頓，啟動第 {attempt + 1} 次全面重試，後台避風頭 {wait_time:.1f} 秒...")
                        time.sleep(wait_time)
                    
                    p_stats = get_pitcher_stats(p_id, season=current_season)
                    break  
                except Exception as retry_err:
                    p_stats = None
                    if attempt == max_retries - 1:
                        print(f"      ❌ 嘗試 {max_retries}次 後仍連線失敗，原因: {retry_err}")
            
            if p_stats is not None and isinstance(p_stats, pd.DataFrame) and not p_stats.empty:
                pitchers_data_list.append(p_stats)
            else:
                print(f"      ⚠️ 投手 ID {p_id} 最終無法取得有效數據，跳過此球員。")
                
        if pitchers_data_list:
            df_pitchers = pd.concat(pitchers_data_list, ignore_index=True)
        else:
            df_pitchers = pd.DataFrame()
        
        print("\n☁️ 正在連線至 Google Sheets 進行智慧同步 (Upsert)...")
        
        # 寫入比賽數據表
        upsert_to_google_sheet_hybrid(
            spreadsheet_id=TARGET_SPREADSHEET_ID, 
            sheet_name="team_stats", 
            df=df_games, 
            id_column_name="Game_ID"
        )
        
        # 寫入投手數據表
        if not df_pitchers.empty:
            upsert_to_google_sheet_hybrid(
                spreadsheet_id=TARGET_SPREADSHEET_ID, 
                sheet_name="pitcher_record", 
                df=df_pitchers, 
                id_column_name="PitcherId"
            )
        else:
            print("⚠️ 未收集到任何有效的投手數據，跳過 pitcher_data 工作表更新。")
            
        print("\n🏁 === MLB 每日數據自動化同步流水線 順利執行完畢 ===")
