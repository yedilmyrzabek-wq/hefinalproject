"""Lesson Ready: AI-assisted readiness check, learning check and class insights for university lessons.

Run:  ./start.sh            (local)      or      ./share.sh   (public link + QR for phones)
"""
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote_plus

import segno
from flask import (Flask, abort, flash, g, jsonify, redirect, render_template, request, session,
                   url_for)
from markupsafe import Markup
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import stats as st

BASE = os.path.dirname(os.path.abspath(__file__))
ON_VERCEL = bool(os.environ.get("VERCEL"))
# Vercel's project folder is read-only; only /tmp is writable (and it is reset on cold starts).
DATA_DIR = "/tmp" if ON_VERCEL else BASE
DB_PATH = os.environ.get("LESSONS_DB", os.path.join(DATA_DIR, "lessons.db"))
LEVELS = ["Basic", "Medium", "Advanced"]
LANGUAGES = ["English", "Russian", "Kazakh"]
LANG_CODES = {"English": "en", "Russian": "ru", "Kazakh": "kk"}
DEMO_EMAIL = "demo@lessonready.app"
TAB_OF = {"pre": "before", "post": "after"}
UTC_OFFSET = timedelta(hours=float(os.environ.get("UTC_OFFSET_HOURS", 5)))  # Astana time


def _secret_key():
    if os.environ.get("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    path = os.path.join(DATA_DIR, ".secret_key")
    if not os.path.exists(path):
        with open(path, "w") as f:
            f.write(secrets.token_hex(32))
    return open(path).read().strip()


app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)  # correct https links behind a host's proxy
app.secret_key = _secret_key()
app.config.update(SESSION_COOKIE_SAMESITE="Lax", PERMANENT_SESSION_LIFETIME=timedelta(days=60))


# ---------------- Database ----------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY, name TEXT, email TEXT UNIQUE, password_hash TEXT,
    created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY, code TEXT UNIQUE, topic TEXT, course TEXT, language TEXT,
    prereqs TEXT DEFAULT '[]', created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS quizzes (
    id INTEGER PRIMARY KEY, lesson_id INTEGER, kind TEXT, level TEXT,
    questions TEXT, resources TEXT DEFAULT '[]', status TEXT DEFAULT 'draft');
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY, quiz_id INTEGER, student_id TEXT, student_name TEXT,
    answers TEXT, score INTEGER, total INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(quiz_id, student_id));
CREATE TABLE IF NOT EXISTS joins (
    student_id TEXT, lesson_id INTEGER, name TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (student_id, lesson_id));
CREATE TABLE IF NOT EXISTS analyses (
    id INTEGER PRIMARY KEY, lesson_id INTEGER, content TEXT, responses INTEGER,
    created TEXT DEFAULT CURRENT_TIMESTAMP);
