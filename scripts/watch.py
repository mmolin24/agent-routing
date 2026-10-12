"""Live routing view in the terminal (`route.py --watch`).

Redraws in place several times a second (spinners, flowing links) while a
background thread refreshes the data every few seconds, so slow SSH calls
never freeze the screen. Lanes sit side by side when the terminal is wide
enough, otherwise they stack. Ctrl+C restores the terminal and exits.
"""

import os
import re
import shutil
import sys
import threading
import time

import dashboard as M

R = M.V

ESC = "\033["
RESET = ESC + "0m"
COL = {"lane-desktop": "38;5;75", "lane-mac": "38;5;177", "orch": "38;5;214", "working": "38;5;214",
       "idle": "38;5;78", "done": "38;5;78", "blocked": "38;5;203", "unknown": "38;5;245",
       "muted": "38;5;244", "fg": "38;5;255", "rule": "38;5;238"}
SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
FLOW = ["─╴", "╶─"]
ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


def c(text, key, bold=False):
    return f"{ESC}{'1;' if bold else ''}{COL.get(key, COL['fg'])}m{text}{RESET}"


def vlen(s):
    return len(ANSI_RE.sub("", s))


def fit(s, width):
    """Pad or cut a colored string to exactly `width` visible columns."""
    out, n, i = [], 0, 0
    while i < len(s) and n < width:
        m = ANSI_RE.match(s, i)
        if m:
            out.append(m.group(0)); i = m.end(); continue
        out.append(s[i]); n += 1; i += 1
    if width <= 0:
        return ""
    if i < len(s) and n >= width and out:
        out[-1] = "…"
    return "".join(out) + RESET + " " * (width - n)


def dot(status, frame):
    if status == "working":
        return c(SPIN[frame % len(SPIN)], "working", True)
    if status == "blocked":
        return c("◉" if frame % 4 < 2 else "○", "blocked", True)
    return c("●", status if status in COL else "unknown")


def short_model(m):
    """claude-opus-5-5 -> Opus 5.5; other ids unchanged."""
    mm = re.match(r"claude-([a-z]+)-(\d+)-(\d+)", m or "")
    return f"{mm.group(1).title()} {mm.group(2)}.{mm.group(3)}" if mm else (m or "")


def profile(a):
    """`Opus 5.5 medium ⚡fast`; `≈` when agents share a directory and the profile may be a neighbour's (no session id); kind if unknown."""
    if not a.get("model"):
        return c(a.get("agent") or "?", "muted")
    txt = (c("≈", "muted") if a.get("profile_shared") else "") + c(short_model(a["model"]), "fg")
    if a.get("effort"):
        txt += c(f" {a['effort']}", "orch")
    if a.get("fast"):
        txt += " " + c("⚡fast", "working", True)
    return txt


def state(st):
    return c(st or "unknown", st if st in COL else "unknown")


def gauge(used, cap):
    if not isinstance(cap, int):
        return c(f"{used}", "fg")
    return c("▰" * min(used, cap), "working") + c("▱" * max(cap - used, 0), "rule") + c(f" {used}/{cap}", "fg")


LEVEL_COL = {"ok": "idle", "warn": "working", "hot": "blocked", "unknown": "unknown", None: "unknown"}


def bar(frac, level, width=6):
    n = max(0, min(width, round(frac * width)))
    return c("▰" * n, LEVEL_COL.get(level)) + c("▱" * (width - n), "rule")


def load_line(ld, lv):
    """cpu ▮▮▯▯▯▯ 0.8/16  mem 13.0/15.2G free  swap 0/4G  docker 1.2/15G (3)  — colored ok/warn/hot."""
    if not ld:
        return c("load unavailable", "unknown")
    gb = lambda b: f"{(b or 0) / 2**30:.1f}"
    parts = []
    if ld.get("load1") is not None and ld.get("cores"):
        parts.append(c("cpu ", "muted") + bar(ld["load1"] / ld["cores"], lv.get("cpu")) +
                     c(f" {ld['load1']:.1f}/{ld['cores']}", LEVEL_COL.get(lv.get("cpu"))))
    if ld.get("mem_total"):
        used = 1 - (ld.get("mem_avail") or 0) / ld["mem_total"]
        parts.append(c("mem ", "muted") + bar(used, lv.get("mem")) +
                     c(f" {gb(ld.get('mem_avail'))}G free", LEVEL_COL.get(lv.get("mem"))) +
                     (c(f" pressure {ld['pressure']}", LEVEL_COL.get(lv.get("mem"))) if ld.get("pressure") not in (None, "normal") else ""))
    if ld.get("swap_total"):
        parts.append(c("swap ", "muted") + c(f"{gb(ld.get('swap_used'))}/{gb(ld['swap_total'])}G", LEVEL_COL.get(lv.get("swap"))))
    if ld.get("docker") == "up":
        parts.append(c("docker ", "muted") + c(f"{gb(ld.get('docker_used'))}/{gb(ld.get('docker_limit'))}G", LEVEL_COL.get(lv.get("docker"))) +
                     c(f" ({ld.get('containers', 0)})", "muted"))
    elif ld.get("docker"):
        parts.append(c("docker down", "blocked"))
    head = c("● ", LEVEL_COL.get(lv.get("overall")), True)
    tail = c("  HOT: heavy work queues here", "blocked", True) if lv.get("hot") else ""
    return head + "  ".join(parts) + tail


