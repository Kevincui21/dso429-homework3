"""Answer every unanswered quiz round and push the answer files to main.

Safe to run repeatedly (e.g. every few minutes from launchd): it only writes
rounds that do not already have a file in answers/, then commits and pushes.
"""
import csv
import io
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from solver import solve

REPO = Path(__file__).resolve().parent
ANSWERS = REPO / "answers"
QUESTIONS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv"
API_URL = "https://api.github.com/repos/FaisalXL/dso429-hourly-quiz/contents/questions.csv"


def log(msg):
    print(f"[{datetime.now().astimezone():%Y-%m-%d %H:%M:%S %Z}] {msg}", flush=True)


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def fetch_questions():
    """Fetch questions.csv; try the API too since raw.githubusercontent.com is cached ~5 min."""
    texts = []
    for url, headers in [(QUESTIONS_URL + f"?t={int(time.time())}", {}),
                         (API_URL, {"Accept": "application/vnd.github.raw"})]:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dso429-hw3", **headers})
            with urllib.request.urlopen(req, timeout=60) as r:
                texts.append(r.read().decode("utf-8"))
        except Exception as e:
            log(f"fetch failed for {url.split('?')[0]}: {e}")
    if not texts:
        raise RuntimeError("could not download questions.csv")
    rounds = {}
    for text in texts:
        for row in csv.DictReader(io.StringIO(text)):
            rounds.setdefault(int(row["round_id"]), {})[int(row["q_index"])] = row
    return rounds


def build(round_id, questions):
    answers = {}
    for qi in range(1, 26):
        row = questions[qi]
        letter, method = solve(row["question_text"], [row[f"option_{c}"] for c in "abcd"])
        if method != "calc":
            log(f"  round {round_id} q{qi}: answered by {method}")
        answers[f"q{qi}"] = letter
    return {"round_id": round_id, "answers": answers}


def valid(path, round_id):
    data = json.loads(path.read_text())
    a = data["answers"]
    return (data["round_id"] == round_id and sorted(a) == sorted(f"q{i}" for i in range(1, 26))
            and all(v in "ABCD" and len(v) == 1 for v in a.values()))


def push_with_retry(attempts=5):
    for i in range(1, attempts + 1):
        r = git("push", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            return True
        log(f"push attempt {i} failed: {r.stderr.strip()}")
        git("pull", "--rebase", "origin", "main", check=False)
        time.sleep(min(60, 5 * 2 ** i))
    return False


def main():
    git("checkout", "main", check=False)
    git("pull", "--rebase", "origin", "main", check=False)
    rounds = fetch_questions()
    ANSWERS.mkdir(exist_ok=True)

    now = datetime.now(timezone.utc)
    todo = []
    for rid, qs in rounds.items():
        path = ANSWERS / f"round-{rid:04d}.json"
        if path.exists() or len(qs) != 25:
            if len(qs) != 25:
                log(f"round {rid} has {len(qs)} questions, skipping for now")
            continue
        released = datetime.fromisoformat(qs[1]["released_at"].replace("Z", "+00:00"))
        todo.append(((now - released).total_seconds() / 3600, rid, qs))

    if not todo:
        log(f"nothing new (latest round {max(rounds)})")
    # Newest first: rounds still inside the 3-hour full-credit window matter most.
    for age, rid, qs in sorted(todo):
        path = ANSWERS / f"round-{rid:04d}.json"
        path.write_text(json.dumps(build(rid, qs), indent=2) + "\n")
        if not valid(path, rid):
            path.unlink()
            raise RuntimeError(f"round {rid} produced an invalid file")
        log(f"wrote {path.name} ({age:.1f} h after release)")

    git("add", "answers")
    if git("diff", "--cached", "--quiet", check=False).returncode != 0:
        ids = sorted(rid for _, rid, _ in todo)
        git("commit", "-m", f"Answers for round(s) {', '.join(map(str, ids)) or 'update'}")

    ahead = git("rev-list", "--count", "origin/main..HEAD", check=False).stdout.strip()
    if ahead not in ("", "0") or git("rev-parse", "--verify", "origin/main", check=False).returncode != 0:
        if push_with_retry():
            log("pushed to origin/main")
        else:
            log("PUSH FAILED; will retry next run")
            sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
