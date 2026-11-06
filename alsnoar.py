# -*- coding: utf-8 -*-
"""نظام الامتحانات المدرسي عبر الإنترنت - نسخة Windows 7 / Python 3.7.
تم تطويرها لتقترب في الهيكل والوظائف من نسخة Wi-Fi الأصلية، مع بوابة إنترنت.
"""
from __future__ import unicode_literals

import base64
import binascii
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import uuid

import pytz
from functools import wraps
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, session, url_for, send_file
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import UniqueConstraint
from werkzeug.security import check_password_hash, generate_password_hash

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "change-me-before-production")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Permit an optimized PNG screenshot of the submitted answer sheet, while keeping a bounded upload size.
app.config["MAX_CONTENT_LENGTH"] = 12 * 1024 * 1024

# مهم: المسار مطلق حتى لا تضيع البيانات في مجلد instance مختلف عن المتوقع.
db_path = (BASE_DIR / "school_exam_internet.db").resolve()
db_url = os.getenv("DATABASE_URL", "sqlite:///" + str(db_path).replace("\\", "/"))
if db_url.startswith("postgres://"):
    db_url = "postgresql+psycopg2://" + db_url[len("postgres://"):]
if db_url.startswith("postgresql://"):
    db_url = "postgresql+psycopg2://" + db_url[len("postgresql://"):]
app.config["SQLALCHEMY_DATABASE_URI"] = db_url

db = SQLAlchemy(app)

GRADE_NAMES = [
    "أولى ابتدائي", "ثانية ابتدائي", "ثالثة ابتدائي", "رابعة ابتدائي", "خامسة ابتدائي", "سادسة ابتدائي",
    "أولى إعدادي", "ثانية إعدادي", "ثالثة إعدادي",
    "أولى ثانوي", "ثانية ثانوي", "ثالثة ثانوي",
]
SECTION_NAMES = ["أول", "ثاني", "ثالث", "رابع"]
DEFAULT_YEAR = os.getenv("DEFAULT_ACADEMIC_YEAR", "2026/2027")
ROLE_AR = {"admin": "مدير المدرسة", "teacher": "معلم", "student": "طالب"}
QTYPE_AR = {"mcq": "اختيار من متعدد", "matching": "توصيل", "essay": "مقالي"}


def utcnow():
    """UTC-naive timestamps for elapsed-time records (attempts and grading)."""
    return dt.datetime.utcnow()


def school_now():
    """Local school wall-clock time used by datetime-local scheduling fields.

    datetime-local inputs have no timezone information. Interpret them in the
    configured school's timezone rather than comparing them with UTC-naive now.
    """
    zone_name = os.getenv("SCHOOL_TIMEZONE", "Africa/Cairo")
    zone = pytz.timezone(zone_name)
    return dt.datetime.now(zone).replace(tzinfo=None)


