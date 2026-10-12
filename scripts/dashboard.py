"""Render the routing map: one lane per machine, orchestrators on top, then
host, tasks, workers and their child runs, as a self-contained HTML page.

Used by `route.py --dashboard`. The page is static (inline SVG and CSS);
with --every the router rewrites it and the page reloads itself.
"""

import html
import time

import route as V

# Defaults for legacy configs without presentation fields. Styles describe OS,
# not routing IDs; any number of hosts can use the same style.
LEGACY_LANES = {"desktop": ("Desktop", "Ubuntu on WSL2", "linux"),
                "mac": ("Mac", "macOS", "darwin")}
OS_STYLES = {"linux": ("desktop", "lane-desktop"), "darwin": ("mac", "lane-mac")}


def host_style(cfg, host):
    info = cfg["hosts"].get(host, {})
    name, desc, system = LEGACY_LANES.get(host, (host.title(), "", ""))
    system = V.H.host_os(info) or system
    icon, lane = OS_STYLES.get(system, ("desktop", "lane-other"))
    return info.get("label", name), info.get("description", desc), icon, lane


def host_order(cfg, hosts):
    home = cfg.get("routing_home")
    return ([home] if home in hosts else []) + [h for h in cfg["hosts"] if h in hosts and h != home] + [
        h for h in hosts if h not in cfg["hosts"] and h != home]
STATE = {"working": "st-working", "idle": "st-idle", "done": "st-idle", "blocked": "st-blocked",
         "unknown": "st-unknown", None: "st-unknown"}

R, SLOT, PAD = 27, 172, 36
Y_ORCH, Y_HOST, Y_TASK, Y_WORKER, Y_CHILD = 70, 200, 330, 450, 570

ICONS = {
    "desktop": '<rect x="-10" y="-9" width="20" height="13" rx="2"/><path d="M-4 8h8M0 4v4"/>',
    "mac": '<rect x="-8" y="-8" width="16" height="11" rx="1.5"/><path d="M-12 6h24"/>',
    "task": '<path d="M-6-10h8l5 5v15h-13z"/><path d="M2-10v5h5M-3 1h7M-3 5h7"/>',
    "codex": '<path d="M-9-5l5 5-5 5"/><path d="M0 6h9"/>',
    "claude": '<path d="M0-10v20M-10 0h20M-7-7l14 14M7-7l-14 14"/>',
    "orchestrator": '<circle cx="0" cy="0" r="3.2"/><circle cx="0" cy="-9" r="2.2"/>'
                    '<circle cx="-8" cy="6" r="2.2"/><circle cx="8" cy="6" r="2.2"/>'
                    '<path d="M0-3v-4M-2.6 1.6l-4 3M2.6 1.6l4 3"/>',
    "queued": '<circle cx="0" cy="0" r="9"/><path d="M0-5v5l4 3"/>',
    "child": '<path d="M-6-8v8a6 6 0 0 0 6 6h6"/><circle cx="8" cy="6" r="2.5"/><circle cx="-6" cy="-9" r="2.5"/>',
    "stray": '<circle cx="0" cy="0" r="8"/><path d="M-3-2a3 3 0 1 1 3 3v2M0 6v.5"/>',
}


def prof(a):
    """Short model line for a node: `gpt-6.1-sol high ⚡`."""
    if not a.get("model"):
        return ""
    import watch
    return " · " + ("≈" if a.get("profile_shared") else "") + watch.short_model(a["model"]) + \
        (f" {a['effort']}" if a.get("effort") else "") + \
        (" ⚡fast" if a.get("fast") else "")


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def node(x, y, icon, cls, label, sub, small=False, title=None):
    r = R * (0.72 if small else 1)
    scale = 0.8 if small else 1
    tip = f"<title>{esc(title)}</title>" if title else ""
    return (f'<g class="node {cls}" transform="translate({x:.1f},{y:.1f})">{tip}'
            f'<circle class="ring" r="{r:.1f}"/>'
            f'<g class="icon" transform="scale({scale})">{ICONS[icon]}</g>'
            f'<text class="label" y="{r + 17:.1f}">{esc(label)}</text>'
            f'<text class="sub" y="{r + 31:.1f}">{esc(sub)}</text></g>')


def edge(x1, y1, x2, y2, cls, flow=False):
    ym = (y1 + y2) / 2
    d = f"M{x1:.1f} {y1:.1f} C{x1:.1f} {ym:.1f},{x2:.1f} {ym:.1f},{x2:.1f} {y2:.1f}"
    out = f'<path class="edge {cls}" d="{d}"/>'
    if flow:
        out += f'<path class="flow" d="{d}"/>'
    return out