def lane_lines(cfg, h, lane, frame):
    name, desc, _, style = M.host_style(cfg, h)
    key = style if style in COL else "unknown"
    out = [c(f"{name.upper()}", key, True) + c(f" · {desc}", "muted"),
           c("heavy ", "muted") + gauge(lane.get("heavy", 0), lane.get("max_heavy")) +
           c(f"   queue {len(lane['queued'])}/{lane.get('max_queue', '–')}   light {lane.get('light', 0)}", "muted") +
           (c("   " + M.slots_text(lane), "working" if (lane.get("slots") or {}).get("waiting") else "muted")
            if lane.get("slots") is not None else "")]
    out.append(load_line(lane.get("load"), lane.get("levels") or {}))
    items = lane["tasks"] + [{"queued": q} for q in lane["queued"]]
    if not items:
        out.append(c("└─ ", key) + c("no routed tasks", "muted"))
    for i, t in enumerate(items):
        last = i == len(items) - 1
        branch, stem = ("└─ ", "   ") if last else ("├─ ", "│  ")
        if "queued" in t:
            out.append(c(branch, key) + c("○ ", "unknown") + c(t["queued"], "fg") + c(f"  queued #{i - len(lane['tasks']) + 1}", "muted"))
            continue
        tree = M.task_tree(t["agents"])
        worker = tree["agent"] if tree else None
        load = c("heavy", "working") if t["heavy"] else c("light", "muted")
        title = t.get("summary") or ""
        if title.upper().startswith(M.issue_label(t["task_id"])):
            title = title[len(M.issue_label(t["task_id"])):].lstrip(" :·-")
        out.append(c(branch, key) + c("◆ ", key, True) + c(M.issue_label(t["task_id"]), "fg", True) +
                   (c("  " + title, "fg") if title else "") + "  " + load +
                   c(f"  {t['task_id']} · {M.where(t)}" +
                     ("" if t["dispatch"] == "dispatched" else f" · {t['dispatch']}"), "muted"))
        if worker:
            st = worker.get("status")
            link = c(FLOW[frame % 2] if st == "working" else "──", "working" if st == "working" else key)
            out.append(c(stem, key) + c("└", key) + link + " " + dot(st, frame) + " " +
                       c(M.short_name(worker, M.task_prefix(t["task_id"])), "fg", True) + "  " + profile(worker) +
                       c(" · ", "muted") + state(st))
            out += tree_lines(tree["children"], c(stem, key) + "    ", key, frame, 2, M.task_prefix(t["task_id"]))
    if lane["stray"]:
        mine = [s for s in lane["stray"] if s.get("issue")]
        rest = [s for s in lane["stray"] if not s.get("issue")]
        for s in mine:   # a worker re-prompted after its task closed (e.g. a fix round)
            out.append(c("↺ ", "working") + c(M.short_name(s), "fg", True) + c(f" → {s['issue']}", "orch") + "  " +
                       profile(s) + c(" · ", "muted") + state(s.get("status")) + c("  (fix round)", "muted"))
        if rest:
            names = ", ".join(f"{M.short_name(s)} ({short_model(s.get('model')) or s.get('agent')}"
                              f"{' ' + s['effort'] if s.get('effort') else ''}{' fast' if s.get('fast') else ''}, {s.get('status')})"
                              for s in rest)
            out.append(c("not routed: ", "muted") + c(names, "unknown"))
    if lane.get("quiet"):
        out.append(c(f"not routed: {lane['quiet']} idle agent{'s' if lane['quiet'] != 1 else ''} hidden", "muted"))
    return out


