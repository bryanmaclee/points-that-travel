"""Agent-based simulation of the cost-weighted engagement system.

Pure stdlib (no numpy) so it runs anywhere python3 does.

    python3 sim.py              # full matrix: every scenario x every seed, writes results.json
    python3 sim.py --quick      # one seed, fewer epochs (smoke test)

Question it answers: does the farm groups' share of the mint fall toward zero while honest
shares hold, and which safeguard is doing the work? Ablations knock out one safeguard each.
"""

import json
import math
import random
import sys
from collections import defaultdict, deque
from multiprocessing import Pool

# ---------------------------------------------------------------- parameters

BASE = dict(
    epochs=35, warmup=10,          # metrics count epochs > warmup
    M=5000.0,                       # mint pool per epoch
    delta=0.05,                     # demurrage per epoch
    allowance=10.0,                 # per-epoch basic allowance, every account
    start_allowance=30.0,           # one-time, for accounts with no legacy balance
    a=0.85, pr_iters=30,            # personalized PageRank
    eta=0.1, sustain=5,             # moving seed
    window=5,                       # flow-graph window, epochs
    lam=0.1,                        # dwell EMA
    D=1.5,                          # independence scale
    alpha=1.0, beta=2.0, gamma=2.0, # insider / outsider / bridging
    wcap=5.0,                       # cap on engager weight (relative to mean 1)
    reads=8,                        # posts an honest reader opens per epoch
    flag_cost=2.0,
    # switches (ablations flip these)
    use_trust=True, dwell="overlap", burn=True, bridging=True, flags=True,
    seed_mode="moving",             # moving | static | uniform
    use_escape=True, escape_steps=3,  # traversal: share of a k-step walk that leaves your circle
    farm_mimic=0.0,                   # share of farm engagements spent on honest posts, to look normal
    farm_dwell="parallel",
    filler_q=0.30,                    # how likeable farm filler is to a real reader
    mod_acc=0.90,                     # P(moderator upholds a flag on farm content)          # parallel (many tabs) | careful (one real-time stream per account)
)

# Flags dominate whenever they are on, and they rest on the strongest assumption in the model (a
# moderator who is right 90% of the time). So the structural safeguards are tested with flags OFF.
NF = {"flags": False}
CF = {"flags": False, "farm_dwell": "careful"}
SCENARIOS = {
    # --- structural only (flags off), naive farm: many parallel tabs
    "s_full":           dict(NF),
    "s_no_dwell":       dict(NF, dwell="off"),
    "s_raw_dwell":      dict(NF, dwell="raw"),      # dwell counted, overlap NOT split
    "s_no_escape":      dict(NF, use_escape=False),
    "s_no_dwell_escape": dict(NF, dwell="off", use_escape=False),
    "s_no_trust":       dict(NF, use_trust=False),
    "s_no_burn":        dict(NF, burn=False),
    "s_no_bridging":    dict(NF, bridging=False),
    "s_uniform_seed":   dict(NF, seed_mode="uniform"),
    # --- structural only, careful farm: one real-time stream per account (dwell is beaten)
    "c_full":           dict(CF),
    "c_no_escape":      dict(CF, use_escape=False),
    "c_no_trust":       dict(CF, use_trust=False),
    "c_no_burn":        dict(CF, burn=False),
    "c_no_bridging":    dict(CF, bridging=False),
    "c_uniform_seed":   dict(CF, seed_mode="uniform"),
    "c_static_seed":    dict(CF, seed_mode="static"),
    "c_mimic":          dict(CF, farm_mimic=0.33),
    "c_mimic_no_escape": dict(CF, farm_mimic=0.33, use_escape=False),
    # --- with flags: the received-filler residual
    "full":             {},
    "careful":          {"farm_dwell": "careful"},
    "mimic":            {"farm_dwell": "careful", "farm_mimic": 0.33},
    "moderator_0.6":    {"mod_acc": 0.6},
    "filler_0.15":      {"filler_q": 0.15},
    "filler_0.45":      {"filler_q": 0.45},
    "s_filler_0.15":    dict(NF, filler_q=0.15),
    "s_filler_0.45":    dict(NF, filler_q=0.45),
    # --- economy: can the jury keep engaging without a per-epoch allowance?
    "no_allowance":     {"allowance": 0.0},
}

