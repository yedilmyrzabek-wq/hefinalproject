"""Fill Lesson Ready with a demo teacher and sample classes.

    python seed.py              reset lessons.db with the demo data (no AI calls, works offline)
    python seed.py --generate   first re-create demo/demo_content.json with real AI calls, then seed
    python seed.py --if-empty   seed only when there is no data yet (used when the server starts)

What is real and what is simulated:
- All quiz questions, study notes, prerequisites and the class analysis were produced by the app's own
  AI pipeline (ai.py, Claude Haiku 5.5) and stored in demo/demo_content.json.
- The STUDENTS and their answers are simulated (seeded random numbers), so the dashboards have data.
  Such lessons are marked "Sample class" in the app.
"""
import json
import os
import random
import secrets
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

from werkzeug.security import generate_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
CONTENT = os.path.join(BASE, "demo", "demo_content.json")
DEMO_EMAIL = "demo@lessonready.app"
DEMO_PASSWORD = "lessonready"

STUDENTS = [
    "Aruzhan B.", "Dias M.", "Alikhan S.", "Dana O.", "Nursultan A.", "Aigerim T.", "Yerlan Z.",
    "Madina K.", "Temirlan S.", "Kamila R.", "Arman N.", "Zhansaya E.", "Daniyar K.", "Tomiris A.",
    "Sanzhar I.", "Assel D.", "Bekzat O.", "Inkar S.", "Ruslan O.", "Ayana B.", "Miras T.",
    "Adil K.", "Saule M.", "Nurdaulet A.",
]
STUDENTS_RU = ["Алия С.", "Максим П.", "Айдана К.", "Артём В.", "Диана Ж.", "Ерасыл Б.", "Мадина Н.",
               "Тимур А.", "Камила Д.", "Ильяс Р.", "Анель Т.", "Никита Л.", "Жанна М.", "Санжар Е.",
               "Виктория Г."]
STUDENTS_KZ = ["Айгерім Т.", "Ерназар Қ.", "Әсем Б.", "Нұрислам Ж.", "Гүлнұр С.", "Бауыржан Ө.",
               "Мөлдір А.", "Дәулет Н.", "Құралай Е."]

# The plan for each sample lesson. "targets": chance that an average student answers each question
# correctly (by position); "lures": which wrong option most wrong answers go to (a typical misconception).
# "edits": corrections the teacher made to AI mistakes during review (the original stays in ai_questions).
PLAN = {
    "knn": {"days_ago": 3, "students": STUDENTS, "pre_n": 24, "post_n": 22,
            "pre": {"targets": [.92, .88, .85, .80, .70, .76, .62, .36], "lures": {7: 1, 6: 1, 3: 0, 4: 0}},
            "post": {"targets": [.92, .86, .5, .78, .5, .84], "lures": {2: 0, 4: 1, 3: 1, 1: 2}}},
    "calculus": {"days_ago": 1, "students": STUDENTS[3:21], "pre_n": 18,
                 "pre": {"targets": [.82, .66, .42, .7, .86, .5], "lures": {2: 0, 5: 0, 1: 0, 0: 2},
                         "edits": {0: {"explanation": "The derivative of x^3 is 3x^2, so x^3 + C is an antiderivative. "
                                       "x^3/3 + C is wrong: its derivative is x^2, not 3x^2."}}}},
    "normalization": {"days_ago": 5, "students": STUDENTS_RU, "pre_n": 15,
                      "pre": {"targets": [.88, .7, .84, .74, .4, .62], "lures": {4: 1, 1: 0, 5: 2},
                              "edits": {4: {"explanation": "Запись A → B значит: если две строки имеют одинаковое "
                                            "значение A, то у них одинаковое и значение B. Значение A однозначно определяет B."}}}},
    "regression_kz": {"days_ago": 6, "students": STUDENTS_KZ, "pre_n": 9,
                      "pre": {"targets": [.8, .9, .9, .72, .78, .45], "lures": {5: 1, 0: 0}}},
}


# ---------------------------------------------------------------- generate content with real AI