MAX_DEPTH = 8
# How each step class (set by route.py's classify) is drawn: glyph, color.
STEP_LOOK = {"done": ("✓", "idle"), "fail": ("✗", "blocked"), "need": ("⚑ decision", "blocked"),
             "wait": ("◌ stopped", "working"), "stale": ("↻ re-run", "working"), "unknown": ("?", "unknown"),
             "queued": ("⧗ queued", "muted"), "active": ("…", "working")}


def step_cell(name, st, frame):
    """impl ✓ / gate ⠋ mac / review ↻ re-run / deploy … running / merge ·"""
    if not st:
        return c(f"{name} ·", "rule")
    cls = st.get("cls", "active")
    if cls == "skip":
        return c(f"{name} – n/a", "muted")
    if cls == "working":
        return c(name, "fg") + " " + c(SPIN[frame % len(SPIN)], "working", True) + c(f" {st.get('host')}", "muted")
    glyph, col = STEP_LOOK.get(cls, ("…", "working"))
    extra = ""
    if cls == "queued":
        extra = c(f" #{st.get('queued')} {st.get('host')}", "muted")
    elif cls in ("need", "wait") and st.get("host"):
        extra = c(f" {st['host']}", "muted")
    elif cls == "active":
        extra = c(f" {st.get('state')}", "muted") + (c(" ⚠ quiet", "blocked") if st.get("quiet") else "")
    elif cls == "unknown":
        extra = c(" unreachable" if "unreachable" in st.get("label", "") else " no result", "unknown")
    return c(name, "fg") + " " + c(glyph, col, cls in ("done", "fail", "need")) + extra


def step_glyph(name, st, frame):
    """The compact cell for the one-line pipeline: impl ✓ · gate ⠋ · staging ⚑ · merge ·"""
    if not st:
        return c(f"{name} ·", "rule")
    cls = st.get("cls", "active")
    if cls == "skip":
        return c(f"{name} –", "muted")
    if cls == "working":
        return c(name, "fg") + " " + c(SPIN[frame % len(SPIN)], "working", True)
    glyph, col = STEP_LOOK.get(cls, ("…", "working"))
    glyph = glyph.split(" ")[0]                           # "⚑ decision" -> "⚑"
    return c(name, "fg") + " " + c(glyph, col, cls in ("done", "fail", "need")) + (c(" ⚠", "blocked") if st.get("quiet") else "")


# Most urgent first: what the one-line view says the issue needs next.
NEXT_RANK = {"need": 0, "fail": 1, "wait": 2, "stale": 3, "unknown": 4}
NEXT_TEXT = {"need": "{n} needs a decision", "fail": "{n} failed", "wait": "{n} worker stopped: read it",
             "stale": "{n}: re-run on the new head", "unknown": "{n}: no result recorded"}


def next_action(r):
    """(text, color) of the issue's most urgent item, plus '+N more'; ("", "") when calm."""
    items = []
    for i, n in enumerate(R.row_pipeline(r)):
        st = r["steps"].get(n) or {}
        cls = st.get("cls")
        if cls in NEXT_RANK:
            items.append((NEXT_RANK[cls], i, NEXT_TEXT[cls].format(n=n), "blocked" if cls in ("need", "fail") else "working"))
        elif st.get("quiet"):
            m = re.search(r"no update for (\S+)", st.get("label") or "")
            items.append((5, i, f"{n} quiet" + (f" {m.group(1)}" if m else ""), "working"))
    lin = r["steps"].get(R.tracker_kind(r)) or {}
    if lin.get("behind"):
        items.append((6, 0, f"{R.tracker_label(r)} should be {lin['behind']}", "blocked"))
    if not items:
        return "", ""
    items.sort()
    more = f"  +{len(items) - 1}" if len(items) > 1 else ""
    return items[0][2] + more, items[0][3]


def detail_lines(r):
    """The expanded issue: every step's full label, where and when, then PR and Linear."""
    out = []
    for n in R.row_pipeline(r):
        st = r["steps"].get(n)
        if not st:
            continue
        glyph, col = STEP_LOOK.get(st.get("cls", "active"), ("…", "working"))
        where = " · ".join(x for x in (st.get("host"), st.get("task"), ago(st["at"]) if st.get("at") else None) if x)
        label = st.get("label") or st.get("state") or ""
        out.append("        " + c(f"{n:8}", "fg") + c(glyph.split(" ")[0] + " ", col) + c(label[:140], "fg") +
                   (c(f"   {where}", "muted") if where else ""))
    pr = r.get("pr") or {}
    if pr.get("url"):
        out.append("        " + c("PR      ", "fg") + c(pr["url"], "muted") + ci_text(pr))
    lin = r["steps"].get(R.tracker_kind(r))
    if lin:
        out.append("        " + c(f"{R.tracker_label(r):8}", "fg") + linear_text(lin, R.tracker_label(r)).strip())
    return out


