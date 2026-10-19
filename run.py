"""Answer every unanswered quiz round and push the answer files to main.

Runs from GitHub Actions every 5 minutes (and optionally from a local backup
job). Each run syncs with origin/main, answers *every* round that has no file
in answers/ yet, commits and pushes. If the push is rejected (e.g. another
runner pushed first), it re-syncs and tries again.

A round containing a question the solver cannot handle is skipped (no partial
or guessed file is written); the other rounds are still submitted and the run
exits non-zero so the failure is visible.
"""
import csv
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from solver import UnsolvableQuestion, solve

REPO = Path(__file__).resolve().parent
ANSWERS = REPO / "answers"
QUESTIONS_URL = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/questions.csv"
API_URL = "https://api.github.com/repos/FaisalXL/dso429-hourly-quiz/contents/questions.csv"

# Round 168 is released Thu 22 Oct 2026 1:00 PM PDT = 20:00 UTC; the assignment is
# due 2:00 PM PDT = 21:00 UTC. Keep running a few hours past that in case the last
# rounds are released late, then stop for good.
LAST_ROUND = 168
STOP_AT = datetime(2026, 10, 23, 0, 0, tzinfo=timezone.utc)

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"
# The local Mac job is only a backup: it leaves fresh rounds to GitHub Actions
# and only steps in if a round is still unanswered this long after release.
MIN_AGE = timedelta(0) if IN_CI else timedelta(minutes=int(os.environ.get("BACKUP_DELAY_MIN", "40")))


def log(msg):
    print(f"[{datetime.now(timezone.utc):%Y-%m-%d %H:%M:%S} UTC] {msg}", flush=True)


def git(*args, check=True):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r


def fetch_questions():
    """Fetch questions.csv from raw and the API (raw.githubusercontent.com is cached ~5 min)."""
    texts = []
    for url, headers in [(QUESTIONS_URL + f"?t={int(time.time())}", {}),
                         (API_URL, {"Accept": "application/vnd.github.raw"})]:
        for attempt in range(3):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "dso429-hw3", **headers})
                with urllib.request.urlopen(req, timeout=60) as r:
                    texts.append(r.read().decode("utf-8"))
                break
            except Exception as e:
                log(f"fetch attempt {attempt + 1} failed for {url.split('?')[0]}: {e}")
                time.sleep(5)
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
        answers[f"q{qi}"] = solve(row["question_text"], [row[f"option_{c}"] for c in "abcd"])
    return {"round_id": round_id, "answers": answers}


def valid(data, round_id):
    a = data["answers"]
    return (data["round_id"] == round_id and sorted(a) == sorted(f"q{i}" for i in range(1, 26))
            and all(v in ("A", "B", "C", "D") for v in a.values()))


def sync():
    """Make the local branch match origin/main, keeping any uncommitted code edits."""
    git("fetch", "origin", "main")
    git("checkout", "main", check=False)
    if git("reset", "--keep", "origin/main", check=False).returncode != 0:
        git("reset", "--hard", "origin/main")


def answer_missing(rounds):
    """Write a file for every complete, unanswered round. Returns (written ids, failed ids)."""
    now = datetime.now(timezone.utc)
    todo = []
    for rid, qs in rounds.items():
        if (ANSWERS / f"round-{rid:04d}.json").exists():
            continue
        if len(qs) != 25:
            log(f"round {rid} has {len(qs)} questions so far, waiting")
            continue
        released = datetime.fromisoformat(qs[1]["released_at"].replace("Z", "+00:00"))
        if now - released < MIN_AGE:
            log(f"round {rid} is {(now - released).seconds // 60} min old; leaving it to GitHub Actions")
            continue
        todo.append((now - released, rid, qs))

    written, failed = [], []
    ANSWERS.mkdir(exist_ok=True)
    for age, rid, qs in sorted(todo):  # newest first: those still earn full timing credit
        try:
            data = build(rid, qs)
        except (UnsolvableQuestion, KeyError) as e:
            log(f"round {rid} NOT submitted: {e}")
            failed.append(rid)
            continue
        if not valid(data, rid):
            log(f"round {rid} NOT submitted: invalid answer data")
            failed.append(rid)
            continue
        path = ANSWERS / f"round-{rid:04d}.json"
        path.write_text(json.dumps(data, indent=2) + "\n")
        json.loads(path.read_text())  # double-check the file on disk parses
        written.append(rid)
        log(f"wrote {path.name} ({age.total_seconds() / 3600:.1f} h after release)")
    return written, failed


def main():
    if datetime.now(timezone.utc) >= STOP_AT:
        log("assignment is over, nothing to do")
        return 0

    rounds = fetch_questions()
    log(f"questions.csv has rounds {min(rounds)}-{max(rounds)}")
    failed = []
    for attempt in range(1, 6):
        sync()
        written, failed = answer_missing(rounds)
        if not written:
            log("nothing new to submit")
            break
        git("add", "answers")
        who = "GitHub Actions" if IN_CI else "local backup"
        git("commit", "-m", f"Answers for round(s) {', '.join(map(str, sorted(written)))} ({who})")
        r = git("push", "origin", "HEAD:main", check=False)
        if r.returncode == 0:
            log(f"pushed round(s) {sorted(written)} to origin/main")
            break
        log(f"push attempt {attempt} rejected: {r.stderr.strip()}")
        time.sleep(min(60, 5 * 2 ** attempt))
    else:
        log("PUSH FAILED after 5 attempts; the next run will retry")
        return 1

    if failed:
        log(f"ERROR: could not solve round(s) {failed}; fix solver.py")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
