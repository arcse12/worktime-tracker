import json
from datetime import date
from io import BytesIO

import gspread
import pandas as pd
import streamlit as st
from google.oauth2.service_account import Credentials

# ===== Google Sheets 基本配置 =====
# 从 Streamlit Secrets 读取 Google 凭证
creds_json = st.secrets["gcp_service_account"]
creds_info = json.loads(creds_json)
creds = Credentials.from_service_account_info(
    creds_info,
    scopes=[
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
)
SPREADSHEET_NAME = "Massage_Work_Log"  # Google 表格文件名
SHEET_RECORD = "工时记录"
SHEET_STAFF = "员工表"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# 记录表列（加了 ID 方便修改/删除）
COLUMNS = [
    "ID",
    "日期",
    "员工姓名",
    "客人姓名",
    "服务项目",
    "服务时长(分钟)",
    "工时(小时)",
    "服务收入",
    "小费",
    "总收入",
]

STAFF_COLUMNS = ["员工姓名"]
DURATION_OPTIONS = [30, 45, 60, 75, 90, 105, 120]


# ------------ 价格规则：60分钟 = 65 ------------
def calc_price(duration_min: int) -> float:
    return round(duration_min / 60 * 65, 2)


# ------------ Google Sheets 客户端 & 工作表 ------------

@st.cache_resource
def get_gsheet_client():
    """使用 Streamlit Cloud 的 secrets 创建 gspread 客户端"""
    raw = st.secrets["gcp_service_account"]

    # 兼容两种写法：
    # 1) gcp_service_account = """{...json...}"""
    # 2) [gcp_service_account] type="service_account" ...
    if isinstance(raw, str):
        # 是字符串，就按 JSON 解析
        creds_info = json.loads(raw)
    else:
        # 是字典/映射，直接用
        creds_info = dict(raw)

    creds = Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return gspread.authorize(creds)


def get_or_create_worksheet(title: str):
    """打开指定工作表，不存在就创建并写表头"""
    client = get_gsheet_client()
    try:
        sh = client.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        # 如果表格不存在，就创建一个新的
        sh = client.create(SPREADSHEET_NAME)

    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows="1000", cols="20")
        # 新 sheet 写表头
        if title == SHEET_RECORD:
            ws.append_row(COLUMNS)
        elif title == SHEET_STAFF:
            ws.append_row(STAFF_COLUMNS)
    return ws


# ------------ 读写工时记录 ------------

def load_records() -> pd.DataFrame:
    """从 Google Sheets 读取工时记录"""
    ws = get_or_create_worksheet(SHEET_RECORD)
    records = ws.get_all_records()  # list[dict]
    df = pd.DataFrame(records)

    if df.empty:
        df = pd.DataFrame(columns=COLUMNS)

    # 确保所有列存在
    for col in COLUMNS:
        if col not in df.columns:
            if col in ["服务时长(分钟)", "工时(小时)", "服务收入", "小费", "总收入", "ID"]:
                df[col] = 0
            else:
                df[col] = ""

    # ID 处理
    df["ID"] = pd.to_numeric(df["ID"], errors="coerce")
    if df["ID"].isna().all():
        df["ID"] = range(1, len(df) + 1)
    else:
        max_id = int(df["ID"].max()) if not df["ID"].isna().all() else 0
        for idx, val in df["ID"].items():
            if pd.isna(val):
                max_id += 1
                df.at[idx, "ID"] = max_id

    df["ID"] = df["ID"].astype(int)

    return df[COLUMNS]


def save_all(records_df: pd.DataFrame):
    """把工时记录写回 Google Sheets 的“工时记录”工作表"""
    ws = get_or_create_worksheet(SHEET_RECORD)
    ws.clear()
    ws.append_row(COLUMNS)
    if not records_df.empty:
        rows = records_df[COLUMNS].astype(object).values.tolist()
        ws.append_rows(rows)


# ------------ 读写员工表 ------------

def load_staff() -> pd.DataFrame:
    """从 Google Sheets 读取员工列表"""
    ws = get_or_create_worksheet(SHEET_STAFF)
    rows = ws.get_all_records()
    df = pd.DataFrame(rows)

    if df.empty:
        df = pd.DataFrame(columns=STAFF_COLUMNS)

    if "员工姓名" not in df.columns:
        df["员工姓名"] = ""

    return df[["员工姓名"]]