def linear_text(lin, label="Linear"):
    """Linear: in-review (live | recorded 3h ago | from the pipeline) ⚠ should be in-review"""
    if not lin:
        return ""
    src = ("live" if lin.get("live") else "from the pipeline" if lin.get("inferred")
           else (lin.get("note") or "").split(" · ")[0] if lin.get("at") else "")
    return (c(f"   {label}: {lin.get('state')}", "muted") + (c(f" ({src})", "rule") if src else "") +
            (c(f" ⚠ should be {lin['behind']}", "blocked") if lin.get("behind") else ""))


def ci_text(pr):
    ck = (pr or {}).get("checks")
    if not ck or not sum(ck.values()):
        return ""
    if ck["fail"]:
        return c(f"  CI ✗ {ck['fail']}", "blocked")
    if ck["pending"]:
        return c(f"  CI … {ck['pending']}", "working")
    return c("  CI ✓", "idle")


def attention(r):
    """Lines under an issue for steps that need a look: stale, quiet, no result, decisions."""
    out = []
    for n in R.row_pipeline(r):
        st = r["steps"].get(n) or {}
        if st.get("cls") in ("stale", "need", "unknown", "wait") or st.get("quiet"):
            text = f"{n}: {st.get('label')}"
            out.append("       " + c(text if len(text) <= 110 else text[:109] + "…", "blocked" if st.get("cls") == "need" else "working"))
    return out[:3]


def ago(ts):
    import time as _t
    d = max(0, _t.time() - (ts or 0))
    return f"{d / 60:.0f}m ago" if d < 3600 else f"{d / 3600:.0f}h ago" if d < 86400 else f"{d / 86400:.0f}d ago"


EXPAND = False  # route.py --watch --expand: full rows for merged and completed issues


def inbox_text(agent, fb):
    """For the routing maintainer: its feedback inbox, e.g. ' · inbox 2 waiting, 3 fixed today'."""
    if agent.get("name") != "routing-maintainer" or not fb:
        return ""
    return c(f" · inbox {fb.get('new', 0)} waiting, {fb.get('done_24h', 0)} fixed today",
             "working" if fb.get("new") else "muted")


def usage_lines(usage):
    """USAGE  codex pro · week 85% (resets Wed 12:07) · credits 12.3k
              claude · 5h 23% (resets in 3h) · week 41% (resets Mon 09:00)"""
    col = {"ok": "muted", "warn": "working", "hot": "blocked", "unknown": "rule"}
    out = []
    for i, (tool, txt, lvl) in enumerate(M.usage_parts(usage)):
        out.append(" " + c("USAGE  " if i == 0 else "       ", "fg", True) + c(f"{tool} ", "fg") + c(txt, col[lvl]))
    return out + [""]


def landed(r):
    """Merged but not yet completed: later pipeline checks remain, so fold it."""
    return "merge" in R.row_pipeline(r) and (r["steps"].get("merge") or {}).get("cls") == "done" and not r.get("completed")


def remaining_steps(r):
    pipeline = R.row_pipeline(r)
    return pipeline[pipeline.index("merge") + 1:] if "merge" in pipeline else pipeline


def awaiting_step(r):
    return next((n for n in remaining_steps(r) if (r["steps"].get(n) or {}).get("cls") not in ("done", "skip")),
                R.row_pipeline(r)[-1])


def clean_title(r, n):
    title = r.get("title") or ""
    if title.upper().startswith(r["issue"]):
        title = title[len(r["issue"]):].lstrip(" :·-")
    if len(title) <= n:
        return title
    cut = title[:n - 1].rsplit(" ", 1)[0] if " " in title[:n - 1] else title[:n - 1]   # whole words
    return cut.rstrip(" ,:;-·(") + "…"


OPEN = set()            # issues the viewer expanded (click, or Tab + Enter)
SEL = {"key": None}     # the issue Tab/Enter act on