# group: (count, post prob, quality range, legacy points, honest reader?)
GROUPS = {
    "novel":       (60,  0.5, (0.60, 0.95), 200, True),
    "regular":     (200, 0.3, (0.35, 0.65), 100, True),
    "middle":      (200, 0.2, (0.10, 0.30),  60, True),
    "pod":         (12,  0.4, (0.35, 0.65), 100, True),   # real humans, coordinating
    "ring":        (40,  1.0, (0.30, 0.30), 400, False),  # farmed the OLD system: high legacy
    "beneficiary": (1,   1.0, (0.30, 0.30), 400, False),
    "sybil":       (80,  0.0, (0.0, 0.0),     0, False),  # new accounts, no legacy
}
FARM = {"ring", "beneficiary", "sybil"}
N_COMMUNITIES = 6
DAY = 86400.0


# ---------------------------------------------------------------- helpers

def g_indep(d, D):
    """0 for a direct neighbour, -> 1 for strangers. d=None means unreachable."""
    if d is None:
        return 1.0
    return 1.0 - math.exp(-(d - 1) / D)


def effective_dwell(intervals):
    """Split every second among the intervals open during it. Returns per-interval credit."""
    pts = sorted({x for iv in intervals for x in iv})
    out = [0.0] * len(intervals)
    for x0, x1 in zip(pts, pts[1:]):
        active = [k for k, (s, e) in enumerate(intervals) if s <= x0 and e >= x1]
        if active:
            share = (x1 - x0) / len(active)
            for k in active:
                out[k] += share
    return out


def bfs(adj, src, cap=4):
    dist = {src: 0}
    q = deque([src])
    while q:
        u = q.popleft()
        if dist[u] >= cap:
            continue
        for v in adj[u]:
            if v not in dist:
                dist[v] = dist[u] + 1
                q.append(v)
    return dist


def normalize(vec):
    s = sum(vec)
    return [x / s for x in vec] if s > 0 else [1.0 / len(vec)] * len(vec)


# ---------------------------------------------------------------- the model