def save_staff(df: pd.DataFrame):
    """把员工列表写回 Google Sheets 的“员工表”工作表"""
    ws = get_or_create_worksheet(SHEET_STAFF)
    ws.clear()
    ws.append_row(STAFF_COLUMNS)
    if not df.empty:
        ws.append_rows(df[STAFF_COLUMNS].astype(object).values.tolist())


def ensure_staff_exists(name: str):
    """如果员工不在员工表里，就自动加入。"""
    if not name:
        return
    df = load_staff()
    if name not in df["员工姓名"].astype(str).tolist():
        df = pd.concat([df, pd.DataFrame([{"员工姓名": name}])], ignore_index=True)
        save_staff(df)


def make_summary(df: pd.DataFrame) -> pd.DataFrame:
    """按 日期 + 员工 汇总。"""
    if df.empty:
        return pd.DataFrame(
            columns=["日期", "员工姓名", "工时(小时)", "服务收入", "小费", "总收入"]
        )
    return (
        df.groupby(["日期", "员工姓名"])[["工时(小时)", "服务收入", "小费", "总收入"]]
        .sum()
        .reset_index()
    )


# ------------ 导出相关（仍然导出为本地 Excel） ------------

def to_excel_bytes(detail_df: pd.DataFrame, summary_df: pd.DataFrame) -> bytes:
    """导出：当前筛选结果（选定员工+日期）"""
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        detail_df.to_excel(writer, sheet_name="工时记录_当前筛选", index=False)
        summary_df.to_excel(writer, sheet_name="汇总_当前筛选", index=False)
    output.seek(0)
    return output.read()


def to_excel_all_bytes() -> bytes:
    """导出：全部数据 + 每个月一个表，并附上每月总收入（含小费）"""
    records_df = load_records()
    staff_df = load_staff()

    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # 先写总表 & 员工表
        summary_df = make_summary(records_df)
        records_df.to_excel(writer, sheet_name="工时记录_全部", index=False)
        summary_df.to_excel(writer, sheet_name="汇总_全部", index=False)
        staff_df.to_excel(writer, sheet_name="员工表", index=False)

        if not records_df.empty:
            # 加一个字段：年月（例如 2025-10）
            date_series = pd.to_datetime(records_df["日期"], errors="coerce")
            tmp = records_df.copy()
            tmp["_ym"] = date_series.dt.strftime("%Y-%m")

            # ===== 月度汇总 Sheet =====
            monthly_summary = (
                tmp.groupby("_ym")[["服务收入", "小费", "总收入"]]
                .sum()
                .reset_index()
                .rename(columns={"_ym": "月份"})
            )
            monthly_summary.to_excel(writer, sheet_name="月度汇总", index=False)

            # ===== 每个月单独一个 Sheet，末尾加“合计”行 =====
            for ym in sorted(tmp["_ym"].dropna().unique()):
                month_df = tmp[tmp["_ym"] == ym].drop(columns=["_ym"])

                # 计算本月合计
                totals = month_df[["服务收入", "小费", "总收入"]].sum()
                total_row = {col: "" for col in month_df.columns}
                total_row["日期"] = "合计"
                total_row["服务收入"] = totals["服务收入"]
                total_row["小费"] = totals["小费"]
                total_row["总收入"] = totals["总收入"]

                month_df_with_total = pd.concat(
                    [month_df, pd.DataFrame([total_row])],
                    ignore_index=True,
                )

                # sheet 名就是 2025-10 这种
                month_df_with_total.to_excel(writer, sheet_name=ym, index=False)

    output.seek(0)
    return output.read()


# ------------ 页面：新增记录 ------------