def issue_lines(issues, frame, width, expand=False, rows=None):
    """One line per open issue: its pipeline and the one thing it needs next. A click
    (or Tab + Enter) opens the issue's details below it. Merged ones fold to one line,
    and the completed backlog to a single line (--expand shows everything). `rows`
    collects {line index: issue} for clicks."""
    if not issues:
        return []
    other = [r for r in issues if r.get("unrouted")]
    issues = [r for r in issues if not r.get("unrouted")]
    done = [r for r in issues if r.get("completed")]
    merged = [r for r in issues if landed(r)] if not expand else []
    active = [r for r in issues if not r.get("completed") and r not in merged]
    n_merged = sum(landed(r) for r in issues)
    out = [" " + c("ISSUES", "fg", True) + c(f"  {len(active) - (0 if not expand else n_merged)} open · {n_merged} merged · "
                                             f"{len(done)} completed (14 days)", "muted")]
    for r in active[:12]:
        key, opened = r["issue"], r["issue"] in OPEN
        pr = r.get("pr") or {}
        prtxt = (c(f"  #{pr['number']}", "muted") + (c(" draft", "rule") if pr.get("draft") else "") + ci_text(pr)) if pr else ""
        pipe = c(" › ", "rule").join(step_glyph(n, r["steps"].get(n), frame) for n in R.row_pipeline(r))
        nxt, col = next_action(r)
        mark = c("▾ " if opened else "▸ ", "orch", True) if SEL["key"] != key else c("▼ " if opened else "▶ ", "working", True)
        left = " " + mark + c(key, "fg", True) + "  " + c(clean_title(r, 34), "fg") + prtxt + "  " + pipe
        room = width - vlen(left) - 4
        tail = (c("  → ", "rule") + c(nxt if len(nxt) <= room else nxt[:max(0, room - 1)] + "…", col)) if nxt and room > 8 else ""
        if rows is not None:
            rows[len(out)] = key
        out.append(left + tail)
        if opened:
            for ln in detail_lines(r):
                if rows is not None:
                    rows[len(out)] = key
                out.append(ln)
    # Merged issues fold into one line; any that needs a look (a stuck or failed deploy, a
    # quiet step, Linear behind) keeps its own line. --expand lists them all in full.
    def needs_look(r):
        return (any((r["steps"].get(n) or {}).get("quiet") or
                    (r["steps"].get(n) or {}).get("cls") in ("fail", "unknown", "need")
                    for n in remaining_steps(r)) or (r["steps"].get(R.tracker_kind(r)) or {}).get("behind"))
    calm = [r for r in merged if not needs_look(r)]
    merged = [r for r in merged if needs_look(r)]
    if calm:
        names = ", ".join(r["issue"] for r in calm[:8]) + (f" +{len(calm) - 8}" if len(calm) > 8 else "")
        waiting = [awaiting_step(r) for r in calm]
        order = list(dict.fromkeys(n for r in calm for n in reversed(remaining_steps(r))))
        last_steps = {R.row_pipeline(r)[-1] for r in calm}
        parts = [f"{waiting.count(n)} awaiting {n}" for n in order if waiting.count(n) or n in last_steps]
        out.append(" " + c("▸ ", "muted") + c(f"{len(calm)} merged", "idle") + c(f" · {', '.join(parts)}: ", "muted") +
                   c(names, "rule") + c("  (--expand to list)", "rule"))
    for r in merged:
        pr = r.get("pr") or {}
        name = awaiting_step(r)
        st = r["steps"].get(name) or {}
        nxt = f"{name} {st.get('state') or 'pending'}"
        quiet = any((r["steps"].get(n) or {}).get("quiet") for n in remaining_steps(r))
        lin = r["steps"].get(R.tracker_kind(r)) or {}
        out.append(" " + c("▸ ", "muted") + c(r["issue"], "fg") + "  " + c(clean_title(r, 48), "muted") +
                   (c(f"  #{pr['number']}", "muted") if pr else "") + c("  merged", "idle") + c(f" · {nxt}", "muted") +
                   (c(" ⚠ quiet", "blocked") if quiet else "") +
                   (c(f" · {R.tracker_label(r)} ⚠ should be {lin['behind']}", "blocked") if lin.get("behind") else ""))
    if other:
        red = sum(1 for r in other if ((r.get("pr") or {}).get("checks") or {}).get("fail"))
        names = ", ".join(r["issue"] + (" ✗" if ((r.get("pr") or {}).get("checks") or {}).get("fail") else "")
                          for r in (other if expand else other[:8])) + ("" if expand or len(other) <= 8 else f" +{len(other) - 8}")
        out.append(" " + c(f"○ {len(other)} other open PRs", "muted") + (c(f" ({red} CI ✗)", "blocked") if red else "") +
                   c(": " + names, "rule"))
    if done:
        if expand:
            out.append(" " + c("COMPLETED", "idle", True))
            for r in done:
                pr = r.get("pr") or {}
                out.append("   " + c("✓ ", "idle", True) + c(r["issue"], "fg") + "  " + c(clean_title(r, 56), "muted") +
                           (c(f"  #{pr['number']}", "muted") if pr else "") + c(f"  {ago(r.get('updated'))}", "muted"))
        else:
            names = ", ".join(r["issue"] for r in done[:8]) + (f" +{len(done) - 8}" if len(done) > 8 else "")
            out.append(" " + c(f"✓ {len(done)} completed: ", "idle") + c(names, "muted"))
    return out + [""]


