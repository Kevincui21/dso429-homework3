"""Check solver accuracy on practice rounds 1-48 against practice_keys.csv."""
import csv
import io
import urllib.request

from solver import solve

BASE = "https://raw.githubusercontent.com/FaisalXL/dso429-hourly-quiz/main/"


def fetch_csv(name):
    with urllib.request.urlopen(BASE + name, timeout=60) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))


keys = {(int(k["round_id"]), int(k["q_index"])): k["answer"].strip() for k in fetch_csv("practice_keys.csv")}
right = total = 0
methods = {}
for row in fetch_csv("questions.csv"):
    key = (int(row["round_id"]), int(row["q_index"]))
    if key not in keys:
        continue
    opts = [row[f"option_{c}"] for c in "abcd"]
    letter, method = solve(row["question_text"], opts)
    methods[method] = methods.get(method, 0) + 1
    total += 1
    if letter == keys[key]:
        right += 1
    else:
        print("WRONG", key, row["question_text"], opts, "got", letter, "want", keys[key])
print(f"Practice accuracy: {right}/{total} = {right / total:.2%}   methods: {methods}")