def page_add_record():
    st.header("➕ 新增 Massage 预约记录")

    # ===== 如果上一次保存成功，在这里显示提示 =====
    success_msg = st.session_state.get("just_saved_msg", "")
    if success_msg:
        st.success(success_msg)
        # 显示一次后清空
        st.session_state["just_saved_msg"] = ""

    # ===== 数据准备 =====
    records_df = load_records()
    staff_df = load_staff()

    staff_list = sorted(
        [x for x in staff_df["员工姓名"].dropna().unique().tolist() if str(x).strip()]
    )
    staff_list_display = ["（手动输入新员工）"] + staff_list

    # ===== 输入表单 =====
    date_value = st.date_input("日期", value=date.today())
    staff_choice = st.selectbox("员工姓名（可选择或新填）", staff_list_display)

    if staff_choice == "（手动输入新员工）":
        staff_name = st.text_input("输入员工姓名")
    else:
        staff_name = staff_choice

    client_name = st.text_input("客人姓名")

    duration = st.selectbox("服务时长（分钟）", DURATION_OPTIONS, index=2)
    hours = round(duration / 60, 2)
    auto_price = calc_price(duration)

    st.info(f"💰 系统建议价格：${auto_price}")

    service_income = st.number_input(
        "服务收入（可修改）",
        min_value=0.0,
        step=0.5,
        value=auto_price,
        key=f"income_input_{duration}",
    )

    tip = st.number_input(
        "小费",
        min_value=0.0,
        step=0.5,
        value=0.0,
        key=f"tip_input_{duration}",  # 防止缓存同值
    )

    # ===== 保存按钮 =====
    if st.button("保存记录 ✅"):
        if not staff_name or not client_name:
            st.error("员工姓名 和 客人姓名 不能为空。")
            return

        # 生成新的 ID（自增）
        if not records_df.empty:
            new_id = int(records_df["ID"].max()) + 1
        else:
            new_id = 1

        total_income = round(service_income + tip, 2)
        record = {
            "ID": new_id,
            "日期": date_value.strftime("%Y-%m-%d"),
            "员工姓名": staff_name,
            "客人姓名": client_name,
            "服务项目": "Massage",
            "服务时长(分钟)": duration,
            "工时(小时)": hours,
            "服务收入": service_income,
            "小费": tip,
            "总收入": total_income,
        }

        # 保存到 Google Sheets
        records_df = pd.concat(
            [records_df, pd.DataFrame([record])], ignore_index=True
        )
        ensure_staff_exists(staff_name)
        save_all(records_df)

        # ✅ 保存成功后，把提示信息放入 session_state
        st.session_state["just_saved_msg"] = (
            f"✅ 已保存：ID {new_id} | {staff_name} | {duration}分钟 | 收入 {service_income} + 小费 {tip} = 总 {total_income}"
        )

        # 🔄 刷新页面（重置所有输入，小费恢复为 0）
        st.rerun()


# ------------ 页面：汇总统计（可修改记录） ------------