def generate():
    sys.path.insert(0, BASE)
    import ai
    content = load_content() if os.path.exists(CONTENT) else {}

    # 1. KNN: the readiness quiz that the team generated and published while testing the app
    if "knn" not in content:
        old = sqlite3.connect(os.path.join(BASE, "lessons.db"))
        old.row_factory = sqlite3.Row
        l = old.execute("SELECT * FROM lessons WHERE topic LIKE 'KNN%' ORDER BY id LIMIT 1").fetchone()
        q = old.execute("SELECT * FROM quizzes WHERE lesson_id=? AND kind='pre'", (l["id"],)).fetchone()
        content["knn"] = {"topic": "KNN algorithm", "course": "Machine Learning Engineering 1 · 2nd year",
                          "language": "English", "prereqs": json.loads(l["prereqs"]),
                          "pre": {"level": "Basic", "questions": json.loads(q["questions"]),
                                  "resources": json.loads(q["resources"])}}
    if "post" not in content["knn"]:
        print("KNN learning check …")
        content["knn"]["post"] = {"level": "Medium", **ai.make_post_quiz(
            "KNN algorithm", "Machine Learning Engineering 1", "How KNN predicts (majority vote / average), "
            "choosing k and the bias-variance trade-off, why feature scaling matters for distances, "
            "and KNN's cost at prediction time.", "Medium", 6, "English")}

    specs = {
        "calculus": ("Integration by parts", "Calculus II · 1st year", "English", "Medium", 6),
        "normalization": ("Нормализация баз данных", "Базы данных · 2 курс", "Russian", "Basic", 6),
        "regression_kz": ("Сызықтық регрессия", "Машиналық оқыту негіздері · 3 курс", "Kazakh", "Basic", 6),
        "gradient": ("Gradient descent", "Machine Learning Engineering 1 · 2nd year", "English", "Basic", 6),
    }
    for key, (topic, course, lang, level, n) in specs.items():
        if key in content:
            continue
        print(f"{topic}: prerequisites …")
        prereqs = ai.get_prerequisites(topic, course, lang)
        chosen = [p["name"] for p in prereqs][:4]
        print(f"{topic}: readiness quiz …")
        quiz = ai.make_pre_quiz(topic, course, chosen, level, n, lang)
        content[key] = {"topic": topic, "course": course, "language": lang, "prereqs": prereqs,
                        "pre": {"level": level, **quiz}}
    save_content(content)

    # 2. The class analysis needs the (simulated) results, so seed first, then ask the AI.
    if "analysis" not in content["knn"]:
        seed(content)
        import importlib
        os.environ["LESSONS_DB"] = os.path.join(BASE, "lessons.db")
        app = importlib.import_module("app")
        with app.app.app_context():
            lesson = app.db().execute("SELECT * FROM lessons WHERE code='KNN24'").fetchone()
            ctx = app.lesson_context(lesson)
            data = app.st.analysis_input(ctx["stats"], ctx["progress"])
            print("KNN class analysis …")
            content["knn"]["analysis"] = {"responses": ctx["responses"],
                                          "content": ai.analyse_class(lesson["topic"], lesson["course"], "English", data)}
        save_content(content)


def load_content():
    with open(CONTENT, encoding="utf-8") as f:
        return json.load(f)


def save_content(content):
    os.makedirs(os.path.dirname(CONTENT), exist_ok=True)
    with open(CONTENT, "w", encoding="utf-8") as f:
        json.dump(content, f, ensure_ascii=False, indent=1)


# ---------------------------------------------------------------- simulate a class

def ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def simulate(rng, questions, abilities, targets=None, lures=None):
    """Answers for each student. ability shifts every question's chance of being right."""
    targets = targets or [.75] * len(questions)
    lures = lures or {}
    out = []
    for ability in abilities:
        answers = []
        for i, q in enumerate(questions):
            p = min(.98, max(.04, targets[i % len(targets)] + ability))
            if rng.random() < p:
                answers.append(q["correct_index"])
            else:
                wrong = [j for j in range(len(q["options"])) if j != q["correct_index"]]
                lure = lures.get(i, wrong[0])
                answers.append(lure if rng.random() < .7 else rng.choice(wrong))
        out.append(answers)
    return out


