"""Deterministic solver for the DSO 429 hourly quiz questions.

Each question template is recognised with a regex, the answer is computed
exactly, and the option whose value is closest to the computed answer wins.
Unknown templates fall back to an LLM via OpenRouter (if a key is set).
"""
import json
import math
import os
import re
import urllib.request
from datetime import datetime, timedelta

NUM = r"\$?(-?[\d,]+(?:\.\d+)?)"


def num(s):
    return float(s.replace(",", "").replace("$", ""))


def option_value(opt):
    """Turn an option like '$1,234.50', '61.0%', '667 units' or a date into a comparable value."""
    opt = opt.strip()
    try:
        return datetime.strptime(opt, "%d %B %Y")
    except ValueError:
        pass
    m = re.search(r"-?[\d,]*\.?\d+", opt.replace("$", ""))
    return num(m.group(0)) if m else None


def pick(target, options):
    """Return the letter of the option closest to target (number or date)."""
    best, best_d = None, None
    for letter, opt in zip("ABCD", options):
        v = option_value(opt)
        if v is None or type(v) is not type(target):
            continue
        d = abs((v - target).days) if isinstance(target, datetime) else abs(v - target)
        if best_d is None or d < best_d:
            best, best_d = letter, d
    return best


def compute(q):
    """Return the exact answer for a known template, or None."""
    m = re.search(r"invoice for " + NUM + r" is converted to \w+ at ([\d.]+) \w+ per", q)
    if m:
        return num(m.group(1)) * float(m.group(2))
    m = re.search(r"sells ([\d,]+) units at " + NUM + " each", q)
    if m:
        return num(m.group(1)) * num(m.group(2))
    m = re.search(r"generates " + NUM + r" in revenue against " + NUM + " in cost", q)
    if m:
        rev, cogs = num(m.group(1)), num(m.group(2))
        return (rev - cogs) / rev * 100
    m = re.search(r"Fixed costs are " + NUM + r"\. Each unit sells for " + NUM + r" with a variable cost of " + NUM, q)
    if m:
        f, p, v = num(m.group(1)), num(m.group(2)), num(m.group(3))
        return f / (p - v)
    m = re.search(r"listed at " + NUM + r" is discounted by ([\d.]+)%", q)
    if m:
        return num(m.group(1)) * (1 - float(m.group(2)) / 100)
    m = re.search(r"moved from " + NUM + " to " + NUM, q)
    if m:
        a, b = num(m.group(1)), num(m.group(2))
        return (b - a) / a * 100
    m = re.search(r"investment of " + NUM + r" grows at ([\d.]+)% compounded (\w+)\. What is its value after ([\d.]+) years", q)
    if m:
        p, r, freq, n = num(m.group(1)), float(m.group(2)) / 100, m.group(3), float(m.group(4))
        k = {"annually": 1, "semiannually": 2, "semi-annually": 2, "quarterly": 4, "monthly": 12, "daily": 365}.get(freq, 1)
        return p * (1 + r / k) ** (n * k)
    m = re.search(r"A had ([\d,]+) orders averaging " + NUM + r"; region B had ([\d,]+) orders averaging " + NUM, q)
    if m:
        n1, a1, n2, a2 = (num(g) for g in m.groups())
        return (n1 * a1 + n2 * a2) / (n1 + n2)
    m = re.search(r"holds (.+?)\. If one is drawn at random, what is the probability it is an? (\w+) ticket", q)
    if m:
        counts = {kind: num(n) for n, kind in re.findall(r"([\d,]+) (\w+) tickets", m.group(1))}
        if m.group(2) in counts:
            return counts[m.group(2)] / sum(counts.values()) * 100
    m = re.search(r"A " + NUM + r" .*? is split between two \w+ in the ratio ([\d.]+):([\d.]+)\. What does the (first|second)", q)
    if m:
        total, a, b = num(m.group(1)), float(m.group(2)), float(m.group(3))
        return total * (a if m.group(4) == "first" else b) / (a + b)
    m = re.search(r"series runs ([-\d., ]+?)\. If the pattern continues", q)
    if m:
        xs = [num(x) for x in m.group(1).split(",")]
        diffs = [b - a for a, b in zip(xs, xs[1:])]
        if all(abs(d - diffs[0]) < 1e-9 for d in diffs):
            return xs[-1] + diffs[0]
        ratios = [b / a for a, b in zip(xs, xs[1:]) if a]
        if len(ratios) == len(diffs) and all(abs(r - ratios[0]) < 1e-9 for r in ratios):
            return xs[-1] * ratios[0]
        dd = [b - a for a, b in zip(diffs, diffs[1:])]
        if dd and all(abs(d - dd[0]) < 1e-9 for d in dd):
            return xs[-1] + diffs[-1] + dd[0]
        return None
    m = re.search(r"signed on (\d{1,2} \w+ \d{4}) has an? ([\d,]+)-day", q)
    if m:
        return datetime.strptime(m.group(1), "%d %B %Y") + timedelta(days=num(m.group(2)))
    return None


def ask_llm(q, options):
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return None
    prompt = (q + "\n" + "\n".join(f"{l}) {o}" for l, o in zip("ABCD", options))
              + "\nWork it out carefully, then reply with only the single letter of the correct option.")
    body = json.dumps({"model": "openai/gpt-4.1-mini", "temperature": 0,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request("https://openrouter.ai/api/v1/chat/completions", data=body,
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            text = json.load(r)["choices"][0]["message"]["content"]
        letters = re.findall(r"\b([ABCD])\b", text)
        return letters[-1] if letters else None
    except Exception as e:
        print(f"  LLM fallback failed: {e}")
        return None


def solve(q, options):
    """Return (letter, method). Always returns a valid letter."""
    target = compute(q)
    if target is not None:
        letter = pick(target, options)
        if letter:
            return letter, "calc"
    letter = ask_llm(q, options)
    if letter:
        return letter, "llm"
    return "A", "guess"