def tree_lines(children, prefix, key, frame, depth, strip=""):
    """Recursive ├─/└─ tree; deeper than MAX_DEPTH collapses to a count."""
    out = []
    for k, n in enumerate(children):
        last = k == len(children) - 1
        a, st = n["agent"], n["agent"].get("status")
        if depth > MAX_DEPTH:
            out.append(prefix + c("└─ ", key) + c(f"… {count(children)} more below", "muted"))
            break
        mark = c("◇ ", "muted") if n["kind"] == "sub" else dot(st, frame) + " "
        if n["kind"] == "sub":
            t = a.get("sub_type")
            what = (c(f"{t} ", "muted") if t and t != "general-purpose" else "") + \
                (c(short_model(a.get("model")), "fg") if a.get("model") else c("subagent", "muted"))
        else:
            what = profile(a)
        label = a.get("name") if n["kind"] == "sub" else M.short_name(a, strip)
        out.append(prefix + c("└─ " if last else "├─ ", key) + mark + c(label, "fg") + "  " +
                   what + c(" · ", "muted") + state(st))
        out += tree_lines(n["children"], prefix + c("   " if last else "│  ", key), key, frame, depth + 1, strip)
    return out


def count(nodes):
    return sum(1 + count(n["children"]) for n in nodes)