def task_prefix(task_id):
    """`app1-impl` -> `app1-`; prefix stripped from agent names under the task."""
    head = task_id.split("-")[0]
    return head + "-" if len(task_id) > len(head) else ""


def short_name(a, prefix=""):
    """Readable label: strip the task's issue prefix; unnamed panes show kind + pane."""
    name = a.get("name")
    if not name:
        return f"{a.get('agent') or 'agent'} {a.get('pane_id', '')}".strip()
    return name.removeprefix(prefix) if prefix and name != prefix else name


def issue_label(task_id):
    """`app1-impl` -> `APP-1`; anything else unchanged."""
    import re
    m = re.match(r"([a-z]+)(\d+)", task_id)
    return f"{m.group(1).upper()}-{m.group(2)}" if m else task_id


def task_tree(agents):
    """Worker at the root; pane-based agents nested by their `parent` token
    (untagged ones under the worker); in-process subagents under their owner.
    Each node: {"agent": entry, "kind": "pane"|"sub", "children": [...]}."""
    worker = next((a for a in agents if a.get("role") == "worker"), None)
    if worker is None:
        return None
    nodes = {a["pane_id"]: {"agent": a, "kind": "pane", "children": []} for a in agents}
    for a in agents:
        if a is worker:
            continue
        parent = nodes.get(a.get("parent")) if a.get("parent") != a["pane_id"] else None
        (parent or nodes[worker["pane_id"]])["children"].append(nodes[a["pane_id"]])
    for n in nodes.values():
        n["children"] += sub_tree(n["agent"].get("subagents") or [])
    return nodes[worker["pane_id"]]


def sub_tree(subs):
    """Nest in-process subagents by parent id when known, else by spawnDepth order."""
    out, by_id, last_at = [], {}, {}
    for s in sorted(subs, key=lambda x: x.get("depth") or 1):
        node = {"agent": {"name": s.get("description") or s["id"], "agent": s.get("type") or "subagent",
                          "status": "working" if s.get("running") else "done", "model": s.get("model"),
                          "pane_id": "sub:" + s["id"], "sub_type": s.get("type")},
                "kind": "sub", "children": []}
        by_id[s["id"]] = node
        depth = s.get("depth") or 1
        parent = by_id.get(s.get("parent")) or (last_at.get(depth - 1) if depth > 1 else None)
        (parent["children"] if parent else out).append(node)
        last_at[depth] = node
    return out


LEVEL = 120   # vertical distance between tree levels
MAX_DRAW_DEPTH = 6


def place(svg, n, x, y, span, depth=1, strip=""):
    """Draw n's children one level down, spread across `span`; recurse."""
    kids = n["children"]
    if not kids:
        return
    if depth > MAX_DRAW_DEPTH:
        svg.append(f'<text class="sub" x="{x:.1f}" y="{y + R + 52:.1f}">+{len(kids)} deeper</text>')
        return
    shown = kids[:4]
    step = min(64, span / max(len(shown), 1))
    for k, ch in enumerate(shown):
        cx = x + (k - (len(shown) - 1) / 2) * step
        a, st = ch["agent"], ch["agent"].get("status")
        cls = STATE.get(st, "st-unknown")
        svg.append(edge(x, y + R + 34, cx, y + LEVEL - R * 0.72, cls, flow=st == "working"))
        sub = f"{a.get('sub_type') or 'subagent'} · {st}" if ch["kind"] == "sub" else f"{a.get('agent')} · {st}{prof(a)}"
        svg.append(node(cx, y + LEVEL, "child", f"agent {cls}{' sub' if ch['kind'] == 'sub' else ''}",
                        (a.get("name") if ch["kind"] == "sub" else short_name(a, strip))[:22] or "", sub, small=True,
                        title=f"{a.get('name')} · {a.get('model') or ''}"))
        place(svg, ch, cx, y + LEVEL, step, depth + 1, strip)
    if len(kids) > 4:
        svg.append(f'<text class="sub" x="{x:.1f}" y="{y + LEVEL + 62:.1f}">+{len(kids) - 4} more</text>')