def parse_dt(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except Exception:
        return None


class SchoolSetting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    school_name = db.Column(db.String(250), nullable=False, default="اسم المدرسة")


class AcademicYear(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    sort_order = db.Column(db.Integer, nullable=False, default=1)


class Grade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    stage_name = db.Column(db.String(80), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=1)


class Section(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    grade_id = db.Column(db.Integer, db.ForeignKey("grade.id"), nullable=False)
    name = db.Column(db.String(80), nullable=False)
    sort_order = db.Column(db.Integer, nullable=False, default=1)
    __table_args__ = (UniqueConstraint("grade_id", "name", name="uq_grade_section"),)


class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), unique=True, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    full_name = db.Column(db.String(200), nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=True)
    phone = db.Column(db.String(40), nullable=True)
    grade_id = db.Column(db.Integer, db.ForeignKey("grade.id"), nullable=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    address = db.Column(db.String(250), nullable=True)
    birth_date = db.Column(db.String(30), nullable=True)
    notes = db.Column(db.Text, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)


class TeacherAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_year.id", ondelete="CASCADE"), nullable=False)
    grade_id = db.Column(db.Integer, db.ForeignKey("grade.id", ondelete="CASCADE"), nullable=False)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("teacher_id", "academic_year_id", "grade_id", "section_id", name="uq_teacher_assignment"),)


class TeacherStudent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    __table_args__ = (UniqueConstraint("teacher_id", "student_id", name="uq_teacher_student"),)


class TeacherFolder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    folder_type = db.Column(db.String(30), nullable=False, default="custom")


class Exam(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    teacher_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey("subject.id"), nullable=False)
    title = db.Column(db.String(250), nullable=False)
    grade_id = db.Column(db.Integer, db.ForeignKey("grade.id"), nullable=True)
    section_id = db.Column(db.Integer, db.ForeignKey("section.id"), nullable=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_year.id"), nullable=True)
    duration_minutes = db.Column(db.Integer, nullable=False, default=30)
    publish_at = db.Column(db.DateTime, nullable=True)
    close_at = db.Column(db.DateTime, nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=False)
    status = db.Column(db.String(30), nullable=False, default="draft")
    folder_id = db.Column(db.Integer, db.ForeignKey("teacher_folder.id"), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ExamTarget(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id", ondelete="CASCADE"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_exam_target") ,)


class Question(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id", ondelete="CASCADE"), nullable=False)
    number = db.Column(db.Integer, nullable=False)
    qtype = db.Column(db.String(20), nullable=False)
    prompt = db.Column(db.Text, nullable=False)
    options_json = db.Column(db.Text, nullable=True)
    correct_json = db.Column(db.Text, nullable=True)
    points = db.Column(db.Float, nullable=False, default=1)


class Attempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exam.id"), nullable=False)
    student_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    submitted_at = db.Column(db.DateTime, nullable=True)
    answers_json = db.Column(db.Text, nullable=False, default="{}")
    auto_score = db.Column(db.Float, nullable=False, default=0)
    essay_score = db.Column(db.Float, nullable=True)
    essay_total = db.Column(db.Float, nullable=False, default=0)
    final_score = db.Column(db.Float, nullable=True)
    grading_status = db.Column(db.String(30), nullable=False, default="not_submitted")
    answer_snapshot_url = db.Column(db.Text, nullable=True)
    __table_args__ = (UniqueConstraint("exam_id", "student_id", name="uq_exam_student_attempt"),)


class EssayGrade(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    attempt_id = db.Column(db.Integer, db.ForeignKey("attempt.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("question.id", ondelete="CASCADE"), nullable=False)
    score = db.Column(db.Float, nullable=False, default=0)
    feedback = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, nullable=False, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("attempt_id", "question_id", name="uq_attempt_question_grade"),)


@app.context_processor
def globals_ctx():
    user = current_user()
    return {"current_user": user, "role_ar": ROLE_AR, "qtype_ar": QTYPE_AR, "school_name": school_name()}


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return User.query.get(uid)


def login_required(role=None):
    def deco(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            user = current_user()
            if not user or not user.active:
                return redirect(url_for("login"))
            if role and user.role != role:
                abort(403)
            return fn(*args, **kwargs)
        return wrapped
    return deco


def school_name():
    row = SchoolSetting.query.first()
    return row.school_name if row else "اسم المدرسة"


def stage_name(grade):
    n = grade.name if grade else ""
    if "ابتدائي" in n:
        return "المرحلة الابتدائية"
    if "إعدادي" in n:
        return "المرحلة الإعدادية"
    if "ثانوي" in n:
        return "المرحلة الثانوية"
    return "غير محدد"


def exam_student_state(exam):
    """Return a clear reason when a targeted exam is not currently startable."""
    if Question.query.filter_by(exam_id=exam.id).count() == 0:
        return "no_questions"
    if not exam.active:
        return "pending_activation"
    now = school_now()
    if exam.publish_at and now < exam.publish_at:
        return "scheduled"
    if exam.close_at and now > exam.close_at:
        return "closed"
    return "open"


def exam_is_open(exam):
    return exam_student_state(exam) == "open"


def fmt_dt(value):
    return value.strftime("%d/%m/%Y %H:%M") if value else "—"


@app.template_filter("dt")
def dt_filter(value):
    return fmt_dt(value)


def ensure_structure():
    db.create_all()
    if not SchoolSetting.query.first():
        db.session.add(SchoolSetting(school_name=os.getenv("SCHOOL_NAME", "اسم المدرسة")))
    if not AcademicYear.query.first():
        db.session.add(AcademicYear(name=DEFAULT_YEAR, sort_order=1, active=True))
    for idx, name in enumerate(GRADE_NAMES, 1):
        grade = Grade.query.filter_by(name=name).first()
        if not grade:
            grade = Grade(name=name, stage_name="", sort_order=idx)
            db.session.add(grade)
            db.session.flush()
        grade.stage_name = stage_name(grade)
        for sidx, sname in enumerate(SECTION_NAMES, 1):
            if not Section.query.filter_by(grade_id=grade.id, name=sname).first():
                db.session.add(Section(grade_id=grade.id, name=sname, sort_order=sidx))
    db.session.commit()


def generate_student_username(phone, full_name):
    base = re.sub(r"[^A-Za-z0-9]", "", phone or "")
    if base:
        base = "STU" + base[-8:]
    else:
        base = "STU" + uuid.uuid4().hex[:8].upper()
    candidate = base
    n = 1
    while User.query.filter_by(username=candidate).first():
        candidate = base + str(n)
        n += 1
    return candidate


def normalize_student_name(value):
    """Normalize spacing and common Arabic letter variants for duplicate detection."""
    value = " ".join(str(value or "").split()).casefold()
    return value.translate(str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي"}))


def normalize_student_phone(value):
    """Normalize phone digits, including Arabic numerals and Egypt's +20 prefix."""
    value = str(value or "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789"))
    digits = re.sub(r"\D", "", value)
    if digits.startswith("0020") and len(digits) >= 12:
        digits = digits[4:]
    elif digits.startswith("20") and len(digits) >= 12:
        digits = digits[2:]
    return digits.lstrip("0")


def same_student_identity(a, b):
    """True only when both name and a non-empty normalized phone match."""
    phone_a = normalize_student_phone(a.phone)
    phone_b = normalize_student_phone(b.phone)
    return bool(phone_a and phone_b and phone_a == phone_b and
                normalize_student_name(a.full_name) == normalize_student_name(b.full_name))


def active_duplicate_for_phone(student, phone):
    """Find an active duplicate with the same normalized name and phone; archived accounts do not block edits."""
    normalized_phone = normalize_student_phone(phone)
    normalized_name = normalize_student_name(student.full_name)
    if not normalized_phone or not normalized_name:
        return None
    for other in User.query.filter_by(role="student", active=True).all():
        if other.id == student.id:
            continue
        if (normalize_student_name(other.full_name) == normalized_name and
                normalize_student_phone(other.phone) == normalized_phone):
            return other
    return None


def teacher_student_ids(teacher_id, academic_year_id=None):
    """Return students explicitly linked to a teacher plus students whose class/section
    is assigned to that teacher for the selected academic year. This keeps the roster
    usable even when the manager registered the student instead of the teacher."""
    ids = {row.student_id for row in TeacherStudent.query.filter_by(teacher_id=teacher_id).all()}
    assignments = TeacherAssignment.query.filter_by(teacher_id=teacher_id).all()
    if academic_year_id:
        assignments = [a for a in assignments if a.academic_year_id == academic_year_id]
    pairs = {(a.grade_id, a.section_id) for a in assignments}
    if pairs:
        students = User.query.filter_by(role="student", active=True).all()
        ids.update(s.id for s in students if (s.grade_id, s.section_id) in pairs)
    return ids


def auto_target_existing_exams_for_student(student):
    """Link a newly added student to matching existing exams for the same teacher/class."""
    if not student or student.role != "student" or not student.active:
        return 0
    teacher_ids = {row.teacher_id for row in TeacherStudent.query.filter_by(student_id=student.id).all()}
    if not teacher_ids:
        return 0
    exams = Exam.query.filter(Exam.teacher_id.in_(teacher_ids), Exam.grade_id == student.grade_id).all()
    added = 0
    for exam in exams:
        if exam.section_id and exam.section_id != student.section_id:
            continue
        if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student.id).first():
            db.session.add(ExamTarget(exam_id=exam.id, student_id=student.id))
            added += 1
    return added


def eligible_student_ids_for_exam(exam):
    ids = teacher_student_ids(exam.teacher_id, exam.academic_year_id)
    if not ids or not exam.grade_id:
        return set()
    query = User.query.filter(User.id.in_(ids), User.role == "student", User.active.is_(True), User.grade_id == exam.grade_id)
    if exam.section_id:
        query = query.filter(User.section_id == exam.section_id)
    return {s.id for s in query.all()}


def auto_target_exam(exam):
    """Default a new exam to the teacher's students in its selected grade/section."""
    for student_id in sorted(eligible_student_ids_for_exam(exam)):
        if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student_id).first():
            db.session.add(ExamTarget(exam_id=exam.id, student_id=student_id))


def sections_by_grade_payload(allowed_grade_ids=None, allowed_section_ids=None):
    payload = {}
    query = Section.query.order_by(Section.grade_id, Section.sort_order, Section.name)
    if allowed_grade_ids is not None:
        allowed_grade_ids = set(allowed_grade_ids)
        query = query.filter(Section.grade_id.in_(allowed_grade_ids)) if allowed_grade_ids else query.filter(Section.id == -1)
    if allowed_section_ids is not None:
        allowed_section_ids = set(allowed_section_ids)
        query = query.filter(Section.id.in_(allowed_section_ids)) if allowed_section_ids else query.filter(Section.id == -1)
    for sec in query.all():
        payload.setdefault(str(sec.grade_id), []).append({"id": sec.id, "name": sec.name})
    return payload


def matching_auto_score(points, student_answer, correct_json, options_json=None):
    """Score matching answers item-by-item for new questions; preserve the old mode for existing questions.

    New matching questions store points_mode='per_pair', so each correct match earns `points`.
    Older saved questions without that flag keep their old total-question-points calculation.
    """
    try:
        student_map = json.loads(student_answer or "{}")
        correct_map = json.loads(correct_json or "{}")
        pair_count = len(correct_map)
        if not pair_count:
            return 0.0
        correct_pairs = 0
        for left_index, correct_right_index in correct_map.items():
            chosen = student_map.get(str(left_index), student_map.get(left_index))
            if chosen is not None and str(chosen) == str(correct_right_index):
                correct_pairs += 1
        config = json.loads(options_json or "{}") if options_json else {}
        if config.get("points_mode") == "per_pair":
            return round(float(points) * correct_pairs, 2)
        return round(float(points) * correct_pairs / pair_count, 2)
    except (TypeError, ValueError, AttributeError):
        return 0.0


def question_max_points(question):
    """Maximum points for a question based on its saved scoring mode."""
    if question.qtype == "matching":
        try:
            data = json.loads(question.options_json or "{}")
            if data.get("points_mode") == "per_pair":
                return round(float(question.points) * len(data.get("left", [])), 2)
        except (TypeError, ValueError, AttributeError):
            pass
    return round(float(question.points), 2)


def exam_max_points(exam_id):
    return round(sum(question_max_points(q) for q in Question.query.filter_by(exam_id=exam_id).all()), 2)


def exam_auto_max_points(exam_id):
    return round(sum(question_max_points(q) for q in Question.query.filter_by(exam_id=exam_id).all() if q.qtype != "essay"), 2)


def safe_filename_part(value, limit=60):
    value = re.sub(r'[\\/:*?"<>|\r\n]+', "_", str(value or "")).strip(" ._")
    return (value or "غير_معروف")[:limit]



def save_browser_screenshot(data_url, attempt, exam, student):
    """Decode the PNG captured from the student's rendered exam page and store it as evidence."""
    if not data_url or not data_url.startswith("data:image/png;base64,"):
        return None
    encoded = data_url.split(",", 1)[1]
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        return None
    # Guard against unexpected files and oversized payloads (8 MB binary maximum).
    if not raw.startswith(b"\x89PNG\r\n\x1a\n") or len(raw) > 8 * 1024 * 1024:
        return None

    student_folder = UPLOAD_DIR / "screenshots" / str(exam.id) / str(student.id)
    student_folder.mkdir(parents=True, exist_ok=True)
    local_name = "attempt_{}.png".format(attempt.id)
    local_path = student_folder / local_name
    local_path.write_bytes(raw)

    # On a locally hosted Windows installation, also keep a convenient copy on the server PC's Desktop.
    # A hosted Linux service has no teacher's desktop, so the protected web link remains the access path there.
    if os.name == "nt":
        try:
            desktop_folder = Path.home() / "Desktop" / "صور إجابات الطلاب"
            desktop_folder.mkdir(parents=True, exist_ok=True)
            stamp = school_now().strftime("%Y%m%d_%H%M%S")
            filename = "{}_{}_{}_exam-{}_attempt-{}.png".format(
                stamp, safe_filename_part(student.full_name), safe_filename_part(exam.title), exam.id, attempt.id
            )
            shutil.copyfile(str(local_path), str(desktop_folder / filename))
        except (OSError, IOError):
            # Keep the main protected copy even if the optional Desktop copy is unavailable.
            pass
    return url_for("answer_snapshot", attempt_id=attempt.id)


@app.get("/answer-snapshot/<int:attempt_id>")
@login_required()
def answer_snapshot(attempt_id):
    """View a student's saved screen capture only for that student, their teacher, or the admin."""
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = Exam.query.get_or_404(attempt.exam_id)
    viewer = current_user()
    if viewer.role == "student" and viewer.id != attempt.student_id:
        abort(403)
    if viewer.role == "teacher" and viewer.id != exam.teacher_id:
        abort(403)
    if viewer.role not in ("admin", "teacher", "student"):
        abort(403)
    path = UPLOAD_DIR / "screenshots" / str(exam.id) / str(attempt.student_id) / ("attempt_{}.png".format(attempt.id))
    if not path.is_file():
        abort(404)
    return send_file(str(path), mimetype="image/png", as_attachment=False)

def save_snapshot(attempt, exam, student):
    answers = json.loads(attempt.answers_json or "{}")
    teacher = User.query.get(exam.teacher_id)
    questions = Question.query.filter_by(exam_id=exam.id).order_by(Question.number).all()
    lines = [
        "المدرسة: " + school_name(),
        "المعلم: " + (teacher.full_name if teacher else "—"),
        "التخصص: " + (Subject.query.get(exam.subject_id).name if Subject.query.get(exam.subject_id) else "—"),
        "الطالب: " + student.full_name,
        "الامتحان: " + exam.title,
        "التاريخ: " + dt.datetime.now().strftime("%d/%m/%Y %H:%M"),
    ]
    y = 70
    texts = []
    for line in lines:
        texts.append('<text x="1140" y="{}" text-anchor="end" font-size="28">{}</text>'.format(y, html.escape(line)))
        y += 46
    y += 10
    for q in questions:
        raw = str(answers.get(str(q.id), "—"))
        if q.qtype == "matching":
            try:
                data = json.loads(q.options_json or "{}")
                chosen = json.loads(raw or "{}")
                left_items = data.get("left", [])
                right_items = data.get("right", [])
                readable = []
                for left_index, answer_index in chosen.items():
                    li = int(left_index)
                    ri = int(answer_index)
                    if 0 <= li < len(left_items) and 0 <= ri < len(right_items):
                        readable.append("{} ↔ {}".format(left_items[li], right_items[ri]))
                raw = "؛ ".join(readable) if readable else "لم تتم الإجابة"
            except Exception:
                raw = "تعذر عرض تفاصيل التوصيل"
        if len(raw) > 140:
            raw = raw[:137] + "..."
        texts.append('<text x="1140" y="{}" text-anchor="end" font-size="24">س{}: {}</text>'.format(y, q.number, html.escape(q.prompt[:90])))
        y += 36
        texts.append('<text x="1100" y="{}" text-anchor="end" font-size="22">الإجابة: {}</text>'.format(y, html.escape(raw)))
        y += 40
    height = max(240, y + 40)
    data = ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="{}" viewBox="0 0 1200 {}">'
            '<rect width="1200" height="{}" fill="#fffdf7"/>'
            '<rect x="25" y="25" width="1150" height="{}" rx="18" fill="none" stroke="#999"/>{}</svg>').format(height, height, height, height - 50, ''.join(texts)).encode("utf-8")
    path = UPLOAD_DIR / "answers" / str(exam.id) / str(student.id) / (uuid.uuid4().hex + ".svg")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return "/local-snapshot/" + str(path.relative_to(UPLOAD_DIR)).replace(os.sep, "/")


@app.get("/local-snapshot/<path:relpath>")
@login_required()
def local_snapshot(relpath):
    path = (UPLOAD_DIR / relpath).resolve()
    if UPLOAD_DIR.resolve() not in path.parents or not path.is_file():
        abort(404)
    viewer = current_user()
    old_url = request.path
    attempt = Attempt.query.filter_by(answer_snapshot_url=old_url).first()
    if not attempt:
        abort(404)
    exam = Exam.query.get_or_404(attempt.exam_id)
    if viewer.role == "student" and viewer.id != attempt.student_id:
        abort(403)
    if viewer.role == "teacher" and viewer.id != exam.teacher_id:
        abort(403)
    if viewer.role not in ("admin", "teacher", "student"):
        abort(403)
    return send_file(str(path), mimetype="image/svg+xml")


@app.route("/", methods=["GET"])
def home():
    user = current_user()
    if not user:
        return redirect(url_for("login"))
    target = {"admin": "admin_dashboard", "teacher": "teacher_dashboard", "student": "student_dashboard"}.get(user.role)
    return redirect(url_for(target or "login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        # وضع التجربة الأولية: تسجيل الدخول باسم المستخدم فقط دون طلب كلمة مرور.
        # يُعاد تفعيل التحقق بكلمة المرور بعد انتهاء الاختبارات بموافقة المستخدم.
        user = User.query.filter_by(username=username, active=True).first()
        if not user:
            flash("اسم المستخدم غير موجود أو أن الحساب غير نشط. راجع الاسم وحاول مرة أخرى.", "error")
            accounts = User.query.filter_by(active=True).order_by(User.role, User.full_name).all()
            return render_template("login.html", accounts=accounts)
        session.clear()
        session["user_id"] = user.id
        return home()
    accounts = User.query.filter_by(active=True).order_by(User.role, User.full_name).all()
    return render_template("login.html", accounts=accounts)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/admin")
@login_required("admin")
def admin_dashboard():
    # Keep inactive accounts out of normal operational lists; show them only in the collapsed archive.
    users = User.query.filter_by(active=True).order_by(User.role, User.full_name).all()
    archived_users = User.query.filter_by(active=False).order_by(User.role, User.full_name).all()
    subjects = Subject.query.filter_by(active=True).order_by(Subject.name).all()
    grades = Grade.query.order_by(Grade.sort_order).all()
    sections = Section.query.order_by(Section.grade_id, Section.sort_order, Section.name).all()
    years = AcademicYear.query.filter_by(active=True).order_by(AcademicYear.sort_order, AcademicYear.name).all()
    teachers = User.query.filter_by(role="teacher", active=True).order_by(User.full_name).all()
    exams = Exam.query.order_by(Exam.id.desc()).limit(100).all()
    students = User.query.filter_by(role="student", active=True).order_by(User.grade_id, User.section_id, User.full_name).all()
    assignment_rows = TeacherAssignment.query.order_by(TeacherAssignment.teacher_id, TeacherAssignment.academic_year_id, TeacherAssignment.grade_id, TeacherAssignment.section_id).all()
    teacher_map = {u.id: u for u in teachers}
    grade_map = {g.id: g for g in grades}
    section_map = {s.id: s for s in sections}
    year_map = {y.id: y for y in years}
    subject_map = {s.id: s for s in subjects}
    exam_rows = []
    for e in exams:
        exam_rows.append({
            "exam": e, "teacher": teacher_map.get(e.teacher_id), "subject": subject_map.get(e.subject_id),
            "grade": grade_map.get(e.grade_id), "section": section_map.get(e.section_id),
            "year": year_map.get(e.academic_year_id)
        })
    latest_attempts = Attempt.query.filter(Attempt.submitted_at.isnot(None)).order_by(Attempt.submitted_at.desc()).limit(100).all()
    attempt_rows = []
    for attempt in latest_attempts:
        attempt_exam = Exam.query.get(attempt.exam_id)
        attempt_student = User.query.get(attempt.student_id)
        attempt_teacher = User.query.get(attempt_exam.teacher_id) if attempt_exam else None
        attempt_rows.append({"attempt": attempt, "exam": attempt_exam, "student": attempt_student, "teacher": attempt_teacher,
                             "max_score": exam_max_points(attempt.exam_id) if attempt_exam else 0,
                             "auto_max_score": exam_auto_max_points(attempt.exam_id) if attempt_exam else 0})
    assignment_rows_view = []
    for a in assignment_rows:
        assignment_rows_view.append({"row": a, "teacher": teacher_map.get(a.teacher_id) or User.query.get(a.teacher_id),
                                     "year": year_map.get(a.academic_year_id) or AcademicYear.query.get(a.academic_year_id),
                                     "grade": grade_map.get(a.grade_id) or Grade.query.get(a.grade_id),
                                     "section": section_map.get(a.section_id) or Section.query.get(a.section_id)})
    return render_template("admin.html", users=users, archived_users=archived_users, subjects=subjects, grades=grades, sections=sections, years=years,
                           teachers=teachers, students=students, exams=exams, exam_rows=exam_rows,
                           assignment_rows=assignment_rows_view, roles=ROLE_AR, section_payload=sections_by_grade_payload(),
                           school_name=school_name(), attempt_rows=attempt_rows)


@app.post("/admin/settings")
@login_required("admin")
def admin_settings():
    s = SchoolSetting.query.first()
    if not s:
        s = SchoolSetting()
        db.session.add(s)
    s.school_name = request.form.get("school_name", "").strip() or "اسم المدرسة"
    db.session.commit()
    flash("تم حفظ اسم المدرسة.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/year")
@login_required("admin")
def admin_year():
    name = request.form.get("name", "").strip()
    if name and not AcademicYear.query.filter_by(name=name).first():
        max_order = db.session.query(db.func.max(AcademicYear.sort_order)).scalar() or 0
        db.session.add(AcademicYear(name=name, sort_order=max_order + 1, active=True))
        db.session.commit()
        flash("تمت إضافة السنة الدراسية.", "ok")
    else:
        flash("السنة موجودة بالفعل أو غير مكتملة.", "error")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/subject")
@login_required("admin")
def admin_subject():
    name = request.form.get("name", "").strip()
    if name and not Subject.query.filter_by(name=name).first():
        db.session.add(Subject(name=name, active=True))
        db.session.commit()
        flash("تمت إضافة المادة.", "ok")
    else:
        flash("المادة موجودة بالفعل أو غير مكتملة.", "error")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/section")
@login_required("admin")
def admin_section():
    try:
        grade_id = int(request.form.get("grade_id", ""))
    except Exception:
        grade_id = 0
    name = request.form.get("name", "").strip()
    if grade_id and name and Grade.query.get(grade_id) and not Section.query.filter_by(grade_id=grade_id, name=name).first():
        db.session.add(Section(grade_id=grade_id, name=name, sort_order=(db.session.query(db.func.max(Section.sort_order)).filter_by(grade_id=grade_id).scalar() or 0) + 1))
        db.session.commit()
        flash("تمت إضافة الفصل.", "ok")
    else:
        flash("تحقق من الصف واسم الفصل.", "error")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/user")
@login_required("admin")
def admin_user():
    username = request.form.get("username", "").strip()
    full_name = request.form.get("full_name", "").strip()
    password = request.form.get("password", "")
    role = request.form.get("role", "student")
    phone = request.form.get("phone", "").strip()
    try:
        subject_id = int(request.form.get("subject_id", "")) if request.form.get("subject_id") else None
        grade_id = int(request.form.get("grade_id", "")) if request.form.get("grade_id") else None
        section_id = int(request.form.get("section_id", "")) if request.form.get("section_id") else None
    except Exception:
        subject_id = grade_id = section_id = None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
        flash("اسم المستخدم إنجليزي وأرقام فقط مع . _ - .", "error")
        return redirect(url_for("admin_dashboard"))
    if not full_name or role not in ROLE_AR or len(password) < 8:
        flash("أكمل الاسم وكلمة المرور ونوع الحساب.", "error")
        return redirect(url_for("admin_dashboard"))
    if User.query.filter_by(username=username).first():
        flash("اسم المستخدم موجود بالفعل.", "error")
        return redirect(url_for("admin_dashboard"))
    if role == "student" and phone:
        duplicate = next((u for u in User.query.filter_by(role="student", active=True).all()
                          if normalize_student_name(u.full_name) == normalize_student_name(full_name)
                          and normalize_student_phone(u.phone) == normalize_student_phone(phone)
                          and normalize_student_phone(phone)), None)
        if duplicate:
            flash("هذا الطالب مسجل بالفعل بالاسم والهاتف نفسيهما تحت اسم المستخدم: {}. لا تنشئ نسخة ثانية؛ استخدم ربط طالب موجود من ملف المعلم.".format(duplicate.username), "error")
            return redirect(url_for("admin_dashboard"))
    db.session.add(User(username=username, full_name=full_name, password_hash=generate_password_hash(password), role=role,
                        subject_id=subject_id if role == "teacher" else None,
                        grade_id=grade_id if role == "student" else None,
                        section_id=section_id if role == "student" else None, phone=phone or None))
    db.session.commit()
    flash("تم تسجيل البيانات وحفظها في قاعدة البيانات.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/teacher-assignment")
@login_required("admin")
def admin_teacher_assignment():
    try:
        teacher_id = int(request.form.get("teacher_id", "0"))
        year_id = int(request.form.get("academic_year_id", "0"))
        grade_id = int(request.form.get("grade_id", "0"))
    except Exception:
        flash("اختر المعلم والسنة والصف.", "error")
        return redirect(url_for("admin_dashboard"))
    teacher = User.query.filter_by(id=teacher_id, role="teacher", active=True).first()
    year = AcademicYear.query.get(year_id)
    grade = Grade.query.get(grade_id)
    selected = []
    for raw in request.form.getlist("section_ids"):
        if raw.isdigit():
            sec = Section.query.filter_by(id=int(raw), grade_id=grade_id).first()
            if sec:
                selected.append(sec.id)
    remove = request.form.get("remove") == "1"
    if not teacher or not year or not grade:
        flash("بيانات الإسناد غير صحيحة.", "error")
        return redirect(url_for("admin_dashboard"))
    TeacherAssignment.query.filter_by(teacher_id=teacher.id, academic_year_id=year.id, grade_id=grade.id).delete(synchronize_session=False)
    if not remove:
        for sid in selected:
            db.session.add(TeacherAssignment(teacher_id=teacher.id, academic_year_id=year.id, grade_id=grade.id, section_id=sid))
    db.session.commit()
    flash("تم حفظ / تعديل إسناد الفصول للمعلم." if not remove else "تمت إزالة إسناد الصف والسنة للمعلم.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/exam")
@login_required("admin")
def admin_create_exam():
    title = request.form.get("title", "").strip()
    try:
        teacher_id = int(request.form.get("teacher_id", ""))
        subject_id = int(request.form.get("subject_id", ""))
        grade_id = int(request.form.get("grade_id", ""))
        year_id = int(request.form.get("academic_year_id", ""))
        duration = max(1, int(request.form.get("duration", "30") or 30))
    except Exception:
        flash("اختر المدرس والمادة والسنة والصف والمدة.", "error")
        return redirect(url_for("admin_dashboard"))
    section_raw = request.form.get("section_id", "")
    section_id = int(section_raw) if section_raw.isdigit() and int(section_raw) > 0 else None
    if section_id and not Section.query.filter_by(id=section_id, grade_id=grade_id).first():
        flash("الفصل لا يتبع الصف المختار.", "error")
        return redirect(url_for("admin_dashboard"))
    teacher = User.query.filter_by(id=teacher_id, role="teacher", active=True).first()
    subject = Subject.query.get(subject_id); grade = Grade.query.get(grade_id); year = AcademicYear.query.get(year_id)
    if not title or not teacher or not subject or not grade or not year:
        flash("بيانات إنشاء الامتحان غير مكتملة.", "error")
        return redirect(url_for("admin_dashboard"))
    publish_at = parse_dt(request.form.get("publish_at", "")); close_at = parse_dt(request.form.get("close_at", ""))
    if publish_at and close_at and close_at <= publish_at:
        flash("وقت الإغلاق يجب أن يكون بعد وقت الفتح.", "error")
        return redirect(url_for("admin_dashboard"))
    exam = Exam(teacher_id=teacher.id, subject_id=subject.id, title=title, grade_id=grade.id, section_id=section_id,
                academic_year_id=year.id, duration_minutes=duration, publish_at=publish_at, close_at=close_at,
                active=False, status="scheduled" if publish_at else "draft")
    db.session.add(exam); db.session.commit()
    auto_target_exam(exam)
    db.session.commit()
    flash("تم إنشاء الامتحان من المدير، وتم استهداف طلاب الصف/الفصل تلقائيًا ويمكن تعديل المجموعة لاحقًا.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/change-own-password")
@login_required("admin")
def admin_change_own_password():
    user = current_user()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")
    if not check_password_hash(user.password_hash, current_password):
        flash("كلمة المرور الحالية غير صحيحة.", "error")
    elif len(new_password) < 8:
        flash("كلمة المرور الجديدة يجب ألا تقل عن 8 أحرف.", "error")
    elif new_password != confirm_password:
        flash("تأكيد كلمة المرور الجديدة غير مطابق.", "error")
    else:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash("تم تغيير كلمة مرور المدير بنجاح. حدّث كلمة المرور المحفوظة في المتصفح إذا ظهرت مطالبة بذلك.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/reset-password")
@login_required("admin")
def admin_reset_password():
    user = User.query.get(int(request.form.get("user_id", "0") or 0))
    new_password = request.form.get("new_password", "")
    if not user or len(new_password) < 8:
        flash("تعذر تغيير كلمة المرور.", "error")
    else:
        user.password_hash = generate_password_hash(new_password)
        db.session.commit()
        flash("تم تغيير كلمة المرور.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/teacher/student")
@login_required("teacher")
def teacher_add_student():
    teacher = current_user()
    full_name = request.form.get("full_name", "").strip()
    phone = request.form.get("phone", "").strip()
    password = request.form.get("password", "Student123!").strip()
    username = request.form.get("username", "").strip() or generate_student_username(phone, full_name)
    try:
        grade_id = int(request.form.get("grade_id", ""))
        section_id = int(request.form.get("section_id", ""))
    except Exception:
        grade_id = section_id = 0
    section = Section.query.filter_by(id=section_id, grade_id=grade_id).first() if grade_id and section_id else None
    if not full_name or not grade_id or not section:
        flash("اكتب اسم الطالب واختر الصف والفصل الصحيحين.", "error")
        return redirect(url_for("teacher_dashboard"))
    if not phone:
        flash("اكتب رقم هاتف الطالب.", "error")
        return redirect(url_for("teacher_dashboard"))
    if User.query.filter_by(username=username).first():
        flash("كود/اسم المستخدم مستخدم بالفعل.", "error")
        return redirect(url_for("teacher_dashboard"))
    duplicate = next((u for u in User.query.filter_by(role="student", active=True).all()
                      if normalize_student_name(u.full_name) == normalize_student_name(full_name)
                      and normalize_student_phone(u.phone) == normalize_student_phone(phone)
                      and normalize_student_phone(phone)), None)
    if duplicate:
        flash("هذا الطالب مسجل بالفعل تحت اسم المستخدم {}. استخدم «ربط طالب موجود» واختر الحساب المطلوب بدل إنشاء حساب مكرر.".format(duplicate.username), "error")
        return redirect(url_for("teacher_dashboard"))
    if len(password) < 8:
        flash("كلمة مرور الطالب يجب أن تكون 8 أحرف على الأقل.", "error")
        return redirect(url_for("teacher_dashboard"))
    student = User(username=username, full_name=full_name, password_hash=generate_password_hash(password), role="student",
                   phone=phone, grade_id=grade_id, section_id=section_id, active=True)
    db.session.add(student)
    db.session.flush()
    db.session.add(TeacherStudent(teacher_id=teacher.id, student_id=student.id))
    db.session.commit()
    auto_target_existing_exams_for_student(student)
    db.session.commit()
    flash("تم حفظ الطالب وربطه بملف المعلم. كود الدخول: {}".format(username), "ok")
    return redirect(url_for("teacher_dashboard"))


@app.post("/teacher/student/link-existing")
@login_required("teacher")
def teacher_link_existing_student():
    """Link a specific existing account to the logged-in teacher; resolve matching duplicates."""
    teacher = current_user()
    try:
        student_id = int(request.form.get("student_id", "0"))
    except (TypeError, ValueError):
        student_id = 0
    student = User.query.filter_by(id=student_id, role="student", active=True).first()
    if not student:
        flash("الحساب المختار غير موجود أو موقوف. اطلب من المدير تفعيله أولًا.", "error")
        return redirect(url_for("teacher_dashboard"))

    # The selected username is the canonical account. If duplicate active
    # accounts have the same full name and phone, deactivate the others and
    # remove their current targeting links, without deleting past attempts.
    duplicates = [u for u in User.query.filter_by(role="student", active=True).all()
                  if u.id != student.id and same_student_identity(u, student)]
    for duplicate in duplicates:
        duplicate.active = False
        ExamTarget.query.filter_by(student_id=duplicate.id).delete(synchronize_session=False)
        TeacherStudent.query.filter_by(student_id=duplicate.id).delete(synchronize_session=False)

    link = TeacherStudent.query.filter_by(teacher_id=teacher.id, student_id=student.id).first()
    if not link:
        db.session.add(TeacherStudent(teacher_id=teacher.id, student_id=student.id))
    added_targets = auto_target_existing_exams_for_student(student)
    db.session.commit()
    if duplicates:
        old_codes = ", ".join(d.username for d in duplicates)
        flash("تم ربط {} بالمعلمة {}. تم إيقاف الحساب/الحسابات المكررة ({}) مع الاحتفاظ بمحاولاتها ونتائجها القديمة. أضيف الحساب المختار إلى {} امتحانًا مطابقًا.".format(
            student.username, teacher.full_name, old_codes, added_targets), "ok")
    else:
        flash("تم تثبيت الحساب {} في ملف المعلمة {}، وأضيف إلى {} امتحانًا مطابقًا إن وُجدت.".format(
            student.username, teacher.full_name, added_targets), "ok")
    return redirect(url_for("teacher_dashboard"))


@app.post("/teacher/student/<int:student_id>/remove")
@login_required("teacher")
def teacher_remove_student(student_id):
    teacher = current_user()
    link = TeacherStudent.query.filter_by(teacher_id=teacher.id, student_id=student_id).first()
    if not link:
        flash("الطالب غير موجود في ملفك.", "error")
        return redirect(url_for("teacher_dashboard"))
    db.session.delete(link)
    db.session.commit()
    flash("تمت إزالة الطالب من ملف المعلم فقط، ولم تُحذف نتائجه القديمة.", "ok")
    return redirect(url_for("teacher_dashboard"))


@app.post("/admin/student/<int:student_id>/phone")
@login_required("admin")
def admin_update_student_phone(student_id):
    student = User.query.filter_by(id=student_id, role="student", active=True).first_or_404()
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("اكتب رقم هاتف الطالب قبل الحفظ.", "error")
        return redirect(url_for("admin_dashboard"))
    duplicate = active_duplicate_for_phone(student, phone)
    if duplicate:
        flash("هذا الاسم ورقم الهاتف مستخدمان بالفعل في الحساب {}؛ لم يتم تغيير الهاتف.".format(duplicate.username), "error")
        return redirect(url_for("admin_dashboard"))
    student.phone = phone
    db.session.commit()
    flash("تم حفظ رقم هاتف الطالب {} بنجاح.".format(student.full_name), "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/teacher/student/<int:student_id>/phone")
@login_required("teacher")
def teacher_update_student_phone(student_id):
    teacher = current_user()
    student = User.query.filter_by(id=student_id, role="student", active=True).first_or_404()
    if student.id not in teacher_student_ids(teacher.id):
        abort(403)
    phone = request.form.get("phone", "").strip()
    if not phone:
        flash("اكتب رقم هاتف الطالب قبل الحفظ.", "error")
        return redirect(url_for("teacher_dashboard"))
    duplicate = active_duplicate_for_phone(student, phone)
    if duplicate:
        flash("هذا الاسم ورقم الهاتف مستخدمان بالفعل في الحساب {}؛ لم يتم تغيير الهاتف.".format(duplicate.username), "error")
        return redirect(url_for("teacher_dashboard"))
    student.phone = phone
    db.session.commit()
    flash("تم حفظ رقم هاتف الطالب {} بنجاح.".format(student.full_name), "ok")
    return redirect(url_for("teacher_dashboard"))


@app.post("/admin/student/<int:student_id>/delete")
@login_required("admin")
def admin_delete_student(student_id):
    student = User.query.filter_by(id=student_id, role="student").first_or_404()
    if not student.active:
        flash("الطالب محذوف/موقوف بالفعل.", "error")
        return redirect(url_for("admin_dashboard"))
    student.active = False
    ExamTarget.query.filter_by(student_id=student.id).delete(synchronize_session=False)
    TeacherStudent.query.filter_by(student_id=student.id).delete(synchronize_session=False)
    db.session.commit()
    flash("تم إيقاف حساب الطالب وإزالته من المجموعات الحالية، مع الاحتفاظ بالنتائج القديمة.", "ok")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/student/<int:student_id>/activate")
@login_required("admin")
def admin_activate_student(student_id):
    student = User.query.filter_by(id=student_id, role="student").first_or_404()
    if student.active:
        flash("حساب الطالب نشط بالفعل.", "error")
    else:
        student.active = True
        db.session.commit()
        flash("تمت إعادة تفعيل الحساب {}. إذا أردت ربطه بالمعلم، استخدم «ربط طالب موجود» من ملف المعلم.".format(student.username), "ok")
    return redirect(url_for("admin_dashboard"))


@app.get("/teacher")
@login_required("teacher")
def teacher_dashboard():
    teacher = current_user()
    # Show the newest active academic year first so old assignments don't take
    # precedence over the current school year in the teacher's default fields.
    years = AcademicYear.query.filter_by(active=True).order_by(AcademicYear.sort_order.desc(), AcademicYear.id.desc()).all()
    grades = Grade.query.order_by(Grade.sort_order).all()
    sections = Section.query.order_by(Section.grade_id, Section.sort_order).all()
    subjects = Subject.query.filter_by(active=True).order_by(Subject.name).all()
    linked_ids = teacher_student_ids(teacher.id)
    if linked_ids:
        students = User.query.filter(User.id.in_(linked_ids), User.role == "student", User.active.is_(True)).order_by(User.grade_id, User.section_id, User.full_name).all()
    else:
        students = []

    all_assignments = TeacherAssignment.query.filter_by(teacher_id=teacher.id).all()
    current_year_id = years[0].id if years else None
    assignments = [a for a in all_assignments if a.academic_year_id == current_year_id] if current_year_id else []
    # Prefer the latest year's actual assignments. If they haven't been entered,
    # show the grades/sections found in this teacher's roster rather than the
    # first grade in the school's entire grade list.
    assignment_grade_ids = set(a.grade_id for a in assignments)
    assignment_section_ids = set(a.section_id for a in assignments)
    roster_grade_ids = set(s.grade_id for s in students if s.grade_id)
    roster_section_ids = set(s.section_id for s in students if s.section_id)
    if assignments or students:
        # Include actual roster grades as well as latest-year assignments. This
        # avoids defaulting to an old/stale assigned class when the teacher's
        # current students are in another grade.
        available_grade_ids = sorted(assignment_grade_ids | roster_grade_ids)
        available_section_ids = sorted(assignment_section_ids | roster_section_ids)
    elif all_assignments:
        latest_assigned_year_id = max(all_assignments, key=lambda a: a.academic_year_id).academic_year_id
        latest_assignments = [a for a in all_assignments if a.academic_year_id == latest_assigned_year_id]
        available_grade_ids = sorted(set(a.grade_id for a in latest_assignments))
        available_section_ids = sorted(set(a.section_id for a in latest_assignments))
    else:
        # Initial setup only: allow choices until the manager assigns a class.
        available_grade_ids = [g.id for g in grades]
        available_section_ids = [sec.id for sec in sections]
    available_grades = [g for g in grades if g.id in set(available_grade_ids)]
    available_sections = [sec for sec in sections if sec.id in set(available_section_ids)]
    teacher_grade_names = [g.name for g in available_grades]
    # Prefer the current student roster for the initially selected grade.
    # If the roster contains one grade, select it even when an older assignment
    # appears earlier in alphabetical/sort order.
    if len(roster_grade_ids) == 1:
        preferred_grade_id = next(iter(roster_grade_ids))
    elif len(available_grade_ids) == 1:
        preferred_grade_id = available_grade_ids[0]
    elif assignments:
        preferred_grade_id = min(assignment_grade_ids)
    else:
        preferred_grade_id = available_grade_ids[0] if available_grade_ids else None
    preferred_grade = Grade.query.get(preferred_grade_id) if preferred_grade_id else None
    preferred_grade_name = preferred_grade.name if preferred_grade else "غير محدد"

    # Show all active student accounts in a clearly labeled selector so the
    # teacher can choose an existing account by username when names are duplicated.
    existing_student_accounts = User.query.filter_by(role="student", active=True).order_by(User.full_name, User.username).all()
    folders = TeacherFolder.query.filter_by(teacher_id=teacher.id).order_by(TeacherFolder.folder_type, TeacherFolder.name).all()
    exams = Exam.query.filter_by(teacher_id=teacher.id).order_by(Exam.id.desc()).all()
    attempts = Attempt.query.join(Exam, Attempt.exam_id == Exam.id).filter(Exam.teacher_id == teacher.id, Attempt.submitted_at.isnot(None)).order_by(Attempt.submitted_at.desc()).limit(100).all()
    student_by_id = {s.id: s for s in students}
    historical_student_ids = {a.student_id for a in attempts}
    if historical_student_ids:
        historical_students = User.query.filter(User.id.in_(historical_student_ids)).all()
        student_by_id.update({s.id: s for s in historical_students})
    exam_by_id = {e.id: e for e in exams}
    exam_points_by_id = {e.id: exam_max_points(e.id) for e in exams}
    exam_auto_points_by_id = {e.id: exam_auto_max_points(e.id) for e in exams}
    return render_template("teacher.html", teacher=teacher, years=years, grades=grades, sections=sections, subjects=subjects,
                           available_grades=available_grades, available_sections=available_sections,
                           teacher_grade_names=teacher_grade_names, preferred_grade_id=preferred_grade_id,
                           preferred_grade_name=preferred_grade_name, students=students,
                           existing_student_accounts=existing_student_accounts, folders=folders, exams=exams,
                           attempts=attempts, student_by_id=student_by_id, exam_by_id=exam_by_id, exam_points_by_id=exam_points_by_id,
                           exam_auto_points_by_id=exam_auto_points_by_id,
                           section_payload=sections_by_grade_payload(available_grade_ids, available_section_ids))


@app.post("/teacher/folder")
@login_required("teacher")
def teacher_folder():
    name = request.form.get("name", "").strip()
    folder_type = request.form.get("folder_type", "custom")
    if name:
        db.session.add(TeacherFolder(teacher_id=current_user().id, name=name, folder_type=folder_type))
        db.session.commit()
        flash("تم حفظ المجلد.", "ok")
    return redirect(url_for("teacher_dashboard"))


@app.post("/teacher/exam")
@login_required("teacher")
def teacher_create_exam():
    teacher = current_user()
    title = request.form.get("title", "").strip()
    try:
        subject_id = int(request.form.get("subject_id", ""))
        grade_id = int(request.form.get("grade_id", ""))
        year_id = int(request.form.get("academic_year_id", ""))
        duration = max(1, int(request.form.get("duration", "30") or 30))
    except Exception:
        flash("اختر المادة والصف والسنة والمدة.", "error")
        return redirect(url_for("teacher_dashboard"))
    section_raw = request.form.get("section_id", "")
    section_id = int(section_raw) if section_raw.isdigit() and int(section_raw) > 0 else None
    if not title or not Subject.query.get(subject_id) or not Grade.query.get(grade_id) or not AcademicYear.query.get(year_id):
        flash("بيانات إنشاء الامتحان غير مكتملة.", "error")
        return redirect(url_for("teacher_dashboard"))
    if section_id and not Section.query.filter_by(id=section_id, grade_id=grade_id).first():
        flash("الفصل المختار لا يتبع الصف الدراسي المحدد. اختر الصف أولًا ثم الفصل التابع له.", "error")
        return redirect(url_for("teacher_dashboard"))
    publish_at = parse_dt(request.form.get("publish_at", ""))
    close_at = parse_dt(request.form.get("close_at", ""))
    if publish_at and close_at and close_at <= publish_at:
        flash("وقت الإغلاق يجب أن يكون بعد وقت الفتح.", "error")
        return redirect(url_for("teacher_dashboard"))
    exam = Exam(teacher_id=teacher.id, subject_id=subject_id, title=title, grade_id=grade_id, section_id=section_id,
                academic_year_id=year_id, duration_minutes=duration, publish_at=publish_at, close_at=close_at,
                active=False, status="scheduled" if publish_at else "draft")
    folder_id = request.form.get("folder_id", "")
    if folder_id.isdigit():
        folder = TeacherFolder.query.filter_by(id=int(folder_id), teacher_id=teacher.id).first()
        if folder:
            exam.folder_id = folder.id
    db.session.add(exam)
    db.session.commit()
    auto_target_exam(exam)
    db.session.commit()
    flash("تم حفظ الامتحان. تمت إضافة طلاب المعلم في الصف/الفصل تلقائيًا، ويمكنك تعديل المجموعة من صفحة الإدارة.", "ok")
    return redirect(url_for("teacher_exam_edit", exam_id=exam.id))


@app.route("/teacher/exam/<int:exam_id>", methods=["GET", "POST"])
@login_required("teacher")
def teacher_exam_edit(exam_id):
    teacher = current_user()
    exam = Exam.query.filter_by(id=exam_id, teacher_id=teacher.id).first_or_404()
    if request.method == "POST":
        qtype = request.form.get("qtype", "")
        prompt = request.form.get("prompt", "").strip()
        try:
            points = float(request.form.get("points", "1"))
        except Exception:
            points = 1
        if not prompt or qtype not in ("mcq", "matching", "essay") or points <= 0:
            flash("بيانات السؤال غير مكتملة.", "error")
            return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
        questions = Question.query.filter_by(exam_id=exam.id).order_by(Question.number).all()
        number = questions[-1].number + 1 if questions else 1
        if qtype == "mcq":
            opts = []
            for idx in range(4):
                value = request.form.get("opt_{}".format(idx), "").strip()
                if value:
                    opts.append(value)
            # دعم النموذج القديم أيضًا إن أُرسل.
            if not opts:
                opts = [x.strip() for x in request.form.get("options", "").split("|") if x.strip()]
            try:
                correct = int(request.form.get("correct_index", "0"))
            except Exception:
                correct = -1
            if len(opts) < 2 or correct < 0 or correct >= len(opts):
                flash("اكتب الاختيارات وحدد الإجابة الصحيحة من صناديق الاختيارات.", "error")
                return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
            options_json = json.dumps(opts, ensure_ascii=False)
            correct_json = json.dumps(correct)
        elif qtype == "matching":
            left = []
            right = []
            mapping = {}
            # واجهة التوصيل المبسطة: كل صف في محرر المعلم يمثل زوجًا صحيحًا.
            # الاحتفاظ بدعم النماذج القديمة التي ترسل match_correct_* أو pairs.
            legacy_mapping_form = any("match_correct_{}".format(i) in request.form for i in range(4))
            for idx in range(4):
                lv = request.form.get("match_left_{}".format(idx), "").strip()
                rv = request.form.get("match_right_{}".format(idx), "").strip()
                if bool(lv) != bool(rv):
                    flash("في سؤال التوصيل اكتب العنصر ومطابقه في الصف نفسه، أو اترك الخانتين فارغتين.", "error")
                    return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
                if lv and rv:
                    pair_index = len(left)
                    left.append(lv)
                    right.append(rv)
                    if not legacy_mapping_form:
                        mapping[str(pair_index)] = str(pair_index)
            if not left and not right:
                left = [x.strip() for x in request.form.get("left_items", "").split("|") if x.strip()]
                right = [x.strip() for x in request.form.get("right_items", "").split("|") if x.strip()]
                old_pairs = request.form.get("pairs", "{}").strip() or "{}"
                try:
                    mapping = json.loads(old_pairs)
                except Exception:
                    mapping = {}
            elif legacy_mapping_form:
                for idx, _ in enumerate(left):
                    raw = request.form.get("match_correct_{}".format(idx), "")
                    if raw.isdigit():
                        mapping[str(idx)] = raw
            if len(left) < 2 or len(right) < 2:
                flash("سؤال التوصيل يحتاج عنصرين على الأقل في الجانبين.", "error")
                return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
            if len(mapping) < len(left):
                flash("تأكد من اكتمال كل أزواج سؤال التوصيل.", "error")
                return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
            correct_json = json.dumps(mapping, ensure_ascii=False)
            # New behavior: the visible points field is the value of EACH correct match.
            options_json = json.dumps({"left": left, "right": right, "points_mode": "per_pair"}, ensure_ascii=False)
        else:
            options_json = "[]"
            correct_json = None
        db.session.add(Question(exam_id=exam.id, number=number, qtype=qtype, prompt=prompt,
                                options_json=options_json, correct_json=correct_json, points=points))
        db.session.commit()
        flash("تمت إضافة السؤال وحفظه.", "ok")
        return redirect(url_for("teacher_exam_edit", exam_id=exam.id))

    questions = Question.query.filter_by(exam_id=exam.id).order_by(Question.number).all()
    students = User.query.filter_by(role="student", active=True).order_by(User.grade_id, User.section_id, User.full_name).all()
    targets = {x.student_id for x in ExamTarget.query.filter_by(exam_id=exam.id).all()}
    years = AcademicYear.query.filter_by(active=True).order_by(AcademicYear.sort_order).all()
    grades = Grade.query.order_by(Grade.sort_order).all()
    sections = Section.query.order_by(Section.grade_id, Section.sort_order).all()
    return render_template("exam_edit.html", exam=exam, questions=questions, students=students, targets=targets,
                           years=years, grades=grades, sections=sections, section_payload=sections_by_grade_payload(),
                           question_points={q.id: question_max_points(q) for q in questions},
                           matching_modes={q.id: (json.loads(q.options_json or "{}").get("points_mode") == "per_pair") if q.qtype == "matching" else False for q in questions})


@app.post("/teacher/exam/<int:exam_id>/targets")
@login_required("teacher")
def teacher_save_targets(exam_id):
    teacher = current_user()
    exam = Exam.query.filter_by(id=exam_id, teacher_id=teacher.id).first_or_404()
    question_count = Question.query.filter_by(exam_id=exam.id).count()
    if question_count == 0:
        flash("أضف سؤالًا واحدًا على الأقل قبل تفعيل الامتحان.", "error")
        return redirect(url_for("teacher_exam_edit", exam_id=exam.id))
    ExamTarget.query.filter_by(exam_id=exam.id).delete(synchronize_session=False)
    for raw_id in request.form.getlist("student_ids"):
        if raw_id.isdigit():
            student = User.query.get(int(raw_id))
            if student and student.role == "student" and student.active:
                db.session.add(ExamTarget(exam_id=exam.id, student_id=student.id))
    exam.active = True
    exam.status = "scheduled" if exam.publish_at and exam.publish_at > school_now() else "ready"
    db.session.commit()
    flash("تم حفظ مجموعة الطلاب وتفعيل الامتحان وفق موعد الفتح والإغلاق.", "ok")
    return redirect(url_for("teacher_exam_edit", exam_id=exam.id))


@app.post("/teacher/exam/<int:exam_id>/sync-targets")
@login_required("teacher")
def teacher_sync_targets(exam_id):
    teacher = current_user()
    exam = Exam.query.filter_by(id=exam_id, teacher_id=teacher.id).first_or_404()
    added = 0
    for student_id in sorted(eligible_student_ids_for_exam(exam)):
        if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student_id).first():
            db.session.add(ExamTarget(exam_id=exam.id, student_id=student_id))
            added += 1
    question_count = Question.query.filter_by(exam_id=exam.id).count()
    if question_count:
        exam.active = True
        exam.status = "scheduled" if exam.publish_at and exam.publish_at > school_now() else "ready"
        db.session.commit()
        flash("تم تحديث المستهدفين وإضافة {} طالب/طلاب، وتفعيل الامتحان حسب موعد الفتح.".format(added), "ok")
    else:
        db.session.commit()
        flash("تم تحديث مجموعة الطلاب، لكن الامتحان لم يُفعّل: أضف سؤالًا واحدًا على الأقل أولًا.", "error")
    return redirect(url_for("teacher_exam_edit", exam_id=exam.id))


@app.get("/teacher/grade/<int:attempt_id>")
@login_required("teacher")
def teacher_grade_page(attempt_id):
    teacher = current_user()
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = Exam.query.get_or_404(attempt.exam_id)
    if exam.teacher_id != teacher.id:
        abort(403)
    student = User.query.get(attempt.student_id)
    questions = Question.query.filter_by(exam_id=exam.id, qtype="essay").order_by(Question.number).all()
    answers = json.loads(attempt.answers_json or "{}")
    existing = {g.question_id: g for g in EssayGrade.query.filter_by(attempt_id=attempt.id).all()}
    return render_template("grade.html", attempt=attempt, exam=exam, student=student, questions=questions, answers=answers, existing=existing,
                           max_score=exam_max_points(exam.id), auto_max_score=exam_auto_max_points(exam.id),
                           snapshot_url=attempt.answer_snapshot_url)


@app.post("/teacher/grade/<int:attempt_id>")
@login_required("teacher")
def teacher_save_grade(attempt_id):
    teacher = current_user()
    attempt = Attempt.query.get_or_404(attempt_id)
    exam = Exam.query.get_or_404(attempt.exam_id)
    if exam.teacher_id != teacher.id:
        abort(403)
    essay_questions = Question.query.filter_by(exam_id=exam.id, qtype="essay").all()
    for q in essay_questions:
        try:
            score = float(request.form.get("score_{}".format(q.id), "0") or 0)
        except Exception:
            score = 0
        score = max(0.0, min(score, float(q.points)))
        row = EssayGrade.query.filter_by(attempt_id=attempt.id, question_id=q.id).first()
        if not row:
            row = EssayGrade(attempt_id=attempt.id, question_id=q.id)
            db.session.add(row)
        row.score = score
        row.feedback = request.form.get("feedback_{}".format(q.id), "").strip() or None
        row.updated_at = utcnow()
    db.session.flush()
    grades = EssayGrade.query.filter_by(attempt_id=attempt.id).all()
    attempt.essay_total = sum(q.points for q in essay_questions)
    attempt.essay_score = sum(g.score for g in grades)
    attempt.final_score = attempt.auto_score + attempt.essay_score if len(grades) >= len(essay_questions) else None
    attempt.grading_status = "graded" if len(grades) >= len(essay_questions) else "awaiting_essay"
    db.session.commit()
    flash("تم حفظ التصحيح واعتماد النتيجة.", "ok")
    return redirect(url_for("teacher_dashboard"))


@app.get("/student")
@login_required("student")
def student_dashboard():
    student = current_user()
    targets = ExamTarget.query.filter_by(student_id=student.id).all()
    ids = [t.exam_id for t in targets]
    exams = Exam.query.filter(Exam.id.in_(ids)).order_by(Exam.publish_at, Exam.id.desc()).all() if ids else []
    items = []
    teacher_by_exam = {}
    subject_by_exam = {}
    grade_by_exam = {}
    section_by_exam = {}
    for exam in exams:
        attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()
        state = exam_student_state(exam)
        items.append((exam, attempt, state == "open", state))
        teacher_by_exam[exam.id] = User.query.get(exam.teacher_id)
        subject_by_exam[exam.id] = Subject.query.get(exam.subject_id)
        grade_by_exam[exam.id] = Grade.query.get(exam.grade_id) if exam.grade_id else None
        section_by_exam[exam.id] = Section.query.get(exam.section_id) if exam.section_id else None
    return render_template("student.html", student=student, items=items, teacher_by_exam=teacher_by_exam, subject_by_exam=subject_by_exam, grade_by_exam=grade_by_exam, section_by_exam=section_by_exam)


@app.get("/student/exam/<int:exam_id>")
@login_required("student")
def student_exam(exam_id):
    student = current_user()
    exam = Exam.query.get_or_404(exam_id)
    if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student.id).first():
        abort(403)
    if not exam_is_open(exam):
        flash("الامتحان غير متاح الآن.", "error")
        return redirect(url_for("student_dashboard"))
    attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()
    if attempt and attempt.submitted_at:
        return redirect(url_for("student_result", exam_id=exam.id))
    if not attempt:
        attempt = Attempt(exam_id=exam.id, student_id=student.id, started_at=utcnow())
        db.session.add(attempt)
        db.session.commit()
    questions = Question.query.filter_by(exam_id=exam.id).order_by(Question.number).all()
    option_map = {}
    match_map = {}
    question_points = {}
    for q in questions:
        question_points[q.id] = {"max": question_max_points(q), "per_pair": False, "pair_count": 0}
        if q.qtype == "mcq":
            option_map[q.id] = json.loads(q.options_json or "[]")
        elif q.qtype == "matching":
            match_data = json.loads(q.options_json or "{}")
            left_items = list(match_data.get("left", []))
            right_items = list(match_data.get("right", []))
            question_points[q.id] = {"max": question_max_points(q), "per_pair": match_data.get("points_mode") == "per_pair", "pair_count": len(left_items), "per_pair_points": q.points}
            # اعرض عمود الإجابات بترتيب عشوائي، مع إبقاء الفهرس الأصلي للتصحيح.
            right_for_display = [{"index": idx, "text": text} for idx, text in enumerate(right_items)]
            random.shuffle(right_for_display)
            match_map[q.id] = {"left": left_items, "right": right_for_display}
    remaining = max(0, exam.duration_minutes * 60 - int((utcnow() - attempt.started_at).total_seconds()))
    teacher = User.query.get(exam.teacher_id)
    subject = Subject.query.get(exam.subject_id)
    return render_template("take_exam.html", student=student, exam=exam, teacher=teacher, subject=subject,
                           questions=questions, option_map=option_map, match_map=match_map, remaining=remaining,
                           question_points=question_points)


@app.post("/student/exam/<int:exam_id>/submit")
@login_required("student")
def student_submit(exam_id):
    student = current_user()
    exam = Exam.query.get_or_404(exam_id)
    if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student.id).first():
        abort(403)
    attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first()
    if not attempt or attempt.submitted_at:
        abort(403)
    questions = Question.query.filter_by(exam_id=exam.id).order_by(Question.number).all()
    answers = {}
    auto_score = 0.0
    essay_total = 0.0
    for q in questions:
        value = request.form.get("q_{}".format(q.id), "").strip()
        answers[str(q.id)] = value
        if q.qtype == "essay":
            essay_total += q.points
        elif q.qtype == "mcq":
            try:
                if value == str(json.loads(q.correct_json or "null")):
                    auto_score += q.points
            except Exception:
                pass
        elif q.qtype == "matching":
            auto_score += matching_auto_score(q.points, value, q.correct_json, q.options_json)
    attempt.answers_json = json.dumps(answers, ensure_ascii=False)
    attempt.submitted_at = utcnow()
    attempt.auto_score = auto_score
    attempt.essay_total = essay_total
    attempt.essay_score = 0.0
    attempt.final_score = auto_score if essay_total == 0 else None
    attempt.grading_status = "graded" if essay_total == 0 else "awaiting_essay"
    # Prefer a browser-rendered PNG of the actual answer sheet. The old SVG data summary is only a fallback
    # for browsers that could not load the screenshot library; the result page labels the fallback clearly.
    screenshot_data = request.form.get("screenshot_data", "")
    snapshot_url = save_browser_screenshot(screenshot_data, attempt, exam, student)
    if not snapshot_url:
        snapshot_url = save_snapshot(attempt, exam, student)
    attempt.answer_snapshot_url = snapshot_url
    db.session.commit()
    return redirect(url_for("student_result", exam_id=exam.id))


@app.get("/student/result/<int:exam_id>")
@login_required("student")
def student_result(exam_id):
    student = current_user()
    exam = Exam.query.get_or_404(exam_id)
    attempt = Attempt.query.filter_by(exam_id=exam.id, student_id=student.id).first_or_404()
    if not ExamTarget.query.filter_by(exam_id=exam.id, student_id=student.id).first():
        abort(403)
    teacher = User.query.get(exam.teacher_id)
    subject = Subject.query.get(exam.subject_id)
    return render_template("result.html", student=student, exam=exam, teacher=teacher, subject=subject, attempt=attempt,
                           max_score=exam_max_points(exam.id), auto_max_score=exam_auto_max_points(exam.id))


@app.cli.command("init-db")
def init_db():
    ensure_structure()
    if not User.query.filter_by(role="admin").first():
        username = os.getenv("ADMIN_USERNAME", "admin")
        password = os.getenv("ADMIN_PASSWORD", "Admin123!")
        db.session.add(User(username=username, full_name=os.getenv("ADMIN_NAME", "مدير المدرسة"),
                            password_hash=generate_password_hash(password), role="admin"))
        db.session.commit()
        print("تم إنشاء المدير:", username)
    print("تم تهيئة قاعدة البيانات:", str(db_path))


@app.cli.command("seed-demo")
def seed_demo():
    ensure_structure()
    subject = Subject.query.filter_by(name="الرياضيات").first()
    if not subject:
        subject = Subject(name="الرياضيات", active=True)
        db.session.add(subject)
        db.session.flush()
    teacher = User.query.filter_by(username="demo_teacher").first()
    if not teacher:
        teacher = User(username="demo_teacher", full_name="أحمد محمد", password_hash=generate_password_hash("Teacher123!"), role="teacher", subject_id=subject.id)
        db.session.add(teacher)
        db.session.flush()
    else:
        teacher.subject_id = subject.id
    grade = Grade.query.filter_by(name="أولى إعدادي").first()
    section = Section.query.filter_by(grade_id=grade.id, name="أول").first()
    student = User.query.filter_by(username="demo_student").first()
    if not student:
        student = User(username="demo_student", full_name="محمد الطالب", password_hash=generate_password_hash("Student123!"), role="student",
                       phone="01000000000", grade_id=grade.id, section_id=section.id)
        db.session.add(student)
        db.session.flush()
    else:
        student.grade_id = grade.id
        student.section_id = section.id
    year = AcademicYear.query.order_by(AcademicYear.sort_order).first()
    if not TeacherStudent.query.filter_by(teacher_id=teacher.id, student_id=student.id).first():
        db.session.add(TeacherStudent(teacher_id=teacher.id, student_id=student.id))
    if not TeacherAssignment.query.filter_by(teacher_id=teacher.id, academic_year_id=year.id, grade_id=grade.id, section_id=section.id).first():
        db.session.add(TeacherAssignment(teacher_id=teacher.id, academic_year_id=year.id, grade_id=grade.id, section_id=section.id))
    db.session.commit()
    exam = Exam.query.filter_by(teacher_id=teacher.id, title="اختبار تجريبي للطالب").first()
    if not exam:
        exam = Exam(teacher_id=teacher.id, subject_id=subject.id, title="اختبار تجريبي للطالب", grade_id=grade.id, section_id=section.id,
                    academic_year_id=year.id, duration_minutes=30,
                    publish_at=school_now() - dt.timedelta(minutes=5), close_at=school_now() + dt.timedelta(days=30), active=True, status="scheduled")
        db.session.add(exam)
        db.session.flush()
        db.session.add_all([
            Question(exam_id=exam.id, number=1, qtype="mcq", prompt="كم يساوي 2 + 2؟", options_json=json.dumps(["3", "4", "5"], ensure_ascii=False), correct_json=json.dumps(1), points=1),
            Question(exam_id=exam.id, number=2, qtype="essay", prompt="اشرح خطوات حل المعادلة 2x + 2 = 10.", options_json="[]", correct_json=None, points=4),
        ])
        db.session.flush()
    target = ExamTarget.query.filter_by(exam_id=exam.id, student_id=student.id).first()
    if not target:
        db.session.add(ExamTarget(exam_id=exam.id, student_id=student.id))
    db.session.commit()
    print("تم تجهيز بيانات التجربة للمدرس والطالب.")


with app.app_context():
    ensure_structure()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port, debug=False)