def frame_lines(snap, cfg, frame, width, every, age, error):
    report, queues, cap = snap
    known = cfg.get("known_agents", {})
    hosts, orchs = M.build_model(cfg, report, queues, cap, known)
    order = M.host_order(cfg, hosts)
    working = sum(1 for h in order for t in hosts[h]["tasks"] for a in t["agents"] if a.get("status") == "working")
    blocked = sum(1 for h in order for t in hosts[h]["tasks"] for a in t["agents"] if a.get("status") == "blocked")
    tasks = sum(len(hosts[h]["tasks"]) for h in order)
    queued = sum(len(hosts[h]["queued"]) for h in order)
    lines = [c(" AGENT ROUTING ", "fg", True) + c(time.strftime("%H:%M:%S"), "muted") +
             c(f"  ·  data {age:.0f}s old, refresh {every}s  ·  Ctrl+C quits", "muted"),
             f" {c(str(tasks), 'fg', True)} {c('open', 'muted')}   {c(str(working), 'working', True)} {c('working', 'muted')}   "
             f"{c(str(blocked), 'blocked' if blocked else 'fg', True)} {c('blocked', 'muted')}   "
             f"{c(str(queued), 'fg', True)} {c('queued', 'muted')}"
             + (c("   → route.py --blocked", "blocked") if blocked else ""), ""]
    # Plan usage is part of the pinned header: it stays on screen while the rest scrolls.
    use = usage_lines(report.get("usage"))
    idle = report.get("events_idle_min")       # nobody reading --events while tasks run: say so loudly
    if idle is not None and report.get("tasks") and (idle < 0 or idle >= 15):
        use = [c(" ⚠ EVENTS " + ("NEVER STARTED" if idle < 0 else f"CONSUMER IDLE {idle} MIN") +
                 ": nobody sees workers stop · route.py --events --follow", "blocked", True)] + use
    use = [c(f" ⚠ NO WORKER {t['task_id']} on {t['host']} ({why}, {m} min): holds its slot · route.py --status", "blocked", True)
           for t, m, why in __import__("route").no_worker(report)] + use
    hit = {}
    for t in report.get("tasks") or []:
        if t.get("interrupted"):
            hit.setdefault(t["host"], []).append(t)
    use = [c(f" ⚠ HOST RESET {h}: {len(ts)} task(s) interrupted · route.py --status", "blocked", True)
           for h, ts in sorted(hit.items())] + use
    lines = lines[:2] + [ln for ln in use if ln] + [""] + lines[3:]
    pinned["n"] = len(lines)
    rows = {}
    base = len(lines)
    lines += issue_lines(report.get("issues") or [], frame, width, EXPAND, rows)
    pinned["rows"] = {base + i: k for i, k in rows.items()}
    pinned["issues"] = list(dict.fromkeys(rows.values()))
    maint = [o for o in orchs if o.get("name") == "routing-maintainer"]
    orchs = [o for o in orchs if o.get("name") != "routing-maintainer"]
    if orchs:
        lines.append(" " + c("ORCHESTRATORS  ", "orch", True) + "   ".join(
            dot(o.get("status"), frame) + " " + c(o.get("name"), "fg") + c(f" ({o['host']})  ", "muted") + profile(o) +
            c(" · ", "muted") + state(o.get("status"))
            for o in orchs))
        for o in orchs:
            subs = M.sub_tree(o.get("subagents") or [])
            if subs:
                lines += [" " + x for x in tree_lines(subs, c("│  ", "orch"), "orch", frame, 1)]
        arrows = "   ".join(c(f"↓ {h}", M.host_style(cfg, h)[3] if M.host_style(cfg, h)[3] in COL else "muted") for h in order)
        lines += [" " + c("│", "orch") + "  " + arrows, ""]
    for o in maint:     # the routing maintainer: fixes reported routing friction (docs/maintainer.md)
        lines.append(" " + c("MAINTAINER  ", "orch", True) + dot(o.get("status"), frame) + " " + c(o.get("name"), "fg") +
                     c(f" ({o['host']})  ", "muted") + profile(o) + c(" · ", "muted") + state(o.get("status")) +
                     inbox_text(o, report.get("feedback")))
        subs = M.sub_tree(o.get("subagents") or [])        # its reviewers and helpers, like an orchestrator's
        if subs:
            lines += [" " + x for x in tree_lines(subs, c("   ", "orch"), "orch", frame, 1)]
        lines.append("")
    blocks = [lane_lines(cfg, h, hosts[h], frame) for h in order]
    gap = 4
    col = (width - 2 - gap * (len(blocks) - 1)) // max(len(blocks), 1)
    # Side by side only when every line fits its column; deep trees stack instead.
    if len(blocks) > 1 and col >= 46 and all(vlen(x) <= col for b in blocks for x in b):
        rows = max(len(b) for b in blocks)
        for r in range(rows):
            cells = [fit(b[r] if r < len(b) else "", col) for b in blocks]
            sep = c(" ┆  ", "rule")
            lines.append(" " + sep.join(cells))
    else:
        for b in blocks:
            lines += [" " + fit(x, width - 2) for x in b] + [""]
    if error:
        lines += ["", " " + c(f"last refresh failed: {error}", "blocked")]
    return lines