def slots_text(cap):
    """'slots 2/3 · 1 waiting: app1-impl (pnpm typecheck)', or '' when unknown."""
    sl = cap.get("slots")
    if sl is None:
        return ""
    n = cap.get("max_heavy") or "–"
    w = sl.get("waiting") or []
    txt = f"slots {len(sl.get('holders') or [])}/{n}"
    if w:
        first = w[0]
        txt += f" · {len(w)} waiting: {first.get('task') or first.get('pid')} ({(first.get('cmd') or '')[:28]})"
    return txt


def usage_parts(usage, now=None):
    """[(tool, text, level)] for the usage line; level ok/warn/hot by the higher window."""
    import time as _t
    now = now or _t.time()
    out = []
    for tool in ("codex", "claude"):
        u = (usage or {}).get(tool)
        if not u:
            out.append((tool, "not available", "unknown"))
            continue
        bits, worst = [], 0.0
        for key, name in (("five_hour", "5h"), ("week", "week")):
            w = u.get(key)
            if not w or w.get("used_percent") is None:
                continue
            pct = float(w["used_percent"])
            worst = max(worst, pct)
            reset = ""
            if w.get("resets_at"):
                left = w["resets_at"] - now
                reset = (f" (resets in {left / 3600:.0f}h)" if left < 36 * 3600
                         else f" (resets {_t.strftime('%a %H:%M', _t.localtime(w['resets_at']))})")
            bits.append(f"{name} {pct:.0f}%{reset}")
        if u.get("credits"):
            bits.append(f"credits {int(u['credits']) / 1000:.1f}k")
        age = now - (u.get("at") or now)
        if age > 600:
            bits.append(f"as of {age / 3600:.0f}h ago" if age >= 3600 else f"as of {age / 60:.0f}m ago")
        if u.get("exhausted"):
            bits.append(f"limit reached ({u['exhausted']})")
            worst = 100.0
        label = f"{tool}{' ' + u['plan'] if u.get('plan') else ''}"
        out.append((label, " · ".join(bits) or "no data", "hot" if worst >= 90 else "warn" if worst >= 75 else "ok"))
    return out


def where(t):
    """Where a task lives in Herdr: 'APP-1 › gate (w9:t2)' (grouped), else 'ws w9'."""
    if t.get("group") and t.get("tab_label"):
        return f"{t['group']} › {t['tab_label']}"
    return f"ws {t.get('workspace_id') or '–'}"


def build_model(cfg, report, queues, capacity, known):
    hosts = {}
    for h in cfg["hosts"]:
        hosts[h] = {"tasks": [], "queued": queues.get(h, []), "stray": [], "quiet": 0, **capacity.get(h, {})}
    orchestrators = []
    for t in report["tasks"]:
        hosts.setdefault(t["host"], {"tasks": [], "queued": [], "stray": []})["tasks"].append(t)
    for u in report["untracked"]:
        if u.get("name") in known:
            orchestrators.append(u)
        else:
            lane = hosts.setdefault(u["host"], {"tasks": [], "queued": [], "stray": [], "quiet": 0})
            # Only agents doing something are worth a slot; idle or finished
            # ones (old sessions, closed tasks' panes) collapse into a count.
            if u.get("status") in QUIET:
                lane["quiet"] = lane.get("quiet", 0) + 1
            else:
                lane["stray"].append(u)
    return hosts, orchestrators


QUIET = {"idle", "done"}


