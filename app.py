"""
AI Study Planner
----------------
A Flask web app that generates a personalised day-by-day study schedule.

How the algorithm works
========================
Each subject has:
    - total_hours : total hours of work remaining for that subject
    - deadline     : the date by which the subject must be finished

For every day between "today" and the planning horizon, the app:
    1. Finds every subject that still has hours left AND whose
       deadline has not passed.
    2. Gives each of those subjects an "urgency score":

           urgency = remaining_hours / days_left

       A subject with a close deadline and lots of work left gets a
       much higher score than one with a distant deadline or little
       work left.
    3. Splits the day's available study hours between subjects in
       proportion to their urgency score (more urgent -> bigger
       share of today's time).
    4. Subtracts whatever time was allocated from each subject's
       remaining hours, and moves on to the next day.

This naturally front-loads urgent subjects, automatically rebalances
every day, and spreads work out instead of cramming everything at the
last minute.
"""

from datetime import date, timedelta
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, flash

app = Flask(__name__)
app.secret_key = "study-planner-secret-key"  # only used for flash messages
DB_PATH = "study_planner.db"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS subjects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            total_hours REAL NOT NULL,
            deadline TEXT NOT NULL,
            priority TEXT NOT NULL DEFAULT 'medium'
        )
        """
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Scheduling algorithm
# ---------------------------------------------------------------------------
def generate_schedule(subjects, daily_hours, horizon_days=14):
    """
    subjects: list of dicts with keys: name, total_hours, deadline (date), priority
    daily_hours: how many hours per day the student can study
    horizon_days: how many days ahead to plan

    Returns:
        schedule: list of dicts, one per day:
            {
              "date": date,
              "allocations": [ {"subject": name, "hours": h}, ... ],
              "total_hours": sum of hours studied that day
            }
        leftover: list of subjects whose work couldn't be finished
                  within the horizon (with hours still remaining)
    """
    # priority multiplier nudges urgency score for high/low priority subjects
    priority_weight = {"high": 1.5, "medium": 1.0, "low": 0.6}

    remaining = {s["name"]: s["total_hours"] for s in subjects}
    deadlines = {s["name"]: s["deadline"] for s in subjects}
    weights = {s["name"]: priority_weight.get(s["priority"], 1.0) for s in subjects}

    today = date.today()
    schedule = []

    for day_offset in range(horizon_days):
        current_day = today + timedelta(days=day_offset)

        # subjects that still need work and haven't passed their deadline
        active = [
            name
            for name in remaining
            if remaining[name] > 0.001 and deadlines[name] >= current_day
        ]

        if not active:
            schedule.append(
                {"date": current_day, "allocations": [], "total_hours": 0}
            )
            continue

        # urgency score for each active subject
        scores = {}
        for name in active:
            days_left = max((deadlines[name] - current_day).days, 1)
            scores[name] = (remaining[name] / days_left) * weights[name]

        total_score = sum(scores.values())
        allocations = []
        hours_left_today = daily_hours

        # sort so the most urgent subject is allocated first (gets priority
        # if rounding leaves a tiny bit of slack)
        for name in sorted(active, key=lambda n: scores[n], reverse=True):
            if total_score <= 0:
                share = hours_left_today / len(active)
            else:
                share = daily_hours * (scores[name] / total_score)

            alloc = min(share, remaining[name], hours_left_today)
            alloc = round(alloc, 2)

            if alloc > 0:
                allocations.append({"subject": name, "hours": alloc})
                remaining[name] -= alloc
                hours_left_today -= alloc

        schedule.append(
            {
                "date": current_day,
                "allocations": allocations,
                "total_hours": round(sum(a["hours"] for a in allocations), 2),
            }
        )

    leftover = [
        {"subject": name, "hours_remaining": round(hrs, 2)}
        for name, hrs in remaining.items()
        if hrs > 0.05
    ]

    return schedule, leftover


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    conn = get_db()
    subjects = conn.execute("SELECT * FROM subjects ORDER BY deadline ASC").fetchall()
    conn.close()
    return render_template("index.html", subjects=subjects, today=date.today())


@app.route("/add", methods=["POST"])
def add_subject():
    name = request.form.get("name", "").strip()
    hours = request.form.get("total_hours", "").strip()
    deadline = request.form.get("deadline", "").strip()
    priority = request.form.get("priority", "medium")

    if not name or not hours or not deadline:
        flash("Please fill in all fields.", "error")
        return redirect(url_for("index"))

    try:
        hours_val = float(hours)
        if hours_val <= 0:
            raise ValueError
    except ValueError:
        flash("Hours needed must be a positive number.", "error")
        return redirect(url_for("index"))

    try:
        deadline_date = date.fromisoformat(deadline)
        if deadline_date < date.today():
            flash("Deadline can't be in the past.", "error")
            return redirect(url_for("index"))
    except ValueError:
        flash("Invalid deadline date.", "error")
        return redirect(url_for("index"))

    conn = get_db()
    conn.execute(
        "INSERT INTO subjects (name, total_hours, deadline, priority) VALUES (?, ?, ?, ?)",
        (name, hours_val, deadline, priority),
    )
    conn.commit()
    conn.close()
    flash(f"Added '{name}' to your subjects.", "success")
    return redirect(url_for("index"))


@app.route("/delete/<int:subject_id>", methods=["POST"])
def delete_subject(subject_id):
    conn = get_db()
    conn.execute("DELETE FROM subjects WHERE id = ?", (subject_id,))
    conn.commit()
    conn.close()
    flash("Subject removed.", "success")
    return redirect(url_for("index"))


@app.route("/plan", methods=["GET", "POST"])
def plan():
    conn = get_db()
    subjects_rows = conn.execute("SELECT * FROM subjects").fetchall()
    conn.close()

    if not subjects_rows:
        flash("Add at least one subject before generating a plan.", "error")
        return redirect(url_for("index"))

    daily_hours = float(request.form.get("daily_hours", request.args.get("daily_hours", 4)))
    horizon_days = int(request.form.get("horizon_days", request.args.get("horizon_days", 14)))

    subjects = []
    for row in subjects_rows:
        subjects.append(
            {
                "name": row["name"],
                "total_hours": row["total_hours"],
                "deadline": date.fromisoformat(row["deadline"]),
                "priority": row["priority"],
            }
        )

    schedule, leftover = generate_schedule(subjects, daily_hours, horizon_days)

    return render_template(
        "plan.html",
        schedule=schedule,
        leftover=leftover,
        daily_hours=daily_hours,
        horizon_days=horizon_days,
    )


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)