def run(fetch, cfg, every):
    state = {"snap": None, "at": 0.0, "error": None, "stop": False}

    def refresher():
        while not state["stop"]:
            try:
                state["snap"], state["at"], state["error"] = fetch(), time.time(), None
            except Exception as e:  # keep the last good snapshot on screen
                state["error"] = str(e)[:160]
            for _ in range(every * 10):
                if state["stop"]:
                    return
                time.sleep(0.1)

    threading.Thread(target=refresher, daemon=True).start()
    out = sys.stdout
    keys = Keys()
    out.write(ESC + "?1049h" + ESC + "?25l" + (MOUSE_ON if keys.tty else ""))   # alternate screen, hide cursor
    frame, offset, page = 0, 0, 10
    try:
        while True:
            width, height = shutil.get_terminal_size((120, 40))
            if width < 20 or height < 5:          # some ptys report 0x0
                width, height = 120, 40
            if state["snap"] is None:
                body = [" " + c("AGENT ROUTING", "fg", True), "", " " + c("reading both machines…", "muted")]
            else:
                body = frame_lines(state["snap"], cfg, frame, width, every, time.time() - state["at"], state["error"])
            # The title and counts stay pinned; everything below scrolls.
            n_pin = pinned["n"] if state["snap"] is not None else PINNED
            head, rest = body[:n_pin], body[n_pin:]
            room = max(1, height - 1 - len(head))
            page = max(1, room - 2)
            offset = max(0, min(offset, len(rest) - room))
            view = rest[offset:offset + room]
            if len(rest) > room:
                above, below = offset, len(rest) - offset - len(view)
                hint = c(f"  ↑ {above} more" if above else "", "working") + c(f"  ↓ {below} more" if below else "", "working") + \
                    c("  · scroll: wheel ↑↓ PgUp PgDn · details: click or Tab+Enter", "muted")
                head = head[:1] + [head[1] + hint] + head[2:] if len(head) > 1 else head
            screen = ESC + "H" + "".join(fit(x, width) + "\n" for x in head + view) + ESC + "J"
            out.write(screen)
            out.flush()
            frame += 1
            for k in keys.read(0.25):
                if k == "quit":
                    return 0
                if isinstance(k, tuple):           # ("click", screen row): open or fold the issue there
                    y = k[1] - 1
                    line = y if y < len(head) else n_pin + offset + (y - len(head))
                    hit = pinned.get("rows", {}).get(line)
                    if hit:
                        OPEN.symmetric_difference_update({hit})
                        SEL["key"] = hit
                    continue
                if k in ("next", "prev") and pinned.get("issues"):
                    ks = pinned["issues"]
                    i = ks.index(SEL["key"]) if SEL["key"] in ks else -1
                    SEL["key"] = ks[(i + (1 if k == "next" else -1)) % len(ks)]
                    continue
                if k == "toggle" and SEL["key"]:
                    OPEN.symmetric_difference_update({SEL["key"]})
                    continue
                if k == "fold":
                    OPEN.clear()
                    continue
                offset = {"up": offset - 1, "down": offset + 1, "wheel-up": offset - 3, "wheel-down": offset + 3,
                          "pgup": offset - page, "pgdn": offset + page, "home": 0, "end": 10**6}.get(k, offset)
                offset = max(0, offset)
    except KeyboardInterrupt:
        return 0
    finally:
        state["stop"] = True
        keys.close()
        out.write((MOUSE_OFF if keys.tty else "") + ESC + "?25h" + ESC + "?1049l")
        out.flush()


PINNED = 3    # title, counts, blank line (before the first snapshot)
pinned = {"n": PINNED}   # set by frame_lines: title, counts, usage lines, blank
MOUSE_ON = ESC + "?1000h" + ESC + "?1006h"     # wheel events, SGR encoding (shift+drag still selects text)
MOUSE_OFF = ESC + "?1006l" + ESC + "?1000l"
KEYMAP = [("\033[A", "up"), ("\033OA", "up"), ("\033[B", "down"), ("\033OB", "down"), ("\033[5~", "pgup"),
          ("\033[6~", "pgdn"), ("\033[H", "home"), ("\033[1~", "home"), ("\033[F", "end"), ("\033[4~", "end"),
          ("k", "up"), ("j", "down"), ("g", "home"), ("G", "end"), (" ", "pgdn"), ("b", "pgup"), ("q", "quit"),
          ("\033[Z", "prev"), ("\t", "next"), ("\r", "toggle"), ("\n", "toggle"), ("x", "fold")]
MOUSE_RE = re.compile(r"\033\[<(\d+);(\d+);(\d+)([Mm])")


class Keys:
    """Non-blocking key and mouse-wheel input while the view runs (cbreak mode,
    so Ctrl+C still quits). Does nothing when stdin isn't a terminal."""

    def __init__(self):
        self.tty, self.saved = False, None
        try:
            import termios, tty
            if sys.stdin.isatty():
                self.fd = sys.stdin.fileno()
                self.saved = termios.tcgetattr(self.fd)
                tty.setcbreak(self.fd)
                self.tty = True
        except (ImportError, OSError, ValueError):
            pass

    def read(self, timeout):
        if not self.tty:
            time.sleep(timeout)
            return []
        import select
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return []
        buf = os.read(self.fd, 4096).decode("utf-8", "ignore")
        out = []
        while buf:
            m = MOUSE_RE.match(buf)
            if m:
                b = int(m.group(1))
                out += (["wheel-up"] if b == 64 else ["wheel-down"] if b == 65 else
                        [("click", int(m.group(3)))] if b == 0 and m.group(4) == "M" else [])
                buf = buf[m.end():]
                continue
            hit = next(((seq, name) for seq, name in KEYMAP if buf.startswith(seq)), None)
            if hit:
                out.append(hit[1])
                buf = buf[len(hit[0]):]
            else:
                buf = buf[1:]
        return out

    def close(self):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
