import os
import sqlite3
import webbrowser
import base64
import html
from threading import Timer
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify

base_dir = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(base_dir, 'static'))
app.secret_key = "abumousa_real_estate_secret_key"

UPLOAD_FOLDER = os.path.join(base_dir, 'static', 'uploads')
SNAPSHOT_FOLDER = os.path.join(base_dir, 'static', 'snapshots')
TREASURY_SNAPSHOT_FOLDER = os.path.join(SNAPSHOT_FOLDER, 'treasury')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(SNAPSHOT_FOLDER, exist_ok=True)
os.makedirs(TREASURY_SNAPSHOT_FOLDER, exist_ok=True)

DB_NAME = os.path.join(base_dir, 'real_estate_data.db')

def rental_overlap(conn, property_name, unit_no, floor_no, start_date, end_date, exclude_id=None):
    """Return an existing contract if the same unit has an overlapping rental period."""
    if not property_name or not unit_no or not floor_no or not start_date or not end_date:
        return None
    if start_date > end_date:
        return "INVALID_DATES"
    sql = """SELECT id, tenant_name, start_date, end_date FROM contracts
             WHERE TRIM(COALESCE(property_name,'')) = TRIM(?)
               AND TRIM(COALESCE(unit_no,'')) = TRIM(?)
               AND TRIM(COALESCE(floor_no,'')) = TRIM(?)
               AND COALESCE(start_date,'') <= ?
               AND COALESCE(end_date,'9999-12-31') >= ?"""
    params = [property_name, unit_no, floor_no, end_date, start_date]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def active_sale_conflict(conn, property_name, exclude_id=None):
    """Prevent selling the same registered property more than once while a sale is active."""
    name = (property_name or '').strip()
    if not name:
        return None
    sql = """SELECT id, client_name, op_date FROM sales
             WHERE LOWER(TRIM(COALESCE(property_name,''))) = LOWER(TRIM(?))
               AND TRIM(COALESCE(operation_type,'')) = 'بيع عقار'
               AND TRIM(COALESCE(status,'نشطة')) = 'نشطة'"""
    params = [name]
    if exclude_id is not None:
        sql += " AND id != ?"
        params.append(exclude_id)
    sql += " ORDER BY id DESC LIMIT 1"
    return conn.execute(sql, params).fetchone()


def sale_conflict_response(conflict):
    message = ("مرفوض: هذا العقار مسجل عليه بيع نشط بالفعل، ولا يمكن بيعه مرة أخرى قبل إلغاء/تسوية الصفقة السابقة.\n"
               f"رقم الصفقة السابقة: {conflict['id']}\n"
               f"العميل المسجل: {conflict['client_name'] or 'غير محدد'}\n"
               f"تاريخ الصفقة: {conflict['op_date'] or 'غير محدد'}\n"
               "لم يتم حفظ الصفقة الجديدة أو اعتماد أي حركة خزينة.")
    return "<script>alert(" + __import__('json').dumps(message, ensure_ascii=False) + "); window.location.href='/sales';</script>"

def rental_conflict_response(conflict):
    if conflict == "INVALID_DATES":
        message = "تاريخ نهاية العقد لا يمكن أن يسبق تاريخ بدايته. لم يتم حفظ العقد ولم تُعتمد أي أموال."
    else:
        message = ("مرفوض: هذه الوحدة مؤجرة بالفعل بعقد متداخل الفترة!\n"
                   f"المستأجر الحالي: {conflict['tenant_name']}\n"
                   f"الفترة المسجلة: {conflict['start_date']} إلى {conflict['end_date']}\n"
                   "لم يتم حفظ العقد الجديد ولم تُعتمد أي أموال في الخزينة.")
    return "<script>alert(" + __import__('json').dumps(message, ensure_ascii=False) + "); window.location.href='/contracts';</script>"

def get_db_connection():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL;')  # يمنع قفل القاعدة نهائياً
    return conn

