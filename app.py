import streamlit as st
import pandas as pd
import sqlite3
import datetime
import requests
import io

# ==========================================
# 1. 시스템 설정 및 기본 상수
# ==========================================
st.set_page_config(page_title="4조2교대 대체근무 배정 시스템", layout="wide")
DB_FILE = "shift_schedule.db"

# 2026-10-26 기준 Day 0 설정
BASE_DATE = datetime.date(2026, 10, 26)

# [B-C 페어] & [A-D 페어] 불변 8일 완전 순환 매핑
CYCLE_PATTERN = [
    {"day": 0, "day_work": "B", "night_work": "C", "day_ot": "A", "night_ot": "D"},
    {"day": 1, "day_work": "B", "night_work": "C", "day_ot": "D", "night_ot": "A"},
    {"day": 2, "day_work": "D", "night_work": "A", "day_ot": "B", "night_ot": "C"},
    {"day": 3, "day_work": "D", "night_work": "A", "day_ot": "C", "night_ot": "B"},
    {"day": 4, "day_work": "C", "night_work": "B", "day_ot": "D", "night_ot": "A"},
    {"day": 5, "day_work": "C", "night_work": "B", "day_ot": "A", "night_ot": "D"},
    {"day": 6, "day_work": "A", "night_work": "D", "day_ot": "C", "night_ot": "B"},
    {"day": 7, "day_work": "A", "night_work": "D", "day_ot": "B", "night_ot": "C"},
]

DEPTS = ["선탄", "수송"]

