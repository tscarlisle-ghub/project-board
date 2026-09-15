#!/usr/bin/env python3
"""
Reads dictated task lines from inbox/*.txt, matches each to a project on
the board, appends a task, updates data.json, and clears processed inbox
files. Runs inside the "Import Board Tasks" GitHub Action.

Expected line format (one task per line):
  <project fragment>: <task text>[, due <date>][, <assignee>]

Examples:
  Davis LP: submit permit set, due 2026-10-05, Maddie
  Visintainer: send revised elevations, due friday, Kat
  Ivy: schedule site visit, due next tuesday

Project fragments are matched against board project names with substring
matching first, then fuzzy matching (so "Ivy" matches "Sarah & Zach Ivey",
"Visint" matches "Visintainer"). Lines that can't be parsed or matched are
moved to inbox/needs_review.txt instead of being silently dropped.
"""
import json
import re
import difflib
from datetime import date, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data.json"
INBOX_DIR = ROOT / "inbox"
NEEDS_REVIEW = INBOX_DIR / "needs_review.txt"

STAFF_MEMBERS = ["Scott", "Maddie", "Kat", "Kathleen"]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def parse_due(text, today):
    text = text.strip().lower().rstrip(".")
    if not text:
        return None

    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", text)
    if m:
        return text

    if text == "today":
        return today.isoformat()
    if text == "tomorrow":
        return (today + timedelta(days=1)).isoformat()

    m = re.match(r"^(next\s+)?(\w+day)$", text)
    if m and m.group(2) in WEEKDAYS:
        target = WEEKDAYS.index(m.group(2))
        delta = (target - today.weekday()) % 7
        if delta == 0:
            delta = 7
        if m.group(1):
            delta += 7
        return (today + timedelta(days=delta)).isoformat()

    m = re.match(r"^(in\s+)?(\d+)\s+days?$", text)
    if m:
        return (today + timedelta(days=int(m.group(2)))).isoformat()

    m = re.match(r"^(the\s+)?(\d{1,2})(st|nd|rd|th)?$", text)
    if m:
        day = int(m.group(2))
        try:
            candidate = today.replace(day=day)
        except ValueError:
            candidate = None
        if candidate and candidate < today:
            if today.month == 12:
                candidate = candidate.replace(year=today.year + 1, month=1)
            else:
                candidate = candidate.replace(month=today.month + 1)
        if candidate:
            return candidate.isoformat()

    try:
        from dateutil import parser as dateparser
        dt = dateparser.parse(text, default=datetime.combine(today, datetime.min.time()), fuzzy=True)
        return dt.date().isoformat()
    except Exception:
        return None


def match_project(fragment, projects):
    frag = fragment.strip().lower()
    if not frag:
        return None, []

    exact = [p for p in projects if not p.get("archived") and (frag in p["name"].lower() or p["name"].lower() in frag)]
    if len(exact) == 1:
        return exact[0], []
    if len(exact) > 1:
        return None, [p["name"] for p in exact]

    scored = []
    for p in projects:
        if p.get("archived"):
            continue
        name = p["name"].lower()
        tokens = [t for t in re.split(r"[\s,&/-]+", name) if t]
        ratios = [difflib.SequenceMatcher(None, frag, name).ratio()]
        ratios += [difflib.SequenceMatcher(None, frag, t).ratio() for t in tokens]
        best = max(ratios)
        if best >= 0.72:
            scored.append((best, p))

    if not scored:
        return None, []
    scored.sort(key=lambda x: -x[0])
    if len(scored) >= 2 and scored[0][0] - scored[1][0] < 0.05:
        return None, [p["name"] for _, p in scored[:4]]
    return scored[0][1], []


def parse_line(line, projects, today):
    raw = line.strip()
    if not raw:
        return None
    if ":" not in raw:
        return {"error": "no project separator ':' found", "raw": raw}

    frag, rest = raw.split(":", 1)

    who = None
    for name in STAFF_MEMBERS:
        if re.search(rf"\b{name}\b", rest, re.IGNORECASE):
            who = name
            rest = re.sub(rf"\b{name}\b", "", rest, flags=re.IGNORECASE)
            break

    due = None
    m = re.search(r"\bdue\b\s+([^,]+)", rest, re.IGNORECASE)
    if m:
        due = parse_due(m.group(1), today)
        rest = rest[:m.start()] + rest[m.end():]

    task_text = re.sub(r"\s*,\s*,\s*", ", ", rest)
    task_text = task_text.strip(" ,.")
    if not task_text:
        return {"error": "no task text found", "raw": raw}

    project, candidates = match_project(frag, projects)
    if project is None:
        detail = f" (candidates: {', '.join(candidates)})" if candidates else ""
        return {"error": f"project not matched{detail}", "raw": raw}

    return {"project": project, "task": task_text, "due": due, "who": who, "raw": raw}


def main():
    if not INBOX_DIR.exists():
        return
    files = sorted(p for p in INBOX_DIR.glob("*.txt") if p.name != "needs_review.txt")
    if not files:
        return

    data = json.loads(DATA_PATH.read_text())
    projects = data["projects"]
    today = date.today()

    added = []
    failed = []

    for f in files:
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            result = parse_line(line, projects, today)
            if result is None:
                continue
            if "error" in result:
                failed.append(f"{result['raw']}  -- {result['error']}")
                continue

            task_id = data["nextId"]
            data["nextId"] += 1
            created_at = datetime.utcnow().isoformat() + "Z"
            days = 0
            if result["due"]:
                try:
                    days = (date.fromisoformat(result["due"]) - today).days
                except Exception:
                    days = 0

            task = {
                "id": task_id,
                "name": result["task"],
                "days": days,
                "done": False,
                "createdAt": created_at,
                "due": result["due"],
                "who": result["who"],
            }
            result["project"].setdefault("tasks", []).append(task)
            summary = f"{result['project']['name']}: {result['task']}"
            if result["due"]:
                summary += f" (due {result['due']})"
            if result["who"]:
                summary += f" [{result['who']}]"
            added.append(summary)

    if added:
        DATA_PATH.write_text(json.dumps(data, indent=2) + "\n")

    for f in files:
        f.unlink()

    if failed:
        INBOX_DIR.mkdir(exist_ok=True)
        with NEEDS_REVIEW.open("a") as fh:
            for line in failed:
                fh.write(f"[{datetime.utcnow().isoformat()}Z] {line}\n")

    print(f"Added {len(added)} task(s):")
    for a in added:
        print(" -", a)
    if failed:
        print(f"{len(failed)} line(s) need review:")
        for l in failed:
            print(" !", l)


if __name__ == "__main__":
    main()