def init_db():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        name TEXT, job TEXT, password TEXT, 
        salary REAL DEFAULT 0, deductions REAL DEFAULT 0, 
        advances REAL DEFAULT 0, taxes REAL DEFAULT 0,
        status TEXT DEFAULT "نشط", image TEXT,
        address TEXT DEFAULT "", national_id TEXT DEFAULT ""
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        employee_name TEXT, action_time TEXT, details TEXT,
        snapshot_file TEXT DEFAULT "", month_year TEXT DEFAULT "", category TEXT DEFAULT "عام"
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS properties (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        building_name TEXT, owner_name TEXT, address TEXT, 
        total_floors INTEGER, total_units INTEGER, property_type TEXT, image TEXT,
        owner_national_id TEXT DEFAULT "", owner_address TEXT DEFAULT "",
        ownership_status TEXT DEFAULT "ملك الغير", notes TEXT DEFAULT ""
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        tenant_name TEXT, tenant_address TEXT DEFAULT "", tenant_national_id TEXT DEFAULT "",
        property_name TEXT, unit_no TEXT, floor_no TEXT, 
        rent_amount REAL DEFAULT 0, paid_amount REAL DEFAULT 0, remaining_amount REAL DEFAULT 0, 
        deposit_amount REAL DEFAULT 0, deposit_paid REAL DEFAULT 0, deposit_remaining REAL DEFAULT 0,
        maintenance_amount REAL DEFAULT 0, maintenance_paid REAL DEFAULT 0, maintenance_remaining REAL DEFAULT 0,
        employee_name TEXT, total_commission REAL DEFAULT 0, office_commission REAL DEFAULT 0, employee_commission REAL DEFAULT 0, 
        start_date TEXT, end_date TEXT, contract_image TEXT, created_at TEXT
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS sales (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        operation_type TEXT, property_name TEXT, client_name TEXT, 
        client_address TEXT DEFAULT "", client_national_id TEXT DEFAULT "",
        total_price REAL DEFAULT 0, paid_price REAL DEFAULT 0, remaining_price REAL DEFAULT 0, 
        employee_name TEXT, total_commission REAL DEFAULT 0, office_commission REAL DEFAULT 0, employee_commission REAL DEFAULT 0, 
        op_date TEXT, document_image TEXT, created_at TEXT,
        status TEXT DEFAULT "نشطة", notes TEXT DEFAULT ""
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS legal_affairs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, 
        case_title TEXT, client_name TEXT, case_details TEXT, case_status TEXT, document_image TEXT,
        case_party_type TEXT DEFAULT "من المكتب على الغير", lawyer_name TEXT DEFAULT ""
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS legal_expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        case_id INTEGER,
        expense_amount REAL DEFAULT 0,
        expense_type TEXT DEFAULT "",
        expense_date TEXT DEFAULT "",
        notes TEXT DEFAULT "",
        status TEXT DEFAULT "معتمدة"
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS office_finances (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fin_type TEXT DEFAULT "مصروف عابر",
        title TEXT,
        amount REAL DEFAULT 0,
        fin_date TEXT,
        notes TEXT DEFAULT "",
        voucher_no TEXT DEFAULT "",
        counterparty TEXT DEFAULT "",
        snapshot_file TEXT DEFAULT "",
        created_by TEXT DEFAULT "",
        status TEXT DEFAULT "معتمدة",
        source_type TEXT DEFAULT "",
        source_id INTEGER,
        office_income REAL DEFAULT 0,
        third_party_due REAL DEFAULT 0
    )''')
    
    tables_cols = {
        'employees': [('address', 'TEXT DEFAULT ""'), ('national_id', 'TEXT DEFAULT ""'), ('deductions', 'REAL DEFAULT 0'), ('advances', 'REAL DEFAULT 0'), ('taxes', 'REAL DEFAULT 0')],
        'properties': [('owner_national_id', 'TEXT DEFAULT ""'), ('owner_address', 'TEXT DEFAULT ""'), ('ownership_status', 'TEXT DEFAULT "ملك الغير"'), ('notes', 'TEXT DEFAULT ""')],
        'contracts': [
            ('tenant_address', 'TEXT DEFAULT ""'), ('tenant_national_id', 'TEXT DEFAULT ""'),
            ('deposit_amount', 'REAL DEFAULT 0'), ('deposit_paid', 'REAL DEFAULT 0'), ('deposit_remaining', 'REAL DEFAULT 0'),
            ('maintenance_amount', 'REAL DEFAULT 0'), ('maintenance_paid', 'REAL DEFAULT 0'), ('maintenance_remaining', 'REAL DEFAULT 0'),
            ('total_commission', 'REAL DEFAULT 0'), ('office_commission', 'REAL DEFAULT 0'), ('employee_commission', 'REAL DEFAULT 0')
        ],
        'sales': [
            ('client_address', 'TEXT DEFAULT ""'), ('client_national_id', 'TEXT DEFAULT ""'),
            ('total_commission', 'REAL DEFAULT 0'), ('office_commission', 'REAL DEFAULT 0'), ('employee_commission', 'REAL DEFAULT 0'),
            ('status', 'TEXT DEFAULT "نشطة"'), ('notes', 'TEXT DEFAULT ""')
        ],
        'legal_affairs': [('case_party_type', 'TEXT DEFAULT "من المكتب على الغير"'), ('lawyer_name', 'TEXT DEFAULT ""'), ('document_image', 'TEXT DEFAULT ""')],
        'activity_logs': [('snapshot_file', 'TEXT DEFAULT ""'), ('month_year', 'TEXT DEFAULT ""'), ('category', 'TEXT DEFAULT "عام"')],
        'office_finances': [
            ('voucher_no', 'TEXT DEFAULT ""'), ('counterparty', 'TEXT DEFAULT ""'),
            ('snapshot_file', 'TEXT DEFAULT ""'), ('created_by', 'TEXT DEFAULT ""'),
            ('status', 'TEXT DEFAULT "معتمدة"'), ('source_type', 'TEXT DEFAULT ""'),
            ('source_id', 'INTEGER'), ('office_income', 'REAL DEFAULT 0'),
            ('third_party_due', 'REAL DEFAULT 0')
        ],
        'legal_expenses': [('status', 'TEXT DEFAULT "معتمدة"')]
    }
    for tbl, cols in tables_cols.items():
        for col_name, col_type in cols:
            try:
                c.execute("ALTER TABLE " + tbl + " ADD COLUMN " + col_name + " " + col_type)
            except sqlite3.OperationalError:
                pass

    # تصنيف حركات الخزينة القديمة غير المصنفة مرة واحدة حتى لا تُعامل متحصلات الغير كإيراد للمكتب.
    c.execute("""
        UPDATE office_finances
        SET office_income = CASE
            WHEN source_type='sale_payment' AND fin_type IN ('إيراد عابر','قبض') THEN
                CASE WHEN (SELECT p.ownership_status FROM sales s LEFT JOIN properties p ON p.building_name=s.property_name WHERE s.id=office_finances.source_id)='ملك المكتب'
                     THEN amount
                     ELSE MIN(amount, COALESCE((SELECT s.office_commission FROM sales s WHERE s.id=office_finances.source_id),0)) END
            WHEN source_type='contract_payment' AND fin_type IN ('إيراد عابر','قبض') THEN
                MIN(amount, COALESCE((SELECT ct.office_commission FROM contracts ct WHERE ct.id=office_finances.source_id),0))
            ELSE office_income END,
            third_party_due = CASE
            WHEN source_type='sale_payment' AND fin_type IN ('إيراد عابر','قبض') THEN
                amount - CASE WHEN (SELECT p.ownership_status FROM sales s LEFT JOIN properties p ON p.building_name=s.property_name WHERE s.id=office_finances.source_id)='ملك المكتب'
                              THEN amount ELSE MIN(amount, COALESCE((SELECT s.office_commission FROM sales s WHERE s.id=office_finances.source_id),0)) END
            WHEN source_type='contract_payment' AND fin_type IN ('إيراد عابر','قبض') THEN
                amount - MIN(amount, COALESCE((SELECT ct.office_commission FROM contracts ct WHERE ct.id=office_finances.source_id),0))
            ELSE third_party_due END
        WHERE office_income=0 AND third_party_due=0
          AND source_type IN ('sale_payment','contract_payment')
          AND fin_type IN ('إيراد عابر','قبض')
    """)

    c.execute("SELECT COUNT(*) FROM employees")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO employees (name, job, password, salary, deductions, advances, taxes, status, image, address, national_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
                  ("عمر ابوهبدة", "المدير العام", "1", 5000, 0, 0, 0, "نشط", "", "العنوان الرئيسي", "29900000000000"))
    
    c.execute("UPDATE employees SET name = 'عمر ابوهبدة', job = 'المدير العام' WHERE name LIKE '%المجموعة المصرية%'")
    c.execute("UPDATE employees SET password = '1' WHERE password IS NULL OR TRIM(password) = ''")
    conn.commit()
    conn.close()

init_db()

def save_files(file_list):
    if not file_list:
        return ""
    saved_names = []
    if not isinstance(file_list, list):
        file_list = [file_list]
    for f in file_list:
        if f and hasattr(f, 'filename') and f.filename != '':
            fn = datetime.now().strftime("%Y%m%d%H%M%S") + "_" + secure_filename(f.filename)
            f.save(os.path.join(UPLOAD_FOLDER, fn))
            saved_names.append(fn)
    return ",".join(saved_names)

def render_file_badges(file_str):
    if not file_str:
        return "بدون"
    files = str(file_str).split(",")
    html_list = []
    for f in files:
        f = f.strip()
        if f:
            html_list.append(f"<a href='/static/uploads/{f}' target='_blank'><img src='/static/uploads/{f}' class='contract-img me-1 mb-1'></a>")
    return "".join(html_list) if html_list else "بدون"

def save_screen_snapshot(snapshot_data):
    if not snapshot_data:
        return ""
    try:
        if "," in snapshot_data:
            header, encoded = snapshot_data.split(",", 1)
        else:
            encoded = snapshot_data
        binary_data = base64.b64decode(encoded)
        filename = "snap_" + datetime.now().strftime('%Y%m%d_%H%M%S') + ".png"
        filepath = os.path.join(SNAPSHOT_FOLDER, filename)
        with open(filepath, "wb") as fh:
            fh.write(binary_data)
        return filename
    except Exception as e:
        print("Error saving visual snapshot:", e)
        return ""

def save_treasury_snapshot(snapshot_data, voucher_id=None):
    """حفظ لقطة حركة الخزينة داخل مجلد مستقل داخل البرنامج."""
    if not snapshot_data:
        return ""
    try:
        encoded = snapshot_data.split(",", 1)[1] if "," in snapshot_data else snapshot_data
        binary_data = base64.b64decode(encoded, validate=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        prefix = ("voucher_" + str(voucher_id) + "_") if voucher_id else "treasury_"
        filename = prefix + stamp + ".png"
        filepath = os.path.join(TREASURY_SNAPSHOT_FOLDER, filename)
        with open(filepath, "wb") as fh:
            fh.write(binary_data)
        return "treasury/" + filename
    except Exception as e:
        print("Error saving treasury snapshot:", e)
        return ""


def log_act(emp, det, snapshot_b64="", category="عام"):
    snap_file = save_screen_snapshot(snapshot_b64)
    now = datetime.now()
    time_str = now.strftime("%Y-%m-%d | %I:%M:%S %p")
    month_year = now.strftime("%Y-%m")
    
    conn = get_db_connection()
    conn.execute("INSERT INTO activity_logs (employee_name, action_time, details, snapshot_file, month_year, category) VALUES (?, ?, ?, ?, ?, ?)", 
                 (emp, time_str, det, snap_file, month_year, category))
    conn.commit()
    conn.close()
    return snap_file

def get_today_date():
    return datetime.now().strftime("%Y-%m-%d")


def record_treasury_movement(conn, direction, title, amount, movement_date, source_type, source_id,
                             counterparty="", notes="", snapshot_data=""):
    """تسجيل أي قبض/صرف ناتج عن قسم آخر في سجل الخزينة الموحد."""
    amount = float(amount or 0)
    if amount <= 0:
        return None
    fin_type = "إيراد عابر" if direction == "قبض" else "مصروف عابر"
    cur = conn.execute("""
        INSERT INTO office_finances
        (fin_type, title, amount, fin_date, notes, counterparty, created_by, status, source_type, source_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, 'معتمدة', ?, ?)
    """, (fin_type, title, amount, movement_date or get_today_date(), notes, counterparty,
          session.get('user', ''), source_type, source_id))
    finance_id = cur.lastrowid
    voucher_no = "خز-" + datetime.now().strftime("%Y%m") + "-" + str(finance_id).zfill(6)
    snapshot_file = save_treasury_snapshot(snapshot_data, finance_id)
    conn.execute("UPDATE office_finances SET voucher_no=?, snapshot_file=? WHERE id=?",
                 (voucher_no, snapshot_file, finance_id))
    return finance_id


HTML_LAYOUT = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>عمر عبدالمنعم للإستثمار العقاري</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800;900&family=Amiri:wght@700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
:root {
    --bg: #0f172a;
    --nav: #1e293b;
    --card: #1e293b;
    --text: #f8fafc;
}
body.theme-dark { --bg: #0f172a; --nav: #1e293b; --card: #1e293b; --text: #f8fafc; }
body.theme-classic { --bg: #09203f; --nav: #133b6b; --card: #133b6b; --text: #ffffff; }
body.theme-amber { --bg: #1c1917; --nav: #292524; --card: #292524; --text: #fef3c7; }
body.theme-light { --bg: #f1f5f9; --nav: #e2e8f0; --card: #ffffff; --text: #0f172a; }

* { box-sizing: border-box; }
html { width: 100%; max-width: 100%; overflow-x: hidden; }
body { background-color: var(--bg); color: var(--text); font-family: 'Tajawal', sans-serif; min-height: 100vh; display: flex; flex-direction: column; margin: 0; padding: 0; transition: background 0.3s, color 0.3s; overflow-x: hidden; }
img, canvas, video, iframe { max-width: 100%; }
input, select, textarea, button { max-width: 100%; }
.navbar-custom { background: var(--nav); border-bottom: 2px solid #334155; padding: 10px 20px; }
.card-custom { border-radius: 12px; border: 1px solid #334155; background: var(--card); color: var(--text); margin-bottom: 20px; box-shadow: 0 4px 12px rgba(0,0,0,0.3); }
.contract-img, .table-img { width: 50px; height: 50px; object-fit: cover; border-radius: 8px; border: 1px solid #334155; }
footer { margin-top: auto; background: #020617; color: #94a3b8; padding: 15px 20px; border-top: 1px solid #1e293b; font-size: 14px; }
input[type="date"] { color-scheme: dark; cursor: pointer; }

.result-banner {
    background: var(--card); border: 2px solid #f59e0b; border-radius: 12px; padding: 12px 22px; margin-bottom: 20px;
    display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 15px rgba(0,0,0,0.4);
}
.clock-box { width: 85px; height: 85px; display: flex; justify-content: center; align-items: center; }
.container-fluid { padding-left: 20px; padding-right: 20px; }
.navbar-custom .container-fluid { min-width: 0; }
.navbar-custom .navbar-nav { min-width: 0; flex-wrap: wrap; }
.table-responsive { width: 100%; max-width: 100%; -webkit-overflow-scrolling: touch; }
.table-responsive > .table { white-space: nowrap; }
.card-custom, .card, .modal-content, .result-banner { min-width: 0; }
.row > [class*="col-"] { min-width: 0; }
.interactive-card { cursor: pointer; transition: transform 0.2s, box-shadow 0.2s; }
.interactive-card:hover { transform: translateY(-4px); box-shadow: 0 8px 20px rgba(0,0,0,0.5) !important; }
@media (max-width: 991.98px) {
    .navbar-custom .container-fluid { align-items: flex-start; }
    .navbar-custom .navbar-brand { white-space: normal; font-size: 1.05rem !important; line-height: 1.35; }
    .navbar-custom .navbar-nav { width: 100%; margin: 8px 0 0 !important; display: flex; flex-direction: row; justify-content: flex-start; gap: 2px; }
    .navbar-custom .nav-link { padding: 7px 9px !important; white-space: nowrap; font-size: 13px; }
}
@media (max-width: 767.98px) {
    .container-fluid { padding-left: 10px; padding-right: 10px; }
    .navbar-custom { padding: 9px 6px; }
    .result-banner { padding: 10px; gap: 10px; flex-wrap: wrap; align-items: flex-start; }
    .result-banner > div { max-width: 100%; }
    .result-banner .badge { white-space: normal; font-size: 12px !important; padding: 7px 9px !important; }
    .clock-box { width: 54px; height: 54px; }
    .clock-box canvas { width: 52px; height: 52px; }
    .card-custom { padding: 12px !important; }
    .table { font-size: 12px; }
    .table-responsive > .table { min-width: 650px; }
    .btn { white-space: normal; }
    .modal-dialog { margin: 8px; }
    .modal-dialog-centered { min-height: calc(100% - 16px); }
    footer { padding: 12px 10px 78px; }
    footer .container-fluid { flex-direction: column; align-items: flex-start !important; gap: 6px; }
    #themeSelectorPanel { left: 8px !important; right: auto !important; bottom: 8px !important; max-width: calc(100vw - 16px); padding: 7px 9px !important; gap: 6px !important; }
    #themeSelectorPanel select { max-width: 190px; }
}
@media (max-width: 420px) {
    .navbar-custom .navbar-brand { font-size: .95rem !important; }
    .quran-header { font-size: 22px !important; }
    .app-title { font-size: 20px !important; }
}
</style>
</head>
<body class="theme-dark" id="mainBody">
<nav class="navbar navbar-expand-lg navbar-dark navbar-custom">
  <div class="container-fluid">
    <a class="navbar-brand fw-bold fs-4 text-warning px-2" href="/contracts">مكتب عمر عبدالمنعم للإستثمار العقاري</a>
    <div class="navbar-nav align-items-center ms-auto" style="font-size: 14px;">
      <a class="nav-link px-3 fw-bold" href="/contracts">العقود</a>
      <a class="nav-link px-3 fw-bold" href="/sales">البيع والشراء</a>
      <a class="nav-link px-3 fw-bold" href="/properties">العقارات</a>
      <a class="nav-link px-3 fw-bold" href="/employees">الموظفين والصلاحيات</a>
      <a class="nav-link px-3 fw-bold" href="/office_finances">إيرادات ومصروفات المكتب</a>
      <a class="nav-link px-3 fw-bold" href="/reports">التقارير الشهرية والصافي</a>
      <a class="nav-link px-3 fw-bold" href="/legal">الشؤون القانونية</a>
      <a class="nav-link px-3 fw-bold" href="/logs">سجل الأنشطة</a>
      <a class="nav-link px-3 fw-bold text-warning" href="/data_editor">تعديل كل البيانات</a>
      <a class="nav-link text-danger fw-bold px-3" href="/logout">خروج</a>
    </div>
  </div>
</nav>

<div class="container-fluid my-3">
    <div class="result-banner">
        <div class="d-flex align-items-center gap-4">
            <div class="clock-box"><canvas id="analogClock" width="80" height="80"></canvas></div>
            <div>
                <div id="full-date-box" style="font-size: 15px; font-weight: 800; color: #f59e0b;"></div>
            </div>
        </div>

        <div>
            <div class="badge bg-success fs-5 py-2 px-4 fw-bold shadow">👤 الموظف الحالي: {{ current_user }}</div>
        </div>
    </div>

    {{ content | safe }}
</div>

<div id="themeSelectorPanel" style="position: fixed; bottom: 70px; left: 15px; z-index: 1050; background: rgba(15, 23, 42, 0.95); padding: 10px 16px; border-radius: 20px; border: 1px solid #f59e0b; box-shadow: 0 4px 15px rgba(0,0,0,0.5); display: flex; align-items: center; gap: 10px;">
    <span style="font-size: 13px; color: #f59e0b; font-weight: bold;">🎨 الثيم:</span>
    <select id="themeSelector" class="form-select form-select-sm bg-dark text-white border-warning" onchange="changeTheme(this.value)" style="width: auto; font-size: 13px;">
        <option value="theme-dark">مودرن داكن</option>
        <option value="theme-classic">كلاسيكي أزرق</option>
        <option value="theme-amber">عنابي / ذهبي</option>
        <option value="theme-light">فاتح (ناصع)</option>
    </select>
</div>

<footer>
    <div class="container-fluid d-flex justify-content-between align-items-center">
        <span style="color: #38bdf8; font-weight: bold; font-size: 14px;">هذا البرنامج من تصميم <span style="color: #f59e0b; font-weight: bold;">عمر عبد المنعم</span></span>
        <span class="text-warning fw-bold fs-6">مكتب عمر عبدالمنعم للإستثمار العقاري</span>
    </div>
</footer>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
function changeTheme(themeName) {
    document.getElementById('mainBody').className = themeName;
    localStorage.setItem('abumousa_theme', themeName);
}
window.addEventListener('DOMContentLoaded', (event) => {
    let savedTheme = localStorage.getItem('abumousa_theme');
    if(savedTheme) {
        document.getElementById('mainBody').className = savedTheme;
        let sel = document.getElementById('themeSelector');
        if(sel) sel.value = savedTheme;
    }
    updateCalendarBanner();
});

function drawAnalogClock() {
    const canvas = document.getElementById("analogClock");
    if(!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height, radius = (w / 2) * 0.92;

    ctx.save();
    ctx.clearRect(0, 0, w, h);
    ctx.translate(w / 2, h / 2);

    ctx.beginPath();
    ctx.arc(0, 0, radius, 0, 2 * Math.PI);
    ctx.fillStyle = '#0f172a';
    ctx.fill();
    ctx.strokeStyle = '#f59e0b';
    ctx.lineWidth = 2.5;
    ctx.stroke();

    for (let i = 0; i < 60; i++) {
        let angle = (i * Math.PI) / 30;
        let isHourMark = i % 5 === 0;
        let markLength = isHourMark ? radius * 0.15 : radius * 0.07;
        let markWidth = isHourMark ? 2 : 1;

        ctx.beginPath();
        ctx.lineWidth = markWidth;
        ctx.strokeStyle = isHourMark ? '#f59e0b' : '#64748b';
        let x1 = (radius - markLength) * Math.sin(angle);
        let y1 = -(radius - markLength) * Math.cos(angle);
        let x2 = radius * Math.sin(angle);
        let y2 = -radius * Math.cos(angle);
        ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
    }

    ctx.font = "bold " + (radius * 0.32) + "px Tajawal";
    ctx.fillStyle = "#f59e0b";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for(let num = 1; num <= 12; num++) {
        let ang = num * Math.PI / 6;
        let nx = (radius * 0.65) * Math.sin(ang);
        let ny = -(radius * 0.65) * Math.cos(ang);
        ctx.fillText(num.toString(), nx, ny);
    }

    const now = new Date();
    let hr = now.getHours() % 12, min = now.getMinutes(), sec = now.getSeconds();
    let hrAngle = (hr * Math.PI / 6) + (min * Math.PI / (6 * 60));
    ctx.beginPath(); ctx.lineWidth = 3.5; ctx.lineCap = "round"; ctx.strokeStyle = "#ffffff";
    ctx.moveTo(0, 0); ctx.lineTo((radius * 0.42) * Math.sin(hrAngle), -(radius * 0.42) * Math.cos(hrAngle)); ctx.stroke();

    let minAngle = (min * Math.PI / 30);
    ctx.beginPath(); ctx.lineWidth = 2.5; ctx.lineCap = "round"; ctx.strokeStyle = "#ffffff";
    ctx.moveTo(0, 0); ctx.lineTo((radius * 0.6) * Math.sin(minAngle), -(radius * 0.6) * Math.cos(minAngle)); ctx.stroke();

    let secAngle = (sec * Math.PI / 30);
    ctx.beginPath(); ctx.lineWidth = 1.2; ctx.lineCap = "round"; ctx.strokeStyle = "#ef4444";
    ctx.moveTo(0, 0); ctx.lineTo((radius * 0.72) * Math.sin(secAngle), -(radius * 0.72) * Math.cos(secAngle)); ctx.stroke();

    ctx.beginPath(); ctx.arc(0, 0, 3.5, 0, 2 * Math.PI); ctx.fillStyle = "#f59e0b"; ctx.fill();
    ctx.restore();
}

setInterval(drawAnalogClock, 1000);
drawAnalogClock();

function updateCalendarBanner() {
    const now = new Date();
    const grOpt = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    const dateGr = now.toLocaleDateString('ar-EG', grOpt);
    let dateHi = "";
    try { dateHi = now.toLocaleDateString('ar-SA-u-ca-islamic', { day: 'numeric', month: 'long', year: 'numeric' }); } catch(e) { dateHi = ""; }
    const box = document.getElementById('full-date-box');
    if(box) box.innerText = "📅 " + dateGr + " | 🌙 " + dateHi;
}
updateCalendarBanner();

function captureScreenAndSubmit(formElem) {
    window.scrollTo(0, 0);
    html2canvas(document.body, { scrollY: -window.scrollY, scale: 1, useCORS: true }).then(function(cvs) {
        let snapshotData = cvs.toDataURL('image/png');
        let hiddenInput = formElem.querySelector('input[name="screen_snapshot"]');
        if(!hiddenInput) {
            hiddenInput = document.createElement('input');
            hiddenInput.type = 'hidden';
            hiddenInput.name = 'screen_snapshot';
            formElem.appendChild(hiddenInput);
        }
        hiddenInput.value = snapshotData;
        formElem.submit();
    }).catch(function(err) { formElem.submit(); });
}
function captureRequiredScreenAndSubmit(formElem) {
    window.scrollTo(0, 0);
    if (typeof html2canvas !== 'function') {
        alert('تعذر تشغيل أداة تصوير الشاشة. لم يتم حفظ الحركة حتى لا تُسجل بدون صورة.');
        return;
    }
    html2canvas(document.body, { scrollY: -window.scrollY, scale: 1, useCORS: true }).then(function(cvs) {
        let hiddenInput = formElem.querySelector('input[name="screen_snapshot"]');
        if(!hiddenInput) {
            hiddenInput = document.createElement('input');
            hiddenInput.type = 'hidden';
            hiddenInput.name = 'screen_snapshot';
            formElem.appendChild(hiddenInput);
        }
        hiddenInput.value = cvs.toDataURL('image/png');
        formElem.submit();
    }).catch(function(err) {
        alert('فشل التقاط صورة الشاشة؛ لم يتم حفظ حركة الخزينة. حاول مرة أخرى.');
    });
}
</script>
</body>
</html>"""

def render_page(content, current_user="زائر"):
    return render_template_string(HTML_LAYOUT, content=content, current_user=current_user)

@app.route('/')
def index():
    return redirect(url_for('login'))
@app.route('/login', methods=['GET', 'POST'])
def login():
    error_msg = ""
    conn = get_db_connection()
    employees = conn.execute("SELECT * FROM employees WHERE status = 'نشط'").fetchall()
    
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')
        snap_b64 = request.form.get('screen_snapshot', '')
        user = conn.execute("SELECT * FROM employees WHERE name = ? AND password = ? AND status = 'نشط'", (u, p)).fetchone()
        if user:
            session['user'] = user['name']
            log_act(user['name'], "تسجيل دخول ناجح للنظام", snap_b64, category="الدخول والزيارات")
            conn.close()
            return redirect(url_for('contracts_page'))
        else:
            error_msg = '<div class="alert alert-danger text-center py-2 mb-3" style="font-size:13px; font-weight: bold;">كلمة المرور غير صحيحة أو تم إلغاء الصلاحية!</div>'
    conn.close()
    options = "".join(['<option value="' + emp["name"] + '">' + emp["name"] + ' (' + emp["job"] + ')</option>' for emp in employees])
    
    return """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>تسجيل الدخول - أبو هبدة للإستثمار العقاري</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.rtl.min.css">
<link href="https://fonts.googleapis.com/css2?family=Tajawal:wght@400;500;700;800;900&family=Amiri:wght@700&display=swap" rel="stylesheet">
<script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
<style>
:root {
    --bg: #0f172a;
    --nav: #1e293b;
    --card: #1e293b;
    --text: #f8fafc;
}
body.theme-dark { --bg: #0f172a; --nav: #1e293b; --card: #1e293b; --text: #f8fafc; }
body.theme-classic { --bg: #09203f; --nav: #133b6b; --card: #133b6b; --text: #ffffff; }
body.theme-amber { --bg: #1c1917; --nav: #292524; --card: #292524; --text: #fef3c7; }
body.theme-light { --bg: #f1f5f9; --nav: #e2e8f0; --card: #ffffff; --text: #0f172a; }

* { box-sizing: border-box; font-family: 'Tajawal', sans-serif; }
body { background-color: var(--bg); color: var(--text); margin: 0; padding: 0; min-height: 100vh; display: flex; flex-direction: column; justify-content: space-between; transition: background 0.3s, color 0.3s; }

/* 1. المستطيل العلوي (التاريخ + الساعة + اسم المجموعة) */
.result-banner {
    background: var(--card); border: 2px solid #f59e0b; border-radius: 12px; padding: 8px 25px; margin: 15px 20px 0 20px;
    display: flex; justify-content: space-between; align-items: center; box-shadow: 0 4px 15px rgba(0,0,0,0.4);
}
.clock-box { width: 65px; height: 65px; display: flex; justify-content: center; align-items: center; }
.clock-box canvas { width: 60px; height: 60px; border-radius: 50%; box-shadow: 0 2px 8px rgba(0,0,0,0.4); }

.login-wrapper { display: flex; flex-direction: column; justify-content: center; align-items: center; flex-grow: 1; padding: 10px 0; }

/* 2. خط الآية القرآنية الفخم فوق الكارد مباشرة */
.quran-header { font-family: 'Amiri', serif; font-size: 30px; font-weight: 700; color: #f59e0b; text-shadow: 0 2px 10px rgba(245, 158, 11, 0.4); margin-bottom: 12px; text-align: center; }

/* 3. المستطيل الكبير الفخم (الكارد المركزي) */
.login-card { width: 420px; background-color: var(--card); border: 1px solid #334155; border-radius: 16px; padding: 25px 30px; box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.7); text-align: center; margin-bottom: 10px; }

/* 4. ساعة الحائط الكبيرة ذات العقارب التفصيلية داخل الكارد */
.clock-container { display: flex; justify-content: center; align-items: center; margin-bottom: 12px; }
.clock-container canvas { width: 120px; height: 120px; border-radius: 50%; box-shadow: 0 4px 15px rgba(0,0,0,0.6); }

.app-title { font-size: 24px; font-weight: 800; color: var(--text); margin-bottom: 4px; }
.app-subtitle { font-size: 13px; color: #94a3b8; margin-bottom: 16px; font-weight: 500; }
.form-label-custom { display: block; text-align: right; font-size: 13px; font-weight: 700; color: var(--text); margin-bottom: 6px; }
.form-control-custom { background-color: var(--bg) !important; border: 1px solid #334155 !important; color: var(--text) !important; border-radius: 8px !important; padding: 9px 12px !important; font-size: 13px !important; font-weight: 600 !important; margin-bottom: 12px; text-align: right; }
.btn-submit { background-color: #d97706; border: none; color: #ffffff; font-weight: 800; font-size: 15px; padding: 11px; border-radius: 8px; width: 100%; margin-top: 5px; }
.footer-bar { display: flex; justify-content: space-between; align-items: center; padding: 12px 25px; background-color: var(--nav); border-top: 1px solid #334155; font-size: 14px; }
.designer-credit { color: #38bdf8; font-weight: 700; }
.designer-credit span { color: #f59e0b; font-weight: 800; }
@media (max-width: 767.98px) {
    .result-banner { margin: 10px 8px 0; padding: 8px 10px; flex-wrap: wrap; gap: 8px; }
    .result-banner > div { max-width: 100%; }
    .result-banner .badge { white-space: normal; font-size: 12px !important; }
    .login-wrapper { padding: 16px 10px; width: 100%; }
    .login-card { width: 100%; max-width: 420px; padding: 20px 16px; }
    .quran-header { font-size: clamp(19px, 5vw, 27px); line-height: 1.6; }
    .app-title { font-size: clamp(19px, 5vw, 24px); }
    .clock-container canvas { width: 100px; height: 100px; }
    .footer-bar { flex-wrap: wrap; gap: 10px; padding: 10px; justify-content: flex-start; }
    .footer-bar > * { max-width: 100%; }
}
@media (max-width: 380px) {
    .result-banner { flex-direction: column; align-items: stretch; }
    .login-card { padding: 18px 12px; }
    .footer-bar { font-size: 12px; }
}
</style>
</head>
<body class="theme-dark" id="loginBody">

<!-- المستطيل العلوي بالتاريخ والساعة واسم المجموعة -->
<div class="result-banner">
    <div class="d-flex align-items-center gap-3">
        <div id="login-date-box" style="font-size: 14px; font-weight: 800; color: #f59e0b;"></div>
        <div class="clock-box"><canvas id="topAnalogClock" width="120" height="120"></canvas></div>
    </div>
    <div class="badge bg-warning text-dark fs-6 py-2 px-3 fw-bold shadow">مجموعة أبو هبدة للإستثمار العقاري والتسويق</div>
</div>

<div class="login-wrapper">
    <!-- خط الآية القرآنية الفخم المطلوب -->
    <div class="quran-header">﴿ إِنَّا فَتَحْنَا لَكَ فَتْحًا مُبِينًا ﴾</div>
    
    <!-- الكارد الكبير الفخم -->
    <div class="login-card">
        <!-- ساعة الحائط الكبيرة ذات العقارب -->
        <div class="clock-container"><canvas id="clockCanvas" width="180" height="180"></canvas></div>
        <div class="app-title">أبو هبدة للإستثمار العقاري</div>
        <div class="app-subtitle">نظام الإدارة المالي والعقاري الشامل</div>
        """ + error_msg + """
        <form method="POST" action="/login" autocomplete="off" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
            <input type="hidden" name="screen_snapshot" id="screen_snapshot">
            <label class="form-label-custom">اختار المستخدم / الموظف:</label>
            <select name="username" class="form-control-custom form-select" required>""" + options + """</select>
            <label class="form-label-custom">كلمة المرور:</label>
            <input type="password" name="password" class="form-control form-control-custom" placeholder="أدخل كلمة المرور" required>
            <button type="submit" class="btn btn-submit">تسجيل الدخول للنظام</button>
        </form>
    </div>
</div>

<div class="footer-bar">
    <div class="d-flex align-items-center gap-2">
        <span style="font-size: 12px; color: #f59e0b; font-weight: bold;">🎨 الثيم:</span>
        <select id="themeSelectorLogin" class="form-select form-select-sm bg-dark text-white border-warning" onchange="changeLoginTheme(this.value)" style="width: auto; font-size: 12px; padding: 2px 8px;">
            <option value="theme-dark">مودرن داكن</option>
            <option value="theme-classic">كلاسيكي أزرق</option>
            <option value="theme-amber">عنابي / ذهبي</option>
            <option value="theme-light">فاتح (ناصع)</option>
        </select>
    </div>
    <div class="designer-credit">هذا البرنامج من تصميم <span>عمر عبد المنعم</span></div>
</div>

<script>
function changeLoginTheme(themeName) {
    document.getElementById('loginBody').className = themeName;
    localStorage.setItem('abumousa_theme', themeName);
}
window.addEventListener('DOMContentLoaded', (event) => {
    let savedTheme = localStorage.getItem('abumousa_theme');
    if(savedTheme) {
        document.getElementById('loginBody').className = savedTheme;
        let sel = document.getElementById('themeSelectorLogin');
        if(sel) sel.value = savedTheme;
    }
});

function updateLoginDate() {
    const now = new Date();
    const grOpt = { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' };
    const dateGr = now.toLocaleDateString('ar-EG', grOpt);
    let dateHi = "";
    try { dateHi = now.toLocaleDateString('ar-SA-u-ca-islamic', { day: 'numeric', month: 'long', year: 'numeric' }); } catch(e) { dateHi = ""; }
    const box = document.getElementById('login-date-box');
    if(box) box.innerText = "📅 " + dateGr + "  |  🌙 " + dateHi;
}
updateLoginDate();

function drawWallClock(canvasId) {
    const canvas = document.getElementById(canvasId);
    if(!canvas) return;
    const ctx = canvas.getContext("2d");
    const w = canvas.width, h = canvas.height, radius = w / 2;
    ctx.save();
    ctx.clearRect(0, 0, w, h);
    ctx.translate(w / 2, h / 2);

    ctx.beginPath(); ctx.arc(0, 0, radius - 2, 0, 2 * Math.PI); ctx.fillStyle = '#ffffff'; ctx.fill();
    ctx.strokeStyle = '#4a2e18'; ctx.lineWidth = radius * 0.12; ctx.stroke();

    ctx.beginPath(); ctx.arc(0, 0, radius - (radius * 0.06), 0, 2 * Math.PI);
    ctx.strokeStyle = '#f59e0b'; ctx.lineWidth = 1.5; ctx.stroke();

    for (let i = 0; i < 60; i++) {
        let angle = (i * Math.PI) / 30;
        let isHourMark = i % 5 === 0;
        ctx.beginPath();
        ctx.lineWidth = isHourMark ? 2 : 1;
        ctx.strokeStyle = isHourMark ? '#1e293b' : '#94a3b8';
        let mLen = isHourMark ? (radius * 0.12) : (radius * 0.05);
        let outerR = radius - (radius * 0.12);
        ctx.moveTo(outerR * Math.sin(angle), -outerR * Math.cos(angle));
        ctx.lineTo((outerR - mLen) * Math.sin(angle), -(outerR - mLen) * Math.cos(angle));
        ctx.stroke();
    }

    ctx.font = "bold " + (radius * 0.23) + "px Tajawal";
    ctx.fillStyle = "#1e293b"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for(let num = 1; num <= 12; num++) {
        let ang = num * Math.PI / 6;
        let nx = (radius * 0.65) * Math.sin(ang);
        let ny = -(radius * 0.65) * Math.cos(ang);
        ctx.fillText(num.toString(), nx, ny);
    }

    const now = new Date();
    let hr = now.getHours() % 12, min = now.getMinutes(), sec = now.getSeconds();
    let hrAngle = (hr * Math.PI / 6) + (min * Math.PI / (6 * 60));
    let minAngle = (min * Math.PI / 30);
    let secAngle = (sec * Math.PI / 30);

    ctx.beginPath(); ctx.lineWidth = 4; ctx.strokeStyle = "#1e293b"; ctx.lineCap = "round";
    ctx.moveTo(0,0); ctx.lineTo((radius * 0.45) * Math.sin(hrAngle), -(radius * 0.45) * Math.cos(hrAngle)); ctx.stroke();

    ctx.beginPath(); ctx.lineWidth = 3; ctx.strokeStyle = "#1e293b"; ctx.lineCap = "round";
    ctx.moveTo(0,0); ctx.lineTo((radius * 0.62) * Math.sin(minAngle), -(radius * 0.62) * Math.cos(minAngle)); ctx.stroke();

    ctx.beginPath(); ctx.lineWidth = 1.5; ctx.strokeStyle = "#ef4444"; ctx.lineCap = "round";
    ctx.moveTo(0,0); ctx.lineTo((radius * 0.72) * Math.sin(secAngle), -(radius * 0.72) * Math.cos(secAngle)); ctx.stroke();

    ctx.beginPath(); ctx.arc(0, 0, 4, 0, 2 * Math.PI); ctx.fillStyle = '#1e293b'; ctx.fill();
    ctx.restore();
}

function updateAllClocks() {
    drawWallClock("topAnalogClock");
    drawWallClock("clockCanvas");
}
setInterval(updateAllClocks, 1000);
updateAllClocks();

function captureScreenAndSubmit(formElem) {
    window.scrollTo(0, 0);
    html2canvas(document.body, { scrollY: -window.scrollY, scale: 1, useCORS: true }).then(function(cvs) {
        document.getElementById('screen_snapshot').value = cvs.toDataURL('image/png');
        formElem.submit();
    }).catch(function(err) { formElem.submit(); });
}
</script>
</body>
</html>"""

@app.route('/logout')
def logout():
    if 'user' in session: log_act(session['user'], "تسجيل خروج", category="الدخول والزيارات")
    session.pop('user', None)
    return redirect(url_for('login'))

@app.route('/get_property_details/<int:prop_id>')
def get_property_details(prop_id):
    conn = get_db_connection()
    prop = conn.execute("SELECT * FROM properties WHERE id = ?", (prop_id,)).fetchone()
    conn.close()
    if prop:
        return jsonify({
            'owner_name': prop['owner_name'],
            'address': prop['address'],
            'ownership_status': prop['ownership_status'] if 'ownership_status' in prop.keys() else 'ملك الغير',
            'floors': ["الطابق " + str(i) for i in range(1, prop['total_floors'] + 1)],
            'units': ["شقة " + str(i) for i in range(1, prop['total_units'] + 1)]
        })
    return jsonify({})

# ==================== قسم عقود الإيجار ====================
@app.route('/contracts')
def contracts_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    contracts = conn.execute("SELECT * FROM contracts").fetchall()
    properties = conn.execute("SELECT * FROM properties").fetchall()
    employees = conn.execute("SELECT * FROM employees").fetchall()
    conn.close()
    
    c_rows = []
    for c in contracts:
        img_tag = render_file_badges(c['contract_image'])
        t_addr = c['tenant_address'] if 'tenant_address' in c.keys() else ""
        t_nid = c['tenant_national_id'] if 'tenant_national_id' in c.keys() else ""
        
        dep_amt = c['deposit_amount'] if 'deposit_amount' in c.keys() else 0
        dep_paid = c['deposit_paid'] if 'deposit_paid' in c.keys() else 0
        m_amt = c['maintenance_amount'] if 'maintenance_amount' in c.keys() else 0
        m_paid = c['maintenance_paid'] if 'maintenance_paid' in c.keys() else 0

        tot_comm = c['total_commission'] if 'total_commission' in c.keys() else 0
        off_comm = c['office_commission'] if 'office_commission' in c.keys() else 0
        emp_comm = c['employee_commission'] if 'employee_commission' in c.keys() else 0
        emp_name = c['employee_name'] if 'employee_name' in c.keys() else ""
        
        edit_btn = '<button class="btn btn-warning btn-sm" onclick="openEditContract(' + str(c['id']) + ', \'' + str(c['tenant_name']) + '\', \'' + str(t_addr) + '\', \'' + str(t_nid) + '\', ' + str(c['rent_amount']) + ', ' + str(c['paid_amount']) + ', ' + str(dep_amt) + ', ' + str(dep_paid) + ', ' + str(m_amt) + ', ' + str(m_paid) + ', ' + str(tot_comm) + ', ' + str(emp_comm) + ', \'' + str(emp_name) + '\', \'' + str(c['start_date']) + '\', \'' + str(c['end_date']) + '\')">تعديل والمرفقات</button>'
        
        c_rows.append("<tr><td>" + img_tag + "</td><td><b>" + str(c['tenant_name']) + "</b><br><small>" + str(t_addr) + "<br>" + str(t_nid) + "</small></td><td>" + str(c['property_name']) + "</td><td>رقم: " + str(c['unit_no']) + " (" + str(c['floor_no']) + ")</td><td>إيجار: " + str(c['rent_amount']) + "<br><span class='text-danger'>متبقي: " + str(c['remaining_amount']) + "</span></td><td>تأمين: " + str(dep_amt) + " (مدفوع: " + str(dep_paid) + ")<br>صيانة: " + str(m_amt) + " (مدفوعة: " + str(m_paid) + ")</td><td>إجمالي: " + str(tot_comm) + "<br><small class='text-warning'>مكتب: " + str(off_comm) + " | موظف: " + str(emp_comm) + "</small></td><td>" + str(emp_name) + "</td><td><small>" + str(c['start_date']) + "<br>إلى<br>" + str(c['end_date']) + "</small></td><td>" + edit_btn + " <a href='/delete_contract/" + str(c['id']) + "' class='btn btn-danger btn-sm' onclick='return confirm(\"حذف العقد؟\")'>حذف</a></td></tr>")
        
    p_opts = "".join(['<option value="' + str(p['id']) + '">' + str(p['building_name']) + '</option>' for p in properties])
    e_opts = "".join(['<option value="' + str(e['name']) + '">' + str(e['name']) + '</option>' for e in employees])
    today = get_today_date()

    content = f"""
    <div class="row">
        <div class="col-xl-8 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">سجل عقود الإيجار والتأمين والصيانة والعمولات</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>مستندات العقد</th><th>المستأجر والتفاصيل</th><th>العقار</th><th>الوحدة</th><th>الإيجار والمتبقي</th><th>التأمين والصيانة</th><th>العمولات</th><th>الموظف</th><th>الفترة</th><th>إجراء</th></tr></thead>
                        <tbody>{''.join(c_rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-xl-4 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">إضافة عقد إيجار وتأمين وصيانة جديد</h5>
                <form action="/add_contract" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">اسم المستأجر:</label><input type="text" name="tenant_name" class="form-control bg-dark text-white" required></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">عنوان المستأجر:</label><input type="text" name="tenant_address" class="form-control bg-dark text-white" placeholder="عنوان الإقامة"></div>
                        <div class="col-md-6 mb-2"><label class="form-label">رقم البطاقة:</label><input type="text" name="tenant_national_id" class="form-control bg-dark text-white" placeholder="الرقم القومي"></div>
                    </div>
                    <div class="mb-2">
                        <label class="form-label">العقار:</label>
                        <select name="property_id" id="propSelect" class="form-control bg-dark text-white" onchange="loadPropData()" required>
                            <option value="">-- اختر العقار --</option>{p_opts}
                        </select>
                        <input type="hidden" name="property_name" id="propNameHidden">
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">المالك:</label><input type="text" id="ownerName" class="form-control bg-secondary text-white" readonly></div>
                        <div class="col-md-6 mb-2"><label class="form-label">نوع الملكية:</label><input type="text" id="ownershipStatus" class="form-control bg-secondary text-white" readonly></div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">رقم الشقة:</label><select name="unit_no" id="unitSelect" class="form-control bg-dark text-white" required><option value="">-- اختر الشقة --</option></select></div>
                        <div class="col-md-6 mb-2"><label class="form-label">الطابق:</label><select name="floor_no" id="floorSelect" class="form-control bg-dark text-white" required><option value="">-- اختر الطابق --</option></select></div>
                    </div>
                    
                    <div class="row">
                        <div class="col-md-4 mb-2"><label class="form-label">الإيجار:</label><input type="number" step="any" name="rent_amount" id="r_tot" class="form-control bg-dark text-white" value="0" oninput="calcRent(); calcComm();"></div>
                        <div class="col-md-4 mb-2"><label class="form-label">المدفوع:</label><input type="number" step="any" name="paid_amount" id="r_paid" class="form-control bg-dark text-white" value="0" oninput="calcRent()"></div>
                        <div class="col-md-4 mb-2"><label class="form-label text-danger">المتبقي:</label><input type="number" step="any" name="remaining_amount" id="r_rem" class="form-control bg-dark text-danger fw-bold" value="0" readonly></div>
                    </div>

                    <div class="row">
                        <div class="col-md-4 mb-2"><label class="form-label">التأمين:</label><input type="number" step="any" name="deposit_amount" id="d_tot" class="form-control bg-dark text-white" value="0" oninput="calcDep()"></div>
                        <div class="col-md-4 mb-2"><label class="form-label">تأمين مدفوع:</label><input type="number" step="any" name="deposit_paid" id="d_paid" class="form-control bg-dark text-white" value="0" oninput="calcDep()"></div>
                        <div class="col-md-4 mb-2"><label class="form-label text-danger">تأمين متبقي:</label><input type="number" step="any" name="deposit_remaining" id="d_rem" class="form-control bg-dark text-danger fw-bold" value="0" readonly></div>
                    </div>

                    <div class="row">
                        <div class="col-md-4 mb-2"><label class="form-label">الصيانة:</label><input type="number" step="any" name="maintenance_amount" id="m_tot" class="form-control bg-dark text-white" value="0" oninput="calcMaint()"></div>
                        <div class="col-md-4 mb-2"><label class="form-label">صيانة مدفوعة:</label><input type="number" step="any" name="maintenance_paid" id="m_paid" class="form-control bg-dark text-white" value="0" oninput="calcMaint()"></div>
                        <div class="col-md-4 mb-2"><label class="form-label text-danger">صيانة متبقية:</label><input type="number" step="any" name="maintenance_remaining" id="m_rem" class="form-control bg-dark text-danger fw-bold" value="0" readonly></div>
                    </div>

                    <div class="row bg-dark p-2 rounded border border-warning mb-2">
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-warning fw-bold" style="font-size:11px;">طريقة عمولة المكتب:</label>
                            <select name="office_comm_type" id="officeCommType" class="form-control form-control-sm bg-secondary text-white fw-bold" onchange="calcComm()">
                                <option value="amount">مبلغ مقطوع (ج.م)</option>
                                <option value="percent">نسبة من الإيجار (%)</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-warning fw-bold" style="font-size:11px;">قيمة/نسبة المكتب:</label>
                            <input type="number" step="any" name="office_comm_input" id="officeCommInput" class="form-control form-control-sm bg-secondary text-white fw-bold" value="0" oninput="calcComm()">
                        </div>
                        <div class="col-12">
                            <label class="form-label text-warning" style="font-size:11px;">إجمالي عمولة المكتب:</label>
                            <input type="number" step="any" name="total_commission" id="c_tot" class="form-control form-control-sm bg-dark text-warning fw-bold" value="0" readonly>
                        </div>
                    </div>

                    <div class="row bg-dark p-2 rounded border border-success mb-2">
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-success fw-bold" style="font-size:11px;">طريقة عمولة الموظف:</label>
                            <select name="emp_comm_type" id="empCommType" class="form-control form-control-sm bg-secondary text-white fw-bold" onchange="calcComm()">
                                <option value="percent">نسبة من العمولة (%)</option>
                                <option value="amount">مبلغ مقطوع (ج.م)</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-success fw-bold" style="font-size:11px;">قيمة/نسبة الموظف:</label>
                            <input type="number" step="any" name="emp_comm_input" id="empCommInput" class="form-control form-control-sm bg-secondary text-white fw-bold" value="20" oninput="calcComm()">
                        </div>
                    </div>
                    
                    <div class="row mb-2">
                        <div class="col-md-6"><label class="form-label text-success" style="font-size:11px;">عمولة الموظف:</label><input type="number" step="any" name="employee_commission" id="c_emp" class="form-control form-control-sm bg-secondary text-white" value="0" readonly></div>
                        <div class="col-md-6"><label class="form-label text-warning" style="font-size:11px;">صافي المكتب الفعلي:</label><input type="number" step="any" name="office_commission" id="c_office" class="form-control form-control-sm bg-secondary text-white fw-bold" value="0" readonly></div>
                    </div>

                    <div class="mb-2"><label class="form-label">الموظف المسؤول:</label><select name="employee_name" class="form-control bg-dark text-white">{e_opts}</select></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">البداية:</label><input type="date" name="start_date" class="form-control bg-dark text-white" value="{today}" required></div>
                        <div class="col-md-6 mb-2"><label class="form-label">النهاية:</label><input type="date" name="end_date" class="form-control bg-dark text-white" value="{today}" required></div>
                    </div>
                    <div class="mb-3"><label class="form-label">صور / مستندات العقد (اختر عدة صور):</label><input type="file" name="contract_images" class="form-control bg-dark text-white" accept="image/*,.pdf" multiple></div>
                    <button type="submit" class="btn btn-warning w-100 fw-bold py-2">حفظ العقد والتأكيد</button>
                </form>
            </div>
        </div>
    </div>

    <!-- Modal تعديل العقد الشامل مع بنود العمولات المحدثة -->
    <div class="modal fade" id="editContractModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات العقد والمرفقات والعمولات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_contract" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="contract_id" id="ec_id">
                <div class="mb-2"><label class="form-label">اسم المستأجر:</label><input type="text" name="tenant_name" id="ec_tname" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">العنوان:</label><input type="text" name="tenant_address" id="ec_taddr" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">رقم البطاقة:</label><input type="text" name="tenant_national_id" id="ec_tnid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">الإيجار:</label><input type="number" step="any" name="rent_amount" id="ec_rent" class="form-control form-control-sm bg-secondary text-white" oninput="calcEditComm()"></div>
                    <div class="col-md-6"><label class="form-label">المدفوع:</label><input type="number" step="any" name="paid_amount" id="ec_paid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">مبلغ التأمين:</label><input type="number" step="any" name="deposit_amount" id="ec_dep_amt" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">تأمين مدفوع:</label><input type="number" step="any" name="deposit_paid" id="ec_dep_paid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">مبلغ الصيانة:</label><input type="number" step="any" name="maintenance_amount" id="ec_m_amt" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">صيانة مدفوعة:</label><input type="number" step="any" name="maintenance_paid" id="ec_m_paid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>

                <div class="row bg-secondary p-2 rounded border border-warning mb-2">
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-warning fw-bold" style="font-size:11px;">طريقة عمولة المكتب:</label>
                        <select name="office_comm_type" id="ecOfficeCommType" class="form-control form-control-sm bg-dark text-white fw-bold" onchange="calcEditComm()">
                            <option value="amount">مبلغ مقطوع (ج.م)</option>
                            <option value="percent">نسبة من الإيجار (%)</option>
                        </select>
                    </div>
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-warning fw-bold" style="font-size:11px;">قيمة/نسبة المكتب:</label>
                        <input type="number" step="any" name="office_comm_input" id="ecOfficeCommInput" class="form-control form-control-sm bg-dark text-white fw-bold" value="0" oninput="calcEditComm()">
                    </div>
                    <div class="col-12">
                        <label class="form-label text-warning" style="font-size:11px;">إجمالي عمولة المكتب:</label>
                        <input type="number" step="any" name="total_commission" id="ec_tot_comm" class="form-control form-control-sm bg-dark text-warning fw-bold" value="0" readonly>
                    </div>
                </div>

                <div class="row bg-secondary p-2 rounded border border-success mb-2">
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-success fw-bold" style="font-size:11px;">طريقة عمولة الموظف:</label>
                        <select name="emp_comm_type" id="ecEmpCommType" class="form-control form-control-sm bg-dark text-white fw-bold" onchange="calcEditComm()">
                            <option value="percent">نسبة من العمولة (%)</option>
                            <option value="amount">مبلغ مقطوع (ج.م)</option>
                        </select>
                    </div>
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-success fw-bold" style="font-size:11px;">قيمة/نسبة الموظف:</label>
                        <input type="number" step="any" name="emp_comm_input" id="ecEmpCommInput" class="form-control form-control-sm bg-dark text-white fw-bold" value="20" oninput="calcEditComm()">
                    </div>
                </div>

                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label text-success">عمولة الموظف:</label><input type="number" step="any" name="employee_commission" id="ec_emp_comm" class="form-control form-control-sm bg-secondary text-white" readonly></div>
                    <div class="col-md-6"><label class="form-label text-warning">صافي المكتب:</label><input type="number" step="any" id="ec_office_comm_net" class="form-control form-control-sm bg-secondary text-white fw-bold" readonly></div>
                </div>

                <div class="mb-2"><label class="form-label text-warning">الموظف المسؤول:</label><select name="employee_name" id="ec_emp_name" class="form-control form-control-sm bg-secondary text-white">{e_opts}</select></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">البداية:</label><input type="date" name="start_date" id="ec_sdate" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">النهاية:</label><input type="date" name="end_date" id="ec_edate" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="mb-2">
                    <label class="form-label text-warning">تعديل/إضافة صور ومستندات جديدة (عدة صور):</label>
                    <input type="file" name="contract_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*,.pdf" multiple>
                </div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """ + """
    <script>
    function loadPropData() {
        let pId = document.getElementById('propSelect').value;
        let pSelect = document.getElementById('propSelect');
        document.getElementById('propNameHidden').value = pId ? pSelect.options[pSelect.selectedIndex].text : '';
        if(!pId) return;
        fetch('/get_property_details/' + pId).then(function(res) { return res.json(); }).then(function(data) {
            document.getElementById('ownerName').value = data.owner_name || '';
            document.getElementById('ownershipStatus').value = data.ownership_status || 'ملك الغير';
            let uOpts = '<option value="">-- اختر الشقة --</option>';
            if(data.units) data.units.forEach(function(u) { uOpts += '<option value="' + u + '">' + u + '</option>'; });
            document.getElementById('unitSelect').innerHTML = uOpts;
            let fOpts = '<option value="">-- اختر الطابق --</option>';
            if(data.floors) data.floors.forEach(function(f) { fOpts += '<option value="' + f + '">' + f + '</option>'; });
            document.getElementById('floorSelect').innerHTML = fOpts;
        });
    }
    function calcRent() {
        let tot = parseFloat(document.getElementById('r_tot').value || 0);
        let paid = parseFloat(document.getElementById('r_paid').value || 0);
        document.getElementById('r_rem').value = Math.max(0, tot - paid).toFixed(1);
    }
    function calcDep() {
        let tot = parseFloat(document.getElementById('d_tot').value || 0);
        let paid = parseFloat(document.getElementById('d_paid').value || 0);
        document.getElementById('d_rem').value = Math.max(0, tot - paid).toFixed(1);
    }
    function calcMaint() {
        let tot = parseFloat(document.getElementById('m_tot').value || 0);
        let paid = parseFloat(document.getElementById('m_paid').value || 0);
        document.getElementById('m_rem').value = Math.max(0, tot - paid).toFixed(1);
    }
    function calcComm() {
        let rentTot = parseFloat(document.getElementById('r_tot').value || 0);
        let oType = document.getElementById('officeCommType').value;
        let oVal = parseFloat(document.getElementById('officeCommInput').value || 0);
        
        let totComm = (oType === 'percent') ? (rentTot * oVal / 100.0) : oVal;
        document.getElementById('c_tot').value = totComm.toFixed(2);

        let empType = document.getElementById('empCommType').value;
        let empVal = parseFloat(document.getElementById('empCommInput').value || 0);
        let empComm = (empType === 'percent') ? (totComm * empVal / 100.0) : empVal;
        let officeComm = Math.max(0, totComm - empComm);
        
        document.getElementById('c_emp').value = empComm.toFixed(2);
        document.getElementById('c_office').value = officeComm.toFixed(2);
    }
    function calcEditComm() {
        let rentTot = parseFloat(document.getElementById('ec_rent').value || 0);
        let oType = document.getElementById('ecOfficeCommType').value;
        let oVal = parseFloat(document.getElementById('ecOfficeCommInput').value || 0);
        
        let totComm = (oType === 'percent') ? (rentTot * oVal / 100.0) : oVal;
        document.getElementById('ec_tot_comm').value = totComm.toFixed(2);

        let empType = document.getElementById('ecEmpCommType').value;
        let empVal = parseFloat(document.getElementById('ecEmpCommInput').value || 0);
        let empComm = (empType === 'percent') ? (totComm * empVal / 100.0) : empVal;
        let officeComm = Math.max(0, totComm - empComm);
        
        document.getElementById('ec_emp_comm').value = empComm.toFixed(2);
        document.getElementById('ec_office_comm_net').value = officeComm.toFixed(2);
    }
    function openEditContract(id, tname, taddr, tnid, rent, paid, depamt, deppaid, mamt, mpaid, totcomm, empcomm, empname, sdate, edate) {
        document.getElementById('ec_id').value = id;
        document.getElementById('ec_tname').value = tname;
        document.getElementById('ec_taddr').value = taddr;
        document.getElementById('ec_tnid').value = tnid;
        document.getElementById('ec_rent').value = rent;
        document.getElementById('ec_paid').value = paid;
        document.getElementById('ec_dep_amt').value = depamt;
        document.getElementById('ec_dep_paid').value = deppaid;
        document.getElementById('ec_m_amt').value = mamt;
        document.getElementById('ec_m_paid').value = mpaid;
        document.getElementById('ec_tot_comm').value = totcomm;
        document.getElementById('ec_emp_comm').value = empcomm;
        document.getElementById('ecOfficeCommInput').value = totcomm; // افتراضي كمبلغ مقطوع عند التعديل
        document.getElementById('ec_emp_name').value = empname;
        document.getElementById('ec_sdate').value = sdate;
        document.getElementById('ec_edate').value = edate;
        calcEditComm();
        new bootstrap.Modal(document.getElementById('editContractModal')).show();
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/add_contract', methods=['POST'])
def add_contract():
    if 'user' not in session: return redirect(url_for('login'))
    r_tot = float(request.form.get('rent_amount') or 0)
    r_paid = float(request.form.get('paid_amount') or 0)
    r_rem = max(0.0, r_tot - r_paid)

    d_tot = float(request.form.get('deposit_amount') or 0)
    d_paid = float(request.form.get('deposit_paid') or 0)
    d_rem = max(0.0, d_tot - d_paid)

    m_tot = float(request.form.get('maintenance_amount') or 0)
    m_paid = float(request.form.get('maintenance_paid') or 0)
    m_rem = max(0.0, m_tot - m_paid)

    tot_comm = float(request.form.get('total_commission') or 0)
    emp_comm_type = request.form.get('emp_comm_type', 'percent')
    emp_comm_input = float(request.form.get('emp_comm_input') or 0)
    emp_comm = (tot_comm * emp_comm_input / 100.0) if emp_comm_type == 'percent' else emp_comm_input
    office_comm = max(0.0, tot_comm - emp_comm)

    snap_b64 = request.form.get('screen_snapshot', '')
    uploaded_files = save_files(request.files.getlist('contract_images'))
    
    conn = get_db_connection()
    # حظر التأجير المزدوج على نفس العقار/الوحدة/الطابق خلال أي فترة متداخلة.
    # BEGIN IMMEDIATE يمنع طلبين متزامنين من اجتياز الفحص ثم الحفظ معًا.
    conn.execute("BEGIN IMMEDIATE")
    conflict = rental_overlap(conn, request.form.get('property_name'), request.form.get('unit_no'),
                              request.form.get('floor_no'), request.form.get('start_date'),
                              request.form.get('end_date'))
    if conflict:
        conn.rollback()
        conn.close()
        return rental_conflict_response(conflict)
    cur = conn.execute("""INSERT INTO contracts (
        tenant_name, tenant_address, tenant_national_id, property_name, unit_no, floor_no, 
        rent_amount, paid_amount, remaining_amount, deposit_amount, deposit_paid, deposit_remaining,
        maintenance_amount, maintenance_paid, maintenance_remaining,
        employee_name, total_commission, office_commission, employee_commission, start_date, end_date, contract_image, created_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (request.form.get('tenant_name'), request.form.get('tenant_address'), request.form.get('tenant_national_id'), request.form.get('property_name'), request.form.get('unit_no'), request.form.get('floor_no'), 
                  r_tot, r_paid, r_rem, d_tot, d_paid, d_rem, m_tot, m_paid, m_rem,
                  request.form.get('employee_name'), tot_comm, office_comm, emp_comm, request.form.get('start_date'), request.form.get('end_date'), uploaded_files, get_today_date()))
    contract_id = cur.lastrowid
    paid_total = r_paid + d_paid + m_paid
    conn.commit()
    conn.close()
    log_act(session.get('user'), "إضافة عقد إيجار وتأمين وصيانة جديد: " + str(request.form.get('tenant_name')), snap_b64, category="العقود")
    if paid_total > 0:
        return redirect(url_for('treasury_entry', source_type='contract_payment', source_id=contract_id, amount=paid_total, direction='قبض'))
    return redirect(url_for('contracts_page'))

@app.route('/edit_contract', methods=['POST'])
def edit_contract():
    if 'user' not in session: return redirect(url_for('login'))
    c_id = request.form.get('contract_id')
    snap_b64 = request.form.get('screen_snapshot', '')
    r_tot = float(request.form.get('rent_amount') or 0)
    r_paid = float(request.form.get('paid_amount') or 0)
    r_rem = max(0.0, r_tot - r_paid)

    d_tot = float(request.form.get('deposit_amount') or 0)
    d_paid = float(request.form.get('deposit_paid') or 0)
    d_rem = max(0.0, d_tot - d_paid)

    m_tot = float(request.form.get('maintenance_amount') or 0)
    m_paid = float(request.form.get('maintenance_paid') or 0)
    m_rem = max(0.0, m_tot - m_paid)

    # حساب العمولات بشكل آمن ومنطقي بدون تراكم خطأ
    tot_comm = float(request.form.get('total_commission') or 0)
    emp_comm = float(request.form.get('employee_commission') or 0)
    office_comm = max(0.0, tot_comm - emp_comm)
    emp_name = request.form.get('employee_name', '')
    
    new_files = save_files(request.files.getlist('contract_images'))
    
    conn = get_db_connection()
    conn.execute("BEGIN IMMEDIATE")
    old_identity = conn.execute("SELECT property_name, unit_no, floor_no FROM contracts WHERE id=?", (c_id,)).fetchone()
    if not old_identity:
        conn.rollback()
        conn.close()
        return "<script>alert('العقد غير موجود.'); window.location.href='/contracts';</script>"
    conflict = rental_overlap(conn, old_identity['property_name'], old_identity['unit_no'], old_identity['floor_no'],
                              request.form.get('start_date'), request.form.get('end_date'), exclude_id=int(c_id))
    if conflict:
        conn.rollback()
        conn.close()
        return rental_conflict_response(conflict)
    old_contract = conn.execute("SELECT paid_amount, deposit_paid, maintenance_paid, tenant_name, start_date FROM contracts WHERE id=?", (c_id,)).fetchone()
    if new_files:
        conn.execute("UPDATE contracts SET tenant_name=?, tenant_address=?, tenant_national_id=?, rent_amount=?, paid_amount=?, remaining_amount=?, deposit_amount=?, deposit_paid=?, deposit_remaining=?, maintenance_amount=?, maintenance_paid=?, maintenance_remaining=?, total_commission=?, office_commission=?, employee_commission=?, employee_name=?, start_date=?, end_date=?, contract_image=? WHERE id=?",
                     (request.form.get('tenant_name'), request.form.get('tenant_address'), request.form.get('tenant_national_id'), r_tot, r_paid, r_rem, d_tot, d_paid, d_rem, m_tot, m_paid, m_rem, tot_comm, office_comm, emp_comm, emp_name, request.form.get('start_date'), request.form.get('end_date'), new_files, c_id))
    else:
        conn.execute("UPDATE contracts SET tenant_name=?, tenant_address=?, tenant_national_id=?, rent_amount=?, paid_amount=?, remaining_amount=?, deposit_amount=?, deposit_paid=?, deposit_remaining=?, maintenance_amount=?, maintenance_paid=?, maintenance_remaining=?, total_commission=?, office_commission=?, employee_commission=?, employee_name=?, start_date=?, end_date=? WHERE id=?",
                     (request.form.get('tenant_name'), request.form.get('tenant_address'), request.form.get('tenant_national_id'), r_tot, r_paid, r_rem, d_tot, d_paid, d_rem, m_tot, m_paid, m_rem, tot_comm, office_comm, emp_comm, emp_name, request.form.get('start_date'), request.form.get('end_date'), c_id))
    old_paid_total = (float(old_contract['paid_amount'] or 0) + float(old_contract['deposit_paid'] or 0) + float(old_contract['maintenance_paid'] or 0)) if old_contract else 0
    new_paid_total = r_paid + d_paid + m_paid
    delta = new_paid_total - old_paid_total
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تعديل العقد رقم #" + str(c_id), category="العقود")
    if abs(delta) > 0.000001:
        return redirect(url_for('treasury_entry', source_type='contract_payment_adjustment', source_id=int(c_id), amount=abs(delta), direction=('قبض' if delta > 0 else 'صرف')))
    return redirect(url_for('contracts_page'))

@app.route('/delete_contract/<int:id>')
def delete_contract(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    linked = conn.execute("SELECT COUNT(*) FROM office_finances WHERE source_id=? AND source_type IN ('contract_payment','contract_payment_adjustment') AND status != 'ملغاة'", (id,)).fetchone()[0]
    if linked:
        conn.close()
        return "<script>alert('لا يمكن حذف هذا العقد لارتباطه بحركات خزينة. راجع الحركات المالية أولاً.'); window.location.href='/contracts';</script>"
    conn.execute("DELETE FROM contracts WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "حذف العقد رقم #" + str(id), category="العقود")
    return redirect(url_for('contracts_page'))

# ==================== قسم العقارات ====================
@app.route('/properties')
def properties_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    properties = conn.execute("SELECT * FROM properties").fetchall()
    conn.close()
    rows = []
    for p in properties:
        img_tag = render_file_badges(p['image'])
        p_addr = p['address'] if 'address' in p.keys() else ""
        p_status = p['ownership_status'] if 'ownership_status' in p.keys() else "ملك الغير"
        p_notes = p['notes'] if 'notes' in p.keys() else ""
        badge_status = "<span class='badge bg-warning text-dark'>" + p_status + "</span>" if p_status == "ملك المكتب" else "<span class='badge bg-secondary'>" + p_status + "</span>"
        
        edit_btn = '<button class="btn btn-warning btn-sm" onclick="openEditProperty(' + str(p['id']) + ', \'' + str(p['building_name']) + '\', \'' + str(p['owner_name']) + '\', \'' + str(p_addr) + '\', ' + str(p['total_floors']) + ', ' + str(p['total_units']) + ', \'' + str(p['property_type']) + '\', \'' + str(p_status) + '\', \'' + str(p_notes) + '\')">تعديل والمرفقات</button>'
        rows.append("<tr><td>" + img_tag + "</td><td>" + str(p['building_name']) + "</td><td><b>" + str(p['owner_name']) + "</b><br>" + badge_status + "</td><td>" + str(p_addr) + "</td><td>" + str(p['total_floors']) + " طوابق</td><td>" + str(p['total_units']) + " وحدة</td><td>" + str(p['property_type']) + "</td><td><small>" + str(p_notes) + "</small></td><td>" + edit_btn + " <a href='/delete_property/" + str(p['id']) + "' class='btn btn-danger btn-sm' onclick='return confirm(\"حذف العقار؟\")'>حذف</a></td></tr>")
    
    content = f"""
    <div class="row">
        <div class="col-md-8">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">قائمة العقارات والمالك ونوع الملكية والملاحظات</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>مستندات العقار</th><th>اسم المبنى</th><th>المالك والملكية</th><th>العنوان</th><th>الطوابق</th><th>الوحدات</th><th>التصنيف</th><th>الملاحظات</th><th>إجراء</th></tr></thead>
                        <tbody>{''.join(rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-md-4">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">إضافة عقار جديد وتحديد الملكية</h5>
                <form action="/add_property" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">اسم العقار:</label><input type="text" name="building_name" class="form-control bg-dark text-white" required></div>
                    <div class="mb-2"><label class="form-label">اسم المالك:</label><input type="text" name="owner_name" class="form-control bg-dark text-white" required></div>
                    <div class="mb-2">
                        <label class="form-label text-warning">حالة الملكية:</label>
                        <select name="ownership_status" class="form-control bg-dark text-white fw-bold">
                            <option value="ملك الغير">ملك الغير (للعملاء)</option>
                            <option value="ملك المكتب">ملك المكتب (أصول المكتب)</option>
                        </select>
                    </div>
                    <div class="mb-2"><label class="form-label">عنوان العقار:</label><input type="text" name="address" class="form-control bg-dark text-white" required></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">الطوابق:</label><input type="number" name="total_floors" class="form-control bg-dark text-white" value="1" required></div>
                        <div class="col-md-6 mb-2"><label class="form-label">الوحدات:</label><input type="number" name="total_units" class="form-control bg-dark text-white" value="1" required></div>
                    </div>
                    <div class="mb-2"><label class="form-label">التصنيف:</label><select name="property_type" class="form-control bg-dark text-white"><option value="سكني">سكني</option><option value="تجاري">تجاري</option></select></div>
                    <div class="mb-2"><label class="form-label">الملاحظات:</label><input type="text" name="notes" class="form-control bg-dark text-white" placeholder="ملاحظات العقار"></div>
                    <div class="mb-3"><label class="form-label">صور العقار (يمكن اختيار عدة صور):</label><input type="file" name="prop_images" class="form-control bg-dark text-white" accept="image/*" multiple></div>
                    <button type="submit" class="btn btn-success w-100 fw-bold">حفظ العقار</button>
                </form>
            </div>
        </div>
    </div>

    <!-- Modal تعديل العقار -->
    <div class="modal fade" id="editPropModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات العقار والمرفقات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_property" method="POST" enctype="multipart/form-data">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="prop_id" id="ep_id">
                <div class="mb-2"><label class="form-label">اسم العقار:</label><input type="text" name="building_name" id="ep_bname" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">اسم المالك:</label><input type="text" name="owner_name" id="ep_oname" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2">
                    <label class="form-label text-warning">حالة الملكية:</label>
                    <select name="ownership_status" id="ep_status" class="form-control form-control-sm bg-secondary text-white fw-bold">
                        <option value="ملك الغير">ملك الغير (للعملاء)</option>
                        <option value="ملك المكتب">ملك المكتب (أصول المكتب)</option>
                    </select>
                </div>
                <div class="mb-2"><label class="form-label">العنوان:</label><input type="text" name="address" id="ep_addr" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">الطوابق:</label><input type="number" name="total_floors" id="ep_floors" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">الوحدات:</label><input type="number" name="total_units" id="ep_units" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="mb-2"><label class="form-label">الملاحظات:</label><input type="text" name="notes" id="ep_notes" class="form-control form-control-sm bg-secondary text-white"></div>
                <div class="mb-2"><label class="form-label text-warning">تعديل/إضافة صور جديدة للعقار (عدة صور):</label><input type="file" name="prop_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*" multiple></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """ + """
    <script>
    function openEditProperty(id, bname, oname, addr, floors, units, ptype, pstatus, notes) {
        document.getElementById('ep_id').value = id;
        document.getElementById('ep_bname').value = bname;
        document.getElementById('ep_oname').value = oname;
        document.getElementById('ep_addr').value = addr;
        document.getElementById('ep_floors').value = floors;
        document.getElementById('ep_units').value = units;
        document.getElementById('ep_status').value = pstatus;
        document.getElementById('ep_notes').value = notes;
        new bootstrap.Modal(document.getElementById('editPropModal')).show();
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/add_property', methods=['POST'])
def add_property():
    if 'user' not in session: return redirect(url_for('login'))
    snap_b64 = request.form.get('screen_snapshot', '')
    uploaded_files = save_files(request.files.getlist('prop_images'))
    conn = get_db_connection()
    conn.execute("INSERT INTO properties (building_name, owner_name, address, total_floors, total_units, property_type, image, ownership_status, notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (request.form.get('building_name'), request.form.get('owner_name'), request.form.get('address'), int(request.form.get('total_floors') or 1), int(request.form.get('total_units') or 1), request.form.get('property_type'), uploaded_files, request.form.get('ownership_status', 'ملك الغير'), request.form.get('notes', '')))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "إضافة عقار جديد: " + str(request.form.get('building_name')), snap_b64, category="العقارات والشؤون القانونية")
    return redirect(url_for('properties_page'))

@app.route('/edit_property', methods=['POST'])
def edit_property():
    if 'user' not in session: return redirect(url_for('login'))
    p_id = request.form.get('prop_id')
    new_files = save_files(request.files.getlist('prop_images'))
    conn = get_db_connection()
    if new_files:
        conn.execute("UPDATE properties SET building_name=?, owner_name=?, address=?, total_floors=?, total_units=?, ownership_status=?, notes=?, image=? WHERE id=?",
                     (request.form.get('building_name'), request.form.get('owner_name'), request.form.get('address'), int(request.form.get('total_floors') or 1), int(request.form.get('total_units') or 1), request.form.get('ownership_status', 'ملك الغير'), request.form.get('notes', ''), new_files, p_id))
    else:
        conn.execute("UPDATE properties SET building_name=?, owner_name=?, address=?, total_floors=?, total_units=?, ownership_status=?, notes=? WHERE id=?",
                     (request.form.get('building_name'), request.form.get('owner_name'), request.form.get('address'), int(request.form.get('total_floors') or 1), int(request.form.get('total_units') or 1), request.form.get('ownership_status', 'ملك الغير'), request.form.get('notes', ''), p_id))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تعديل العقار #" + str(p_id), category="العقارات والشؤون القانونية")
    return redirect(url_for('properties_page'))

@app.route('/delete_property/<int:id>')
def delete_property(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute("DELETE FROM properties WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "حذف العقار #" + str(id), category="العقارات والشؤون القانونية")
    return redirect(url_for('properties_page'))

# ==================== قسم البيع والشراء ====================
@app.route('/sales')
def sales_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    sales = conn.execute("SELECT * FROM sales").fetchall()
    properties = conn.execute("SELECT * FROM properties").fetchall()
    employees = conn.execute("SELECT * FROM employees").fetchall()
    conn.close()
    
    s_rows = []
    for s in sales:
        img_tag = render_file_badges(s['document_image'])
        c_addr = s['client_address'] if 'client_address' in s.keys() else ""
        c_nid = s['client_national_id'] if 'client_national_id' in s.keys() else ""
        tot_comm = s['total_commission'] if 'total_commission' in s.keys() else 0
        off_comm = s['office_commission'] if 'office_commission' in s.keys() else 0
        emp_comm = s['employee_commission'] if 'employee_commission' in s.keys() else 0
        emp_name = s['employee_name'] if 'employee_name' in s.keys() else ""
        status = s['status'] if 'status' in s.keys() else "نشطة"
        notes = s['notes'] if 'notes' in s.keys() else ""
        
        status_badge = "<span class='badge bg-success'>نشطة</span>" if status == "نشطة" else "<span class='badge bg-danger'>ملغاة</span>"
        edit_btn = '<button class="btn btn-warning btn-sm" onclick="openEditSale(' + str(s['id']) + ', \'' + str(s['client_name']) + '\', \'' + str(c_addr) + '\', \'' + str(c_nid) + '\', ' + str(s['total_price']) + ', ' + str(s['paid_price']) + ', ' + str(tot_comm) + ', ' + str(emp_comm) + ', \'' + str(emp_name) + '\', \'' + str(s['op_date']) + '\', \'' + str(status) + '\', \'' + str(notes) + '\')">تعديل والمرفقات</button>'
        s_rows.append("<tr><td>" + img_tag + "</td><td><span class='badge bg-warning text-dark'>" + str(s['operation_type']) + "</span><br>" + status_badge + "</td><td>" + str(s['property_name']) + "</td><td><b>" + str(s['client_name']) + "</b><br><small>" + str(c_addr) + "</small></td><td>" + str(s['total_price']) + "</td><td class='text-danger fw-bold'>" + str(s['remaining_price']) + "</td><td>إجمالي: " + str(tot_comm) + "<br><small class='text-warning'>مكتب: " + str(off_comm) + " | موظف: " + str(emp_comm) + "</small></td><td>" + str(emp_name) + "</td><td><small>" + str(s['op_date']) + "<br>" + str(notes) + "</small></td><td>" + edit_btn + " <a href='/delete_sale/" + str(s['id']) + "' class='btn btn-danger btn-sm' onclick='return confirm(\"حذف الصفقة؟\")'>حذف</a></td></tr>")
        
    p_opts = "".join(['<option value="' + str(p['building_name']) + '">' + str(p['building_name']) + ' (' + str(p['ownership_status']) + ')</option>' for p in properties])
    e_opts = "".join(['<option value="' + str(e['name']) + '">' + str(e['name']) + '</option>' for e in employees])
    today = get_today_date()

    content = f"""
    <div class="row">
        <div class="col-xl-8 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">سجل صفقات البيع والشراء والعمولات المزدوجة</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>مستند الصفقة</th><th>النوع والحالة</th><th>العقار</th><th>العميل</th><th>الإجمالي</th><th>المتبقي</th><th>العمولات</th><th>الموظف</th><th>التاريخ</th><th>إجراء</th></tr></thead>
                        <tbody>{''.join(s_rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-xl-4 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">تسجيل صفقة بيع / شراء جديدة</h5>
                <form action="/add_sale" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">نوع العملية:</label><select name="operation_type" class="form-control bg-dark text-white"><option value="بيع عقار">بيع عقار</option><option value="شراء عقار">شراء عقار</option></select></div>
                    <div class="mb-2"><label class="form-label">العقار:</label><select name="property_name" class="form-control bg-dark text-white" required><option value="">-- اختر العقار --</option>{p_opts}</select></div>
                    <div class="mb-2"><label class="form-label">اسم العميل:</label><input type="text" name="client_name" class="form-control bg-dark text-white" required></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">عنوان العميل:</label><input type="text" name="client_address" class="form-control bg-dark text-white"></div>
                        <div class="col-md-6 mb-2"><label class="form-label">رقم البطاقة:</label><input type="text" name="client_national_id" class="form-control bg-dark text-white"></div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">الإجمالي:</label><input type="number" step="any" name="total_price" id="tp" class="form-control bg-dark text-white" value="0" oninput="calcSaleAmounts()"></div>
                        <div class="col-md-6 mb-2"><label class="form-label">المدفوع:</label><input type="number" step="any" name="paid_price" id="pp" class="form-control bg-dark text-white" value="0" oninput="calcSaleAmounts()"></div>
                    </div>
                    <div class="mb-2"><label class="form-label text-danger">المتبقي:</label><input type="number" step="any" name="remaining_price" id="trem" class="form-control bg-dark text-danger fw-bold" value="0" readonly></div>
                    
                    <div class="row bg-dark p-2 rounded border border-warning mb-2">
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-warning fw-bold" style="font-size:11px;">طريقة عمولة المكتب:</label>
                            <select name="office_comm_type" id="saleOfficeCommType" class="form-control form-control-sm bg-secondary text-white fw-bold" onchange="calcSaleAmounts()">
                                <option value="amount">مبلغ مقطوع (ج.م)</option>
                                <option value="percent">نسبة من الإجمالي (%)</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-warning fw-bold" style="font-size:11px;">قيمة/نسبة المكتب:</label>
                            <input type="number" step="any" name="office_comm_input" id="saleOfficeCommInput" class="form-control form-control-sm bg-secondary text-white fw-bold" value="0" oninput="calcSaleAmounts()">
                        </div>
                        <div class="col-12">
                            <label class="form-label text-warning" style="font-size:11px;">إجمالي عمولة المكتب / الصفقة:</label>
                            <input type="number" step="any" name="total_commission" id="saleTotComm" class="form-control form-control-sm bg-dark text-warning fw-bold" value="0" readonly>
                        </div>
                    </div>

                    <div class="row bg-dark p-2 rounded border border-success mb-2">
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-success fw-bold" style="font-size:11px;">طريقة عمولة الموظف:</label>
                            <select name="emp_comm_type" id="saleEmpCommType" class="form-control form-control-sm bg-secondary text-white fw-bold" onchange="calcSaleAmounts()">
                                <option value="percent">نسبة من العمولة (%)</option>
                                <option value="amount">مبلغ مقطوع (ج.م)</option>
                            </select>
                        </div>
                        <div class="col-md-6 mb-1">
                            <label class="form-label text-success fw-bold" style="font-size:11px;">قيمة/نسبة الموظف:</label>
                            <input type="number" step="any" name="emp_comm_input" id="saleEmpCommInput" class="form-control form-control-sm bg-secondary text-white fw-bold" value="20" oninput="calcSaleAmounts()">
                        </div>
                    </div>

                    <div class="row mb-2">
                        <div class="col-md-6"><label class="form-label text-success" style="font-size:11px;">عمولة الموظف:</label><input type="number" step="any" name="employee_commission" id="saleEmpComm" class="form-control form-control-sm bg-secondary text-white" value="0" readonly></div>
                        <div class="col-md-6"><label class="form-label text-warning" style="font-size:11px;">صافي المكتب:</label><input type="number" step="any" name="office_commission" id="saleOfficeComm" class="form-control form-control-sm bg-secondary text-white fw-bold" value="0" readonly></div>
                    </div>

                    <div class="mb-2"><label class="form-label">الموظف المسؤول:</label><select name="employee_name" class="form-control bg-dark text-white">{e_opts}</select></div>
                    <div class="mb-2"><label class="form-label">التاريخ:</label><input type="date" name="op_date" class="form-control bg-dark text-white" value="{today}" required></div>
                    <div class="mb-2"><label class="form-label">ملاحظات:</label><input type="text" name="notes" class="form-control bg-dark text-white" placeholder="ملاحظات الصفقة"></div>
                    <div class="mb-3"><label class="form-label">مستندات الصفقة (عدة صور):</label><input type="file" name="sale_images" class="form-control bg-dark text-white" accept="image/*,.pdf" multiple></div>
                    <button type="submit" class="btn btn-success w-100 fw-bold">حفظ الصفقة والتأكيد</button>
                </form>
            </div>
        </div>
    </div>

    <!-- Modal تعديل الصفقة الشامل مع بنود العمولات -->
    <div class="modal fade" id="editSaleModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات الصفقة والمرفقات والعمولات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_sale" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="sale_id" id="es_id">
                <div class="mb-2"><label class="form-label">اسم العميل:</label><input type="text" name="client_name" id="es_cname" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">عنوان العميل:</label><input type="text" name="client_address" id="es_caddr" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">رقم البطاقة:</label><input type="text" name="client_national_id" id="es_cnid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">المبلغ الإجمالي:</label><input type="number" step="any" name="total_price" id="es_tot" class="form-control form-control-sm bg-secondary text-white" oninput="calcEditSaleAmounts()"></div>
                    <div class="col-md-6"><label class="form-label">المدفوع:</label><input type="number" step="any" name="paid_price" id="es_paid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>

                <div class="row bg-secondary p-2 rounded border border-warning mb-2">
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-warning fw-bold" style="font-size:11px;">طريقة عمولة المكتب:</label>
                        <select name="office_comm_type" id="esOfficeCommType" class="form-control form-control-sm bg-dark text-white fw-bold" onchange="calcEditSaleAmounts()">
                            <option value="amount">مبلغ مقطوع (ج.م)</option>
                            <option value="percent">نسبة من الإجمالي (%)</option>
                        </select>
                    </div>
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-warning fw-bold" style="font-size:11px;">قيمة/نسبة المكتب:</label>
                        <input type="number" step="any" name="office_comm_input" id="esOfficeCommInput" class="form-control form-control-sm bg-dark text-white fw-bold" value="0" oninput="calcEditSaleAmounts()">
                    </div>
                    <div class="col-12">
                        <label class="form-label text-warning" style="font-size:11px;">إجمالي العمولة:</label>
                        <input type="number" step="any" name="total_commission" id="es_tot_comm" class="form-control form-control-sm bg-dark text-warning fw-bold" value="0" readonly>
                    </div>
                </div>

                <div class="row bg-secondary p-2 rounded border border-success mb-2">
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-success fw-bold" style="font-size:11px;">طريقة عمولة الموظف:</label>
                        <select name="emp_comm_type" id="esEmpCommType" class="form-control form-control-sm bg-dark text-white fw-bold" onchange="calcEditSaleAmounts()">
                            <option value="percent">نسبة من العمولة (%)</option>
                            <option value="amount">مبلغ مقطوع (ج.م)</option>
                        </select>
                    </div>
                    <div class="col-md-6 mb-1">
                        <label class="form-label text-success fw-bold" style="font-size:11px;">قيمة/نسبة الموظف:</label>
                        <input type="number" step="any" name="emp_comm_input" id="esEmpCommInput" class="form-control form-control-sm bg-dark text-white fw-bold" value="20" oninput="calcEditSaleAmounts()">
                    </div>
                </div>

                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label text-success">عمولة الموظف:</label><input type="number" step="any" name="employee_commission" id="es_emp_comm" class="form-control form-control-sm bg-secondary text-white" readonly></div>
                    <div class="col-md-6"><label class="form-label text-warning">صافي المكتب:</label><input type="number" step="any" id="es_office_comm_net" class="form-control form-control-sm bg-secondary text-white fw-bold" readonly></div>
                </div>

                <div class="mb-2"><label class="form-label text-warning">الموظف المسؤول:</label><select name="employee_name" id="es_emp_name" class="form-control form-control-sm bg-secondary text-white">{e_opts}</select></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">حالة الصفقة:</label><select name="status" id="es_status" class="form-control form-control-sm bg-secondary text-white"><option value="نشطة">نشطة</option><option value="متراجع عنها">متراجع عنها (ملغاة)</option></select></div>
                    <div class="col-md-6"><label class="form-label">تاريخ الصفقة:</label><input type="date" name="op_date" id="es_date" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="mb-2"><label class="form-label">ملاحظات:</label><input type="text" name="notes" id="es_notes" class="form-control form-control-sm bg-secondary text-white"></div>
                <div class="mb-2"><label class="form-label text-warning">تعديل/إضافة صور مستندات جديدة (عدة صور):</label><input type="file" name="sale_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*,.pdf" multiple></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """ + """
    <script>
    function calcSaleAmounts() {
        let tp = parseFloat(document.getElementById('tp').value || 0);
        let pp = parseFloat(document.getElementById('pp').value || 0);
        document.getElementById('trem').value = Math.max(0, tp - pp).toFixed(1);

        let oType = document.getElementById('saleOfficeCommType').value;
        let oVal = parseFloat(document.getElementById('saleOfficeCommInput').value || 0);
        let totComm = (oType === 'percent') ? (tp * oVal / 100.0) : oVal;
        document.getElementById('saleTotComm').value = totComm.toFixed(2);

        let empType = document.getElementById('saleEmpCommType').value;
        let empVal = parseFloat(document.getElementById('saleEmpCommInput').value || 0);
        let empComm = (empType === 'percent') ? (totComm * empVal / 100.0) : empVal;
        let officeComm = Math.max(0, totComm - empComm);

        document.getElementById('saleEmpComm').value = empComm.toFixed(2);
        document.getElementById('saleOfficeComm').value = officeComm.toFixed(2);
    }
    function calcEditSaleAmounts() {
        let tp = parseFloat(document.getElementById('es_tot').value || 0);
        let oType = document.getElementById('esOfficeCommType').value;
        let oVal = parseFloat(document.getElementById('esOfficeCommInput').value || 0);
        let totComm = (oType === 'percent') ? (tp * oVal / 100.0) : oVal;
        document.getElementById('es_tot_comm').value = totComm.toFixed(2);

        let empType = document.getElementById('esEmpCommType').value;
        let empVal = parseFloat(document.getElementById('esEmpCommInput').value || 0);
        let empComm = (empType === 'percent') ? (totComm * empVal / 100.0) : empVal;
        let officeComm = Math.max(0, totComm - empComm);

        document.getElementById('es_emp_comm').value = empComm.toFixed(2);
        document.getElementById('es_office_comm_net').value = officeComm.toFixed(2);
    }
    function openEditSale(id, cname, caddr, cnid, tot, paid, totcomm, empcomm, empname, date, status, notes) {
        document.getElementById('es_id').value = id;
        document.getElementById('es_cname').value = cname;
        document.getElementById('es_caddr').value = caddr;
        document.getElementById('es_cnid').value = cnid;
        document.getElementById('es_tot').value = tot;
        document.getElementById('es_paid').value = paid;
        document.getElementById('es_tot_comm').value = totcomm;
        document.getElementById('es_emp_comm').value = empcomm;
        document.getElementById('esOfficeCommInput').value = totcomm;
        document.getElementById('es_emp_name').value = empname;
        document.getElementById('es_date').value = date;
        document.getElementById('es_status').value = status;
        document.getElementById('es_notes').value = notes;
        calcEditSaleAmounts();
        new bootstrap.Modal(document.getElementById('editSaleModal')).show();
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/add_sale', methods=['POST'])
def add_sale():
    if 'user' not in session: return redirect(url_for('login'))
    prop_name = request.form.get('property_name')
    op_type = request.form.get('operation_type')
    
    conn = get_db_connection()
    # قفل الكتابة قبل الفحص لمنع عمليتي بيع متزامنتين من تجاوز التحقق.
    conn.execute('BEGIN IMMEDIATE')
    if op_type == "بيع عقار":
        existing = active_sale_conflict(conn, prop_name)
        if existing:
            conn.rollback()
            conn.close()
            return sale_conflict_response(existing)

    t = float(request.form.get('total_price') or 0)
    p = float(request.form.get('paid_price') or 0)
    trem = max(0.0, t - p)
    
    tot_comm = float(request.form.get('total_commission') or 0)
    emp_comm_type = request.form.get('emp_comm_type', 'percent')
    emp_comm_input = float(request.form.get('emp_comm_input') or 0)
    emp_comm = (tot_comm * emp_comm_input / 100.0) if emp_comm_type == 'percent' else emp_comm_input
    office_comm = max(0.0, tot_comm - emp_comm)

    snap_b64 = request.form.get('screen_snapshot', '')
    notes = request.form.get('notes', '')
    uploaded_files = save_files(request.files.getlist('sale_images'))
    
    cur = conn.execute("""INSERT INTO sales (
        operation_type, property_name, client_name, client_address, client_national_id, 
        total_price, paid_price, remaining_price, total_commission, office_commission, employee_commission, 
        employee_name, op_date, document_image, created_at, status, notes
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                 (op_type, prop_name, request.form.get('client_name'), request.form.get('client_address'), request.form.get('client_national_id'), 
                  t, p, trem, tot_comm, office_comm, emp_comm, request.form.get('employee_name'), request.form.get('op_date'), uploaded_files, get_today_date(), 'نشطة', notes))
    sale_id = cur.lastrowid
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تسجيل صفقة " + str(op_type) + ": " + str(request.form.get('client_name')), snap_b64, category="البيع والشراء")
    if p > 0:
        return redirect(url_for('treasury_entry', source_type='sale_payment', source_id=sale_id))
    return redirect(url_for('sales_page'))

@app.route('/edit_sale', methods=['POST'])
def edit_sale():
    if 'user' not in session: return redirect(url_for('login'))
    s_id = request.form.get('sale_id')
    snap_b64 = request.form.get('screen_snapshot', '')
    t = float(request.form.get('total_price') or 0)
    p = float(request.form.get('paid_price') or 0)
    trem = max(0.0, t - p)
    
    tot_comm = float(request.form.get('total_commission') or 0)
    emp_comm = float(request.form.get('employee_commission') or 0)
    office_commission = max(0.0, tot_comm - emp_comm)
    emp_name = request.form.get('employee_name', '')
    status = request.form.get('status', 'نشطة')
    notes = request.form.get('notes', '')
    
    new_files = save_files(request.files.getlist('sale_images'))
    
    conn = get_db_connection()
    conn.execute('BEGIN IMMEDIATE')
    old_sale = conn.execute("SELECT operation_type, property_name, paid_price, status, client_name FROM sales WHERE id=?", (s_id,)).fetchone()
    if not old_sale:
        conn.rollback()
        conn.close()
        return "<script>alert('الصفقة المطلوب تعديلها غير موجودة.'); window.location.href='/sales';</script>"
    # عند إعادة تفعيل صفقة بيع أو تعديلها، افحص وجود صفقة نشطة أخرى للعقار نفسه.
    requested_status = request.form.get('status', 'نشطة')
    if old_sale['operation_type'] == 'بيع عقار' and requested_status == 'نشطة':
        conflict = active_sale_conflict(conn, old_sale['property_name'], exclude_id=int(s_id))
        if conflict:
            conn.rollback()
            conn.close()
            return sale_conflict_response(conflict)
    if new_files:
        conn.execute("UPDATE sales SET client_name=?, client_address=?, client_national_id=?, total_price=?, paid_price=?, remaining_price=?, total_commission=?, office_commission=?, employee_commission=?, employee_name=?, op_date=?, status=?, notes=?, document_image=? WHERE id=?",
                     (request.form.get('client_name'), request.form.get('client_address'), request.form.get('client_national_id'), t, p, trem, tot_comm, office_commission, emp_comm, emp_name, request.form.get('op_date'), status, notes, new_files, s_id))
    else:
        conn.execute("UPDATE sales SET client_name=?, client_address=?, client_national_id=?, total_price=?, paid_price=?, remaining_price=?, total_commission=?, office_commission=?, employee_commission=?, employee_name=?, op_date=?, status=?, notes=? WHERE id=?",
                     (request.form.get('client_name'), request.form.get('client_address'), request.form.get('client_national_id'), t, p, trem, tot_comm, office_commission, emp_comm, emp_name, request.form.get('op_date'), status, notes, s_id))
    def signed_sale_cash(op_type_value, paid_value, status_value):
        if status_value != 'نشطة':
            return 0.0
        return float(paid_value or 0) if op_type_value == 'بيع عقار' else -float(paid_value or 0)
    old_signed = signed_sale_cash(old_sale['operation_type'], old_sale['paid_price'], old_sale['status']) if old_sale else 0.0
    new_signed = signed_sale_cash(old_sale['operation_type'], p, status) if old_sale else 0.0
    delta = new_signed - old_signed
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تعديل صفقة البيع/الشراء #" + str(s_id), category="البيع والشراء")
    if abs(delta) > 0.000001:
        return redirect(url_for('treasury_entry', source_type='sale_payment_adjustment', source_id=int(s_id), amount=abs(delta), direction=('قبض' if delta > 0 else 'صرف')))
    return redirect(url_for('sales_page'))

@app.route('/delete_sale/<int:id>')
def delete_sale(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    linked = conn.execute("SELECT COUNT(*) FROM office_finances WHERE source_id=? AND source_type IN ('sale_payment','sale_payment_adjustment') AND status != 'ملغاة'", (id,)).fetchone()[0]
    if linked:
        conn.close()
        return "<script>alert('لا يمكن حذف هذه الصفقة لارتباطها بحركات خزينة. راجع الحركات المالية أولاً.'); window.location.href='/sales';</script>"
    conn.execute("DELETE FROM sales WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "حذف صفقة #" + str(id), category="البيع والشراء")
    return redirect(url_for('sales_page'))

# ==================== قسم الموظفين وشؤون العاملين ====================
@app.route('/employees')
def employees_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    employees = conn.execute("SELECT * FROM employees").fetchall()
    conn.close()
    
    e_rows = []
    for e in employees:
        e_img = render_file_badges(e['image'])
        e_addr = e['address'] if 'address' in e.keys() else ""
        e_nid = e['national_id'] if 'national_id' in e.keys() else ""
        e_pass = e['password'] if 'password' in e.keys() else ""
        ded = e['deductions'] if 'deductions' in e.keys() else 0
        adv = e['advances'] if 'advances' in e.keys() else 0
        tax = e['taxes'] if 'taxes' in e.keys() else 0
        
        name_link = '<a href="/employee_profile/' + str(e['id']) + '" class="text-warning fw-bold text-decoration-none fs-6">' + str(e['name']) + '</a>'
        profile_btn = '<a href="/employee_profile/' + str(e['id']) + '" class="btn btn-info btn-sm text-dark fw-bold">صفحة المعاملات والملف</a>'
        edit_btn = '<button class="btn btn-warning btn-sm" onclick="openEditEmployee(' + str(e['id']) + ', \'' + str(e['name']) + '\', \'' + str(e['job']) + '\', \'' + str(e_pass) + '\', ' + str(e['salary']) + ', ' + str(ded) + ', ' + str(adv) + ', ' + str(tax) + ', \'' + str(e_addr) + '\', \'' + str(e_nid) + '\')">تعديل والمرفقات</button>'
        e_rows.append("<tr><td>" + e_img + "</td><td>" + name_link + "<br><small>" + str(e_addr) + "</small></td><td>" + str(e['job']) + "</td><td>" + str(e['salary']) + " ج.م</td><td><span class='text-danger'>جزاءات: " + str(ded) + "<br>سلف: " + str(adv) + "<br>ضرائب: " + str(tax) + "</span></td><td>" + profile_btn + "</td><td>" + edit_btn + " <a href='/delete_employee/" + str(e['id']) + "' class='btn btn-danger btn-sm' onclick='return confirm(\"حذف الموظف؟\")'>حذف</a></td></tr>")
    
    content = f"""
    <div class="row">
        <div class="col-xl-8 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">إدارة الموظفين وكشوف الحسابات والاستقطاعات (اضغط على اسم الموظف لفتح صفحته الخاصة)</h5>
                <div class="mb-3"><a href="/all_employees_financial" class="btn btn-warning fw-bold btn-sm">📋 عرض تقرير مستحقات كافة العاملين الشامل</a></div>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 12px;">
                        <thead><tr><th>صورة</th><th>الموظف والتفاصيل</th><th>الوظيفة</th><th>الراتب</th><th>الخصومات (جزاءات/سلف/ضرائب)</th><th>صفحة الموظف</th><th>إجراءات</th></tr></thead>
                        <tbody>{''.join(e_rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-xl-4 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">إضافة موظف جديد والمستندات</h5>
                <form action="/add_employee" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">اسم الموظف:</label><input type="text" name="name" class="form-control bg-dark text-white" required></div>
                    <div class="mb-2"><label class="form-label">الوظيفة:</label><input type="text" name="job" class="form-control bg-dark text-white" required></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">العنوان:</label><input type="text" name="address" class="form-control bg-dark text-white"></div>
                        <div class="col-md-6 mb-2"><label class="form-label">الرقم القومي:</label><input type="text" name="national_id" class="form-control bg-dark text-white"></div>
                    </div>
                    <div class="mb-2"><label class="form-label text-warning">كلمة المرور:</label><input type="text" name="password" class="form-control bg-dark text-white" value="1" required></div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label">الراتب الأساسي:</label><input type="number" step="any" name="salary" class="form-control bg-dark text-white" value="0"></div>
                        <div class="col-md-6 mb-2"><label class="form-label text-danger">الجزاءات:</label><input type="number" step="any" name="deductions" class="form-control bg-dark text-white" value="0"></div>
                    </div>
                    <div class="row">
                        <div class="col-md-6 mb-2"><label class="form-label text-warning">السلف:</label><input type="number" step="any" name="advances" class="form-control bg-dark text-white" value="0"></div>
                        <div class="col-md-6 mb-2"><label class="form-label text-info">الضرائب:</label><input type="number" step="any" name="taxes" class="form-control bg-dark text-white" value="0"></div>
                    </div>
                    <div class="mb-3"><label class="form-label">صور الموظف / المستندات:</label><input type="file" name="emp_images" class="form-control bg-dark text-white" accept="image/*" multiple></div>
                    <button type="submit" class="btn btn-success w-100 fw-bold">حفظ الموظف الجديد</button>
                </form>
            </div>
        </div>
    </div>

    <!-- Modal تعديل الموظف مع الحقل المطابق employee_id -->
    <div class="modal fade" id="editEmployeeModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات الموظف وكلمة المرور والمرفقات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_employee" method="POST" enctype="multipart/form-data">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="employee_id" id="ee_id">
                <div class="mb-2"><label class="form-label">اسم الموظف:</label><input type="text" name="name" id="ee_name" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">الوظيفة:</label><input type="text" name="job" id="ee_job" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label text-warning fw-bold">كلمة المرور:</label><input type="text" name="password" id="ee_password" class="form-control form-control-sm bg-secondary text-white fw-bold" required></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">العنوان:</label><input type="text" name="address" id="ee_addr" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">الرقم القومي:</label><input type="text" name="national_id" id="ee_nid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">الراتب:</label><input type="number" step="any" name="salary" id="ee_salary" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label text-danger">الجزاءات:</label><input type="number" step="any" name="deductions" id="ee_deductions" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label text-warning">السلف:</label><input type="number" step="any" name="advances" id="ee_advances" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label text-info">الضرائب:</label><input type="number" step="any" name="taxes" id="ee_taxes" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="mb-2"><label class="form-label text-warning">تعديل/إضافة صور جديدة (عدة صور):</label><input type="file" name="emp_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*" multiple></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """ + """
    <script>
    function openEditEmployee(id, name, job, pass, salary, deductions, advances, taxes, addr, nid) {
        document.getElementById('ee_id').value = id;
        document.getElementById('ee_name').value = name;
        document.getElementById('ee_job').value = job;
        document.getElementById('ee_password').value = pass;
        document.getElementById('ee_salary').value = salary;
        document.getElementById('ee_deductions').value = deductions;
        document.getElementById('ee_advances').value = advances;
        document.getElementById('ee_taxes').value = taxes;
        document.getElementById('ee_addr').value = addr;
        document.getElementById('ee_nid').value = nid;
        new bootstrap.Modal(document.getElementById('editEmployeeModal')).show();
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/edit_employee', methods=['POST'])
def edit_employee():
    if 'user' not in session: return redirect(url_for('login'))
    e_id = request.form.get('employee_id')
    name = request.form.get('name')
    job = request.form.get('job')
    password = request.form.get('password')
    salary = float(request.form.get('salary') or 0)
    deductions = float(request.form.get('deductions') or 0)
    advances = float(request.form.get('advances') or 0)
    taxes = float(request.form.get('taxes') or 0)
    address = request.form.get('address', '')
    national_id = request.form.get('national_id', '')
    
    new_files = save_files(request.files.getlist('emp_images'))
    
    conn = get_db_connection()
    if new_files:
        conn.execute("UPDATE employees SET name = ?, job = ?, password = ?, salary = ?, deductions = ?, advances = ?, taxes = ?, address = ?, national_id = ?, image = ? WHERE id = ?",
                     (name, job, password, salary, deductions, advances, taxes, address, national_id, new_files, e_id))
    else:
        conn.execute("UPDATE employees SET name = ?, job = ?, password = ?, salary = ?, deductions = ?, advances = ?, taxes = ?, address = ?, national_id = ? WHERE id = ?",
                     (name, job, password, salary, deductions, advances, taxes, address, national_id, e_id))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تعديل بيانات الموظف #" + str(e_id), category="الموظفين والصلاحيات")
    return redirect(url_for('employees_page'))

@app.route('/delete_employee/<int:id>')
def delete_employee(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    conn.execute("DELETE FROM employees WHERE id = ?", (id,))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "حذف الموظف رقم #" + str(id), category="الموظفين والصلاحيات")
    return redirect(url_for('employees_page'))

@app.route('/employee_profile/<int:emp_id>')
def employee_profile(emp_id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    emp = conn.execute("SELECT * FROM employees WHERE id = ?", (emp_id,)).fetchone()
    if not emp:
        conn.close()
        return "الموظف غير موجود"
    
    emp_name = emp['name']
    cur_m = datetime.now().strftime("%Y-%m")
    
    con_comms = conn.execute("SELECT 'عقد إيجار - عقار: ' || property_name AS descr, employee_commission AS amt, start_date AS dt FROM contracts WHERE employee_name = ? AND start_date LIKE ?", (emp_name, cur_m + "%")).fetchall()
    sal_comms = conn.execute("SELECT 'صفقة ' || operation_type || ' - عقار: ' || property_name AS descr, employee_commission AS amt, op_date AS dt FROM sales WHERE employee_name = ? AND op_date LIKE ? AND status = 'نشطة'", (emp_name, cur_m + "%")).fetchall()
    
    total_comm = sum([c['amt'] for c in con_comms if c['amt'] is not None]) + sum([s['amt'] for s in sal_comms if s['amt'] is not None])
    total_deductions = (emp['deductions'] or 0) + (emp['advances'] or 0) + (emp['taxes'] or 0)
    net_due = (emp['salary'] or 0) + total_comm - total_deductions
    
    logs = conn.execute("SELECT * FROM activity_logs WHERE employee_name = ? ORDER BY id DESC", (emp_name,)).fetchall()
    conn.close()
    
    e_img_badges = render_file_badges(emp['image'])
    income_rows = "".join(["<tr><td>" + str(item['dt']) + "</td><td>" + str(item['descr']) + "</td><td class='text-warning'>" + str(item['amt']) + " ج.م</td></tr>" for item in list(con_comms) + list(sal_comms)])
    if not income_rows: income_rows = "<tr><td colspan='3' class='text-muted'>لا توجد عمولات مسجلة هذا الشهر</td></tr>"

    e_id = str(emp['id'])
    e_sal = str(emp['salary'] or 0)
    e_ded = str(emp['deductions'] or 0)
    e_adv = str(emp['advances'] or 0)
    e_tax = str(emp['taxes'] or 0)
    e_addr = str(emp['address'] or '')
    e_nid = str(emp['national_id'] or '')
    e_pass = str(emp['password'] or '')

    html_content = f"""
    <div class="row mb-3">
        <div class="col-12">
            <a href="/employees" class="btn btn-secondary btn-sm mb-3">← العودة لقائمة الموظفين</a>
            <div class="card card-custom p-4">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <h4 class="text-warning fw-bold m-0">👤 صفحة المعاملات والملف المالي للموظف: {emp_name}</h4>
                    <button class="btn btn-warning fw-bold btn-sm" onclick="openEditEmployee({e_id}, '{emp_name}', '{emp['job']}', '{e_pass}', {e_sal}, {e_ded}, {e_adv}, {e_tax}, '{e_addr}', '{e_nid}')">✏️ تعديل بيانات الموظف والمرفقات</button>
                </div>

                <div class="row mb-4">
                    <div class="col-md-4">
                        <div class="p-3 bg-dark rounded border border-secondary" style="font-size: 13px;">
                            <b>📂 المستندات والصور الشخصية:</b><br><div class="mt-2">{e_img_badges}</div>
                        </div>
                    </div>
                    <div class="col-md-8">
                        <div class="p-3 bg-dark rounded border border-secondary" style="font-size: 13px;">
                            <b>📋 بيانات الموظف الشخصية والوظيفة:</b><br><br>
                            • الوظيفة: {emp['job']}<br>
                            • العنوان: {e_addr if e_addr else 'غير مسجل'}<br>
                            • الرقم القومي: {e_nid if e_nid else 'غير مسجل'}<br>
                            • حالة الحساب: <span class="badge bg-success">{emp['status']}</span>
                        </div>
                    </div>
                </div>

                <div class="row text-center mb-4">
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-muted">الراتب الأساسي</h6><h5 class="text-white">{e_sal} ج.م</h5></div></div>
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-warning">إجمالي العمولات</h6><h5 class="text-warning">{total_comm} ج.م</h5></div></div>
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-danger">الجزاءات</h6><h5 class="text-danger">{e_ded} ج.م</h5></div></div>
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-warning">السلف</h6><h5 class="text-warning">{e_adv} ج.م</h5></div></div>
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-info">الضرائب</h6><h5 class="text-info">{e_tax} ج.م</h5></div></div>
                    <div class="col-md-2 mb-2"><div class="p-3 border rounded bg-dark border-success"><h6 class="text-success fw-bold">الصافي المستحق</h6><h5 class="text-success fw-bold">{net_due} ج.م</h5></div></div>
                </div>

                <h5 class="text-success mb-2">تفاصيل الدخل والعمولات المرحلة (عن شهر {cur_m})</h5>
                <div class="table-responsive mb-4">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 12px;">
                        <thead><tr><th>التاريخ</th><th>بيان الدخل</th><th>المبلغ</th></tr></thead>
                        <tbody>{income_rows}</tbody>
                    </table>
                </div>

                <h5 class="text-info mb-2">سجل نشاطات الموظف</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>التاريخ والوقت</th><th>التفاصيل</th><th>القسم</th></tr></thead>
                        <tbody>{''.join(["<tr><td>" + str(l['action_time']) + "</td><td>" + str(l['details']) + "</td><td><span class='badge bg-secondary'>" + str(l['category'] if 'category' in l.keys() else 'عام') + "</span></td></tr>" for l in logs])}</tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>

    <!-- Modal تعديل الموظف من صفحته الخاصة -->
    <div class="modal fade" id="editEmployeeModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات الموظف وكلمة المرور والمرفقات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_employee" method="POST" enctype="multipart/form-data">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="employee_id" id="ee_id">
                <div class="mb-2"><label class="form-label">اسم الموظف:</label><input type="text" name="name" id="ee_name" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">الوظيفة:</label><input type="text" name="job" id="ee_job" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label text-warning fw-bold">كلمة المرور:</label><input type="text" name="password" id="ee_password" class="form-control form-control-sm bg-secondary text-white fw-bold" required></div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">العنوان:</label><input type="text" name="address" id="ee_addr" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label">الرقم القومي:</label><input type="text" name="national_id" id="ee_nid" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label">الراتب:</label><input type="number" step="any" name="salary" id="ee_salary" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label text-danger">الجزاءات:</label><input type="number" step="any" name="deductions" id="ee_deductions" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="row mb-2">
                    <div class="col-md-6"><label class="form-label text-warning">السلف:</label><input type="number" step="any" name="advances" id="ee_advances" class="form-control form-control-sm bg-secondary text-white"></div>
                    <div class="col-md-6"><label class="form-label text-info">الضرائب:</label><input type="number" step="any" name="taxes" id="ee_taxes" class="form-control form-control-sm bg-secondary text-white"></div>
                </div>
                <div class="mb-2"><label class="form-label text-warning">تعديل/إضافة صور جديدة (عدة صور):</label><input type="file" name="emp_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*" multiple></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """
    
    js_script = """
    <script>
    function openEditEmployee(id, name, job, pass, salary, deductions, advances, taxes, addr, nid) {
        document.getElementById('ee_id').value = id;
        document.getElementById('ee_name').value = name;
        document.getElementById('ee_job').value = job;
        document.getElementById('ee_password').value = pass;
        document.getElementById('ee_salary').value = salary;
        document.getElementById('ee_deductions').value = deductions;
        document.getElementById('ee_advances').value = advances;
        document.getElementById('ee_taxes').value = taxes;
        document.getElementById('ee_addr').value = addr;
        document.getElementById('ee_nid').value = nid;
        new bootstrap.Modal(document.getElementById('editEmployeeModal')).show();
    }
    </script>
    """
    return render_page(html_content + js_script, session.get('user'))

@app.route('/all_employees_financial')
def all_employees_financial():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    employees = conn.execute("SELECT * FROM employees WHERE status = 'نشط'").fetchall()
    cur_m = datetime.now().strftime("%Y-%m")
    
    rows = []
    tot_salaries = 0
    tot_commissions = 0
    tot_deductions = 0
    tot_net = 0
    
    for e in employees:
        ename = e['name']
        c_comm = conn.execute("SELECT SUM(employee_commission) FROM contracts WHERE employee_name = ? AND start_date LIKE ?", (ename, cur_m + "%")).fetchone()[0] or 0
        s_comm = conn.execute("SELECT SUM(employee_commission) FROM sales WHERE employee_name = ? AND op_date LIKE ? AND status = 'نشطة'", (ename, cur_m + "%")).fetchone()[0] or 0
        e_comm = c_comm + s_comm
        total_ded = (e['deductions'] or 0) + (e['advances'] or 0) + (e['taxes'] or 0)
        net = (e['salary'] or 0) + e_comm - total_ded
        
        tot_salaries += (e['salary'] or 0)
        tot_commissions += e_comm
        tot_deductions += total_ded
        tot_net += net
        
        name_link = '<a href="/employee_profile/' + str(e['id']) + '" class="text-warning fw-bold text-decoration-none">' + str(ename) + '</a>'
        rows.append("<tr><td>" + name_link + "</td><td>" + str(e['job']) + "</td><td>" + str(e['salary']) + " ج.م</td><td>" + str(e_comm) + " ج.م</td><td>جزاء: " + str(e['deductions']) + " | سلف: " + str(e['advances']) + " | ضريبة: " + str(e['taxes']) + "</td><td class='text-success fw-bold'>" + str(net) + " ج.م</td></tr>")
    
    conn.close()

    content = f"""
    <div class="row">
        <div class="col-12">
            <a href="/employees" class="btn btn-secondary btn-sm mb-3">← العودة للموظفين</a>
            <div class="card card-custom p-4">
                <h4 class="text-warning fw-bold mb-4">📋 التقرير المالي الشامل لمستحقات كافة العاملين (عن شهر {cur_m})</h4>
                <div class="row text-center mb-4">
                    <div class="col-md-3 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-muted">إجمالي الرواتب الأساسية</h6><h4 class="text-white">{tot_salaries} ج.م</h4></div></div>
                    <div class="col-md-3 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-warning">إجمالي العمولات المستحقة</h6><h4 class="text-warning">{tot_commissions} ج.م</h4></div></div>
                    <div class="col-md-3 mb-2"><div class="p-3 border rounded bg-dark"><h6 class="text-danger">إجمالي الاستقطاعات</h6><h4 class="text-danger">{tot_deductions} ج.م</h4></div></div>
                    <div class="col-md-3 mb-2"><div class="p-3 border rounded bg-dark border-success"><h6 class="text-success fw-bold">إجمالي الصافي المستحق</h6><h4 class="text-success fw-bold">{tot_net} ج.م</h4></div></div>
                </div>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 12px;">
                        <thead><tr><th>الموظف</th><th>الوظيفة</th><th>الراتب الأساسي</th><th>إجمالي العمولات</th><th>الاستقطاعات</th><th>الصافي المستحق</th></tr></thead>
                        <tbody>{''.join(rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    """
    return render_page(content, session.get('user'))

@app.route('/add_employee', methods=['POST'])
def add_employee():
    if 'user' not in session: return redirect(url_for('login'))
    emp_name = request.form.get('name', '').strip()
    job = request.form.get('job', '').strip()
    pwd = request.form.get('password', '1')
    sal = float(request.form.get('salary') or 0)
    ded = float(request.form.get('deductions') or 0)
    adv = float(request.form.get('advances') or 0)
    tax = float(request.form.get('taxes') or 0)
    addr = request.form.get('address', '')
    nid = request.form.get('national_id', '')
    snap_b64 = request.form.get('screen_snapshot', '')
    uploaded_files = save_files(request.files.getlist('emp_images'))

    conn = get_db_connection()
    existing = conn.execute("SELECT * FROM employees WHERE name = ?", (emp_name,)).fetchone()

    if existing:
        new_ded = (existing['deductions'] or 0) + ded
        new_adv = (existing['advances'] or 0) + adv
        new_tax = (existing['taxes'] or 0) + tax
        new_sal = sal if sal > 0 else existing['salary']

        if uploaded_files:
            cur_imgs = existing['image'] + "," + uploaded_files if existing['image'] else uploaded_files
            conn.execute("UPDATE employees SET salary=?, deductions=?, advances=?, taxes=?, image=? WHERE id=?", 
                         (new_sal, new_ded, new_adv, new_tax, cur_imgs, existing['id']))
        else:
            conn.execute("UPDATE employees SET salary=?, deductions=?, advances=?, taxes=? WHERE id=?", 
                         (new_sal, new_ded, new_adv, new_tax, existing['id']))
        is_update = True
    else:
        emp_cur = conn.execute("INSERT INTO employees (name, job, password, salary, deductions, advances, taxes, image, address, national_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", 
             (emp_name, job, pwd, sal, ded, adv, tax, uploaded_files, addr, nid))
        existing_id = emp_cur.lastrowid
        is_update = False

    target_emp_id = existing['id'] if existing else existing_id
    conn.commit()
    conn.close()

    if is_update:
        log_act(session.get('user'), "إضافة معاملات مالية/سلفة للموظف الحالي: " + emp_name, snap_b64, category="الموظفين والصلاحيات")
    else:
        log_act(session.get('user'), "إضافة موظف جديد: " + emp_name, snap_b64, category="الموظفين والصلاحيات")

    if adv > 0:
        return redirect(url_for('treasury_entry', source_type='employee_advance', source_id=target_emp_id, amount=adv, direction='صرف'))
    return redirect(url_for('employees_page'))

# ==================== الخزينة الموحدة للمكتب ====================
@app.route('/office_finances')
def office_finances_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    finances = conn.execute("SELECT * FROM office_finances ORDER BY id DESC").fetchall()
    totals = conn.execute("""
        SELECT
          COALESCE(SUM(CASE WHEN fin_type IN ('إيراد عابر','قبض') AND status != 'ملغاة' THEN amount ELSE 0 END),0) AS total_in,
          COALESCE(SUM(CASE WHEN fin_type IN ('مصروف عابر','صرف') AND status != 'ملغاة' THEN amount ELSE 0 END),0) AS total_out,
          COALESCE(SUM(CASE WHEN status != 'ملغاة' THEN third_party_due ELSE 0 END),0) AS third_party_total
        FROM office_finances
    """).fetchone()
    conn.close()
    total_in = float(totals['total_in'] or 0)
    total_out = float(totals['total_out'] or 0)
    balance = total_in - total_out
    third_party_total = float(totals['third_party_total'] or 0)

    rows = []
    for f in finances:
        is_in = f['fin_type'] in ("إيراد عابر", "قبض")
        badge = "<span class='badge bg-success'>قبض</span>" if is_in else "<span class='badge bg-danger'>صرف</span>"
        voucher = f['voucher_no'] if 'voucher_no' in f.keys() and f['voucher_no'] else ("خز-" + str(f['id']).zfill(6))
        snapshot = f['snapshot_file'] if 'snapshot_file' in f.keys() else ""
        snap_link = ("<a class='btn btn-info btn-sm text-dark fw-bold' target='_blank' href='/static/snapshots/" + html.escape(str(snapshot), quote=True) + "'>عرض الصورة</a>") if snapshot else "<span class='text-secondary'>لا توجد صورة</span>"
        counterparty = f['counterparty'] if 'counterparty' in f.keys() else ""
        status = f['status'] if 'status' in f.keys() else "معتمدة"
        rows.append(
            "<tr><td><b>" + html.escape(str(voucher)) + "</b></td><td>" + badge +
            "</td><td><b>" + html.escape(str(f['title'] or '')) + "</b><br><small>" + html.escape(str(counterparty or '')) +
            "</small></td><td class='fw-bold'>" + html.escape(str(f['amount'])) + " ج.م<br><small class='text-success'>للمكتب: " + html.escape(str(f['office_income'] if 'office_income' in f.keys() else 0)) + "</small><br><small class='text-info'>للغير: " + html.escape(str(f['third_party_due'] if 'third_party_due' in f.keys() else 0)) + "</small></td><td>" +
            html.escape(str(f['fin_date'] or '')) + "</td><td><small>" + html.escape(str(f['notes'] or '')) +
            "</small></td><td>" + snap_link + "</td><td>" + html.escape(str(status)) +
            "</td><td><a href='/delete_office_finance/" + str(f['id']) +
            "' class='btn btn-outline-danger btn-sm' onclick='return confirm(\"سيتم إلغاء الحركة وتسجيلها كملغاة بدل حذفها نهائيًا. متابعة؟\")'>إلغاء</a></td></tr>"
        )

    today = get_today_date()
    content = f"""
    <div class="row mb-3 text-center">
      <div class="col-md-4 mb-2"><div class="card card-custom p-3 border-success"><div class="text-success fw-bold">إجمالي المقبوضات</div><h4>{total_in:,.2f} ج.م</h4></div></div>
      <div class="col-md-4 mb-2"><div class="card card-custom p-3 border-danger"><div class="text-danger fw-bold">إجمالي المصروفات</div><h4>{total_out:,.2f} ج.م</h4></div></div>
      <div class="col-md-4 mb-2"><div class="card card-custom p-3 border-warning"><div class="text-warning fw-bold">رصيد الخزينة الحالية</div><h4>{balance:,.2f} ج.م</h4></div></div>
      <div class="col-md-4 mb-2"><div class="card card-custom p-3 border-info"><div class="text-info fw-bold">مستحقات الغير داخل الخزينة</div><h4>{third_party_total:,.2f} ج.م</h4></div></div>
    </div>
    <div class="row">
        <div class="col-xl-8 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">سجل الخزينة الموحدة — جميع المقبوضات والمصروفات</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>رقم السند</th><th>الحركة</th><th>البيان / الطرف</th><th>المبلغ</th><th>التاريخ</th><th>ملاحظات</th><th>الصورة</th><th>الحالة</th><th>إجراء</th></tr></thead>
                        <tbody>{''.join(rows)}</tbody>
                    </table>
                </div>
                <small class="text-secondary">صور الحركات تحفظ داخل مجلد البرنامج: static/snapshots/treasury</small>
            </div>
        </div>
        <div class="col-xl-4 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">تسجيل حركة خزينة جديدة</h5>
                <form action="/add_office_finance" method="POST" onsubmit="event.preventDefault(); captureRequiredScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">نوع الحركة:</label><select name="fin_type" class="form-control bg-dark text-white" required><option value="قبض">قبض — إضافة للموجود بالخزينة</option><option value="صرف">صرف — خصم من الخزينة</option></select></div>
                    <div class="mb-2"><label class="form-label">البيان / سبب الحركة:</label><input type="text" name="title" class="form-control bg-dark text-white" placeholder="مثال: تحصيل إيجار أو سداد مصروف" required></div>
                    <div class="mb-2"><label class="form-label">المبلغ (ج.م):</label><input type="number" min="0.01" step="any" name="amount" class="form-control bg-dark text-white" value="0" required></div>
                    <div class="mb-2"><label class="form-label">التاريخ:</label><input type="date" name="fin_date" class="form-control bg-dark text-white" value="{today}" required></div>
                    <div class="mb-2"><label class="form-label">المستلم / الدافع:</label><input type="text" name="counterparty" class="form-control bg-dark text-white" placeholder="اسم الشخص أو الجهة" required></div>
                    <div class="mb-2"><label class="form-label">الجزء الذي يُعد إيرادًا للمكتب:</label><input type="number" min="0" step="any" name="office_income" class="form-control bg-dark text-white" value="0" required></div>
                    <div class="mb-2"><label class="form-label">مستحقات للغير (المقبوض موجب، والسداد للغير بالسالب):</label><input type="number" min="-999999999" step="any" name="third_party_due" class="form-control bg-dark text-white" value="0" required></div>
                    <div class="mb-3"><label class="form-label">ملاحظات:</label><input type="text" name="notes" class="form-control bg-dark text-white"></div>
                    <div class="alert alert-warning py-2" style="font-size:12px">سيتم التقاط صورة للشاشة تلقائيًا عند الضغط على حفظ.</div>
                    <button type="submit" class="btn btn-success w-100 fw-bold">حفظ سند الخزينة والتقاط الصورة</button>
                </form>
            </div>
        </div>
    </div>
    """
    return render_page(content, session.get('user'))

@app.route('/treasury_entry')
def treasury_entry():
    if 'user' not in session: return redirect(url_for('login'))
    source_type = request.args.get('source_type', '')
    source_id = request.args.get('source_id', '')
    direction = request.args.get('direction', 'قبض')
    amount_arg = request.args.get('amount', '')
    title = 'حركة واردة من قسم آخر'
    counterparty = ''
    movement_date = get_today_date()
    amount = 0.0
    office_income = 0.0
    third_party_due = 0.0
    conn = get_db_connection()
    if source_type in ('contract_payment', 'contract_payment_adjustment') and source_id:
        contract = conn.execute("SELECT * FROM contracts WHERE id=?", (source_id,)).fetchone()
        if contract:
            amount = float(amount_arg or (float(contract['paid_amount'] or 0) + float(contract['deposit_paid'] or 0) + float(contract['maintenance_paid'] or 0)))
            direction = request.args.get('direction', 'قبض')
            counterparty = contract['tenant_name'] or ''
            movement_date = contract['start_date'] or get_today_date()
            title = ('تحصيلات عقد إيجار' if direction == 'قبض' else 'تسوية/رد مبالغ عقد إيجار') + ' رقم #' + str(source_id) + ' — ' + str(contract['property_name'] or '')
            if direction == 'قبض':
                office_income = min(amount, float(contract['office_commission'] or 0))
                third_party_due = max(0.0, amount - office_income)
    elif source_type == 'employee_advance' and source_id:
        employee = conn.execute("SELECT name FROM employees WHERE id=?", (source_id,)).fetchone()
        amount = float(amount_arg or 0)
        direction = 'صرف'
        counterparty = employee['name'] if employee else ''
        title = 'سلفة موظف — ' + counterparty
    elif source_type == 'legal_expense' and source_id:
        expense = conn.execute("SELECT e.expense_amount,e.expense_type,e.expense_date,e.case_id,a.case_title FROM legal_expenses e LEFT JOIN legal_affairs a ON a.id=e.case_id WHERE e.id=?", (source_id,)).fetchone()
        if expense:
            amount = float(amount_arg or expense['expense_amount'] or 0)
            direction = 'صرف'
            counterparty = 'الشؤون القانونية'
            movement_date = expense['expense_date'] or get_today_date()
            title = 'مصروف قضية #' + str(expense['case_id']) + ' — ' + str(expense['expense_type'] or '') + ' — ' + str(expense['case_title'] or '')
    if source_type in ('sale_payment', 'sale_payment_adjustment') and source_id:
        sale = conn.execute("SELECT * FROM sales WHERE id=?", (source_id,)).fetchone()
        if sale:
            property_row = conn.execute("SELECT ownership_status, owner_name FROM properties WHERE building_name=?", (sale['property_name'],)).fetchone()
            direction = request.args.get('direction', 'قبض' if sale['operation_type'] == 'بيع عقار' else 'صرف')
            amount = float(amount_arg or sale['paid_price'] or 0)
            counterparty = sale['client_name'] or ''
            movement_date = sale['op_date'] or get_today_date()
            title = ('تحصيل صفقة بيع' if direction == 'قبض' else 'سداد/تسوية صفقة شراء') + ' رقم #' + str(source_id) + ' — ' + str(sale['property_name'] or '')
            if direction == 'قبض':
                if property_row and property_row['ownership_status'] == 'ملك المكتب':
                    office_income = amount
                    third_party_due = 0.0
                else:
                    if source_type == 'sale_payment_adjustment':
                        prior = conn.execute("SELECT COALESCE(SUM(office_income),0) FROM office_finances WHERE source_id=? AND source_type IN ('sale_payment','sale_payment_adjustment') AND status!='ملغاة'", (source_id,)).fetchone()[0] or 0
                        remaining_commission = max(0.0, float(sale['office_commission'] or 0) - float(prior))
                        office_income = min(amount, remaining_commission)
                    else:
                        office_income = min(amount, float(sale['office_commission'] or 0))
                    third_party_due = max(0.0, amount - office_income)
    conn.close()
    if amount <= 0:
        return redirect(url_for('sales_page'))
    safe_title = html.escape(title, quote=True)
    safe_counterparty = html.escape(counterparty, quote=True)
    safe_date = html.escape(movement_date, quote=True)
    safe_source_type = html.escape(source_type, quote=True)
    safe_source_id = html.escape(str(source_id), quote=True)
    safe_direction = html.escape(direction, quote=True)
    content = f"""
    <div class="card card-custom p-4 mx-auto" style="max-width:760px">
      <h4 class="text-warning fw-bold mb-3">استكمال سند الخزينة المركزي</h4>
      <div class="alert alert-info">هذه الحركة لم تُعتمد بعد في الخزينة. راجع بياناتها وصنّف المبلغ قبل الحفظ. إجمالي المقبوض ليس بالضرورة إيرادًا للمكتب.</div>
      <form action="/save_treasury_entry" method="POST" onsubmit="event.preventDefault(); captureRequiredScreenAndSubmit(this);">
        <input type="hidden" name="source_type" value="{safe_source_type}"><input type="hidden" name="source_id" value="{safe_source_id}">
        <input type="hidden" name="fin_type" value="{safe_direction}">
        <div class="mb-3"><label class="form-label">البيان</label><input class="form-control bg-dark text-white" name="title" value="{safe_title}" required></div>
        <div class="row"><div class="col-md-6 mb-3"><label class="form-label">إجمالي الحركة (ج.م)</label><input type="number" class="form-control bg-dark text-white" name="amount" id="te_amount" value="{amount:.2f}" min="0.01" step="any" required readonly></div>
        <div class="col-md-6 mb-3"><label class="form-label">التاريخ</label><input type="date" class="form-control bg-dark text-white" name="fin_date" value="{safe_date}" required></div></div>
        <div class="mb-3"><label class="form-label">الطرف الذي تم التعامل معه</label><input class="form-control bg-dark text-white" name="counterparty" value="{safe_counterparty}" required></div>
        <div class="row" id="receiptSplit"><div class="col-md-6 mb-3"><label class="form-label text-success">إيراد المكتب من المبلغ</label><input type="number" class="form-control bg-dark text-white" name="office_income" id="te_income" value="{office_income:.2f}" min="0" step="any" required></div>
        <div class="col-md-6 mb-3"><label class="form-label text-info">مستحقات للغير من المبلغ</label><input type="number" class="form-control bg-dark text-white" name="third_party_due" id="te_due" value="{third_party_due:.2f}" min="0" step="any" required></div></div>
        <div class="mb-3"><label class="form-label">ملاحظات / تفاصيل السند</label><textarea class="form-control bg-dark text-white" name="notes" rows="3" placeholder="اكتب سبب القبض أو الصرف، واسم المستفيد عند وجود مستحقات للغير"></textarea></div>
        <div class="alert alert-warning">عند القبض يجب أن يساوي (إيراد المكتب + مستحقات الغير) إجمالي المقبوض. سيتم التقاط صورة للشاشة عند الحفظ.</div>
        <button class="btn btn-success w-100 fw-bold" type="submit">اعتماد سند الخزينة وحفظ الصورة</button>
        <a class="btn btn-outline-light w-100 mt-2" href="/sales">رجوع دون اعتماد</a>
      </form>
      <script>const teDir='{safe_direction}'; const incomeCol=document.getElementById('te_income').parentElement; const dueLabel=document.querySelector('label[for="te_due"]'); if(teDir!=='قبض'){{incomeCol.style.display='none'; document.getElementById('te_income').required=false;document.getElementById('te_income').value='0';document.getElementById('te_due').required=false;document.getElementById('te_due').value='0';document.getElementById('te_due').min='-999999999';document.getElementById('te_due').previousElementSibling.textContent='تسوية مستحقات للغير (اكتب المبلغ بالسالب لتخفيض الدين)';}}</script>
    </div>"""
    return render_page(content, session.get('user'))

@app.route('/save_treasury_entry', methods=['POST'])
def save_treasury_entry():
    if 'user' not in session: return redirect(url_for('login'))
    raw_type = request.form.get('fin_type', 'قبض')
    fin_type = 'إيراد عابر' if raw_type == 'قبض' else 'مصروف عابر'
    title = (request.form.get('title') or '').strip()
    counterparty = (request.form.get('counterparty') or '').strip()
    try:
        amount = float(request.form.get('amount') or 0)
        office_income = float(request.form.get('office_income') or 0)
        third_party_due = float(request.form.get('third_party_due') or 0)
    except (TypeError, ValueError):
        return "<script>alert('يرجى مراجعة المبالغ المدخلة');history.back();</script>"
    if not title or not counterparty or amount <= 0 or office_income < 0 or (fin_type == 'إيراد عابر' and third_party_due < 0) or (fin_type == 'مصروف عابر' and third_party_due > 0):
        return "<script>alert('يجب استكمال البيانات المطلوبة');history.back();</script>"
    if fin_type == 'إيراد عابر' and abs(office_income + third_party_due - amount) > 0.01:
        return "<script>alert('مجموع إيراد المكتب ومستحقات الغير يجب أن يساوي إجمالي المقبوض');history.back();</script>"
    if fin_type == 'مصروف عابر': office_income = 0.0
    source_type = request.form.get('source_type', '')
    try: source_id = int(request.form.get('source_id') or 0) or None
    except ValueError: source_id = None
    snap_b64 = request.form.get('screen_snapshot', '')
    conn = get_db_connection()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if source_type == 'sale_payment' and source_id:
            existing = conn.execute("SELECT id FROM office_finances WHERE source_type=? AND source_id=? AND status!='ملغاة'", (source_type, source_id)).fetchone()
            if existing:
                conn.rollback(); conn.close()
                return "<script>alert('تم تسجيل سند خزينة لهذه الحركة من قبل. راجع سجل الخزينة.');window.location.href='/office_finances';</script>"
        cur = conn.execute("""INSERT INTO office_finances
          (fin_type,title,amount,fin_date,notes,counterparty,created_by,status,source_type,source_id,office_income,third_party_due)
          VALUES (?,?,?,?,?,?,?,'معتمدة',?,?,?,?)""",
          (fin_type,title,amount,request.form.get('fin_date') or get_today_date(),request.form.get('notes',''),counterparty,session.get('user',''),source_type,source_id,office_income,third_party_due))
        finance_id = cur.lastrowid
        voucher_no = 'خز-' + datetime.now().strftime('%Y%m') + '-' + str(finance_id).zfill(6)
        snapshot_file = save_treasury_snapshot(snap_b64, finance_id)
        conn.execute('UPDATE office_finances SET voucher_no=?,snapshot_file=? WHERE id=?',(voucher_no,snapshot_file,finance_id))
        conn.commit()
    except Exception:
        conn.rollback(); conn.close(); raise
    conn.close()
    log_act(session.get('user'), 'اعتماد سند خزينة ' + voucher_no + ': ' + title, category='الخزينة المركزية')
    return redirect(url_for('office_finances_page'))

@app.route('/add_office_finance', methods=['POST'])
def add_office_finance():
    if 'user' not in session: return redirect(url_for('login'))
    raw_type = request.form.get('fin_type', 'صرف')
    fin_type = 'إيراد عابر' if raw_type in ('قبض', 'إيراد عابر') else 'مصروف عابر'
    title = (request.form.get('title') or '').strip()
    try:
        amount = float(request.form.get('amount') or 0)
    except (TypeError, ValueError):
        amount = 0
    if not title or amount <= 0:
        return redirect(url_for('office_finances_page'))
    try:
        office_income = float(request.form.get('office_income') or 0)
        third_party_due = float(request.form.get('third_party_due') or 0)
    except (TypeError, ValueError):
        return redirect(url_for('office_finances_page'))
    if office_income < 0 or (fin_type == 'إيراد عابر' and third_party_due < 0) or (fin_type == 'مصروف عابر' and third_party_due > 0):
        return redirect(url_for('office_finances_page'))
    if fin_type == 'إيراد عابر' and abs((office_income + third_party_due) - amount) > 0.01:
        return "<script>alert('يجب أن يساوي إيراد المكتب + مستحقات الغير إجمالي المقبوض.'); history.back();</script>"
    if fin_type == 'مصروف عابر':
        office_income = 0.0
    snap_b64 = request.form.get('screen_snapshot', '')
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("""
            INSERT INTO office_finances
            (fin_type, title, amount, fin_date, notes, counterparty, created_by, status, office_income, third_party_due)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'معتمدة', ?, ?)
        """, (fin_type, title, amount, request.form.get('fin_date') or get_today_date(),
              request.form.get('notes', ''), request.form.get('counterparty', ''),
              session.get('user', ''), office_income, third_party_due))
        finance_id = cur.lastrowid
        voucher_no = "خز-" + datetime.now().strftime("%Y%m") + "-" + str(finance_id).zfill(6)
        snapshot_file = save_treasury_snapshot(snap_b64, finance_id)
        conn.execute("UPDATE office_finances SET voucher_no=?, snapshot_file=? WHERE id=?",
                     (voucher_no, snapshot_file, finance_id))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    log_act(session.get('user'), "حفظ حركة خزينة " + voucher_no + " (" + raw_type + "): " + title,
            category="الإيرادات والمصروفات")
    return redirect(url_for('office_finances_page'))

@app.route('/delete_office_finance/<int:id>')
def delete_office_finance(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    row = conn.execute("SELECT * FROM office_finances WHERE id = ?", (id,)).fetchone()
    if row and row['status'] != 'ملغاة':
        conn.execute("UPDATE office_finances SET status='ملغاة', notes=COALESCE(notes,'') || ? WHERE id=?",
                     (" | إلغاء بواسطة " + str(session.get('user')) + " بتاريخ " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"), id))
        if row['source_type'] == 'legal_expense' and row['source_id']:
            conn.execute("UPDATE legal_expenses SET status='ملغاة' WHERE id=?", (row['source_id'],))
        conn.commit()
    conn.close()
    log_act(session.get('user'), "إلغاء حركة الخزينة رقم #" + str(id), category="الإيرادات والمصروفات")
    return redirect(url_for('office_finances_page'))

# ==================== التقارير الشهرية والشؤون الإدارية ====================
@app.route('/reports')
def reports_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cur_m = datetime.now().strftime("%Y-%m")
    
    contracts = conn.execute("SELECT tenant_name, property_name, office_commission, start_date FROM contracts WHERE start_date LIKE ?", (cur_m + "%",)).fetchall()
    contract_office_comm = sum([c['office_commission'] for c in contracts])
    
    sales_transactions = conn.execute("""
        SELECT s.operation_type, s.property_name, s.client_name, s.paid_price, s.office_commission, s.op_date, p.ownership_status 
        FROM sales s 
        LEFT JOIN properties p ON s.property_name = p.building_name 
        WHERE s.op_date LIKE ? AND s.status = 'نشطة'
    """, (cur_m + "%",)).fetchall()
    
    # لا تُحسب قيمة بيع العقار كإيراد للمكتب؛ الإيراد هو الجزء المصنف فعليًا للمكتب في سند الخزينة.
    purchase_exp = 0
    treasury_income_row = conn.execute("SELECT COALESCE(SUM(office_income),0) FROM office_finances WHERE fin_date LIKE ? AND status != 'ملغاة' AND COALESCE(source_type,'') NOT IN ('contract_payment','contract_payment_adjustment')", (cur_m + "%",)).fetchone()
    treasury_office_income = float(treasury_income_row[0] or 0)
    casual_rev_items = []
    # الإيرادات العابرة القديمة التي لم يكن لها تصنيف تُستبعد من الإيراد المحاسبي منعًا لاعتبار المقبوضات أموالًا للمكتب تلقائيًا.
    casual_rev = 0
    total_rev = contract_office_comm + treasury_office_income
    
    legal_exp_items = conn.execute("""
        SELECT e.expense_amount, e.expense_type, e.expense_date, a.case_title 
        FROM legal_expenses e 
        JOIN legal_affairs a ON e.case_id = a.id 
        WHERE e.expense_date LIKE ? AND COALESCE(e.status, 'معتمدة') != 'ملغاة'
    """, (cur_m + "%",)).fetchall()
    legal_exp = sum([item['expense_amount'] for item in legal_exp_items])
    
    casual_exp_items = conn.execute("SELECT title, amount, fin_date FROM office_finances WHERE fin_type = 'مصروف عابر' AND fin_date LIKE ? AND COALESCE(source_type, '') = '' AND status != 'ملغاة'", (cur_m + "%",)).fetchall()
    casual_exp = sum([item['amount'] for item in casual_exp_items])
    
    total_exp = legal_exp + casual_exp + purchase_exp
    net_inc = total_rev - total_exp

    employees = conn.execute("SELECT * FROM employees WHERE status = 'نشط'").fetchall()
    emp_admin_rows = []
    tot_emp_sal = 0
    tot_emp_comm = 0
    tot_emp_ded = 0
    tot_emp_net = 0
    for e in employees:
        ename = e['name']
        c_c = conn.execute("SELECT SUM(employee_commission) FROM contracts WHERE employee_name = ? AND start_date LIKE ?", (ename, cur_m + "%")).fetchone()[0] or 0
        s_c = conn.execute("SELECT SUM(employee_commission) FROM sales WHERE employee_name = ? AND op_date LIKE ? AND status = 'نشطة'", (ename, cur_m + "%")).fetchone()[0] or 0
        tot_c = c_c + s_c
        total_ded = (e['deductions'] or 0) + (e['advances'] or 0) + (e['taxes'] or 0)
        net_d = (e['salary'] or 0) + tot_c - total_ded
        
        tot_emp_sal += (e['salary'] or 0)
        tot_emp_comm += tot_c
        tot_emp_ded += total_ded
        tot_emp_net += net_d
        
        name_link = '<a href="/employee_profile/' + str(e['id']) + '" class="text-warning fw-bold text-decoration-none">' + str(ename) + '</a>'
        emp_admin_rows.append("<tr><td><b>" + name_link + "</b></td><td>" + str(e['job']) + "</td><td>" + str(e['salary']) + " ج.م</td><td>" + str(tot_c) + " ج.م</td><td>جزاء: " + str(e['deductions']) + " | سلف: " + str(e['advances']) + " | ضريبة: " + str(e['taxes']) + "</td><td class='text-success fw-bold'>" + str(net_d) + " ج.م</td></tr>")

    cases = conn.execute("SELECT * FROM legal_affairs").fetchall()
    case_rows = []
    for c in cases:
        c_id = c['id']
        exps = conn.execute("SELECT SUM(expense_amount) FROM legal_expenses WHERE case_id = ? AND COALESCE(status, 'معتمدة') != 'ملغاة'", (c_id,)).fetchone()[0] or 0
        case_rows.append("<tr><td><b>" + str(c['case_title']) + "</b></td><td>" + str(c['client_name']) + "</td><td>" + str(c['case_party_type']) + "</td><td><span class='badge bg-info text-dark'>" + str(c['case_status']) + "</span></td><td>" + str(exps) + " ج.م</td><td>" + str(c['lawyer_name']) + "</td></tr>")

    treasury_income_items = conn.execute("SELECT title,office_income FROM office_finances WHERE fin_date LIKE ? AND status!='ملغاة' AND office_income>0 AND COALESCE(source_type,'') NOT IN ('contract_payment','contract_payment_adjustment')", (cur_m + "%",)).fetchall()
    conn.close()
    
    rev_details_html = "<h6>عقود الإيجار:</h6><ul class='list-group mb-3 bg-dark text-white'>" + "".join(["<li class='list-group-item bg-dark text-white border-secondary d-flex justify-content-between'><span>" + str(c['tenant_name']) + " (" + str(c['property_name']) + ")</span><b class='text-success'>" + str(c['office_commission']) + " ج.م</b></li>" for c in contracts]) + "</ul>"
    rev_details_html += "<h6>إيراد المكتب المثبت بسندات الخزينة:</h6><ul class='list-group mb-3 bg-dark text-white'>" + "".join(["<li class='list-group-item bg-dark text-white border-secondary d-flex justify-content-between'><span>" + str(i['title']) + "</span><b class='text-success'>" + str(i['office_income']) + " ج.م</b></li>" for i in treasury_income_items]) + "</ul>"
    rev_details_html += "<h6>إيرادات عابرة:</h6><ul class='list-group bg-dark text-white'>" + "".join(["<li class='list-group-item bg-dark text-white border-secondary d-flex justify-content-between'><span>" + str(i['title']) + "</span><b class='text-success'>" + str(i['amount']) + " ج.م</b></li>" for i in casual_rev_items]) + "</ul>"

    exp_details_html = "<h6>مصاريف الشؤون القانونية:</h6><ul class='list-group mb-3 bg-dark text-white'>" + "".join(["<li class='list-group-item bg-dark text-white border-secondary d-flex justify-content-between'><span>" + str(l['case_title']) + " (" + str(l['expense_type']) + ")</span><b class='text-danger'>" + str(l['expense_amount']) + " ج.م</b></li>" for l in legal_exp_items]) + "</ul>"
    exp_details_html += "<h6>مصاريف عابرة للمكتب:</h6><ul class='list-group mb-3 bg-dark text-white'>" + "".join(["<li class='list-group-item bg-dark text-white border-secondary d-flex justify-content-between'><span>" + str(e['title']) + "</span><b class='text-danger'>" + str(e['amount']) + " ج.م</b></li>" for e in casual_exp_items]) + "</ul>"

    content = """
    <div class="row">
        <div class="col-12">
            <div class="card card-custom p-4">
                <div class="d-flex justify-content-between align-items-center mb-4">
                    <h4 class="text-warning fw-bold m-0">📊 التقارير الشهرية الشاملة وصافي دخل المكتب الفعلي (عن شهر """ + cur_m + """)</h4>
                    <button onclick="captureReportSnapshot()" class="btn btn-warning fw-bold btn-sm shadow">📸 التقاط وحفظ صورة التقرير في سجل الأنشطة</button>
                </div>
                
                <div class="row text-center mb-4">
                    <div class="col-md-4 mb-3">
                        <div class="p-3 border rounded bg-dark border-success interactive-card shadow" onclick="new bootstrap.Modal(document.getElementById('revModal')).show();">
                            <h6 class="text-success fw-bold">إجمالي إيرادات المكتب (اضغط للتفاصيل) 🔍</h6>
                            <h3 class="fw-bold text-success m-0">""" + str(total_rev) + """ ج.م</h3>
                        </div>
                    </div>
                    <div class="col-md-4 mb-3">
                        <div class="p-3 border rounded bg-dark border-danger interactive-card shadow" onclick="new bootstrap.Modal(document.getElementById('expModal')).show();">
                            <h6 class="text-danger fw-bold">إجمالي المصروفات (اضغط للتفاصيل) 🔍</h6>
                            <h3 class="fw-bold text-danger m-0">""" + str(total_exp) + """ ج.م</h3>
                        </div>
                    </div>
                    <div class="col-md-4 mb-3">
                        <div class="p-3 border rounded bg-dark border-warning shadow">
                            <h6 class="text-warning fw-bold">🌟 صافي دخل ورصيد المكتب الفعلي</h6>
                            <h3 class="fw-bold text-warning m-0">""" + str(net_inc) + """ ج.م</h3>
                        </div>
                    </div>
                </div>

                <hr class="border-secondary my-4">

                <h4 class="text-info fw-bold mb-3">📋 الشؤون الإدارية والقضايا</h4>
                <div class="row text-center">
                    <div class="col-md-6 mb-3">
                        <div class="p-4 border rounded bg-dark border-warning interactive-card shadow" onclick="new bootstrap.Modal(document.getElementById('empAdminModal')).show();">
                            <h5 class="text-warning fw-bold mb-2">👤 تفاصيل العاملين والمرتبات والعمولات والجزاءات</h5>
                            <p class="text-muted mb-0" style="font-size: 13px;">اضغط هنا لفتح النافذة الشاملة لمستحقات ورواتب وعمولات الموظفين</p>
                        </div>
                    </div>
                    <div class="col-md-6 mb-3">
                        <div class="p-4 border rounded bg-dark border-info interactive-card shadow" onclick="new bootstrap.Modal(document.getElementById('casesModal')).show();">
                            <h5 class="text-info fw-bold mb-2">⚖️ ملخص القضايا والشؤون القانونية ومصروفاتها</h5>
                            <p class="text-muted mb-0" style="font-size: 13px;">اضغط هنا لفتح النافذة التفصيلية للقضايا الجارية وحالاتها والمصروفات</p>
                        </div>
                    </div>
                </div>

            </div>
        </div>
    </div>

    <!-- Modals -->
    <div class="modal fade" id="revModal" tabindex="-1">
      <div class="modal-dialog modal-lg modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-success fw-bold">📊 تفاصيل إجمالي إيرادات المكتب</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">""" + rev_details_html + """</div>
          <div class="modal-footer border-secondary"><button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">إغلاق</button></div>
        </div>
      </div>
    </div>

    <div class="modal fade" id="expModal" tabindex="-1">
      <div class="modal-dialog modal-lg modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-danger fw-bold">💸 تفاصيل إجمالي مصروفات المكتب</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">""" + exp_details_html + """</div>
          <div class="modal-footer border-secondary"><button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">إغلاق</button></div>
        </div>
      </div>
    </div>

    <div class="modal fade" id="empAdminModal" tabindex="-1">
      <div class="modal-dialog modal-xl modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">👥 تفاصيل العاملين والمرتبات والعمولات والجزاءات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle text-center" style="font-size: 12px;">
                    <thead><tr><th>الموظف</th><th>الوظيفة</th><th>الراتب الأساسي</th><th>إجمالي العمولات</th><th>الاستقطاعات</th><th>الصافي المستحق</th></tr></thead>
                    <tbody>""" + "".join(emp_admin_rows) + """</tbody>
                </table>
            </div>
            <div class="p-3 bg-secondary rounded text-end fw-bold">إجمالي الصافي لكافة العاملين: """ + str(tot_emp_net) + """ ج.م</div>
          </div>
          <div class="modal-footer border-secondary"><button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">إغلاق</button></div>
        </div>
      </div>
    </div>

    <div class="modal fade" id="casesModal" tabindex="-1">
      <div class="modal-dialog modal-xl modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-info fw-bold">⚖️ ملخص القضايا والشؤون القانونية ومصروفاتها</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <div class="modal-body">
            <div class="table-responsive">
                <table class="table table-dark table-hover align-middle text-center" style="font-size: 12px;">
                    <thead><tr><th>عنوان القضية</th><th>الخصم</th><th>الاتجاه</th><th>الحالة</th><th>إجمالي المصروفات</th><th>المحامي</th></tr></thead>
                    <tbody>""" + "".join(case_rows) + """</tbody>
                </table>
            </div>
          </div>
          <div class="modal-footer border-secondary"><button type="button" class="btn btn-secondary btn-sm" data-bs-dismiss="modal">إغلاق</button></div>
        </div>
      </div>
    </div>

    <script>
    function captureReportSnapshot() {
        window.scrollTo(0, 0);
        html2canvas(document.body, { scrollY: -window.scrollY, scale: 1, useCORS: true }).then(function(cvs) {
            let snapData = cvs.toDataURL('image/png');
            fetch('/save_report_snapshot', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({snapshot: snapData})
            }).then(res => res.json()).then(data => {
                alert('📸 تم التقاط وحفظ صورة التقارير الشهرية في سجل الأنشطة بنجاح!');
            });
        });
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/save_report_snapshot', methods=['POST'])
def save_report_snapshot():
    if 'user' not in session: return jsonify({'status': 'unauthorized'})
    data = request.get_json()
    snapshot_b64 = data.get('snapshot', '') if data else ''
    log_act(session.get('user'), "التقاط صورة تقارير أداء المكتب الشهري", snapshot_b64, category="الإيرادات والمصروفات")
    return jsonify({'status': 'success'})

# ==================== الشؤون القانونية وسجل الأنشطة ====================
@app.route('/legal')
def legal_page():
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    cases = conn.execute("SELECT * FROM legal_affairs").fetchall()
    
    l_rows = []
    for l in cases:
        case_id = l['id']
        doc_img = render_file_badges(l['document_image'] if 'document_image' in l.keys() else "")
        exps = conn.execute("SELECT * FROM legal_expenses WHERE case_id = ? AND COALESCE(status, 'معتمدة') != 'ملغاة'", (case_id,)).fetchall()
        tot_exp = sum([e['expense_amount'] for e in exps])
        exp_details = "<br>".join(["• " + str(e['expense_amount']) + " ج.م (" + str(e['expense_type']) + ") - " + str(e['expense_date']) for e in exps]) if exps else "لا توجد مصروفات"
        
        add_exp_btn = '<button class="btn btn-sm btn-outline-success me-1" onclick="openAddExpense(' + str(case_id) + ', \'' + str(l['case_title']) + '\')">+ بند مصروف</button>'
        edit_btn = '<button class="btn btn-warning btn-sm me-1" onclick="openEditLegal(' + str(case_id) + ', \'' + str(l['case_title']) + '\', \'' + str(l['client_name']) + '\', \'' + str(l['case_status']) + '\', \'' + str(l['lawyer_name']) + '\')">تعديل والمرفقات</button>'
        l_rows.append("<tr><td>" + doc_img + "</td><td><b>" + str(l['case_title']) + "</b></td><td>" + str(l['client_name']) + "</td><td>" + str(l['case_party_type']) + "</td><td><span class='badge bg-info text-dark'>" + str(l['case_status']) + "</span></td><td><b>إجمالي: " + str(tot_exp) + " ج.م</b><br><small>" + str(exp_details) + "</small></td><td>" + str(l['lawyer_name']) + "</td><td>" + add_exp_btn + edit_btn + " <a href='/delete_legal/" + str(case_id) + "' class='btn btn-danger btn-sm' onclick='return confirm(\"حذف القضية ومصاريفها؟\")'>حذف</a></td></tr>")
    
    conn.close()
    today = get_today_date()
    content = f"""
    <div class="row">
        <div class="col-xl-8 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">سجل الشؤون القانونية وصندوق المصروفات والمستندات</h5>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>المستندات</th><th>عنوان القضية</th><th>الخصم</th><th>الاتجاه</th><th>الحالة</th><th>صندوق المصروفات</th><th>المحامي</th><th>إجراءات</th></tr></thead>
                        <tbody>{''.join(l_rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
        <div class="col-xl-4 mb-3">
            <div class="card card-custom p-4">
                <h5 class="text-success mb-3 fw-bold">تسجيل قضية جديدة والمرفقات</h5>
                <form action="/add_legal" method="POST" enctype="multipart/form-data" onsubmit="event.preventDefault(); captureScreenAndSubmit(this);">
                    <div class="mb-2"><label class="form-label">عنوان القضية:</label><input type="text" name="case_title" class="form-control bg-dark text-white" required></div>
                    <div class="mb-2"><label class="form-label">الخصم / العميل:</label><input type="text" name="client_name" class="form-control bg-dark text-white" required></div>
                    <div class="mb-2"><label class="form-label">الاتجاه:</label><select name="case_party_type" class="form-control bg-dark text-white"><option value="من المكتب على الغير">من المكتب على الغير</option><option value="ضد المكتب">ضد المكتب</option></select></div>
                    <div class="mb-2"><label class="form-label">الحالة:</label><select name="case_status" class="form-control bg-dark text-white"><option value="جاري">جاري</option><option value="تم الحل">تم الحل</option></select></div>
                    <div class="mb-2"><label class="form-label">المحامي:</label><input type="text" name="lawyer_name" class="form-control bg-dark text-white"></div>
                    <div class="mb-3"><label class="form-label">مستندات القضية (عدة صور):</label><input type="file" name="legal_images" class="form-control bg-dark text-white" accept="image/*,.pdf" multiple></div>
                    <button type="submit" class="btn btn-success w-100 fw-bold">حفظ القضية</button>
                </form>
            </div>
        </div>
    </div>

    <!-- modal إضافة بند مصروف متعدد للقضية -->
    <div class="modal fade" id="addExpModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-success fw-bold" id="addExpTitle">إضافة بند مصروف للقضية</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/add_legal_expense" method="POST" onsubmit="event.preventDefault(); captureRequiredScreenAndSubmit(this);">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="case_id" id="exp_case_id">
                <div class="mb-2"><label class="form-label">مبلغ البند:</label><input type="number" step="any" name="expense_amount" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">نوع البند / الوصف:</label><input type="text" name="expense_type" class="form-control form-control-sm bg-secondary text-white" placeholder="رسوم توكيل / تصوير" required></div>
                <div class="mb-2"><label class="form-label">تاريخ الصرف:</label><input type="date" name="expense_date" class="form-control form-control-sm bg-secondary text-white" value="{today}" required></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-success fw-bold btn-sm">إضافة بند المصروف</button></div>
          </form>
        </div>
      </div>
    </div>

    <!-- modal تعديل القضية -->
    <div class="modal fade" id="editLegalModal" tabindex="-1">
      <div class="modal-dialog modal-dialog-scrollable">
        <div class="modal-content bg-dark text-white">
          <div class="modal-header border-secondary"><h5 class="modal-title text-warning fw-bold">تعديل بيانات القضية والمرفقات</h5><button type="button" class="btn-close btn-close-white" data-bs-dismiss="modal"></button></div>
          <form action="/edit_legal" method="POST" enctype="multipart/form-data">
            <div class="modal-body" style="font-size: 12px;">
                <input type="hidden" name="case_id" id="el_id">
                <div class="mb-2"><label class="form-label">عنوان القضية:</label><input type="text" name="case_title" id="el_title" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">الخصم:</label><input type="text" name="client_name" id="el_cname" class="form-control form-control-sm bg-secondary text-white" required></div>
                <div class="mb-2"><label class="form-label">الحالة:</label><select name="case_status" id="el_status" class="form-control form-control-sm bg-secondary text-white"><option value="جاري">جاري</option><option value="تم الحل">تم الحل</option></select></div>
                <div class="mb-2"><label class="form-label">المحامي:</label><input type="text" name="lawyer_name" id="el_lawyer" class="form-control form-control-sm bg-secondary text-white"></div>
                <div class="mb-2"><label class="form-label text-warning">تعديل/إضافة مستندات جديدة (عدة صور):</label><input type="file" name="legal_images" class="form-control form-control-sm bg-secondary text-white" accept="image/*,.pdf" multiple></div>
            </div>
            <div class="modal-footer border-secondary"><button type="submit" class="btn btn-warning fw-bold btn-sm">حفظ التعديلات</button></div>
          </form>
        </div>
      </div>
    </div>
    """ + """
    <script>
    function openAddExpense(caseId, caseTitle) {
        document.getElementById('exp_case_id').value = caseId;
        document.getElementById('addExpTitle').innerText = 'إضافة بند مصروف للقضية: ' + caseTitle;
        new bootstrap.Modal(document.getElementById('addExpModal')).show();
    }
    function openEditLegal(id, title, cname, status, lawyer) {
        document.getElementById('el_id').value = id;
        document.getElementById('el_title').value = title;
        document.getElementById('el_cname').value = cname;
        document.getElementById('el_status').value = status;
        document.getElementById('el_lawyer').value = lawyer;
        new bootstrap.Modal(document.getElementById('editLegalModal')).show();
    }
    </script>
    """
    return render_page(content, session.get('user'))

@app.route('/add_legal', methods=['POST'])
def add_legal():
    if 'user' not in session: return redirect(url_for('login'))
    snap_b64 = request.form.get('screen_snapshot', '')
    uploaded_files = save_files(request.files.getlist('legal_images'))
    conn = get_db_connection()
    conn.execute("INSERT INTO legal_affairs (case_title, client_name, case_status, case_party_type, lawyer_name, document_image) VALUES (?, ?, ?, ?, ?, ?)",
                 (request.form.get('case_title'), request.form.get('client_name'), request.form.get('case_status'), request.form.get('case_party_type'), request.form.get('lawyer_name'), uploaded_files))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "إضافة قضية جديدة: " + str(request.form.get('case_title')), snap_b64, category="العقارات والشؤون القانونية")
    return redirect(url_for('legal_page'))

@app.route('/add_legal_expense', methods=['POST'])
def add_legal_expense():
    if 'user' not in session: return redirect(url_for('login'))
    case_id = request.form.get('case_id')
    amt = float(request.form.get('expense_amount') or 0)
    exp_type = request.form.get('expense_type', '')
    exp_date = request.form.get('expense_date', '')
    
    snap_b64 = request.form.get('screen_snapshot', '')
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute("INSERT INTO legal_expenses (case_id, expense_amount, expense_type, expense_date) VALUES (?, ?, ?, ?)",
                           (case_id, amt, exp_type, exp_date))
        expense_id = cur.lastrowid
        case_row = conn.execute("SELECT case_title FROM legal_affairs WHERE id=?", (case_id,)).fetchone()
        case_title = case_row['case_title'] if case_row else ("القضية #" + str(case_id))
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise
    conn.close()
    log_act(session.get('user'), "إضافة بند مصروف للقضية #" + str(case_id) + " بقيمة " + str(amt) + " ج.م",
            category="العقارات والشؤون القانونية")
    if amt > 0:
        return redirect(url_for('treasury_entry', source_type='legal_expense', source_id=expense_id, amount=amt, direction='صرف'))
    return redirect(url_for('legal_page'))

@app.route('/edit_legal', methods=['POST'])
def edit_legal():
    if 'user' not in session: return redirect(url_for('login'))
    c_id = request.form.get('case_id')
    new_files = save_files(request.files.getlist('legal_images'))
    conn = get_db_connection()
    if new_files:
        conn.execute("UPDATE legal_affairs SET case_title=?, client_name=?, case_status=?, lawyer_name=?, document_image=? WHERE id=?",
                     (request.form.get('case_title'), request.form.get('client_name'), request.form.get('case_status'), request.form.get('lawyer_name'), new_files, c_id))
    else:
        conn.execute("UPDATE legal_affairs SET case_title=?, client_name=?, case_status=?, lawyer_name=? WHERE id=?",
                     (request.form.get('case_title'), request.form.get('client_name'), request.form.get('case_status'), request.form.get('lawyer_name'), c_id))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "تعديل القضية #" + str(c_id), category="العقارات والشؤون القانونية")
    return redirect(url_for('legal_page'))

@app.route('/delete_legal/<int:id>')
def delete_legal(id):
    if 'user' not in session: return redirect(url_for('login'))
    conn = get_db_connection()
    linked = conn.execute("SELECT COUNT(*) FROM office_finances WHERE source_type='legal_expense' AND source_id IN (SELECT id FROM legal_expenses WHERE case_id=?) AND status != 'ملغاة'", (id,)).fetchone()[0]
    if linked:
        conn.close()
        return "<script>alert('لا يمكن حذف القضية لوجود مصروفات مرتبطة بالخزينة. ألغِ السندات المالية أولاً.'); window.location.href='/legal';</script>"
    conn.execute("DELETE FROM legal_affairs WHERE id = ?", (id,))
    conn.execute("DELETE FROM legal_expenses WHERE case_id = ?", (id,))
    conn.commit()
    conn.close()
    log_act(session.get('user'), "حذف قضية ومصاريفها رقم #" + str(id), category="العقارات والشؤون القانونية")
    return redirect(url_for('legal_page'))

# ==================== محرر شامل لتعديل بيانات جميع الأقسام ====================
EDITABLE_TABLES = {
    "employees": "الموظفون",
    "properties": "العقارات",
    "contracts": "العقود",
    "sales": "البيع والشراء",
    "legal_affairs": "الشؤون القانونية",
    "legal_expenses": "مصروفات القضايا",
    "office_finances": "الخزينة",
    "activity_logs": "سجل الأنشطة",
}

@app.route('/data_editor', methods=['GET', 'POST'])
def data_editor_page():
    if 'user' not in session:
        return redirect(url_for('login'))

    table = request.values.get('table', 'properties')
    if table not in EDITABLE_TABLES:
        table = 'properties'
    record_id = request.values.get('record_id', '')
    conn = get_db_connection()
    message = ''

    if request.method == 'POST' and request.form.get('action') == 'save':
        try:
            record_id = int(request.form.get('record_id') or 0)
            columns = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
            editable_columns = [col['name'] for col in columns if col['name'] != 'id']
            assignments, values = [], []
            for col in editable_columns:
                if col in request.form:
                    assignments.append('"' + col.replace('"', '""') + '" = ?')
                    values.append(request.form.get(col, ''))
            if assignments:
                values.append(record_id)
                conn.execute('UPDATE "' + table + '" SET ' + ', '.join(assignments) + ' WHERE id = ?', values)
                if table == 'office_finances':
                    linked = conn.execute("SELECT source_type, source_id, amount, title, fin_date, status FROM office_finances WHERE id=?", (record_id,)).fetchone()
                    if linked and linked['source_type'] == 'legal_expense' and linked['source_id']:
                        exp_title = str(linked['title'] or '')
                        exp_type = exp_title.split('] ', 1)[1] if '] ' in exp_title else exp_title
                        conn.execute("UPDATE legal_expenses SET expense_amount=?, expense_type=?, expense_date=?, status=? WHERE id=?",
                                     (linked['amount'], exp_type, linked['fin_date'], linked['status'], linked['source_id']))
                elif table == 'legal_expenses':
                    linked_finance = conn.execute("SELECT id FROM office_finances WHERE source_type='legal_expense' AND source_id=?", (record_id,)).fetchone()
                    if linked_finance:
                        exp = conn.execute("SELECT expense_amount, expense_type, expense_date, status, case_id FROM legal_expenses WHERE id=?", (record_id,)).fetchone()
                        conn.execute("UPDATE office_finances SET amount=?, title=?, fin_date=?, status=? WHERE id=?",
                                     (exp['expense_amount'], '[قضية #' + str(exp['case_id']) + '] ' + str(exp['expense_type']), exp['expense_date'], exp['status'], linked_finance['id']))
                conn.commit()
                log_act(session.get('user'), "تعديل شامل للبيانات في قسم " + EDITABLE_TABLES[table] + " للسجل #" + str(record_id),
                        category="تعديل البيانات")
                message = '<div class="alert alert-success">تم حفظ تعديلات السجل بنجاح.</div>'
        except (ValueError, sqlite3.Error) as exc:
            conn.rollback()
            message = '<div class="alert alert-danger">تعذر حفظ التعديل: ' + html.escape(str(exc)) + '</div>'

    selected = conn.execute('SELECT * FROM "' + table + '" WHERE id = ?', (record_id,)).fetchone() if str(record_id).isdigit() else None
    records = conn.execute('SELECT * FROM "' + table + '" ORDER BY id DESC LIMIT 150').fetchall()
    columns = conn.execute("PRAGMA table_info(" + table + ")").fetchall()
    conn.close()

    options = ''.join(
        '<option value="' + html.escape(t, quote=True) + '"' + (' selected' if t == table else '') + '>' +
        html.escape(label) + '</option>'
        for t, label in EDITABLE_TABLES.items()
    )
    editor_fields = []
    if selected:
        for col in columns:
            name = col['name']
            if name == 'id':
                continue
            value = '' if selected[name] is None else str(selected[name])
            safe_name = html.escape(name, quote=True)
            safe_value = html.escape(value, quote=True)
            if any(token in name.lower() for token in ('notes', 'details', 'address', 'description')):
                field = '<textarea class="form-control bg-dark text-white mb-2" rows="2" name="' + safe_name + '">' + html.escape(value) + '</textarea>'
            else:
                field = '<input class="form-control bg-dark text-white mb-2" name="' + safe_name + '" value="' + safe_value + '">'
            editor_fields.append('<label class="form-label text-warning">' + html.escape(name) + '</label>' + field)

    record_rows = []
    for row in records:
        preview_cols = [c['name'] for c in columns if c['name'] != 'id'][:3]
        preview = ' | '.join(str(row[col] or '') for col in preview_cols)
        record_rows.append(
            '<tr><td>' + str(row['id']) + '</td><td>' + html.escape(preview[:180]) +
            '</td><td><a class="btn btn-warning btn-sm fw-bold" href="/data_editor?table=' +
            html.escape(table, quote=True) + '&record_id=' + str(row['id']) + '">تعديل جميع البيانات</a></td></tr>'
        )

    content = """
    <div class="row">
      <div class="col-12">
        <div class="card card-custom p-4">
          <h5 class="text-warning fw-bold mb-3">تعديل جميع بيانات سجلات البرنامج</h5>
          <p class="text-secondary">اختر القسم ثم اختر السجل المطلوب؛ ستظهر جميع حقوله القابلة للتعديل. انتبه عند تعديل السجلات المالية أو سجل الأنشطة لأن تعديلها يؤثر في المراجعة والتقارير.</p>
          """ + message + """
          <form method="GET" action="/data_editor" class="row g-2 mb-3">
            <div class="col-md-8"><label class="form-label">القسم:</label><select class="form-select bg-dark text-white" name="table">""" + options + """</select></div>
            <div class="col-md-4 d-flex align-items-end"><button class="btn btn-info fw-bold w-100" type="submit">عرض السجلات</button></div>
          </form>
        </div>
      </div>
    """
    if selected:
        content += """
      <div class="col-xl-5 mb-3">
        <div class="card card-custom p-4">
          <h6 class="text-warning fw-bold">تعديل السجل رقم """ + str(selected['id']) + """ — """ + html.escape(EDITABLE_TABLES[table]) + """</h6>
          <form method="POST" action="/data_editor">
            <input type="hidden" name="action" value="save">
            <input type="hidden" name="table" value=\"""" + html.escape(table, quote=True) + """\">
            <input type="hidden" name="record_id" value=\"""" + str(selected['id']) + """\">
            """ + ''.join(editor_fields) + """
            <button type="submit" class="btn btn-success fw-bold w-100">حفظ جميع التعديلات</button>
          </form>
        </div>
      </div>
        """
    content += """
      <div class="col-xl-7 mb-3">
        <div class="card card-custom p-4">
          <h6 class="text-warning fw-bold">السجلات — آخر 150 سجلًا</h6>
          <div class="table-responsive"><table class="table table-dark table-hover align-middle text-center" style="font-size:12px">
          <thead><tr><th>رقم</th><th>ملخص</th><th>إجراء</th></tr></thead><tbody>""" + ''.join(record_rows) + """</tbody></table></div>
        </div>
      </div>
    </div>
    """
    return render_page(content, session.get('user'))

# ==================== سجل الأنشطة ====================
@app.route('/logs')
def logs_page():
    if 'user' not in session: return redirect(url_for('login'))
    cat = request.args.get('cat', 'الكل')
    conn = get_db_connection()
    
    if cat == 'الكل':
        logs = conn.execute("SELECT * FROM activity_logs ORDER BY id DESC").fetchall()
    else:
        logs = conn.execute("SELECT * FROM activity_logs WHERE category = ? ORDER BY id DESC", (cat,)).fetchall()
    conn.close()
    
    log_rows = []
    for i, l in enumerate(logs):
        snap_file = l['snapshot_file'] if 'snapshot_file' in l.keys() else ""
        category_val = l['category'] if 'category' in l.keys() else "عام"
        snap_link = "<a href='/static/snapshots/" + str(snap_file) + "' target='_blank' class='btn btn-sm btn-info text-dark fw-bold'>عرض الصورة</a>" if snap_file else "بدون صورة"
        row_click = "window.open('/static/snapshots/" + str(snap_file + "', '_blank');") if snap_file else ""
        log_rows.append("<tr onclick=\"" + row_click + "\" style=\"cursor: pointer;\" title=\"اضغط لفتح صورة الشاشة\"><td>" + str(i+1) + "</td><td><span class='badge bg-success'>" + str(l['employee_name']) + "</span></td><td>" + str(l['action_time']) + "</td><td>" + str(l['details']) + "</td><td><span class='badge bg-warning text-dark'>" + str(category_val) + "</span></td><td>" + snap_link + "</td></tr>")
    
    categories = ['الكل', 'الدخول والزيارات', 'العقود', 'البيع والشراء', 'العقارات والشؤون القانونية', 'الموظفين والصلاحيات', 'الإيرادات والمصروفات']
    tabs_html = "".join(['<a href="/logs?cat=' + c + '" class="btn ' + ('btn-warning' if cat == c else 'btn-dark') + ' btn-sm m-1 fw-bold">' + c + '</a>' for c in categories])

    content = f"""
    <div class="row">
        <div class="col-12">
            <div class="card card-custom p-4">
                <h5 class="text-warning mb-3 fw-bold">سجل الأنشطة المبوب والتصنيفي مع لقطات الشاشة الحقيقية</h5>
                <div class="mb-3 p-2 bg-dark rounded border border-secondary">
                    <b>تصنيفات الأنشطة:</b><br><br>{tabs_html}
                </div>
                <div class="table-responsive">
                    <table class="table table-dark table-hover align-middle text-center" style="font-size: 11px;">
                        <thead><tr><th>#</th><th>الموظف</th><th>الوقت والتاريخ</th><th>تفاصيل النشاط</th><th>القسم</th><th>صورة الشاشة الحقيقية (.png)</th></tr></thead>
                        <tbody>{''.join(log_rows)}</tbody>
                    </table>
                </div>
            </div>
        </div>
    </div>
    """
    return render_page(content, session.get('user'))

if __name__ == '__main__':
    print("="*50)
    print("🚀 جاري تشغيل نظام إدارة العقارات لمكتب عمر عبدالمنعم...")
    print("="*50)
    
    app.run(host='0.0.0.0', port=5009, debug=True)