def seed(content):
    db_path = os.environ.get("LESSONS_DB", os.path.join(BASE, "lessons.db"))
    if os.path.exists(db_path):
        backup = os.path.join(os.path.dirname(db_path), f"lessons.backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        shutil.copy(db_path, backup)
        print(f"Old database saved as {os.path.basename(backup)}")
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suffix):
                os.remove(db_path + suffix)

    sys.path.insert(0, BASE)
    os.environ["LESSONS_DB"] = db_path
    import importlib
    app = importlib.import_module("app")
    app.init_db()
    conn = sqlite3.connect(db_path)
    rng = random.Random(2026)
    now = datetime.now(timezone.utc).replace(tzinfo=None, second=0, microsecond=0)

    conn.execute("INSERT INTO teachers (name, email, password_hash, created) VALUES (?,?,?,?)",
                 ("Demo Teacher", DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD), ts(now - timedelta(days=30))))
    teacher_id = 1
    ids = {}  # student name -> browser id (same student across lessons)

    def sid(name):
        return ids.setdefault(name, secrets.token_hex(8))

    def add_lesson(key, code, created, sample=True):
        c = content[key]
        cur = conn.execute(
            "INSERT INTO lessons (teacher_id, code, topic, course, language, prereqs, is_sample, created)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (teacher_id, code, c["topic"], c["course"], c["language"], json.dumps(c["prereqs"], ensure_ascii=False),
             1 if sample else 0, ts(created)))
        return cur.lastrowid

    def add_quiz(lesson_id, kind, quiz, created, status="open", edits=None):
        edits = edits or {}
        questions = [{**q, **edits.get(i, {})} for i, q in enumerate(quiz["questions"])]
        n = len(questions)
        review = {"ai": n, "unchanged": n - len(edits), "edited": len(edits),
                  "removed": 0, "added": 0, "ids": [f"a{i}" for i in range(n)]}
        qs = json.dumps(questions, ensure_ascii=False)
        cur = conn.execute(
            "INSERT INTO quizzes (lesson_id, kind, level, questions, ai_questions, resources, status, review, created, published)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (lesson_id, kind, quiz["level"], qs, json.dumps(quiz["questions"], ensure_ascii=False),
             json.dumps(quiz.get("resources", []), ensure_ascii=False),
             status, json.dumps(review) if status != "draft" else "{}", ts(created),
             ts(created + timedelta(minutes=12)) if status != "draft" else None))
        return cur.lastrowid

    def add_attempts(lesson_id, quiz_id, questions, names, answers, start, spread_minutes):
        for k, (name, ans) in enumerate(zip(names, answers)):
            when = start + timedelta(minutes=int(spread_minutes * k / max(1, len(names))) + rng.randint(0, 9))
            conn.execute("INSERT OR IGNORE INTO joins (student_id, lesson_id, name, created) VALUES (?,?,?,?)",
                         (sid(name), lesson_id, name, ts(when - timedelta(minutes=3))))
            score = sum(a == q["correct_index"] for a, q in zip(ans, questions))
            conn.execute("INSERT INTO attempts (quiz_id, student_id, student_name, answers, score, total, created)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (quiz_id, sid(name), name, json.dumps(ans), score, len(questions), ts(when)))

    abilities = {name: rng.uniform(-.22, .18) for name in STUDENTS + STUDENTS_RU + STUDENTS_KZ}

    # --- Lesson: KNN (before + after + analysis + message) ---
    plan, c = PLAN["knn"], content["knn"]
    day = now - timedelta(days=plan["days_ago"])
    lid = add_lesson("knn", "KNN24", day - timedelta(days=1))
    pre_names = plan["students"][:plan["pre_n"]]
    q_pre = add_quiz(lid, "pre", c["pre"], day - timedelta(days=1))
    ans = simulate(rng, c["pre"]["questions"], [abilities[n] for n in pre_names], **plan["pre"])
    add_attempts(lid, q_pre, c["pre"]["questions"], pre_names, ans, day - timedelta(hours=16), 600)
    if "post" in c:
        post_names = pre_names[:plan["post_n"]]
        q_post = add_quiz(lid, "post", c["post"], day + timedelta(hours=1))
        ans = simulate(rng, c["post"]["questions"], [abilities[n] * .6 + .07 for n in post_names], **plan["post"])
        add_attempts(lid, q_post, c["post"]["questions"], post_names, ans, day + timedelta(hours=2), 240)
    if "analysis" in c:
        conn.execute("INSERT INTO analyses (lesson_id, content, responses, created) VALUES (?,?,?,?)",
                     (lid, json.dumps(c["analysis"]["content"], ensure_ascii=False), c["analysis"]["responses"],
                      ts(day + timedelta(hours=7))))
        conn.execute("UPDATE lessons SET class_note=?, note_published=1 WHERE id=?",
                     (c.get("class_note") or c["analysis"]["content"]["note_to_students"], lid))

    # --- Lessons with a readiness quiz only ---
    for key, code in (("calculus", "CALC2"), ("normalization", "BD2RU"), ("regression_kz", "REGKZ")):
        if key not in content:
            continue
        plan, c = PLAN[key], content[key]
        day = now - timedelta(days=plan["days_ago"])
        lid = add_lesson(key, code, day - timedelta(hours=20))
        q = add_quiz(lid, "pre", c["pre"], day - timedelta(hours=20), edits=plan["pre"].get("edits"))
        names = plan["students"][:plan["pre_n"]]
        quiz_plan = {k: v for k, v in plan["pre"].items() if k != "edits"}
        ans = simulate(rng, c["pre"]["questions"], [abilities[n] for n in names], **quiz_plan)
        add_attempts(lid, q, c["pre"]["questions"], names, ans, day - timedelta(hours=14), 500)

    # --- A fresh lesson for the live demo: AI draft waiting for the teacher's review ---
    if "gradient" in content:
        lid = add_lesson("gradient", "GRAD5", now - timedelta(minutes=40), sample=False)
        add_quiz(lid, "pre", content["gradient"]["pre"], now - timedelta(minutes=38), status="draft")

    conn.commit()
    conn.close()
    print(f"Seeded. Demo teacher: {DEMO_EMAIL} / {DEMO_PASSWORD}")


def has_data():
    path = os.environ.get("LESSONS_DB", os.path.join(BASE, "lessons.db"))
    if not os.path.exists(path):
        return False
    try:
        return sqlite3.connect(path).execute("SELECT COUNT(*) FROM teachers").fetchone()[0] > 0
    except sqlite3.Error:
        return False


if __name__ == "__main__":
    if "--if-empty" in sys.argv and has_data():
        print("Data already present, not seeding.")
        sys.exit(0)
    if "--generate" in sys.argv:
        generate()
    seed(load_content())
