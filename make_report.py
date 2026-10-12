"""
turn a tagged month into a report: charts + report.md + a light tags.csv

  python3 make_report.py papers_tagged_2026-09.csv

writes reports/2026-09/
  report.md          headline numbers, topic table, most surprising papers
  topics.png         share of papers per topic
  papers_per_day.png how many papers were submitted each day
  tags.csv           id, date, topic, one_line, surprise (no abstracts, see README)

needs matplotlib for the charts (pip install matplotlib). everything else is plain python.
"""
import csv, os, sys, collections, datetime

PRICE_IN, PRICE_OUT = 0.10, 0.50          # $ per million tokens (Claude Haiku 5.5)
HIGHLIGHT = {"agents & tool use", "LLM reasoning", "code generation"}   # the topics everyone talks about

SHORT = {   # shorter labels for charts
    "science & health applications (medicine, biology, physics, climate)": "science & health",
    "efficiency (small models, quantization, inference)": "efficiency & small models",
    "AI & society (policy, ethics, education, work)": "AI & society",
    "multimodal / vision-language": "multimodal / vision-language",
}
INK, MUTED, GRID, BASE, ACCENT, SURFACE = "#0b0b0b", "#6b6a66", "#e6e5e0", "#9fb8d9", "#eb6834", "#fcfcfb"


def load(path):
    rows = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r["topic"] != "ERROR"]
    if not rows:
        sys.exit("no tagged rows in " + path)
    return rows