"""

# Columns added after the first version; added to older databases on start.
NEW_COLUMNS = {
    "lessons": {"teacher_id": "INTEGER", "class_note": "TEXT DEFAULT ''",
                "note_published": "INTEGER DEFAULT 0", "is_sample": "INTEGER DEFAULT 0"},
    "quizzes": {"ai_questions": "TEXT DEFAULT '[]'", "review": "TEXT DEFAULT '{}'",
                "created": "TEXT", "published": "TEXT"},
}


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, cols in NEW_COLUMNS.items():
            have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH, timeout=10)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_):
    conn = g.pop("db", None)
    if conn:
        conn.close()


def one(sql, *args):
    row = db().execute(sql, args).fetchone()
    if row is None:
        abort(404)
    return row


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")


def lesson_quizzes(lesson_id):
    return {q["kind"]: q for q in db().execute(
        "SELECT * FROM quizzes WHERE lesson_id=? ORDER BY id", (lesson_id,))}


def attempts_for(quiz_id):
    return db().execute("SELECT * FROM attempts WHERE quiz_id=? ORDER BY id", (quiz_id,)).fetchall()


def new_code():
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I to avoid confusion
    while True:
        code = "".join(secrets.choice(alphabet) for _ in range(5))
        if not db().execute("SELECT 1 FROM lessons WHERE code=?", (code,)).fetchone():
            return code


# ---------------- Template helpers ----------------

@app.template_filter("when")
def when(value, fmt="%d %b, %H:%M"):
    if not value:
        return ""
    try:
        dt = datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S") + UTC_OFFSET
    except ValueError:
        return value
    return dt.strftime(fmt).lstrip("0")


@app.template_filter("ago")
def ago(value):
    try:
        delta = datetime.now(timezone.utc).replace(tzinfo=None) - datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return ""
    s = int(delta.total_seconds())
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    days = s // 86400
    return "yesterday" if days == 1 else f"{days} days ago"


def public_base():
    """The address phones should use: PUBLIC_URL, the tunnel address from share.sh, or this host."""
    url = os.environ.get("PUBLIC_URL", "")
    path = os.path.join(BASE, ".public_url")
    if not url and os.path.exists(path):
        url = open(path).read().strip()
    return (url or request.host_url).rstrip("/")


def join_link(lesson):
    return f"{public_base()}/j/{lesson['code']}"


def qr_svg(data, scale=4):
    qr = segno.make(data, error="m")
    return Markup(qr.svg_inline(scale=scale, border=2, dark="#15302a", light="#fffefa",
                                svgclass="qr", omitsize=True))


@app.context_processor
def helpers():
    return {"qr_svg": qr_svg, "lang_code": lambda l: LANG_CODES.get(l["language"], "en"),
            "teacher": g.get("teacher"), "WEAK": st.WEAK}


# ---------------- Teacher accounts ----------------

@app.before_request
def load_teacher():
    g.teacher = None
    if "teacher_id" in session:
        g.teacher = db().execute("SELECT * FROM teachers WHERE id=?",
                                 (session["teacher_id"],)).fetchone()


def teacher_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if g.teacher is None:
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)
    return wrapped


def own_lesson(lesson_id):
    lesson = one("SELECT * FROM lessons WHERE id=?", lesson_id)
    if lesson["teacher_id"] != g.teacher["id"]:
        abort(404)
    return lesson


def own_quiz(quiz_id):
    quiz = one("SELECT * FROM quizzes WHERE id=?", quiz_id)
    return quiz, own_lesson(quiz["lesson_id"])


def sign_in(teacher):
    session.permanent = True
    session["teacher_id"] = teacher["id"]


def safe_next(default):
    nxt = request.args.get("next") or request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else default


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        t = db().execute("SELECT * FROM teachers WHERE email=?", (email,)).fetchone()
        if t and check_password_hash(t["password_hash"], request.form.get("password", "")):
            sign_in(t)
            return redirect(safe_next(url_for("teacher_home")))
        flash("Wrong email or password.")
    demo = db().execute("SELECT 1 FROM teachers WHERE email=?", (DEMO_EMAIL,)).fetchone()
    return render_template("login.html", demo=demo, demo_email=DEMO_EMAIL)


@app.post("/login/demo")
def login_demo():
    t = one("SELECT * FROM teachers WHERE email=?", DEMO_EMAIL)
    sign_in(t)
    return redirect(url_for("teacher_home"))


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        if not name or "@" not in email or len(password) < 6:
            flash("Please enter your name, a valid email and a password of at least 6 characters.")
        elif db().execute("SELECT 1 FROM teachers WHERE email=?", (email,)).fetchone():
            flash("An account with this email already exists. Please sign in.")
        else:
            cur = db().execute("INSERT INTO teachers (name, email, password_hash) VALUES (?,?,?)",
                               (name, email, generate_password_hash(password)))
            # Lessons made before accounts existed go to the first teacher.
            db().execute("UPDATE lessons SET teacher_id=? WHERE teacher_id IS NULL", (cur.lastrowid,))
            db().commit()
            sign_in({"id": cur.lastrowid})
            flash(f"Welcome, {name}. Create your first lesson below.")
            return redirect(url_for("teacher_home"))
    return render_template("register.html")


@app.post("/logout")
def logout():
    session.pop("teacher_id", None)
    return redirect(url_for("home"))


# ---------------- Home ----------------

@app.route("/")
def home():
    return render_template("home.html")


# ---------------- Teacher: lessons ----------------

@app.route("/teacher")
@teacher_required
def teacher_home():
    lessons = db().execute("SELECT * FROM lessons WHERE teacher_id=? ORDER BY created DESC, id DESC",
                           (g.teacher["id"],)).fetchall()
    rows, students, responses, readiness = [], set(), 0, []
    for l in lessons:
        qs = lesson_quizzes(l["id"])
        info = {"lesson": l}
        for kind, q in qs.items():
            if q["status"] == "draft":
                info[kind] = {"status": "draft"}
                continue
            s = st.quiz_stats(q, attempts_for(q["id"]))
            info[kind] = {"status": q["status"], "count": s["count"], "avg": s["avg"],
                          "weak": len(s["weak_topics"])}
            students.update(x["id"] for x in s["students"])
            responses += s["count"]
            if kind == "pre" and s["count"]:
                readiness.append(s["avg"])
        rows.append(info)
    summary = {"lessons": len(lessons), "students": len(students), "responses": responses,
               "readiness": round(sum(readiness) / len(readiness)) if readiness else None}
    return render_template("teacher_home.html", rows=rows, summary=summary, languages=LANGUAGES)


@app.post("/teacher/new")
@teacher_required
def teacher_new():
    topic = request.form.get("topic", "").strip()
    course = request.form.get("course", "").strip()
    language = request.form.get("language", "English")
    if not topic:
        flash("Please write the lesson topic.")
        return redirect(url_for("teacher_home"))
    try:
        prereqs = ai.get_prerequisites(topic, course, language)
    except ai.AIError as e:
        flash(str(e))
        return redirect(url_for("teacher_home"))
    cur = db().execute(
        "INSERT INTO lessons (teacher_id, code, topic, course, language, prereqs) VALUES (?,?,?,?,?,?)",
        (g.teacher["id"], new_code(), topic, course, language, json.dumps(prereqs)))
    db().commit()
    return redirect(url_for("teacher_prereqs", lesson_id=cur.lastrowid))


@app.post("/teacher/lesson/<int:lesson_id>/delete")
@teacher_required
def teacher_delete(lesson_id):
    lesson = own_lesson(lesson_id)
    for q in lesson_quizzes(lesson_id).values():
        db().execute("DELETE FROM attempts WHERE quiz_id=?", (q["id"],))
    for table in ("quizzes", "joins", "analyses"):
        db().execute(f"DELETE FROM {table} WHERE lesson_id=?", (lesson_id,))
    db().execute("DELETE FROM lessons WHERE id=?", (lesson_id,))
    db().commit()
    flash(f"Deleted “{lesson['topic']}” and its results.")
    return redirect(url_for("teacher_home"))


def has_published(lesson_id, kind):
    return db().execute("SELECT 1 FROM quizzes WHERE lesson_id=? AND kind=? AND status<>'draft'",
                        (lesson_id, kind)).fetchone() is not None


def save_draft(lesson_id, kind, level, questions, resources=()):
    db().execute("DELETE FROM quizzes WHERE lesson_id=? AND kind=? AND status='draft'", (lesson_id, kind))
    cur = db().execute(
        "INSERT INTO quizzes (lesson_id, kind, level, questions, ai_questions, resources, created)"
        " VALUES (?,?,?,?,?,?,?)",
        (lesson_id, kind, level, json.dumps(questions), json.dumps(questions),
         json.dumps(list(resources)), now()))
    db().commit()
    return cur.lastrowid


@app.route("/teacher/lesson/<int:lesson_id>/prerequisites", methods=["GET", "POST"])
@teacher_required
def teacher_prereqs(lesson_id):
    lesson = own_lesson(lesson_id)
    if has_published(lesson_id, "pre"):
        flash("The readiness quiz is already published.")
        return redirect(url_for("teacher_lesson", lesson_id=lesson_id))
    prereqs = json.loads(lesson["prereqs"])
    if request.method == "POST":
        chosen = request.form.getlist("prereq")
        chosen += [x.strip() for x in request.form.get("extra", "").splitlines() if x.strip()]
        if not chosen:
            flash("Keep at least one prerequisite.")
            return redirect(request.url)
        level = request.form.get("level", "Basic")
        n = int(request.form.get("n", 6))
        try:
            quiz = ai.make_pre_quiz(lesson["topic"], lesson["course"], chosen, level, n, lesson["language"])
        except ai.AIError as e:
            flash(str(e))
            return redirect(request.url)
        quiz_id = save_draft(lesson_id, "pre", level, quiz["questions"], quiz["resources"])
        return redirect(url_for("teacher_review", quiz_id=quiz_id))
    return render_template("teacher_prereqs.html", lesson=lesson, prereqs=prereqs, levels=LEVELS)


@app.post("/teacher/lesson/<int:lesson_id>/post-quiz")
@teacher_required
def teacher_post_quiz(lesson_id):
    lesson = own_lesson(lesson_id)
    if has_published(lesson_id, "post"):
        flash("The learning check is already published.")
        return redirect(url_for("teacher_lesson", lesson_id=lesson_id))
    level = request.form.get("level", "Basic")
    n = int(request.form.get("n", 6))
    notes = request.form.get("notes", "").strip()
    try:
        quiz = ai.make_post_quiz(lesson["topic"], lesson["course"], notes, level, n, lesson["language"])
    except ai.AIError as e:
        flash(str(e))
        return redirect(url_for("teacher_lesson", lesson_id=lesson_id, tab="after"))
    quiz_id = save_draft(lesson_id, "post", level, quiz["questions"])
    return redirect(url_for("teacher_review", quiz_id=quiz_id))


# ---------------- Teacher: review and edit AI drafts ----------------

def read_review_form(form, topics):
    """Rebuild the question list from the review form. Returns (questions, error)."""
    questions, kept_ids = [], []
    for qid in form.getlist("qid"):
        if not form.get(f"{qid}_keep"):
            continue
        text = form.get(f"{qid}_text", "").strip()
        raw = [form.get(f"{qid}_opt{j}", "").strip() for j in range(6)]
        try:
            correct = int(form.get(f"{qid}_correct", "-1"))
        except ValueError:
            correct = -1
        if not text and not any(raw):
            continue  # an empty added question: ignore it
        options = [o for o in raw if o]
        if not text or len(options) < 2:
            return None, None, "Every kept question needs text and at least two answer options."
        if not (0 <= correct < len(raw)) or not raw[correct]:
            return None, None, f"Mark the correct answer for: “{text[:60]}”."
        questions.append({
            "text": text, "options": options, "correct_index": options.index(raw[correct]),
            "explanation": form.get(f"{qid}_expl", "").strip(),
            "prerequisite": form.get(f"{qid}_topic", "").strip() or (topics[0] if topics else "General"),
        })
        kept_ids.append(qid)
    return questions, kept_ids, None


def review_summary(ai_questions, questions, kept_ids):
    kept = dict(zip(kept_ids, questions))
    unchanged = edited = removed = 0
    for i, q in enumerate(ai_questions):
        mine = kept.get(f"a{i}")
        if mine is None:
            removed += 1
        elif all(mine[k] == q[k] for k in ("text", "options", "correct_index", "explanation", "prerequisite")):
            unchanged += 1
        else:
            edited += 1
    added = sum(1 for k in kept_ids if not k.startswith("a"))
    return {"ai": len(ai_questions), "unchanged": unchanged, "edited": edited,
            "removed": removed, "added": added, "ids": kept_ids}


@app.route("/teacher/quiz/<int:quiz_id>/review", methods=["GET", "POST"])
@teacher_required
def teacher_review(quiz_id):
    quiz, lesson = own_quiz(quiz_id)
    if quiz["status"] != "draft":
        return redirect(url_for("teacher_lesson", lesson_id=lesson["id"]))
    ai_questions = json.loads(quiz["ai_questions"] or "[]") or json.loads(quiz["questions"])
    resources = json.loads(quiz["resources"])
    if quiz["kind"] == "pre":
        topics = list(dict.fromkeys([q["prerequisite"] for q in ai_questions] +
                                    [r["prerequisite"] for r in resources]))
    else:
        topics = list(dict.fromkeys(q["prerequisite"] for q in ai_questions))

    if request.method == "POST":
        questions, kept_ids, error = read_review_form(request.form, topics)
        for i, r in enumerate(resources):
            r["summary"] = request.form.get(f"res{i}_summary", r["summary"]).strip()
            r["search_query"] = request.form.get(f"res{i}_query", r["search_query"]).strip()
        if error:
            flash(error)
            return redirect(request.url)
        if not questions:
            flash("Keep at least one question.")
            return redirect(request.url)
        review = review_summary(ai_questions, questions, kept_ids)
        publish = request.form.get("action") == "publish"
        db().execute("UPDATE quizzes SET questions=?, resources=?, review=?, status=?, published=? WHERE id=?",
                     (json.dumps(questions), json.dumps(resources), json.dumps(review),
                      "open" if publish else "draft", now() if publish else None, quiz_id))
        db().commit()
        if publish:
            flash(f"Published. Students can now open it with code {lesson['code']}.")
            return redirect(url_for("teacher_lesson", lesson_id=lesson["id"], tab=TAB_OF[quiz["kind"]]))
        flash("Draft saved.")
        return redirect(request.url)

    # Show the teacher's saved version, but keep the "a<i>" ids pointing at the original AI items.
    current = json.loads(quiz["questions"])
    ids = json.loads(quiz["review"] or "{}").get("ids") or [f"a{i}" for i in range(len(current))]
    items = []
    for qid, q in zip(ids, current):
        original = ai_questions[int(qid[1:])] if qid.startswith("a") else None
        items.append({"qid": qid, "q": q, "origin": "ai" if original else "teacher",
                      "edited": original is not None and original != q})
    return render_template("teacher_review.html", quiz=quiz, lesson=lesson, items=items,
                           resources=resources, topics=topics)


@app.post("/teacher/quiz/<int:quiz_id>/status")
@teacher_required
def teacher_quiz_status(quiz_id):
    quiz, lesson = own_quiz(quiz_id)
    new = "closed" if quiz["status"] == "open" else "open"
    db().execute("UPDATE quizzes SET status=? WHERE id=?", (new, quiz_id))
    db().commit()
    return redirect(url_for("teacher_lesson", lesson_id=lesson["id"], tab=TAB_OF[quiz["kind"]]))


# ---------------- Teacher: lesson dashboard (live) ----------------

def lesson_context(lesson):
    quizzes = lesson_quizzes(lesson["id"])
    stats = {kind: st.quiz_stats(q, attempts_for(q["id"]))
             for kind, q in quizzes.items() if q["status"] != "draft"}
    progress = st.progress(stats.get("pre"), stats.get("post"))
    joined = db().execute("SELECT COUNT(*) FROM joins WHERE lesson_id=?", (lesson["id"],)).fetchone()[0]
    responses = sum(s["count"] for s in stats.values())
    signature = f"{joined}-" + "-".join(f"{k}{q['status']}{stats[k]['count'] if k in stats else 'd'}"
                                        for k, q in sorted(quizzes.items()))
    return {"lesson": lesson, "quizzes": quizzes, "stats": stats, "progress": progress,
            "joined": joined, "responses": responses, "signature": signature}


def latest_analysis(lesson_id, stats):
    row = db().execute("SELECT * FROM analyses WHERE lesson_id=? ORDER BY id DESC LIMIT 1",
                       (lesson_id,)).fetchone()
    if not row:
        return None
    content = json.loads(row["content"])
    for m in content["misconceptions"]:
        m["evidence"] = st.evidence(stats, m["question_refs"])
    return {"row": row, "content": content}


@app.route("/teacher/lesson/<int:lesson_id>")
@teacher_required
def teacher_lesson(lesson_id):
    lesson = own_lesson(lesson_id)
    ctx = lesson_context(lesson)
    ctx["reviews"] = {kind: json.loads(q["review"] or "{}") for kind, q in ctx["quizzes"].items()}
    tab = request.args.get("tab")
    if tab not in ("before", "after", "insights"):
        tab = "after" if "post" in ctx["stats"] else "before"
    return render_template("teacher_lesson.html", levels=LEVELS, tab=tab,
                           analysis=latest_analysis(lesson_id, ctx["stats"]), **ctx)


@app.route("/teacher/lesson/<int:lesson_id>/live")
@teacher_required
def teacher_live(lesson_id):
    lesson = own_lesson(lesson_id)
    ctx = lesson_context(lesson)
    if request.args.get("sig") == ctx["signature"]:
        return jsonify(signature=ctx["signature"], changed=False)
    regions = {}
    for kind in ("pre", "post"):
        if kind in ctx["stats"]:
            regions[kind] = render_template("_results.html", s=ctx["stats"][kind], kind=kind, lesson=lesson)
    regions["progress"] = render_template("_progress.html", progress=ctx["progress"])
    newest = db().execute(
        "SELECT a.student_name FROM attempts a JOIN quizzes q ON q.id=a.quiz_id WHERE q.lesson_id=?"
        " ORDER BY a.id DESC LIMIT 1", (lesson_id,)).fetchone()
    return jsonify(signature=ctx["signature"], changed=True, regions=regions, joined=ctx["joined"],
                   counts={k: s["count"] for k, s in ctx["stats"].items()},
                   responses=ctx["responses"], newest=newest[0] if newest else None)


@app.route("/teacher/lesson/<int:lesson_id>/present")
@teacher_required
def teacher_present(lesson_id):
    lesson = own_lesson(lesson_id)
    ctx = lesson_context(lesson)
    return render_template("teacher_present.html", join_url=join_link(lesson), **ctx)


@app.post("/teacher/lesson/<int:lesson_id>/analyse")
@teacher_required
def teacher_analyse(lesson_id):
    lesson = own_lesson(lesson_id)
    ctx = lesson_context(lesson)
    if not ctx["responses"]:
        flash("There are no student results to analyse yet.")
        return redirect(url_for("teacher_lesson", lesson_id=lesson_id, tab="insights"))
    data = st.analysis_input(ctx["stats"], ctx["progress"])
    try:
        content = ai.analyse_class(lesson["topic"], lesson["course"], lesson["language"], data)
    except ai.AIError as e:
        flash(str(e))
        return redirect(url_for("teacher_lesson", lesson_id=lesson_id, tab="insights"))
    db().execute("INSERT INTO analyses (lesson_id, content, responses, created) VALUES (?,?,?,?)",
                 (lesson_id, json.dumps(content), ctx["responses"], now()))
    db().commit()
    return redirect(url_for("teacher_lesson", lesson_id=lesson_id, tab="insights"))


@app.post("/teacher/lesson/<int:lesson_id>/note")
@teacher_required
def teacher_note(lesson_id):
    own_lesson(lesson_id)
    note = request.form.get("note", "").strip()
    action = request.form.get("action")
    published = 1 if action == "publish" and note else 0
    db().execute("UPDATE lessons SET class_note=?, note_published=? WHERE id=?", (note, published, lesson_id))
    db().commit()
    flash("Message sent to students." if published else "Message saved. Students cannot see it yet.")
    return redirect(url_for("teacher_lesson", lesson_id=lesson_id, tab="insights") + "#note")


# ---------------- Student ----------------

@app.route("/j/<code>")
def short_join(code):
    return redirect(url_for("student_join", code=code.upper()))


@app.route("/join", methods=["GET", "POST"])
def student_join():
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        name = " ".join(request.form.get("name", "").split())[:60]
        lesson = db().execute("SELECT * FROM lessons WHERE code=?", (code,)).fetchone()
        if not name or not lesson:
            flash("Please check the lesson code and write your name.")
            return redirect(url_for("student_join", code=code))
        if session.get("name") != name or "student_id" not in session:
            session["student_id"] = secrets.token_hex(8)
        session.permanent = True
        session["name"] = name
        db().execute("INSERT INTO joins (student_id, lesson_id, name, created) VALUES (?,?,?,?)"
                     " ON CONFLICT(student_id, lesson_id) DO UPDATE SET name=excluded.name",
                     (session["student_id"], lesson["id"], name, now()))
        db().commit()
        return redirect(url_for("student_lesson", code=code))
    return render_template("student_join.html", code=request.args.get("code", "").upper())


@app.post("/leave")
def student_leave():
    session.pop("student_id", None)
    session.pop("name", None)
    return redirect(url_for("student_join"))


def require_student():
    if "student_id" not in session:
        abort(redirect(url_for("student_join", code=request.view_args.get("code", ""))))


@app.route("/me")
def student_history():
    require_student()
    sid = session["student_id"]
    joined = db().execute(
        "SELECT l.*, j.created AS joined FROM joins j JOIN lessons l ON l.id=j.lesson_id"
        " WHERE j.student_id=? ORDER BY j.created DESC", (sid,)).fetchall()
    done = {r["quiz_id"]: r for r in db().execute("SELECT * FROM attempts WHERE student_id=?", (sid,))}
    rows = []
    for l in joined:
        quizzes = {k: q for k, q in lesson_quizzes(l["id"]).items() if q["status"] != "draft"}
        rows.append({"lesson": l, "quizzes": quizzes,
                     "attempts": {k: done.get(q["id"]) for k, q in quizzes.items()}})
    return render_template("student_history.html", rows=rows)


@app.route("/lesson/<code>")
def student_lesson(code):
    require_student()
    lesson = one("SELECT * FROM lessons WHERE code=?", code.upper())
    db().execute("INSERT OR IGNORE INTO joins (student_id, lesson_id, name, created) VALUES (?,?,?,?)",
                 (session["student_id"], lesson["id"], session["name"], now()))
    db().commit()
    quizzes = lesson_quizzes(lesson["id"])
    done = {r["quiz_id"]: r for r in db().execute(
        "SELECT * FROM attempts WHERE student_id=?", (session["student_id"],))}
    return render_template("student_lesson.html", lesson=lesson, quizzes=quizzes, done=done)


@app.route("/quiz/<int:quiz_id>", methods=["GET", "POST"])
def student_quiz(quiz_id):
    require_student()
    quiz = one("SELECT * FROM quizzes WHERE id=?", quiz_id)
    lesson = one("SELECT * FROM lessons WHERE id=?", quiz["lesson_id"])
    sid = session["student_id"]
    if db().execute("SELECT 1 FROM attempts WHERE quiz_id=? AND student_id=?", (quiz_id, sid)).fetchone():
        return redirect(url_for("student_result", quiz_id=quiz_id))
    if quiz["status"] != "open":
        flash("This quiz is not open right now.")
        return redirect(url_for("student_lesson", code=lesson["code"]))
    questions = json.loads(quiz["questions"])
    if request.method == "POST":
        answers = []
        for i in range(len(questions)):
            v = request.form.get(f"q{i}")
            answers.append(int(v) if v is not None and v.isdigit() else None)
        score = sum(a == q["correct_index"] for a, q in zip(answers, questions))
        db().execute("INSERT OR IGNORE INTO attempts (quiz_id, student_id, student_name, answers, score, total, created)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (quiz_id, sid, session["name"], json.dumps(answers), score, len(questions), now()))
        db().commit()
        return redirect(url_for("student_result", quiz_id=quiz_id))
    return render_template("student_quiz.html", quiz=quiz, lesson=lesson, questions=questions)


@app.route("/quiz/<int:quiz_id>/result")
def student_result(quiz_id):
    require_student()
    quiz = one("SELECT * FROM quizzes WHERE id=?", quiz_id)
    lesson = one("SELECT * FROM lessons WHERE id=?", quiz["lesson_id"])
    attempt = one("SELECT * FROM attempts WHERE quiz_id=? AND student_id=?", quiz_id, session["student_id"])
    questions = json.loads(quiz["questions"])
    answers = json.loads(attempt["answers"])
    rows = [{"q": q, "chosen": a, "ok": a == q["correct_index"]} for q, a in zip(questions, answers)]

    by_topic = {}
    for r in rows:
        t = by_topic.setdefault(r["q"]["prerequisite"], [0, 0])
        t[0] += r["ok"]
        t[1] += 1
    topics = [{"name": k, "right": v[0], "total": v[1]} for k, v in by_topic.items()]

    # Study notes only for the prerequisites this student missed
    weak = {r["q"]["prerequisite"].strip().lower() for r in rows if not r["ok"]}
    resources = []
    for r in json.loads(quiz["resources"]):
        if r["prerequisite"].strip().lower() in weak:
            q = quote_plus(r["search_query"])
            r["youtube"] = f"https://www.youtube.com/results?search_query={q}"
            r["web"] = f"https://www.google.com/search?q={q}"
            resources.append(r)
    return render_template("student_result.html", quiz=quiz, lesson=lesson, attempt=attempt,
                           rows=rows, resources=resources, topics=topics)


init_db()

if ON_VERCEL:
    import threading
    _seed_lock = threading.Lock()
    _seeded = False

    @app.before_request
    def _seed_demo_once():
        """/tmp starts empty on each new Vercel instance: add the demo teacher and classes."""
        global _seeded
        if _seeded:
            return
        with _seed_lock:
            if not _seeded:
                import seed as _seed
                os.environ["LESSONS_DB"] = DB_PATH
                if not _seed.has_data():
                    _seed.seed(_seed.load_content())
                _seeded = True

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False, threaded=True)