def issue_table(issues):
    """Issue pipeline rows: impl › review › gate › merge › deploy › staging."""
    if not issues:
        return ""
    rows = []
    other = [r for r in issues if r.get("unrouted")]
    issues = [r for r in issues if not r.get("unrouted")]
    done = [r for r in issues if r.get("completed")]
    issues = [r for r in issues if not r.get("completed")]
    for r in issues[:12]:
        cells = []
        for n in V.row_pipeline(r):
            st = r["steps"].get(n)
            cls = (st or {}).get("cls", "pending") if st else "pending"
            label = (st or {}).get("label") or "not started"
            extra = {"working": f" · {(st or {}).get('host')}", "need": " ⚑", "wait": " ◌", "stale": " ↻",
                     "unknown": " ?", "queued": f" ⧗{(st or {}).get('queued', '')}"}.get(cls, "")
            if (st or {}).get("quiet"):
                cls, extra = cls + " quiet", extra + " ⚠"
            cells.append(f'<span class="step {cls}" title="{n}: {esc(label)}">{n}{esc(extra)}</span>')
        pr = r.get("pr") or {}
        ck = pr.get("checks") or {}
        ci = ("" if not sum(ck.values() or [0]) else
              f' <span class="ci fail">CI ✗ {ck["fail"]}</span>' if ck.get("fail") else
              f' <span class="ci active">CI … {ck["pending"]}</span>' if ck.get("pending") else ' <span class="ci done">CI ✓</span>')
        prtxt = (f'<a href="{esc(pr.get("url"))}">#{pr["number"]}</a> {esc("draft" if pr.get("draft") else pr.get("state"))}{ci}'
                 if pr else "")
        title = r.get("title") or ""
        if title.upper().startswith(r["issue"]):
            title = title[len(r["issue"]):].lstrip(" :·-")
        head = (f'<b>{esc(r["issue"])}</b> <span class="ititle">{esc(title[:70])}</span> '
                f'<span class="ipr">{prtxt}</span>')
        flags = [f'<li>{n}: {esc(st.get("label") or "")}</li>' for n in V.row_pipeline(r)
                 for st in [r["steps"].get(n) or {}] if st.get("cls") in ("stale", "need", "unknown", "wait") or st.get("quiet")]
        lin = r["steps"].get(V.tracker_kind(r))
        lintxt = (f'<span class="lin">{V.tracker_label(r)}: {esc(lin.get("state"))} '
                  f'<i>{"live" if lin.get("live") else "from the pipeline" if lin.get("inferred") else esc((lin.get("note") or "").split(" · ")[0])}</i>'
                  f'{(" <b class=lag>⚠ should be " + esc(lin["behind"]) + "</b>") if lin.get("behind") else ""}</span>') if lin else ""
        # One line per issue: key, title, PR and the step pills stay visible; a click opens
        # what each step says (and Linear). Folding survives the refresh (data-key).
        flagged = {n for n in V.row_pipeline(r) for st in [r["steps"].get(n) or {}]
                   if st.get("cls") in ("stale", "need", "unknown", "wait") or st.get("quiet")}
        detail = "".join(f'<li{" class=flag" if n in flagged else ""}><b>{n}</b> {esc(st.get("label") or st.get("state") or "")}</li>'
                         for n in V.row_pipeline(r) for st in [r["steps"].get(n) or {}] if st)
        body = (f'<ul class="detail">{detail}</ul>' if detail else "") + (f'<div class="steps">{lintxt}</div>' if lin else "")
        warn = (f' <span class="lag">⚑ {len(flags)} need a look</span>' if flags else "") + \
            (f' <span class="lag">{V.tracker_label(r)} ⚠ should be {esc(lin["behind"])}</span>' if (lin or {}).get("behind") else "")
        if "merge" in V.row_pipeline(r) and (r["steps"].get("merge") or {}).get("cls") == "done":   # merged: later pipeline checks remain
            rows.append(f'<details class="issue landed" data-key="{esc(r["issue"])}"><summary>{head} '
                        f'<span class="tag">merged</span>{warn}</summary>{body}</details>')
        else:
            rows.append(f'<details class="issue" data-key="{esc(r["issue"])}"><summary>{head}{warn} '
                        f'<span class="steps">{"".join(cells)}</span></summary>{body}</details>')
    done_html = ""
    if done:
        items = "".join(
            f'<li><b>{esc(r["issue"])}</b> {esc((r.get("title") or "")[:60])}'
            f'{(" #" + str((r.get("pr") or {}).get("number"))) if r.get("pr") else ""}</li>' for r in done[:15])
        done_html = f'<details class="done" data-key="completed"><summary>Completed, last 14 days ({len(done)})</summary><ul>{items}</ul></details>'
    if other:
        items = "".join(
            f'<li><a href="{esc((r.get("pr") or {}).get("url"))}">#{(r.get("pr") or {}).get("number")}</a> '
            f'<b>{esc(r["issue"])}</b> {esc((r.get("title") or "")[:70])}'
            f'{" <span class=ci-fail>CI ✗</span>" if ((r.get("pr") or {}).get("checks") or {}).get("fail") else ""}</li>'
            for r in other)
        done_html += (f'<details class="done other" data-key="other-prs"><summary>Other open PRs, not routed '
                      f'({len(other)})</summary><ul>{items}</ul></details>')
    return '<section class="issues">' + "".join(rows) + done_html + "</section>"