# ==========================================
# 2. 데이터베이스(SQLite) 초기화 (선탄/수송 120인)
# ==========================================
def init_db(reset_members=False):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    c = conn.cursor()
    c.execute("PRAGMA journal_mode=WAL;")

    # 인원 테이블 (dept 추가)
    c.execute('''
        CREATE TABLE IF NOT EXISTS members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dept TEXT,
            name TEXT UNIQUE,
            team TEXT,
            pin TEXT,
            reg_order INTEGER,
            emg_order INTEGER,
            status TEXT DEFAULT '재직'
        )
    ''')
    # 휴가 기록 테이블 (dept 추가)
    c.execute('''
        CREATE TABLE IF NOT EXISTS leaves (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dept TEXT,
            name TEXT,
            team TEXT,
            target_date TEXT,
            shift_type TEXT,
            leave_type TEXT,
            created_at TEXT,
            is_late INTEGER DEFAULT 0,
            is_cancelled INTEGER DEFAULT 0
        )
    ''')
    # 설정 테이블
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    ''')

    # 관리자 초기 마스터 비밀번호
    c.execute("SELECT value FROM settings WHERE key = 'admin_password'")
    if not c.fetchone():
        c.execute("INSERT INTO settings (key, value) VALUES ('admin_password', 'admin1234')")

    if reset_members:
        c.execute("DELETE FROM members")

    # 기초 인원 등록 (선탄 60명, 수송 60명 총 120명)
    c.execute("SELECT count(*) FROM members")
    if c.fetchone()[0] == 0:
        sample_members = []
        for d in DEPTS:
            for team in ['A', 'B', 'C', 'D']:
                for i in range(1, 31):
                    name = f"{d}_{team}조_{i:02d}"
                    sample_members.append((d, name, team, "1234", i, i, "재직"))

        c.executemany('''
            INSERT INTO members (dept, name, team, pin, reg_order, emg_order, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', sample_members)

    conn.commit()
    conn.close()

init_db()

# ==========================================
# 3. 설정 관리 및 알림 모듈
# ==========================================
def get_setting(key, default=""):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else default

def save_setting(key, value):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

def send_alert(title, message):
    teams_url = get_setting('teams_webhook')
    tg_token = get_setting('telegram_token')
    tg_chat = get_setting('telegram_chat_id')

    if teams_url:
        try:
            requests.post(teams_url, json={"title": title, "text": message}, timeout=2.0)
        except Exception:
            pass

    if tg_token and tg_chat:
        try:
            tg_url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
            requests.post(tg_url, data={"chat_id": tg_chat, "text": f"[{title}]\n{message}"}, timeout=2.0)
        except Exception:
            pass

# ==========================================
# 4. 배정 알고리즘 엔진 (조직별 완전 독립 구동)
# ==========================================
def get_shift_info(target_date):
    diff = (target_date - BASE_DATE).days
    idx = diff % 8
    return CYCLE_PATTERN[idx]

def get_week_bounds(target_date):
    sun_offset = (target_date.weekday() + 1) % 7
    start_sun = target_date - datetime.timedelta(days=sun_offset)
    end_sat = start_sun + datetime.timedelta(days=6)
    return start_sun, end_sat

def run_auto_assignment_by_dept(dept, start_date, end_date, manual_overrides=None):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    members_df = pd.read_sql_query("SELECT * FROM members WHERE dept = ? AND status = '재직'", conn, params=(dept,))
    leaves_df = pd.read_sql_query("SELECT * FROM leaves WHERE dept = ? AND is_cancelled = 0", conn, params=(dept,))
    conn.close()

    team_queues = {}
    for t in ['A', 'B', 'C', 'D']:
        team_queues[t] = members_df[members_df['team'] == t].sort_values(by='reg_order')['name'].tolist()

    results = []
    person_ot_dates = {m: [] for m in members_df['name']}

    if manual_overrides:
        for (m_date_str, m_shift), m_workers_str in manual_overrides.items():
            if m_workers_str and str(m_workers_str).strip() not in ["-", "(미배정)"]:
                m_date = datetime.datetime.strptime(m_date_str, "%Y-%m-%d").date()
                for w in str(m_workers_str).split(','):
                    w_clean = w.strip().replace('*', '')
                    if w_clean in person_ot_dates and m_date not in person_ot_dates[w_clean]:
                        person_ot_dates[w_clean].append(m_date)

    curr = start_date
    while curr <= end_date:
        shift_info = get_shift_info(curr)
        c_str = curr.strftime("%Y-%m-%d")
        sun, sat = get_week_bounds(curr)

        duty_day = shift_info['day_work']
        ot_day = shift_info['day_ot']
        day_absents = leaves_df[(leaves_df['target_date'] == c_str) & 
                                (leaves_df['team'] == duty_day) & 
                                (leaves_df['shift_type'] == '주간')]['name'].tolist()

        duty_night = shift_info['night_work']
        ot_night = shift_info['night_ot']
        night_absents = leaves_df[(leaves_df['target_date'] == c_str) & 
                                  (leaves_df['team'] == duty_night) & 
                                  (leaves_df['shift_type'] == '야간')]['name'].tolist()

        for s_type, d_team, o_team, absents in [('주간', duty_day, ot_day, day_absents),
                                                ('야간', duty_night, ot_night, night_absents)]:
            key = (c_str, s_type)

            if manual_overrides and key in manual_overrides:
                manual_val = str(manual_overrides[key]).strip()
                assigned_workers = [w.strip() for w in manual_val.split(',') if w.strip()]
                for mw in assigned_workers:
                    mw_clean = mw.replace('*', '')
                    if o_team in team_queues and mw_clean in team_queues[o_team]:
                        if team_queues[o_team] and team_queues[o_team][0] == mw_clean:
                            team_queues[o_team].pop(0)
                            team_queues[o_team].append(mw_clean)
            else:
                assigned_workers = []
                for _ in absents:
                    q = team_queues[o_team]
                    candidate = None

                    if not q:
                        assigned_workers.append("(미배정)")
                        continue

                    for _ in range(len(q)):
                        p = q[0]
                        is_leave_today = not leaves_df[(leaves_df['target_date'] == c_str) & (leaves_df['name'] == p)].empty
                        this_week_ots = [d for d in person_ot_dates[p] if sun <= d <= sat]
                        has_weekly_ot = len(this_week_ots) >= 1
                        has_consecutive = (curr - datetime.timedelta(days=1)) in person_ot_dates[p]

                        if not is_leave_today and not has_weekly_ot and not has_consecutive:
                            candidate = p
                            q.pop(0)
                            q.append(p)
                            break
                        else:
                            q.pop(0)
                            q.append(p)

                    if candidate:
                        assigned_workers.append(candidate)
                        person_ot_dates[candidate].append(curr)
                    else:
                        assigned_workers.append("(미배정)")

            results.append({
                "일자": c_str,
                "요일": ["월", "화", "수", "목", "금", "토", "일"][curr.weekday()],
                "소속": dept,
                "근무형태": s_type,
                "본근무조": d_team,
                "휴가/결원자": ", ".join(absents) if absents else "-",
                "대근배정조": o_team,
                "대체근무자": ", ".join(assigned_workers) if assigned_workers else "-"
            })

        curr += datetime.timedelta(days=1)

    return pd.DataFrame(results), team_queues

# 계획표 DB 저장/조회 헬퍼
def save_published_plan(dept, df):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    df.to_sql(f"published_plan_{dept}", conn, if_exists="replace", index=False)
    conn.close()

def load_published_plan(dept):
    conn = sqlite3.connect(DB_FILE, timeout=10)
    try:
        df = pd.read_sql_query(f"SELECT * FROM published_plan_{dept}", conn)
    except Exception:
        df = pd.DataFrame()
    conn.close()
    return df

# ==========================================
# 5. 사용자 인터페이스 (Streamlit UI)
# ==========================================
if "is_admin" not in st.session_state:
    st.session_state.is_admin = False

with st.sidebar:
    st.header("🔐 관리자 시스템")
    if not st.session_state.is_admin:
        admin_pwd_input = st.text_input("마스터 비밀번호", type="password")
        if st.button("관리자 로그인", use_container_width=True):
            current_master_pwd = get_setting('admin_password', 'admin1234')
            if admin_pwd_input == current_master_pwd:
                st.session_state.is_admin = True
                st.success("관리자 권한 승인")
                st.rerun()
            else:
                st.error("비밀번호 불일치")
    else:
        st.success("🟢 관리자 권한 활성화됨")
        if st.button("로그아웃", use_container_width=True):
            st.session_state.is_admin = False
            st.rerun()

st.title("⚙️ 4조2교대 대체근무 배정 시스템 (선탄 / 수송)")

if st.session_state.is_admin:
    conn = sqlite3.connect(DB_FILE, timeout=10)
    late_leaves_df = pd.read_sql_query("SELECT * FROM leaves WHERE is_late = 1 AND is_cancelled = 0", conn)
    conn.close()
    if not late_leaves_df.empty:
        st.error(f"🚨 **[관리자 알림]** 10일 마감 이후 접수된 긴급 휴가가 **{len(late_leaves_df)}건** 있습니다.")

# 권한별 탭 구성 (일반 조원도 대근계획표 확인 탭 열람 가능)
if not st.session_state.is_admin:
    tab1, tab2 = st.tabs(["👤 근무자 휴가 신청", "📋 대근계획표 확인 (선탄/수송)"])
    tab3, tab4 = None, None
else:
    tab1, tab2, tab3, tab4 = st.tabs([
        "👤 근무자 휴가 신청", 
        "🛠️ 대근계획표 생성/관리", 
        "📊 통계 및 실적 집계", 
        "⚙️ 조원 명단 및 설정"
    ])

# ------------------------------------------
# TAB 1: 근무자 휴가 신청 (공용)
# ------------------------------------------
with tab1:
    st.subheader("내 휴가 등록 및 취소")
    col_l1, col_l2 = st.columns([1, 2])

    with col_l1:
        conn = sqlite3.connect(DB_FILE, timeout=10)
        all_members = pd.read_sql_query("SELECT * FROM members ORDER BY dept, team, reg_order", conn)
        conn.close()

        sel_dept = st.selectbox("소속 조직 선택", DEPTS, key="tab1_dept")
        sel_team = st.selectbox("소속 조 선택", ["A", "B", "C", "D"], key="tab1_team")

        filtered_members = all_members[(all_members['dept'] == sel_dept) & (all_members['team'] == sel_team)]['name'].tolist()
        sel_name = st.selectbox("성명 선택", filtered_members if filtered_members else ["인원 없음"])
        input_pin = st.text_input("개인 PIN 번호 (기본: 1234)", type="password", key="tab1_pin")

        target_date = st.date_input("휴가 대상일", datetime.date.today())
        sel_shift = st.radio("근무 형태", ["주간", "야간"], horizontal=True)
        sel_type = st.selectbox("휴가 종류", ["연차", "청원휴가", "병가", "육아휴직", "기타"])

        if st.button("휴가 등록 제출", use_container_width=True):
            shift_info = get_shift_info(target_date)
            actual_shift = None
            if sel_team == shift_info['day_work']:
                actual_shift = "주간"
            elif sel_team == shift_info['night_work']:
                actual_shift = "야간"

            if actual_shift is None:
                st.error(f"❌ {target_date}은(는) {sel_team}조의 휴무일입니다. 휴가를 신청할 수 없습니다.")
            elif sel_shift != actual_shift:
                st.error(f"❌ {target_date}은(는) {sel_team}조의 [{actual_shift}] 근무일입니다. 신청 형태와 맞지 않습니다.")
            else:
                user_row = all_members[all_members['name'] == sel_name]
                if not user_row.empty and str(user_row.iloc[0]['pin']) == input_pin:
                    now = datetime.datetime.now()
                    is_late = 1 if now.day > 10 else 0

                    conn = sqlite3.connect(DB_FILE, timeout=10)
                    c = conn.cursor()
                    c.execute('''
                        INSERT INTO leaves (dept, name, team, target_date, shift_type, leave_type, created_at, is_late)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (sel_dept, sel_name, sel_team, target_date.strftime("%Y-%m-%d"), sel_shift, sel_type, now.strftime("%Y-%m-%d %H:%M:%S"), is_late))
                    conn.commit()
                    conn.close()

                    if is_late:
                        st.warning("⚠️ 10일 마감일 이후 신청되어 관리자에게 알림이 발송되었습니다.")
                        send_alert("긴급 휴가 알림", f"[{sel_dept}] {sel_team}조 {sel_name}님이 마감일 이후 {target_date}({sel_shift}) 휴가를 신청했습니다.")
                    else:
                        st.success("✅ 휴가 등록이 완료되었습니다.")
                    st.rerun()
                else:
                    st.error("PIN 번호가 일치하지 않습니다.")

    with col_l2:
        st.markdown(f"**[{sel_name}] 님의 휴가 신청 내역**")
        conn = sqlite3.connect(DB_FILE, timeout=10)
        my_leaves = pd.read_sql_query(
            "SELECT id, dept as 소속, target_date as 대상일, shift_type as 형태, leave_type as 구분, created_at as 신청일시, is_late as 마감후신청, is_cancelled as 취소여부 FROM leaves WHERE name = ?", 
            conn, params=(sel_name,)
        )
        conn.close()

        if not my_leaves.empty:
            st.dataframe(my_leaves, use_container_width=True)
            cancel_id = st.number_input("취소할 신청 번호(id)", min_value=0, step=1)
            if st.button("선택한 신청 취소"):
                conn = sqlite3.connect(DB_FILE, timeout=10)
                c = conn.cursor()
                c.execute("UPDATE leaves SET is_cancelled = 1 WHERE id = ? AND name = ?", (cancel_id, sel_name))
                conn.commit()
                conn.close()
                st.info("신청이 취소되었습니다.")
                st.rerun()
        else:
            st.info("등록된 휴가 내역이 없습니다.")

# ------------------------------------------
# TAB 2: 대근계획표 (조원 확인 / 관리자 생성·관리)
# ------------------------------------------
with tab2:
    if not st.session_state.is_admin:
        # 일반 조원 전용 조회 화면
        st.subheader("📋 확정 대근계획표 확인")
        plan_dept_tabs = st.tabs(["⛏️ 선탄 대근계획표", "🚛 수송 대근계획표"])

        for idx, d_name in enumerate(DEPTS):
            with plan_dept_tabs[idx]:
                df_view = load_published_plan(d_name)
                if not df_view.empty:
                    st.dataframe(df_view, use_container_width=True, height=450)
                    out = io.BytesIO()
                    with pd.ExcelWriter(out, engine='openpyxl') as w:
                        df_view.to_excel(w, index=False, sheet_name=f'{d_name}_대근계획표')
                    st.download_button(
                        label=f"📥 {d_name} 대근계획표 엑셀 다운로드",
                        data=out.getvalue(),
                        file_name=f"{d_name}_대근계획표.xlsx",
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        key=f"user_dl_{d_name}"
                    )
                else:
                    st.info(f"현재 등록된 {d_name} 대근계획표가 없습니다. 관리자가 계획표를 생성·저장하면 이곳에 표시됩니다.")
    else:
        # 관리자 전용 생성 및 편집 화면
        st.subheader("🛠️ 조직별 대근계획표 생성, 편집 및 배포")

        col_opt1, col_opt2, col_opt3 = st.columns([1, 1, 1])
        with col_opt1:
            plan_year = st.selectbox("대상 연도", [2026, 2027], index=0)
        with col_opt2:
            plan_month = st.selectbox("대상 월 (대근 계획월)", list(range(1, 13)), index=10)

        if plan_month == 1:
            def_start = datetime.date(plan_year - 1, 12, 16)
        else:
            def_start = datetime.date(plan_year, plan_month - 1, 16)
        def_end = datetime.date(plan_year, plan_month, 15)

        with col_opt3:
            st.info(f"산정 기간: **{def_start} ~ {def_end}**")

        admin_dept_tabs = st.tabs(["⛏️ 선탄 관리", "🚛 수송 관리"])

        for idx, d_name in enumerate(DEPTS):
            with admin_dept_tabs[idx]:
                k_prefix = f"plan_{d_name}"

                col_btn_gen, col_btn_sync = st.columns([1, 1])
                with col_btn_gen:
                    if st.button(f"🚀 {d_name} 대근계획표 자동 생성", key=f"gen_{d_name}", use_container_width=True):
                        with st.spinner(f"{d_name} 큐 순환 및 제약조건 검증 중..."):
                            d_df, d_queues = run_auto_assignment_by_dept(d_name, def_start, def_end)
                            st.session_state[f"{k_prefix}_df"] = d_df
                            st.session_state[f"{k_prefix}_orig"] = d_df.copy()
                            st.session_state[f"{k_prefix}_queues"] = d_queues
                            save_published_plan(d_name, d_df) # 생성 즉시 DB 저장 (조원 공유)
                            st.success(f"{d_name} 초안이 생성되어 조원 열람용 DB에 실시간 저장되었습니다!")

                # 세션에 없으면 DB에서 불러오기
                if f"{k_prefix}_df" not in st.session_state:
                    db_plan = load_published_plan(d_name)
                    if not db_plan.empty:
                        st.session_state[f"{k_prefix}_df"] = db_plan
                        st.session_state[f"{k_prefix}_orig"] = db_plan.copy()

                if f"{k_prefix}_df" in st.session_state:
                    st.write(f"#### 📋 {d_name} 대근계획표 편집기")
                    edited_p = st.data_editor(st.session_state[f"{k_prefix}_df"], use_container_width=True, num_rows="fixed", key=f"editor_{d_name}")

                    if st.button(f"🔄 {d_name} 수동 변경사항 반영 및 순번 재계산 (Skip)", key=f"recalc_{d_name}", use_container_width=True):
                        manual_overrides = {}
                        orig_df = st.session_state[f"{k_prefix}_orig"]
                        for r_i, r_val in edited_p.iterrows():
                            o_v = str(orig_df.iloc[r_i]['대체근무자']).strip()
                            c_v = str(r_val['대체근무자']).strip()
                            if c_v != o_v and c_v not in ["-", "(미배정)"]:
                                manual_overrides[(r_val['일자'], r_val['근무형태'])] = c_v

                        if manual_overrides:
                            re_df, re_q = run_auto_assignment_by_dept(d_name, def_start, def_end, manual_overrides=manual_overrides)
                            st.session_state[f"{k_prefix}_df"] = re_df
                            st.session_state[f"{k_prefix}_queues"] = re_q
                            save_published_plan(d_name, re_df) # 수동 수정 결과도 조원 DB에 즉시 반영
                            st.success(f"총 {len(manual_overrides)}건의 수동 배정이 반영되어 조원 화면에도 저장되었습니다.")
                            st.rerun()
                        else:
                            st.info("수정된 셀이 없습니다. 대체근무자 이름을 바꾼 뒤 눌러주세요.")

                    col_down, col_carry = st.columns([1, 1])
                    with col_down:
                        out = io.BytesIO()
                        with pd.ExcelWriter(out, engine='openpyxl') as w:
                            edited_p.to_excel(w, index=False, sheet_name=f'{d_name}_초안')
                        st.download_button(
                            label=f"📥 {d_name} 대근계획표 엑셀 다운로드",
                            data=out.getvalue(),
                            file_name=f"{d_name}_대근계획표_{plan_year}년_{plan_month}월.xlsx",
                            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                            use_container_width=True,
                            key=f"dl_{d_name}"
                        )
                    with col_carry:
                        if st.button(f"💾 {d_name} 순번을 다음 달 시작 순번으로 확정(이월)", key=f"carry_{d_name}", use_container_width=True):
                            if f"{k_prefix}_queues" in st.session_state:
                                conn = sqlite3.connect(DB_FILE, timeout=10)
                                c = conn.cursor()
                                for team_name, q_list in st.session_state[f"{k_prefix}_queues"].items():
                                    for new_order, member_name in enumerate(q_list, start=1):
                                        c.execute("UPDATE members SET reg_order = ? WHERE name = ? AND dept = ?", (new_order, member_name, d_name))
                                conn.commit()
                                conn.close()
                                st.success(f"✅ {d_name} 순번이 DB에 이월 반영되었습니다!")

# ------------------------------------------
# TAB 3: 통계 및 실적 집계 (관리자 전용)
# ------------------------------------------
if tab3 is not None:
    with tab3:
        st.subheader("실제 운영 대근계획표 업로드 및 종합 실적 집계")
        sel_stat_dept = st.radio("집계 대상 조직 선택", DEPTS, horizontal=True)

        uploaded_excel = st.file_uploader(f"{sel_stat_dept} 실제 운영 엑셀 파일 (.xlsx)", type=["xlsx"])
        if uploaded_excel is not None:
            act_df = pd.read_excel(uploaded_excel)
            st.dataframe(act_df.head(6), use_container_width=True)

            all_ot_entries = []
            for row_val in act_df['대체근무자'].dropna():
                if str(row_val).strip() != "-":
                    for item in str(row_val).split(','):
                        name_clean = item.strip()
                        if name_clean:
                            all_ot_entries.append(name_clean)

            conn = sqlite3.connect(DB_FILE, timeout=10)
            members_data = pd.read_sql_query("SELECT name, team FROM members WHERE dept = ? ORDER BY team, reg_order", conn, params=(sel_stat_dept,)).to_dict('records')
            conn.close()

            summary_rows = []
            for m in members_data:
                name = m['name']
                leave_count = len(act_df[act_df['휴가/결원자'].astype(str).str.contains(name, na=False)])
                regular_ot = all_ot_entries.count(name)
                emergency_ot = all_ot_entries.count(f"{name}*")

                summary_rows.append({
                    "소속": sel_stat_dept,
                    "성명": name,
                    "소속조": m['team'],
                    "휴가 사용일수": leave_count,
                    "정기대근 횟수": regular_ot,
                    "돌발대근(*) 횟수": emergency_ot,
                    "총 대근 횟수": regular_ot + emergency_ot
                })

            st.write(f"### 📊 [{sel_stat_dept}] 인원별 실적 종합 현황")
            st.dataframe(pd.DataFrame(summary_rows), use_container_width=True)

# ------------------------------------------
# TAB 4: 조원 명단 엑셀 관리 및 설정 (관리자 전용)
# ------------------------------------------
if tab4 is not None:
    with tab4:
        st.subheader("👥 조원 명단 엑셀 일괄 동기화 및 보안 설정")

        col_e1, col_e2 = st.columns([1, 1])

        with col_e1:
            st.write("#### 1. 현재 전체 명단 엑셀 다운로드")
            conn = sqlite3.connect(DB_FILE, timeout=10)
            current_members = pd.read_sql_query(
                "SELECT dept as 소속조직, name as 성명, team as 소속조, pin as PIN번호, reg_order as 정기순번, emg_order as 돌발순번, status as 재직상태 FROM members ORDER BY dept, team, reg_order", 
                conn
            )
            conn.close()

            excel_buffer = io.BytesIO()
            with pd.ExcelWriter(excel_buffer, engine='openpyxl') as writer:
                current_members.to_excel(writer, index=False, sheet_name='조원명단_전체')

            st.download_button(
                label="📥 전체 조원 명단 엑셀 다운로드 (.xlsx)",
                data=excel_buffer.getvalue(),
                file_name="전체_조원명단(선탄_수송).xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True
            )

        with col_e2:
            st.write("#### 2. 수정된 명단 엑셀 업로드")
            uploaded_roster = st.file_uploader("수정 완료된 조원 명단 엑셀 파일", type=["xlsx"], key="roster_uploader")

            if uploaded_roster is not None:
                if st.button("📤 엑셀 내용으로 DB 전체 동기화", use_container_width=True):
                    try:
                        new_roster_df = pd.read_excel(uploaded_roster)
                        required_cols = ['소속조직', '성명', '소속조', 'PIN번호', '정기순번', '돌발순번', '재직상태']

                        if all(col in new_roster_df.columns for col in required_cols):
                            conn = sqlite3.connect(DB_FILE, timeout=10)
                            c = conn.cursor()
                            c.execute("DELETE FROM members")

                            bulk_data = []
                            for _, row in new_roster_df.iterrows():
                                bulk_data.append((
                                    str(row['소속조직']).strip(),
                                    str(row['성명']).strip(),
                                    str(row['소속조']).strip().upper(),
                                    str(row['PIN번호']).strip(),
                                    int(row['정기순번']),
                                    int(row['돌발순번']),
                                    str(row['재직상태']).strip()
                                ))

                            c.executemany('''
                                INSERT INTO members (dept, name, team, pin, reg_order, emg_order, status)
                                VALUES (?, ?, ?, ?, ?, ?, ?)
                            ''', bulk_data)
                            conn.commit()
                            conn.close()
                            st.success(f"총 {len(bulk_data)}명의 조원 데이터(선탄/수송)가 정상 동기화되었습니다!")
                            st.rerun()
                        else:
                            st.error(f"컬럼명이 맞지 않습니다. 필수 컬럼: {required_cols}")
                    except Exception as e:
                        st.error(f"업로드 오류: {e}")

        st.markdown("---")

        col_t1, col_t2 = st.columns([1.2, 1])
        with col_t1:
            st.write(f"#### 📋 등록된 전체 인원 목록 ({len(current_members)}명)")
            st.dataframe(current_members, use_container_width=True, height=350)

        with col_t2:
            st.write("#### 🔐 관리자 마스터 비밀번호 변경")
            curr_pwd = get_setting('admin_password', 'admin1234')
            new_pwd = st.text_input("새 관리자 마스터 비밀번호", value=curr_pwd, type="password")
            if st.button("마스터 비밀번호 변경 저장", use_container_width=True):
                if new_pwd.strip():
                    save_setting('admin_password', new_pwd.strip())
                    st.success("관리자 마스터 비밀번호가 변경되었습니다.")
                else:
                    st.error("비밀번호를 공란으로 둘 수 없습니다.")

            st.write("#### 🔔 긴급 알림(Teams / Telegram) 설정")
            new_teams = st.text_input("MS Teams Webhook URL", value=get_setting('teams_webhook'))
            new_tg_token = st.text_input("텔레그램 Bot Token", value=get_setting('telegram_token'))
            new_tg_chat = st.text_input("텔레그램 Chat ID", value=get_setting('telegram_chat_id'))

            if st.button("알림 설정 저장 및 테스트 발송", use_container_width=True):
                save_setting('teams_webhook', new_teams)
                save_setting('telegram_token', new_tg_token)
                save_setting('telegram_chat_id', new_tg_chat)
                send_alert("시스템 연결 테스트", "대근 배정 시스템 테스트 알림입니다.")
                st.success("알림 설정이 저장되었습니다.")