def run(scenario, rng_seed):
    P = dict(BASE, **SCENARIOS[scenario])
    rng = random.Random(rng_seed)

    # accounts
    group, comm, qual, legacy = [], [], [], []
    for gname, (n, _, (qlo, qhi), leg, _) in GROUPS.items():
        for _ in range(n):
            group.append(gname)
            comm.append(rng.randrange(N_COMMUNITIES) if gname not in FARM else -1)
            qual.append(P["filler_q"] if gname in ("ring", "beneficiary") else rng.uniform(qlo, qhi))
            legacy.append(leg * rng.lognormvariate(0, 0.4) if leg else 0.0)
    N = len(group)
    idx = defaultdict(list)
    for i, gname in enumerate(group):
        idx[gname].append(i)
    # pod members are spread across communities on purpose: they look like outsiders to each other
    for k, i in enumerate(idx["pod"]):
        comm[i] = k % N_COMMUNITIES
    benef = idx["beneficiary"][0]

    bal = [l if l > 0 else P["start_allowance"] for l in legacy]
    rho = [0.5] * N
    pen = [1.0] * N                      # flag penalty multiplier on trust
    if P["seed_mode"] == "uniform":
        seed = [1.0 / N] * N
    else:
        seed = normalize([math.sqrt(l) for l in legacy])
    hist_w = deque(maxlen=P["sustain"])  # recent t*rho, for the moving seed
    flow_hist = deque(maxlen=P["window"])

    mint_by = defaultdict(float)
    naive_by = defaultdict(float)
    trust_by = defaultdict(float)
    farm_share_series = []
    rho_by = {}
    eng_by = defaultdict(float)
    phi_by = {}

    for epoch in range(1, P["epochs"] + 1):
        # ---- flow graph over the window (from PREVIOUS epochs only)
        F = defaultdict(lambda: defaultdict(float))
        for fl in flow_hist:
            for (i, j), c in fl.items():
                F[i][j] += c
        adj = defaultdict(set)
        for i in F:
            for j in F[i]:
                adj[i].add(j)
                adj[j].add(i)

        # ---- trust: personalized PageRank from the seed
        if P["use_trust"]:
            t = seed[:]
            outsum = {i: sum(F[i].values()) for i in F}
            for _ in range(P["pr_iters"]):
                nt = [(1 - P["a"]) * s for s in seed]
                dangling = 0.0
                for i in range(N):
                    if t[i] == 0:
                        continue
                    if outsum.get(i, 0) > 0:
                        for j, c in F[i].items():
                            nt[j] += P["a"] * t[i] * c / outsum[i]
                    else:
                        dangling += P["a"] * t[i]
                for i in range(N):
                    nt[i] += dangling * seed[i]
                t = nt
            t = normalize([t[i] * pen[i] for i in range(N)])
            w = [min(t[i] * N, P["wcap"]) for i in range(N)]
        else:
            t = [1.0 / N] * N
            w = [1.0] * N
        dw = w if P["dwell"] == "off" else [w[i] * rho[i] for i in range(N)]
        phi = [1.0] * N
        if P["use_escape"]:
            outs = {i: sum(F[i].values()) for i in F}
            for i in range(N):
                if not outs.get(i):
                    phi[i] = 0.5          # no spending history yet: neutral
                    continue
                mass = {i: 1.0}
                for _ in range(P["escape_steps"]):
                    nxt = defaultdict(float)
                    for u, m in mass.items():
                        if outs.get(u):
                            for v, c in F[u].items():
                                nxt[v] += m * c / outs[u]
                        else:
                            nxt[u] += m   # walk stops where nobody spends onward
                    mass = nxt
                circle = adj[i] | {i}
                phi[i] = sum(m for u, m in mass.items() if u not in circle)
            dw = [dw[i] * phi[i] for i in range(N)]

        # ---- posts this epoch
        posts = []  # (author, quality, read_estimate_seconds)
        for i in range(N):
            gname = group[i]
            if rng.random() < GROUPS[gname][1]:
                q = qual[i] if gname in FARM else min(1, max(0, qual[i] + rng.gauss(0, 0.08)))
                posts.append((i, q, rng.uniform(180, 600)))
        if not posts:
            continue
        by_author = defaultdict(list)
        for pi, (a, _, _) in enumerate(posts):
            by_author[a].append(pi)

        # ---- engagements: (engager, post, cost); dwell intervals per engager
        eng = []
        intervals = defaultdict(list)  # engager -> [(start, end, eng_index)]

        def engage(e, pi, votes, mode):
            c = votes * votes
            while c > bal[e] and votes > 0:
                votes -= 1
                c = votes * votes
            if votes == 0:
                return
            bal[e] -= c
            k = len(eng)
            eng.append((e, pi, c))
            est = posts[pi][2]
            if mode in ("parallel", "lazy") and P["farm_dwell"] == "careful":
                mode = "careful"
            if mode == "careful":    # one stream, back to back, full read time
                last = intervals[e][-1][1] if intervals[e] else rng.uniform(0, DAY * 0.5)
                intervals[e].append((last + 5, last + 5 + est * rng.uniform(0.9, 1.1), k))
            elif mode == "honest":
                last = intervals[e][-1][1] if intervals[e] else rng.uniform(0, DAY * 0.6)
                start = last - rng.uniform(0, 0.5) * est if rng.random() < 0.15 else last + rng.uniform(30, 300)
                intervals[e].append((start, start + est * rng.uniform(0.6, 1.3), k))
            elif mode == "parallel":   # every tab opened at once, left for the full estimate
                intervals[e].append((0.0, est, k))
            else:                      # lazy: four seconds
                intervals[e].append((0.0, 4.0, k))

        # honest feed: own community x4, everything else x1 (farm posts included)
        feed_w = {}
        for cidx in range(N_COMMUNITIES):
            feed_w[cidx] = [4.0 if comm[a] == cidx else 1.0 for (a, _, _) in posts]
        flags = []  # (flagger, post)
        for i in range(N):
            gname = group[i]
            if not GROUPS[gname][4]:
                continue
            seen = set(rng.choices(range(len(posts)), weights=feed_w[comm[i]], k=P["reads"]))
            if gname == "pod":  # the pod reads every other pod post, always
                for j in idx["pod"]:
                    seen.update(by_author.get(j, []))
            for pi in seen:
                a, q, _ = posts[pi]
                if a == i:
                    continue
                if gname == "pod" and group[a] == "pod":
                    engage(i, pi, 3, "honest")
                elif rng.random() < q:
                    engage(i, pi, 1 + int(q * 3.99), "honest")
                naive_by[group[a]] += 1 if rng.random() < q else 0
                if q > 0.7:
                    naive_by[group[a]] += 1  # "love" too
                if group[a] in FARM and rng.random() < 0.06:
                    flags.append((i, pi))
                elif q < 0.3 and group[a] not in FARM and rng.random() < 0.01:
                    flags.append((i, pi))

        honest_posts = [pi for pi, (a, _, _) in enumerate(posts) if group[a] not in FARM]
        if P["farm_mimic"] > 0 and honest_posts:
            n_ring_posts = sum(len(by_author.get(j, [])) for j in idx["ring"])
            k_mimic = int(round(n_ring_posts * P["farm_mimic"] / (1 - P["farm_mimic"])))
            for i in idx["ring"] + idx["sybil"]:
                for pi in rng.sample(honest_posts, min(k_mimic if group[i] == "ring" else 1, len(honest_posts))):
                    engage(i, pi, 2, "parallel")
        # ring: every member hits every other member's post
        for i in idx["ring"]:
            for j in idx["ring"]:
                if j != i:
                    for pi in by_author.get(j, []):
                        engage(i, pi, 2, "parallel")
                        naive_by["ring"] += 2  # like + love, free
        # sybils: each spends its allowance on the beneficiary
        for i in idx["sybil"]:
            for pi in by_author.get(benef, []):
                engage(i, pi, 3, "lazy")
                naive_by["beneficiary"] += 2

        # ---- dwell rating
        for e, ivs in intervals.items():
            spans = [(s, en) for (s, en, _) in ivs]
            credit = effective_dwell(spans) if P["dwell"] == "overlap" else [en - s for s, en in spans]
            for (s, en, k), cr in zip(ivs, credit):
                est = posts[eng[k][1]][2]
                rho[e] = (1 - P["lam"]) * rho[e] + P["lam"] * min(cr / est, 1.0)

        # ---- score + mint
        dist_cache = {}
        I = defaultdict(float)
        O = defaultdict(float)
        farm_v = defaultdict(float)
        all_v = defaultdict(float)
        for (e, pi, c) in eng:
            a = posts[pi][0]
            if a not in dist_cache:
                dist_cache[a] = bfs(adj, a)
            d = dist_cache[a].get(e)
            g = g_indep(d, P["D"])
            v = math.sqrt(c) * dw[e]
            I[pi] += (1 - g) * v
            O[pi] += g * v
            all_v[pi] += v
            if group[e] in FARM:
                farm_v[pi] += v
        score = {}
        for pi in set(I) | set(O):
            if P["bridging"]:
                score[pi] = P["alpha"] * I[pi] + P["beta"] * O[pi] + P["gamma"] * math.sqrt(I[pi] * O[pi])
            else:
                score[pi] = I[pi] + O[pi]
            if P["flags"]:
                score[pi] *= pen[posts[pi][0]]
        tot = sum(score.values())
        epoch_mint = defaultdict(float)
        if tot > 0:
            for pi, s in score.items():
                m = P["M"] * s / tot
                bal[posts[pi][0]] += m
                epoch_mint[group[posts[pi][0]]] += m
                if all_v[pi] > 0:
                    epoch_mint["_farm_made"] += m * farm_v[pi] / all_v[pi]
        if not P["burn"]:
            for (e, pi, c) in eng:
                bal[posts[pi][0]] += c

        # ---- flags: cost, moderator ruling, refund or burn, one-hop penalty
        if P["flags"]:
            spenders = defaultdict(list)
            for (e, pi, c) in eng:
                spenders[pi].append((e, c))
            for (f, pi) in flags:
                if bal[f] < P["flag_cost"]:
                    continue
                bal[f] -= P["flag_cost"]
                a = posts[pi][0]
                upheld = rng.random() < (P["mod_acc"] if group[a] in FARM else 0.05)
                if upheld:
                    bal[f] += P["flag_cost"]
                    pen[a] *= 0.7
                    for (e, c) in spenders[pi]:
                        pen[e] *= 1 - 0.02 * min(c, 9) / 9
        pen = [p + 0.05 * (1 - p) for p in pen]

        # ---- record flows, allowance, demurrage, seed
        fl = defaultdict(float)
        for (e, pi, c) in eng:
            fl[(e, posts[pi][0])] += c
        flow_hist.append(fl)
        bal = [(1 - P["delta"]) * b + P["allowance"] for b in bal]
        hist_w.append([t[i] * rho[i] * phi[i] for i in range(N)])
        if P["seed_mode"] == "moving" and len(hist_w) == P["sustain"]:
            sustained = normalize([min(h[i] for h in hist_w) for i in range(N)])
            seed = [(1 - P["eta"]) * s + P["eta"] * x for s, x in zip(seed, sustained)]

        if epoch > P["warmup"]:
            for gname, m in epoch_mint.items():
                mint_by[gname] += m
            if P["farm_dwell"] == "careful" and epoch == P["epochs"]:
                for gname in ("ring", "sybil", "regular"):
                    rho_by[gname] = sum(rho[i] for i in idx[gname]) / len(idx[gname])
            for i in range(N):
                trust_by[group[i]] += t[i]
            for (e, pi, c) in eng:
                eng_by[group[e]] += 1
            et = sum(m for gname, m in epoch_mint.items() if gname in GROUPS)
            farm_share_series.append(sum(epoch_mint[gname] for gname in FARM) / et if et else 0.0)
        if epoch == P["epochs"]:
            for gname in GROUPS:
                phi_by[gname] = sum(phi[i] for i in idx[gname]) / len(idx[gname])

    counted = P["epochs"] - P["warmup"]
    tm = sum(mint_by[gname] for gname in GROUPS)
    tn = sum(naive_by.values())
    return {
        "scenario": scenario, "seed": rng_seed,
        "mint_share": {gname: mint_by[gname] / tm for gname in GROUPS},
        "naive_share": {gname: naive_by[gname] / tn for gname in GROUPS},
        "trust_share": {gname: trust_by[gname] / counted for gname in GROUPS},
        "pop_share": {gname: GROUPS[gname][0] / N for gname in GROUPS},
        "final_balance": {gname: sum(bal[i] for i in idx[gname]) / len(idx[gname]) for gname in GROUPS},
        "farm_share_series": farm_share_series,
        "farm_made": mint_by["_farm_made"] / tm,
        "rho": rho_by,
        "phi": phi_by,
        "eng_rate": {gname: eng_by[gname] / counted / GROUPS[gname][0] for gname in GROUPS},
    }