def render(cfg, report, queues, capacity, refresh=None):
    known = cfg.get("known_agents", {})
    hosts, orchestrators = build_model(cfg, report, queues, capacity, known)
    order = host_order(cfg, hosts)

    # Lane widths from the number of task/queue slots each needs.
    widths, x0, lanes = {}, 0, []
    for h in order:
        n = max(2, len(hosts[h]["tasks"]) + len(hosts[h]["queued"]), (len(hosts[h]["stray"]) + 1) // 2)
        widths[h] = n * SLOT + 2 * PAD
        lanes.append((h, x0))
        x0 += widths[h]
    total_w = x0
    def depth_of(n):
        return 1 + max([depth_of(c) for c in n["children"]] or [0])
    trees = {id(t): task_tree(t["agents"]) for h in order for t in hosts[h]["tasks"]}
    deepest = max([1] + [1 + (depth_of(tr) if tr else 0) for tr in trees.values()])
    y_stray = Y_TASK + deepest * 120 + 40
    height = y_stray + (130 if any(hosts[h]["stray"] for h in order) else
                        60 if any(hosts[h].get("quiet") for h in order) else 40)

    svg, host_xy = [], {}
    for i, (h, lx) in enumerate(lanes):
        name, desc, icon, lane = host_style(cfg, h)
        cx = lx + widths[h] / 2
        host_xy[h] = (cx, Y_HOST)
        if i:
            svg.append(f'<line class="divider" x1="{lx}" y1="{Y_HOST - 70}" x2="{lx}" y2="{height - 10}"/>')
        hcap = hosts[h]
        sub = f"heavy {hcap.get('heavy', 0)}/{hcap.get('max_heavy', '–')} · queue {len(hcap['queued'])}/{hcap.get('max_queue', '–')}"
        if hcap.get("slots") is not None:
            sub += f" · slots {len(hcap['slots'].get('holders') or [])}/{hcap.get('max_heavy', '–')}"
        svg.append(f'<g class="{lane}">')
        svg.append(node(cx, Y_HOST, icon, "host", f"{name} · {desc}", sub,
                        title=f"{name}: {hcap.get('light', 0)} light task(s) open"))
        ld, lv = hcap.get("load"), hcap.get("levels") or {}
        if ld:
            gb = lambda b: f"{(b or 0) / 2**30:.1f}"
            cpu = f"cpu {ld.get('load1', 0):.1f}/{ld.get('cores')}" if ld.get("load1") is not None else ""
            mem = (f"mem {gb(ld.get('mem_avail'))}G free" + (f" · pressure {ld['pressure']}" if ld.get("pressure") not in (None, "normal") else "")
                   if ld.get("mem_total") else "")
            dk = f"docker {gb(ld.get('docker_used'))}/{gb(ld.get('docker_limit'))}G" if ld.get("docker") == "up" else ""
            svg.append(f'<text class="loadline lv-{esc(lv.get("overall", "unknown"))}" x="{cx:.1f}" y="{Y_HOST + R + 46:.1f}">'
                       f'● {esc("  ·  ".join(x for x in (cpu, mem, dk) if x))}'
                       f'{esc("  ·  HOT") if lv.get("hot") else ""}</text>')
        slots = hcap["tasks"] + [{"queued": q} for q in hcap["queued"]]
        for j, t in enumerate(slots):
            tx = lx + PAD + SLOT * (j + 0.5)
            if "queued" in t:
                svg.append(edge(cx, Y_HOST + R, tx, Y_TASK - R, "dashed"))
                svg.append(node(tx, Y_TASK, "queued", "queued", t["queued"], f"queued #{j - len(hcap['tasks']) + 1}"))
                continue
            tree = trees[id(t)]
            worker = tree["agent"] if tree else None
            working = worker is not None and worker.get("status") == "working"
            load = "heavy" if t["heavy"] else "light"
            svg.append(edge(cx, Y_HOST + R, tx, Y_TASK - R, "solid", flow=working))
            ttl = (t.get("summary") or "")
            if ttl.upper().startswith(issue_label(t["task_id"])):
                ttl = ttl[len(issue_label(t["task_id"])):].lstrip(" :·-")
            svg.append(node(tx, Y_TASK, "task", "task", issue_label(t["task_id"]),
                            (ttl[:24] + "…" if len(ttl) > 25 else ttl) or f"{load} · {where(t)}",
                            title=f"{t['task_id']} · {t.get('summary') or ''} · {load} · {where(t)} · {t['dispatch']}"))
            if worker:
                st = worker.get("status")
                svg.append(edge(tx, Y_TASK + R + 34, tx, Y_WORKER - R, STATE.get(st, "st-unknown"), flow=working))
                svg.append(node(tx, Y_WORKER, "claude" if worker.get("agent") == "claude" else "codex",
                                f"agent {STATE.get(st, 'st-unknown')}", short_name(worker, task_prefix(t["task_id"])),
                                f"{worker.get('agent')} · {st}{prof(worker)}", title=f"pane {worker['pane_id']} · {worker.get('cwd')}"))
                place(svg, tree, tx, Y_WORKER, SLOT - 12, strip=task_prefix(t["task_id"]))
        for k, s in enumerate(hcap["stray"]):
            per_row = max(1, int((widths[h] - 2 * PAD) // 86))
            sx = lx + PAD + 43 + (k % per_row) * 86
            sy = y_stray + (k // per_row) * 92
            st = s.get("status")
            svg.append(node(sx, sy, "stray", f"stray {STATE.get(st, 'st-unknown')}", short_name(s),
                            f"{s.get('agent')} · {st}" + (f" → {s['issue']}" if s.get("issue") else ""), small=True,
                            title=(f"{s['issue']}: fix round" if s.get("issue") else "not routed") +
                                  f" · {s.get('cwd')}{prof(s)}"))
        if hcap["stray"] or hcap.get("quiet"):
            quiet = f" · {hcap['quiet']} idle hidden" if hcap.get("quiet") else ""
            svg.append(f'<text class="caption" x="{lx + PAD}" y="{y_stray - 44}">Not routed{quiet}</text>')
        svg.append("</g>")

    if orchestrators:
        for k, o in enumerate(orchestrators):
            ox = total_w * (k + 1) / (len(orchestrators) + 1)
            maint = o.get("name") == "routing-maintainer"     # maintains routing; dispatches nothing
            for h in ([] if maint else order):
                hx, hy = host_xy[h]
                svg.append(edge(ox, Y_ORCH + R, hx, hy - R, "orch", flow=o.get("status") == "working"))
            fb = report.get("feedback") or {}
            sub = (f"routing maintainer · {o.get('status')} · inbox {fb.get('new', 0)} waiting, "
                   f"{fb.get('done_24h', 0)} fixed today" if maint else
                   f"{known.get(o.get('name'), 'orchestrator')} · {o.get('status')}{prof(o)}")
            svg.append(node(ox, Y_ORCH, "orchestrator", f"orch agent {STATE.get(o.get('status'), 'st-unknown')}",
                            o.get("name"), sub))

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    unreachable = "".join(f'<li>{esc(u)}</li>' for u in report.get("unreachable", []))
    usage_html = '<div class="usage">' + "".join(
        f'<span class="use lv-{lvl}"><b>{esc(tool)}</b> {esc(txt)}</span>'
        for tool, txt, lvl in usage_parts(report.get("usage"))) + '</div>'
    meta = f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    counts = {s: sum(1 for h in order for t in hosts[h]["tasks"] for a in t["agents"] if a.get("status") == s)
              for s in ("working", "blocked")}
    issues_html = issue_table(report.get("issues") or [])
    legend = "".join(f'<span style="--c: var(--{host_style(cfg, h)[3].removeprefix("lane-").replace("other", "unknown")})">'
                     f'{esc(host_style(cfg, h)[0])} lane</span>' for h in order)
    return PAGE.format(meta=meta, stamp=esc(stamp), w=total_w, h=height, svg="".join(svg), issues=issues_html,
                       host_legend=legend,
                       ntasks=sum(len(hosts[h]["tasks"]) for h in order),
                       nqueued=sum(len(hosts[h]["queued"]) for h in order),
                       nworking=counts["working"], nblocked=counts["blocked"],
                       blocked_cls=" hot" if counts["blocked"] else "",
                       refresh=f"refreshes every {int(refresh)}s" if refresh else "snapshot",
                       unreachable=f'<ul class="warn">{unreachable}</ul>' if unreachable else "", usage=usage_html)


PAGE = """<title>Agent Routing Map</title>
{meta}
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Sora:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500&display=swap">
<style>
/* Layout: one lane per machine; orchestrators above, then host, tasks, workers, child runs. Single dark look. */
:root {{
  --bg: #07080b; --panel: #0e1016; --fg: #e9ecf4; --muted: #8a90a3; --rule: #2a2e3a;
  --desktop: #4f8dff; --mac: #d35cf0; --orch: #f2b544;
  --working: #f2b544; --idle: #3ad59a; --blocked: #ff5d6c; --unknown: #7a8094;
  --display: "Sora", "Avenir Next", "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, "SF Mono", Menlo, monospace;
  color-scheme: dark;
}}
body {{ background: var(--bg); color: var(--fg); font-family: var(--display); }}
.wrap {{ padding-inline: 20px; padding-block: 28px 40px; max-width: 1600px; margin: 0 auto; display: grid; gap: 18px; }}
header {{ display: flex; flex-wrap: wrap; align-items: baseline; justify-content: space-between; gap: 10px 24px; }}
h1 {{ font-size: clamp(1.5rem, 3.2vw, 2.4rem); font-weight: 700; margin: 0; letter-spacing: -0.01em; text-wrap: balance; }}
.stamp {{ font-family: var(--mono); font-size: 12px; color: var(--muted); }}
.summary {{ display: flex; flex-wrap: wrap; gap: 8px; font-family: var(--mono); font-size: 12px; }}
.chip {{ border: 1px solid var(--rule); border-radius: 999px; padding: 4px 11px; color: var(--muted); font-variant-numeric: tabular-nums; }}
.chip b {{ color: var(--fg); font-weight: 500; }}
.chip.hot {{ border-color: var(--blocked); color: var(--blocked); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); }}
.legend span::before {{ content: ""; display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 6px;
  background: var(--c); box-shadow: 0 0 8px var(--c); vertical-align: 0; }}
.map {{ overflow-x: auto; border-top: 1px solid var(--rule); padding-top: 8px; }}
svg {{ display: block; min-width: 100%; height: auto; }}
.warn {{ color: var(--blocked); font-family: var(--mono); font-size: 12px; margin: 0; }}
.node .ring {{ fill: var(--panel); stroke: var(--c, var(--unknown)); stroke-width: 1.6; filter: drop-shadow(0 0 7px var(--c, transparent)); }}
.node .icon {{ fill: none; stroke: var(--c, var(--unknown)); stroke-width: 1.6; stroke-linecap: round; stroke-linejoin: round; }}
.node .label {{ fill: var(--fg); font-size: 12.5px; text-anchor: middle; font-weight: 600; }}
.node .sub, text.sub {{ fill: var(--muted); font-family: var(--mono); font-size: 10.5px; text-anchor: middle; }}
.caption {{ fill: var(--muted); font-size: 11px; letter-spacing: 0.08em; text-transform: uppercase; }}
.lane-desktop {{ --c: var(--desktop); }} .lane-mac {{ --c: var(--mac); }} .lane-other {{ --c: var(--unknown); }}
.orch {{ --c: var(--orch); }}
.st-working {{ --c: var(--working); }} .st-idle {{ --c: var(--idle); }} .st-blocked {{ --c: var(--blocked); }} .st-unknown {{ --c: var(--unknown); }}
.queued .ring {{ stroke-dasharray: 4 4; filter: none; }}
.stray .ring {{ stroke-dasharray: 2 3; filter: none; opacity: .75; }}
.sub .ring {{ stroke-dasharray: 1 3; }}
.edge {{ fill: none; stroke: var(--c, var(--unknown)); stroke-width: 1.4; opacity: .55; }}
.edge.dashed {{ stroke-dasharray: 5 5; }}
.edge.orch {{ stroke: var(--orch); opacity: .45; }}
.flow {{ fill: none; stroke: var(--working); stroke-width: 2.4; stroke-linecap: round; stroke-dasharray: 3 14;
  animation: flow 1.1s linear infinite; filter: drop-shadow(0 0 4px var(--working)); }}
.st-blocked.agent .ring {{ animation: pulse 1.4s ease-in-out infinite; }}
.divider {{ stroke: var(--rule); stroke-dasharray: 4 6; }}
.issues {{ display: grid; gap: 10px; }}
.issue {{ display: grid; gap: 6px; font-size: 13px; }}
.issue > summary {{ cursor: pointer; list-style: none; display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; }}
.issue > summary::-webkit-details-marker {{ display: none; }}
.issue > summary::before {{ content: "▸"; color: var(--muted); }} .issue[open] > summary::before {{ content: "▾"; }}
.issue > summary .steps {{ margin-left: auto; gap: 4px; }} .issue > summary .step {{ padding: 1px 7px; }}
.detail {{ margin: 4px 0 0; padding-left: 22px; font-size: 12px; color: var(--muted); display: grid; gap: 3px; }}
.detail .flag {{ color: var(--working); }}
.detail b {{ color: var(--fg); display: inline-block; min-width: 58px; }}
.issue b {{ font-family: var(--mono); }}
.ititle {{ color: var(--fg); }} .ipr, .ipr a {{ color: var(--muted); font-family: var(--mono); font-size: 12px; }}
.steps {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.step {{ font-family: var(--mono); font-size: 11px; padding: 3px 9px; border-radius: 999px; border: 1px solid var(--rule); color: var(--muted); }}
.step.done {{ border-color: var(--idle); color: var(--idle); }}
.step.need {{ border-color: var(--blocked); color: var(--blocked); font-weight: 600; }}
.step.wait, .step.stale, .step.quiet {{ border-color: var(--working); color: var(--working); border-style: dashed; }}
.step.unknown {{ border-style: dotted; }} .step.queued {{ border-style: dashed; }}
.ci {{ font-size: 11px; }} .ci.done {{ color: var(--idle); }} .ci.fail {{ color: var(--blocked); }} .ci.active {{ color: var(--working); }}
.lag {{ color: var(--blocked); font-weight: 600; }}
.lin {{ font-family: var(--mono); font-size: 11px; color: var(--muted); align-self: center; margin-left: 6px; }} .lin i {{ opacity: .7; }}
.other summary {{ color: var(--muted); }} .ci-fail {{ color: var(--blocked); font-size: 11px; }}
.flags {{ margin: 0; padding-left: 18px; font-size: 12px; color: var(--working); display: grid; gap: 2px; }}
.step.skip {{ opacity: .45; text-decoration: line-through; }}
.step.active {{ border-color: var(--working); color: var(--working); box-shadow: 0 0 8px var(--working); }}
.step.fail {{ border-color: var(--blocked); color: var(--blocked); }}
.landed summary {{ cursor: pointer; color: var(--muted); }} .landed[open] {{ gap: 6px; }}
.landed .ititle {{ color: var(--muted); }} .tag {{ font-family: var(--mono); font-size: 11px; color: var(--idle); }}
.done summary {{ color: var(--idle); cursor: pointer; font-size: 13px; }}
.done ul {{ margin: 6px 0 0; padding-left: 18px; color: var(--muted); font-size: 12px; display: grid; gap: 3px; }}
.loadline {{ font-family: var(--mono); font-size: 10.5px; text-anchor: middle; fill: var(--muted); }}
.usage {{ display: flex; flex-wrap: wrap; gap: 6px 18px; font-family: var(--mono); font-size: 12px; color: var(--muted); }}
.use b {{ color: var(--fg); font-weight: 600; }} .use.lv-warn {{ color: var(--working); }} .use.lv-hot {{ color: var(--blocked); }}
.lv-ok {{ fill: var(--idle); }} .lv-warn {{ fill: var(--working); }} .lv-hot {{ fill: var(--blocked); }}
@keyframes flow {{ to {{ stroke-dashoffset: -17; }} }}
@keyframes pulse {{ 50% {{ stroke-width: 3.2; }} }}
@media (prefers-reduced-motion: reduce) {{ .flow, .st-blocked.agent .ring {{ animation: none; }} }}
</style>
<div class="wrap">
  <header>
    <h1>Agent Routing Map</h1>
    <div class="stamp">{stamp} · {refresh}</div>
  </header>
  <div class="summary">
    <span class="chip"><b>{ntasks}</b> open tasks</span>
    <span class="chip"><b>{nworking}</b> working</span>
    <span class="chip{blocked_cls}"><b>{nblocked}</b> blocked</span>
    <span class="chip"><b>{nqueued}</b> queued</span>
  </div>
  <div class="legend">
    {host_legend}
    <span style="--c: var(--orch)">Orchestrator</span><span style="--c: var(--working)">Working</span>
    <span style="--c: var(--idle)">Idle / done</span><span style="--c: var(--blocked)">Blocked: needs a decision</span>
  </div>
  {usage}
  {unreachable}
  {issues}
  <div class="map"><svg viewBox="0 0 {w} {h}" width="{w}" role="img" aria-label="Routing map of tasks, workers and child runs per machine">{svg}</svg></div>
</div>
<script>
// Keep folded rows the way the viewer left them across the auto-refresh.
(() => {{
  let open = [];
  try {{ open = JSON.parse(localStorage.getItem("rt-open") || "[]"); }} catch (e) {{}}
  document.querySelectorAll("details[data-key]").forEach(d => {{
    if (open.includes(d.dataset.key)) d.open = true;
    d.addEventListener("toggle", () => {{
      const keys = [...document.querySelectorAll("details[data-key]")].filter(x => x.open).map(x => x.dataset.key);
      try {{ localStorage.setItem("rt-open", JSON.stringify(keys)); }} catch (e) {{}}
    }});
  }});
}})();
</script>
"""