def page_summary():
    st.header("📊 汇总统计（按日期 + 员工）")

    df_all = load_records()
    if df_all.empty:
        st.info("目前还没有任何记录。")
        return

    # 筛选
    all_staff = sorted(
        [x for x in df_all["员工姓名"].dropna().unique().tolist() if str(x).strip()]
    )
    staff_filter = st.multiselect("筛选员工（可多选）", all_staff, default=all_staff)

    date_series = pd.to_datetime(df_all["日期"], errors="coerce")
    min_date, max_date = date_series.min().date(), date_series.max().date()
    date_range = st.date_input("日期范围", value=(min_date, max_date))

    df_filtered = df_all.copy()
    if staff_filter:
        df_filtered = df_filtered[df_filtered["员工姓名"].isin(staff_filter)]

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_d, end_d = date_range
        df_filtered = df_filtered[
            (pd.to_datetime(df_filtered["日期"]) >= pd.to_datetime(start_d))
            & (pd.to_datetime(df_filtered["日期"]) <= pd.to_datetime(end_d))
        ]

    if df_filtered.empty:
        st.warning("当前条件下没有数据。")
        return

    summary_filtered = make_summary(df_filtered)

    st.subheader("汇总表（当前筛选）")
    st.dataframe(summary_filtered, use_container_width=True)

    # ===== 月度收入统计 =====
    st.markdown("### 💰 月度收入统计（含小费）")

    # 提取年月
    df_filtered["_月份"] = pd.to_datetime(
        df_filtered["日期"], errors="coerce"
    ).dt.strftime("%Y-%m")

    # 按月份汇总收入
    monthly_summary = (
        df_filtered.groupby("_月份")[["服务收入", "小费", "总收入"]]
        .sum()
        .reset_index()
        .rename(columns={"_月份": "月份"})
    )

    if not monthly_summary.empty:
        st.dataframe(monthly_summary, use_container_width=True)
    else:
        st.info("当前筛选条件下没有月度数据。")

    st.subheader("明细表（当前筛选）")
    st.dataframe(df_filtered, use_container_width=True)

    # ---- 导出按钮 ----
    col_dl1, col_dl2 = st.columns(2)
    with col_dl1:
        st.download_button(
            label="📥 下载当前筛选结果（选定员工）",
            data=to_excel_bytes(df_filtered, summary_filtered),
            file_name="work_log_当前筛选.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
    with col_dl2:
        st.download_button(
            label="📦 下载全部数据（含每个月独立表格）",
            data=to_excel_all_bytes(),
            file_name="work_log_全部数据.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    # ---- 在这里直接修改记录 ----
    st.markdown("---")
    st.subheader("✏ 修改记录（当前筛选范围内）")

    id_options = df_filtered["ID"].tolist()
    if not id_options:
        st.info("没有可修改的记录。")
        return

    edit_id = st.selectbox("选择要修改的记录 ID", id_options)

    row = df_filtered[df_filtered["ID"] == edit_id].iloc[0]

    # 预设值
    edit_date = st.date_input(
        "日期（修改）",
        value=pd.to_datetime(row["日期"]).date(),
        key=f"edit_date_{edit_id}",
    )

    # 员工改名：从员工表选择
    staff_all = sorted(
        [x for x in df_all["员工姓名"].dropna().unique().tolist() if str(x).strip()]
    )
    if row["员工姓名"] not in staff_all:
        staff_all.append(row["员工姓名"])
    edit_staff = st.selectbox(
        "员工姓名（修改）",
        staff_all,
        index=staff_all.index(row["员工姓名"]),
        key=f"edit_staff_{edit_id}",
    )

    edit_client = st.text_input(
        "客人姓名（修改）",
        value=str(row["客人姓名"]),
        key=f"edit_client_{edit_id}",
    )

    # 时长
    dur_options = sorted(set(DURATION_OPTIONS + [int(row["服务时长(分钟)"])]))
    edit_duration = st.selectbox(
        "服务时长（分钟，修改）",
        dur_options,
        index=dur_options.index(int(row["服务时长(分钟)"])),
        key=f"edit_duration_{edit_id}",
    )
    edit_hours = round(edit_duration / 60, 2)
    sugg_price = calc_price(edit_duration)
    st.caption(f"当前时长建议价格：{sugg_price}")

    edit_income = st.number_input(
        "服务收入（修改）",
        min_value=0.0,
        step=0.5,
        value=float(row["服务收入"]),
        key=f"edit_income_{edit_id}",
    )
    edit_tip = st.number_input(
        "小费（修改）",
        min_value=0.0,
        step=0.5,
        value=float(row["小费"]),
        key=f"edit_tip_{edit_id}",
    )

    if st.button("💾 保存修改", key=f"save_edit_{edit_id}"):
        df_all = load_records()
        idx = df_all[df_all["ID"] == edit_id].index
        if len(idx) == 0:
            st.error("未找到该 ID 的记录（可能刚刚被删除），请刷新页面。")
            return
        idx = idx[0]

        df_all.at[idx, "日期"] = edit_date.strftime("%Y-%m-%d")
        df_all.at[idx, "员工姓名"] = edit_staff
        df_all.at[idx, "客人姓名"] = edit_client
        df_all.at[idx, "服务时长(分钟)"] = edit_duration
        df_all.at[idx, "工时(小时)"] = edit_hours
        df_all.at[idx, "服务收入"] = edit_income
        df_all.at[idx, "小费"] = edit_tip
        df_all.at[idx, "总收入"] = round(edit_income + edit_tip, 2)

        ensure_staff_exists(edit_staff)
        save_all(df_all)

        st.success(f"已更新 ID {edit_id} 的记录。请重新选择筛选条件查看最新数据。")


# ------------ 页面：删除记录（含全部删除） ------------

def page_delete_records():
    st.header("🗑 删除记录")

    df = load_records()
    if df.empty:
        st.info("目前还没有任何记录。")
        return

    # 危险操作：全部删除
    st.subheader("⚠ 危险操作：删除全部记录")
    confirm_all = st.checkbox("我真的要删除 *所有* 记录（不可恢复）")
    if confirm_all and st.button("❌ 删除全部记录（不可恢复）"):
        empty_df = pd.DataFrame(columns=COLUMNS)
        save_all(empty_df)
        st.success("已删除所有记录。请刷新页面。")
        return

    st.markdown("---")
    st.subheader("按条件删除部分记录")

    all_staff = sorted(
        [x for x in df["员工姓名"].dropna().unique().tolist() if str(x).strip()]
    )
    staff_filter = st.multiselect("先筛选员工（可多选）", all_staff, default=all_staff)

    date_series = pd.to_datetime(df["日期"], errors="coerce")
    min_date, max_date = date_series.min().date(), date_series.max().date()
    date_range = st.date_input("日期范围", value=(min_date, max_date))

    df_filtered = df.copy()
    if staff_filter:
        df_filtered = df_filtered[df_filtered["员工姓名"].isin(staff_filter)]

    if isinstance(date_range, tuple) and len(date_range) == 2:
        start_d, end_d = date_range
        df_filtered = df_filtered[
            (pd.to_datetime(df_filtered["日期"]) >= pd.to_datetime(start_d))
            & (pd.to_datetime(df_filtered["日期"]) <= pd.to_datetime(end_d))
        ]

    if df_filtered.empty:
        st.warning("当前条件下没有可删除记录。")
        return

    st.subheader("当前筛选的记录（含 ID）")
    st.dataframe(df_filtered, use_container_width=True)

    id_options = df_filtered["ID"].tolist()
    selected_ids = st.multiselect("选择要删除的记录（按 ID）", id_options)

    if selected_ids and st.button("❌ 确认删除选中记录"):
        df_all = load_records()
        df_all = df_all[~df_all["ID"].isin(selected_ids)]
        save_all(df_all)
        st.success(f"已删除 {len(selected_ids)} 条记录。重新切换页面可查看最新列表。")


# ------------ 页面：员工管理 ------------

def page_staff_manage():
    st.header("👥 员工管理")

    staff_df = load_staff()

    # ===== 显示当前员工列表 =====
    st.subheader("当前员工列表")
    st.dataframe(staff_df, use_container_width=True)

    # ===== 添加员工 =====
    st.markdown("---")
    st.subheader("➕ 添加新员工")

    name = st.text_input("新增员工姓名")
    if st.button("添加员工"):
        if not name:
            st.error("员工姓名不能为空。")
        elif name in staff_df["员工姓名"].astype(str).tolist():
            st.warning("该员工已存在。")
        else:
            staff_df = pd.concat(
                [staff_df, pd.DataFrame([{"员工姓名": name}])], ignore_index=True
            )
            save_staff(staff_df)
            st.success(f"✅ 已添加员工：{name}")

    # ===== 删除员工 =====
    st.markdown("---")
    st.subheader("🗑 删除员工")

    if not staff_df.empty:
        staff_to_delete = st.multiselect(
            "选择要删除的员工（可多选）",
            staff_df["员工姓名"].astype(str).tolist(),
        )

        if staff_to_delete:
            st.warning("⚠ 注意：删除员工不会删除他的历史工时记录，只会从下拉菜单移除。")
            if st.button("❌ 确认删除选中员工"):
                staff_df = staff_df[~staff_df["员工姓名"].isin(staff_to_delete)]
                save_staff(staff_df)
                st.success(f"已删除员工：{', '.join(staff_to_delete)}")
    else:
        st.info("当前还没有员工。")


# ------------ 主入口 ------------

def main():
    st.set_page_config(page_title="Massage 工时记录器", page_icon="💆", layout="wide")
    st.sidebar.title("Massage 工时记录器")
    page = st.sidebar.radio("功能选择", ("新增记录", "汇总统计", "删除记录", "员工管理"))

    if page == "新增记录":
        page_add_record()
    elif page == "汇总统计":
        page_summary()
    elif page == "删除记录":
        page_delete_records()
    else:
        page_staff_manage()


if __name__ == "__main__":
    main()