# ---------------------------------------------------------------- driver

def mean_sd(xs):
    m = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / max(1, len(xs) - 1))
    return m, sd


def main():
    quick = "--quick" in sys.argv
    seeds = [1] if quick else [1, 2, 3, 4, 5]
    if quick:
        BASE["epochs"], BASE["warmup"] = 15, 5
    jobs = [(s, k) for s in SCENARIOS for k in seeds]
    with Pool() as pool:
        results = pool.starmap(run, jobs)

    agg = {}
    for s in SCENARIOS:
        rs = [r for r in results if r["scenario"] == s]
        row = {}
        for key in ("mint_share", "naive_share", "trust_share", "final_balance"):
            row[key] = {gname: mean_sd([r[key][gname] for r in rs]) for gname in GROUPS}
        row["farm_mint"] = mean_sd([sum(r["mint_share"][gname] for gname in FARM) for r in rs])
        row["farm_made"] = mean_sd([r["farm_made"] for r in rs])
        row["eng_rate"] = {gname: mean_sd([r["eng_rate"][gname] for r in rs]) for gname in GROUPS}
        row["phi"] = {gname: sum(r["phi"][gname] for r in rs) / len(rs) for gname in GROUPS}
        row["farm_series"] = [sum(xs) / len(xs) for xs in zip(*[r["farm_share_series"] for r in rs])]
        agg[s] = row
    pop = results[0]["pop_share"]

    print(f"population share: " + "  ".join(f"{gname} {pop[gname]:.1%}" for gname in GROUPS))
    print(f"naive (free like+love) share, full run: " +
          "  ".join(f"{gname} {agg['full']['naive_share'][gname][0]:.1%}" for gname in GROUPS))
    print()
    hdr = "scenario              farm mint   made-by-farm  " + "  ".join(f"{gname[:7]:>8}" for gname in GROUPS)
    print(hdr)
    for s in SCENARIOS:
        m, sd = agg[s]["farm_mint"]
        cells = "  ".join(f"{agg[s]['mint_share'][gname][0]:>8.1%}" for gname in GROUPS)
        fm, fsd = agg[s]["farm_made"]
        print(f"{s:<21} {m:>6.1%} ±{sd:.1%}  {fm:>6.1%} ±{fsd:.1%}  {cells}")

    out = {"params": BASE, "scenarios": SCENARIOS, "groups": {k: v[0] for k, v in GROUPS.items()},
           "pop_share": pop, "agg": agg, "seeds": seeds}
    if not quick:
        with open("results.json", "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
