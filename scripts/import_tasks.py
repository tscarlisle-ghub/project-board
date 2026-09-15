#!/usr/bin/env python3
"""
Reads dictated task lines from inbox/*.txt, matches each to a project on
the board, appends a task, updates data.json, and clears processed inbox
files. Runs inside the "Import Board Tasks" GitHub Action.

Line format (one task per line) — the project name comes first, either
with a colon or just spoken naturally:
  <project>: <task text>[, due <date>][, <assignee>]
  <project> <task text>[, due <date>][, <assignee>]

Examples:
  Davis LP: submit permit set, due 2026-10-05, Maddie
  Visintainer: send revised elevations, due friday, Kat
  Ray project, revise front door, due friday, Kathleen
  Ivy schedule site visit due next tuesday

Project names are matched against board project names by trying the
longest leading word-sequence first, with substring matching first, then
fuzzy matching (so "Ivy" matches "Sarah & Zach Ivey", "Visint" matches
"Visintainer"). A trailing filler word like "project" or "job" right after
the project name is consumed automatically. Lines that can't be parsed or
matched are moved to inbox/needs_review.txt instead of being silently
dropped or guessed wrong.
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

NUMBER_WORDS = {
    "a": 1, "an": 1, "a couple": 2, "one": 1, "couple": 2, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
}
NUM = r"(\d+|a couple(?:\s+of)?|a|an|couple(?:\s+of)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty)"
UNIT = r"(day|week|month)s?"


def parse_number(tok):
    tok = tok.strip()
    if tok.isdigit():
        return int(tok)
    tok = re.sub(r"\s+of$", "", tok)
    return NUMBER_WORDS.get(tok)


def add_units(base, n, unit):
    unit = unit.rstrip("s")
    if unit == "day":
        return base + timedelta(days=n)
    if unit == "week":
        return base + timedelta(weeks=n)
    if unit == "month":
        import calendar
        total = base.month - 1 + n
        year = base.year + total // 12
        month = total % 12 + 1
        day = min(base.day, calendar.monthrange(year, month)[1])
        return base.replace(year=year, month=month, day=day)
    return base


def resolve_weekday(word, next_prefix, today):
    target = WEEKDAYS.index(word)
    delta = (target - today.weekday()) % 7
    if delta == 0:
        delta = 7
    if next_prefix:
        delta += 7
    return today + timedelta(days=delta)


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

    # "three days from now", "in a couple weeks", "2 months from now"
    m = re.match(rf"^(?:in\s+)?{NUM}\s+{UNIT}(?:\s+from\s+(?:now|today))?$", text)
    if m:
        n = parse_number(m.group(1))
        if n:
            return add_units(today, n, m.group(2)).isoformat()

    # "three days from tomorrow"
    m = re.match(rf"^{NUM}\s+{UNIT}\s+from\s+tomorrow$", text)
    if m:
        n = parse_number(m.group(1))
        if n:
            return add_units(today + timedelta(days=1), n, m.group(2)).isoformat()

    # "three weeks from Tuesday", "two months from next Friday"
    m = re.match(rf"^{NUM}\s+{UNIT}\s+from\s+(next\s+)?(\w+day)$", text)
    if m and m.group(4) in WEEKDAYS:
        n = parse_number(m.group(1))
        if n:
            base = resolve_weekday(m.group(4), m.group(3), today)
            return add_units(base, n, m.group(2)).isoformat()

    # "three weeks from 2026-10-05"
    m = re.match(rf"^{NUM}\s+{UNIT}\s+from\s+(\d{{4}}-\d{{2}}-\d{{2}})$", text)
    if m:
        n = parse_number(m.group(1))
        if n:
            base = date.fromisoformat(m.group(3))
            return add_units(base, n, m.group(2)).isoformat()

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


def score_project(fragment, projects):
    """Return [(score, project), ...] sorted best-first for how well fragment
    matches each non-archived project. Exact substring matches score 1.0;
    otherwise fuzzy-matched against the full name and each name token."""
    frag = fragment.strip().lower()
    if not frag:
        return []

    exact = [p for p in projects if not p.get("archived") and (frag in p["name"].lower() or p["name"].lower() in frag)]
    if exact:
        return [(1.0, p) for p in exact]

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
    scored.sort(key=lambda x: -x[0])
    return scored


def match_project(fragment, projects):
    scored = score_project(fragment, projects)
    if not scored:
        return None, []
    if len(scored) >= 2 and scored[0][0] - scored[1][0] < 0.05:
        return None, [p["name"] for _, p in scored[:4]]
    return scored[0][1], []


FILLER_WORDS = {"project", "job"}


def score_project_prefix(fragment, projects):
    """Like score_project, but only used for leading-prefix detection: the
    exact-match check only accepts the candidate being contained IN the
    project name (never the reverse), and the fuzzy threshold is higher —
    otherwise a short project name matches inside any long candidate that
    happens to start with it, swallowing task words into the project."""
    frag = fragment.strip().lower()
    if not frag:
        return []

    exact = [p for p in projects if not p.get("archived") and frag in p["name"].lower()]
    if exact:
        return [(1.0, p) for p in exact]

    scored = []
    for p in projects:
        if p.get("archived"):
            continue
        name = p["name"].lower()
        tokens = [t for t in re.split(r"[\s,&/-]+", name) if t]
        ratios = [difflib.SequenceMatcher(None, frag, name).ratio()]
        ratios += [difflib.SequenceMatcher(None, frag, t).ratio() for t in tokens]
        best = max(ratios)
        if best >= 0.84:
            scored.append((best, p))
    scored.sort(key=lambda x: -x[0])
    return scored


def find_project_prefix(text, projects, max_words=6):
    """Try to find a project name as a leading run of words in text, longest
    run first. Returns (project, words_consumed) or (None, 0). A filler word
    like "project" right after the matched name is consumed too."""
    words = text.split()
    if not words:
        return None, 0

    top = min(max_words, len(words))
    for k in range(top, 0, -1):
        window = words[:k]
        score_words = window
        if score_words and score_words[-1].strip(",.").lower() in FILLER_WORDS:
            score_words = score_words[:-1]
        if not score_words:
            continue
        candidate = " ".join(score_words)
        scored = score_project_prefix(candidate, projects)
        if not scored:
            continue
        if len(scored) >= 2 and scored[0][0] - scored[1][0] < 0.05:
            continue
        consumed = k
        if consumed < len(words) and words[consumed].strip(",.").lower() in FILLER_WORDS:
            consumed += 1
        return scored[0][1], consumed

    return None, 0


def parse_line(line, projects, today):
    raw = line.strip()
    if not raw:
        return None

    working = raw

    who = None
    for name in STAFF_MEMBERS:
        if re.search(rf"\b{name}\b", working, re.IGNORECASE):
            who = name
            working = re.sub(rf"\b{name}\b", "", working, flags=re.IGNORECASE)
            break

    due = None
    m = re.search(r"\bdue\b\s+([^,]+)", working, re.IGNORECASE)
    if m:
        due = parse_due(m.group(1), today)
        working = working[:m.start()] + working[m.end():]

    working = re.sub(r"\s*,\s*,\s*", ", ", working).strip(" ,.")

    if ":" in working:
        frag, task_text = working.split(":", 1)
        task_text = re.sub(r"^[\s,]+", "", task_text).strip(" ,.")
        project, candidates = match_project(frag, projects)
        if project is None:
            detail = f" (candidates: {', '.join(candidates)})" if candidates else ""
            return {"error": f"project not matched{detail}", "raw": raw}
        if not task_text:
            return {"error": "no task text found", "raw": raw}
        return {"project": project, "task": task_text, "due": due, "who": who, "raw": raw}

    # No colon — find the project as the leading words of the line instead,
    # so natural dictation like "Ray project, revise front door" works too.
    words = working.split()
    project, consumed = find_project_prefix(working, projects)
    if project is None:
        return {"error": "project not matched (say the project name first)", "raw": raw}

    task_text = " ".join(words[consumed:])
    task_text = task_text.strip(" ,.")
    if not task_text:
        return {"error": "no task text found", "raw": raw}

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