def charts(rows, n, topics, days, outdir, month_name):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed: skipping charts (pip install matplotlib)")
        return
    plt.rcParams.update({"font.family": ["Inter", "DejaVu Sans"], "font.size": 12,
                         "axes.edgecolor": GRID, "axes.labelcolor": MUTED,
                         "xtick.color": MUTED, "ytick.color": INK, "figure.facecolor": SURFACE,
                         "axes.facecolor": SURFACE})

    # topics: horizontal bars, biggest on top
    items = topics.most_common()[::-1]
    fig, ax = plt.subplots(figsize=(10, 7.2), dpi=160)
    labels = [SHORT.get(k, k) for k, _ in items]
    vals = [v / n * 100 for _, v in items]
    colors = [ACCENT if k in HIGHLIGHT else BASE for k, _ in items]
    ax.barh(labels, vals, color=colors, height=0.72)
    for i, v in enumerate(vals):
        ax.text(v + 0.25, i, f"{v:.1f}%", va="center", fontsize=10.5, color=INK)
    ax.set_xlim(0, max(vals) * 1.15)
    ax.xaxis.set_visible(False)
    for s in ("top", "right", "bottom"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)
    fig.suptitle(f"What {n:,} AI papers from {month_name} were about", x=0.02, ha="left",
                 fontsize=17, fontweight="bold", color=INK, y=0.985)
    fig.text(0.02, 0.925, "share of papers per topic · orange = the topics that dominate the AI conversation online",
             fontsize=10.5, color=MUTED)
    fig.text(0.02, 0.01, "arXiv cs.AI, cs.LG, cs.CL, cs.CV · one topic per paper, tagged by Claude Haiku 5.5 · paper-pulse",
             fontsize=8.5, color=MUTED)
    fig.tight_layout(rect=(0, 0.03, 1, 0.91))
    fig.savefig(os.path.join(outdir, "topics.png"))
    plt.close(fig)

    # papers per day: weekends lighter
    ds = sorted(days)
    dates = [datetime.date.fromisoformat(d) for d in ds]
    vals = [days[d] for d in ds]
    fig, ax = plt.subplots(figsize=(10, 4.8), dpi=160)
    colors = [BASE if dt.weekday() >= 5 else "#2a78d6" for dt in dates]
    ax.bar(range(len(ds)), vals, color=colors, width=0.78)
    ax.set_xticks(range(0, len(ds), 7) if len(ds) > 7 else range(len(ds)))
    ax.set_xticklabels([f"{dates[int(i)]:%b} {dates[int(i)].day}" for i in ax.get_xticks()], fontsize=10)
    ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="both", length=0)
    peak = max(range(len(vals)), key=vals.__getitem__)
    ax.annotate(f"{vals[peak]:,}", (peak, vals[peak]), xytext=(0, 4), textcoords="offset points",
                ha="center", fontsize=10.5, color=INK, fontweight="bold")
    fig.suptitle(f"AI papers submitted per day, {month_name}", x=0.02, ha="left", fontsize=16,
                 fontweight="bold", color=INK, y=0.975)
    fig.text(0.02, 0.88, "dark = weekday, light = weekend", fontsize=10.5, color=MUTED)
    fig.tight_layout(rect=(0, 0, 1, 0.86))
    fig.savefig(os.path.join(outdir, "papers_per_day.png"))
    plt.close(fig)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python3 make_report.py papers_tagged_YYYY-MM.csv")
    rows = load(sys.argv[1])
    n = len(rows)
    month = min(r["date"] for r in rows)[:7]
    month_name = datetime.date.fromisoformat(month + "-01").strftime("%B %Y")
    outdir = os.path.join("reports", month)
    os.makedirs(outdir, exist_ok=True)

    cost = sum(int(r["tok_in"] or 0) for r in rows) / 1e6 * PRICE_IN + sum(int(r["tok_out"] or 0) for r in rows) / 1e6 * PRICE_OUT
    topics = collections.Counter(r["topic"] for r in rows)
    days = collections.Counter(r["date"] for r in rows)
    ndays = len(days)
    every = ndays * 24 * 60 / n
    weekday = [v for d, v in days.items() if datetime.date.fromisoformat(d).weekday() < 5]
    weekend = [v for d, v in days.items() if datetime.date.fromisoformat(d).weekday() >= 5]
    peak_day, peak = days.most_common(1)[0]

    charts(rows, n, topics, days, outdir, month_name)

    with open(os.path.join(outdir, "tags.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["id", "url", "date", "topic", "one_line", "surprise"])
        for r in sorted(rows, key=lambda r: (r["date"], r["id"])):
            w.writerow([r["id"], "https://arxiv.org/abs/" + r["id"], r["date"], r["topic"], r["one_line"], r["surprise"]])

    surprise = collections.Counter(r["surprise"] for r in rows)
    top = sorted(rows, key=lambda r: (-int(r["surprise"] or 0), r["date"]))
    fives = [r for r in top if r["surprise"] == "5"]

    L = [f"# Paper Pulse · {month_name}", "",
         f"Every AI paper submitted to arXiv in {month_name} (cs.AI, cs.LG, cs.CL, cs.CV), read and tagged by Claude Haiku 5.5.", "",
         "## Headline numbers", "",
         f"- **{n:,} papers** in {ndays} days",
         f"- **one new paper every {every:.1f} minutes**, day and night",
         f"- weekdays averaged **{sum(weekday) / max(1, len(weekday)):.0f}** papers a day, weekends **{sum(weekend) / max(1, len(weekend)):.0f}**",
         f"- busiest day: **{peak_day}** with **{peak:,}** papers",
         f"- cost to read all of them: **${cost:.2f}** (about ${cost / n * 1000:.2f} per 1,000 papers)", "",
         "## What the papers were about", "", "![topics](topics.png)", "",
         "| topic | papers | share |", "|---|---:|---:|"]
    L += [f"| {k} | {v:,} | {v / n * 100:.1f}% |" for k, v in topics.most_common()]
    L += ["", "## When they were submitted", "", "![papers per day](papers_per_day.png)", "",
          f"## The most surprising papers ({len(fives)} of {n:,} scored 5/5)", "",
          "Surprise is Haiku's 1-5 guess at how surprising a smart non-expert would find the paper. "
          "It is a fun filter, not a quality score.", ""]
    L += [f"- [{r['one_line']}](https://arxiv.org/abs/{r['id']}) · {r['topic']}" for r in fives[:20]]
    L += ["", "Surprise scores: " + ", ".join(f"{k}: {surprise.get(k, 0):,}" for k in "12345"), "",
          "## Honest limitations", "",
          "- one topic per paper, so a paper about reasoning benchmarks counts under only one of the two",
          "- topics and one-liners come from a small AI model reading the abstract, not the full paper",
          "- arXiv preprints are not peer reviewed, and arXiv is not all of AI research",
          "- dates are arXiv submission dates", "",
          "Thank you to arXiv for use of its open access interoperability.", ""]
    open(os.path.join(outdir, "report.md"), "w", encoding="utf-8").write("\n".join(L))
    print(f"wrote {outdir}/: report.md, topics.png, papers_per_day.png, tags.csv")


if __name__ == "__main__":
    main()
