"""
paper-pulse: read every AI paper from one month with Claude Haiku 5.5 (via OpenRouter)

setup (once):
  1. get a key at openrouter.ai/keys and give it a credit limit (e.g. $5) for safety
  2. Mac/Linux:  export OPENROUTER_API_KEY="your-key"
     Windows:    set OPENROUTER_API_KEY=your-key

run:
  python3 paper_pulse.py --test                # 50 papers, a few seconds, under 1 cent
  python3 paper_pulse.py --month 2026-09       # a whole month
  python3 make_report.py papers_tagged_2026-09.csv

safe to stop and re-run at any time:
  - the arXiv download is cached, so a re-run doesn't download again
  - every tagged paper is saved the moment it's done, so a re-run only does
    the papers that are left (and retries any that failed)
  - it stops itself before spending more than --budget dollars (default $4.50)
no installs needed, only python 3.

Thank you to arXiv for use of its open access interoperability.
"""
import os, sys, csv, json, time, re, argparse, calendar, threading
import urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

CATS = ["cs.AI", "cs.LG", "cs.CL", "cs.CV"]
MODEL = "anthropic/claude-haiku-5.5"
PRICE_IN, PRICE_OUT = 0.10, 0.50          # $ per million tokens
WORKERS = 8
ARXIV = os.environ.get("ARXIV_URL", "http://export.arxiv.org/api/query")
OPENROUTER = os.environ.get("OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions")

TOPICS = ["LLM reasoning", "agents & tool use", "code generation", "multimodal / vision-language",
          "image & video generation", "robotics & embodied AI", "safety, alignment & security",
          "evaluation & benchmarks", "efficiency (small models, quantization, inference)",
          "reinforcement learning", "retrieval, RAG & memory",
          "science & health applications (medicine, biology, physics, climate)",
          "speech & audio", "theory & optimization", "AI & society (policy, ethics, education, work)", "other"]

FIELDS = ["id", "title", "abstract", "date", "topic", "one_line", "surprise", "tok_in", "tok_out"]
A = "{http://www.w3.org/2005/Atom}"
OS = "{http://a9.com/-/spec/opensearch/1.1/}"


# ---------- step 1: arXiv ----------

def arxiv_page(query, start, size):
    url = ARXIV + "?" + urllib.parse.urlencode({"search_query": query, "start": start, "max_results": size,
                                                "sortBy": "submittedDate", "sortOrder": "ascending"})
    for attempt in range(6):
        try:
            root = ET.fromstring(urllib.request.urlopen(url, timeout=120).read())
            total = int(root.find(OS + "totalResults").text)
            return total, root.findall(A + "entry")
        except Exception as e:
            print(f"    arxiv hiccup ({str(e)[:60]}), waiting {10 * (attempt + 1)}s...")
            time.sleep(10 * (attempt + 1))
    sys.exit("arXiv isn't responding. wait 15 minutes and run the same command again (nothing is lost).")


def fetch_day(day):   # day = "20260901"
    query = "(" + " OR ".join(f"cat:{c}" for c in CATS) + f") AND submittedDate:[{day}0000 TO {day}2359]"
    papers, start, total, empties = {}, 0, None, 0
    while total is None or start < total:
        t, entries = arxiv_page(query, start, 1000)
        total = t if total is None else total
        time.sleep(3)                        # arxiv asks for 3 seconds between requests
        if not entries:
            empties += 1                     # arxiv sometimes returns an empty page by mistake: retry it
            if empties > 3: break
            continue
        empties = 0
        for e in entries:
            pid = re.sub(r"v\d+$", "", e.find(A + "id").text.split("/abs/")[-1])   # 2609.01234v2 -> 2609.01234
            papers[pid] = {"id": pid,
                           "title": " ".join((e.find(A + "title").text or "").split()),
                           "abstract": " ".join((e.find(A + "summary").text or "").split()),
                           "date": e.find(A + "published").text[:10]}
        start += len(entries)
    return papers, total or 0


def fetch_month(month, test):
    cache = f"papers_raw_{month}{'_TEST' if test else ''}.json"
    if os.path.exists(cache):
        papers = json.load(open(cache, encoding="utf-8"))
        print(f"   using {len(papers)} papers already downloaded ({cache})")
        return papers
    y, m = map(int, month.split("-"))
    days = [f"{y}{m:02d}{d:02d}" for d in range(1, calendar.monthrange(y, m)[1] + 1)]
    papers, short = {}, []
    for day in days:
        got, total = fetch_day(day)
        papers.update(got)
        flag = "" if len(got) >= total else f"  (arXiv said {total}, got {len(got)})"
        if flag: short.append(day)
        print(f"   {day[:4]}-{day[4:6]}-{day[6:]}: {len(got)} papers{flag}  | total {len(papers)}")
        if test and len(papers) >= 50: break
    papers = list(papers.values())[:50] if test else list(papers.values())
    if short and not test:
        print(f"   note: {len(short)} day(s) came back a little short from arXiv: {', '.join(short)}")
    json.dump(papers, open(cache, "w", encoding="utf-8"), ensure_ascii=False)
    return papers


# ---------- step 2: Claude Haiku ----------

PROMPT = """You are tagging AI research papers. Reply with JSON only, no other text:
{{"topic": one of {topics}, "one_line": "what this paper does, in plain english, max 15 words", "surprise": 1-5}}
Use "other" only if no topic fits at all.
Surprise = how surprising or delightful a smart non-expert would find it: 1 = routine incremental work, 2 = solid but expected, 3 = interesting idea, 4 = an unexpected finding people would share, 5 = rare, jaw-dropping or delightful (about 1 in 50 papers).

Title: {title}
Abstract: {abstract}"""

stop = threading.Event()
hiccups = {}   # how many retries, by reason (shown in the progress line)
stop_reason = []


def clean(d):
    topic = d.get("topic") if d.get("topic") in TOPICS else "other"
    try:
        s = min(5, max(1, int(round(float(d.get("surprise"))))))
    except (TypeError, ValueError):
        s = ""
    one = " ".join(str(d.get("one_line") or "").split())
    return topic, one, s


def tag(p, key):
    if stop.is_set():
        return None
    body = {"model": MODEL, "max_tokens": 300, "temperature": 0, "reasoning": {"effort": "low"},
            "messages": [{"role": "user", "content": PROMPT.format(topics=json.dumps(TOPICS), **p)}]}
    err = ""
    for attempt in range(6):
        if stop.is_set():
            return None
        try:
            req = urllib.request.Request(OPENROUTER, data=json.dumps(body).encode(),
                                         headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                                  "X-Title": "paper-pulse"})
            r = json.loads(urllib.request.urlopen(req, timeout=90).read())
            if "error" in r:
                raise RuntimeError(str(r["error"])[:150])
            text = r["choices"][0]["message"]["content"] or ""
            m = re.search(r"\{.*\}", text, re.S)
            if not m:
                raise ValueError("no JSON in reply")
            topic, one, s = clean(json.loads(m.group(0)))
            if not one:
                raise ValueError("empty one_line")
            u = r.get("usage") or {}
            return {**p, "topic": topic, "one_line": one, "surprise": s,
                    "tok_in": u.get("prompt_tokens", 0), "tok_out": u.get("completion_tokens", 0)}
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            hiccups[e.code] = hiccups.get(e.code, 0) + 1
            if e.code in (401, 403):
                stop_reason.append("the API key was rejected. check OPENROUTER_API_KEY is set correctly.")
                stop.set(); return None
            if e.code == 402:
                stop_reason.append("out of OpenRouter credit (or the key's limit was reached).")
                stop.set(); return None
            time.sleep(20 if e.code == 429 else 5 * (attempt + 1))
        except Exception as e:
            err = str(e)[:100]
            hiccups["other"] = hiccups.get("other", 0) + 1
            time.sleep(5 * (attempt + 1))
    return {**p, "topic": "ERROR", "one_line": err, "surprise": "", "tok_in": 0, "tok_out": 0}


def cost_of(rows):
    rows = list(rows)
    return sum(int(r["tok_in"] or 0) for r in rows) / 1e6 * PRICE_IN + \
           sum(int(r["tok_out"] or 0) for r in rows) / 1e6 * PRICE_OUT


def last_month():
    import datetime
    first = datetime.date.today().replace(day=1)
    return (first - datetime.timedelta(days=1)).strftime("%Y-%m")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="only 50 papers")
    ap.add_argument("--month", default=last_month(), help="YYYY-MM (default: last month)")
    ap.add_argument("--budget", type=float, default=4.5, help="stop before spending more than this many dollars")
    a = ap.parse_args()
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if not key:
        sys.exit("set OPENROUTER_API_KEY first (see README.md)")

    tagname = f"{a.month}{'_TEST' if a.test else ''}"
    progress, out = f"progress_{tagname}.jsonl", f"papers_tagged_{tagname}.csv"

    print(f"1/2 downloading the list of {a.month} papers from arXiv (a few minutes, 1 line per day)...")
    papers = fetch_month(a.month, a.test)
    if not papers:
        sys.exit("arXiv returned 0 papers. check the --month value.")

    done = {}
    if os.path.exists(progress):
        for line in open(progress, encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue                     # half-written last line from a crash
            if r.get("topic") != "ERROR":
                done[r["id"]] = r
    spent_before = cost_of(done.values())
    todo = [p for p in papers if p["id"] not in done]
    est = len(todo) * 0.000115
    print(f"\n2/2 tagging with Claude Haiku 5.5: {len(papers)} papers, {len(done)} already done, {len(todo)} to go")
    print(f"   estimated cost ~${est:.2f}  (budget ${a.budget:.2f}, already spent ${spent_before:.2f})")
    if spent_before + est > a.budget:
        print(f"   heads up: this may hit the budget and stop early. re-run with --budget {spent_before + est * 1.3:.1f} to allow more.")

    t0, new, spent = time.time(), [], spent_before
    fh = open(progress, "a", encoding="utf-8")
    ex = ThreadPoolExecutor(max_workers=WORKERS)
    futs = [ex.submit(tag, p, key) for p in todo]
    try:
        for i, f in enumerate(as_completed(futs), 1):
            r = f.result()
            if r is not None:
                fh.write(json.dumps(r, ensure_ascii=False) + "\n"); fh.flush()
                new.append(r)
                spent += int(r["tok_in"] or 0) / 1e6 * PRICE_IN + int(r["tok_out"] or 0) / 1e6 * PRICE_OUT
                if r["topic"] != "ERROR":
                    done[r["id"]] = r
            if spent >= a.budget and not stop.is_set():
                stop_reason.append(f"reached the ${a.budget:.2f} budget.")
                stop.set()
            if (i % 100 == 0 or i == len(futs)) and not stop.is_set():
                rate = i / max(1, time.time() - t0)
                h = ", ".join(f"{'slow down (429)' if k == 429 else k} x{v}" for k, v in hiccups.items())
                print(f"   {i}/{len(todo)} done | ${spent:.3f} spent | ~{(len(todo) - i) / rate / 60:.0f} min left" + (f" | retries: {h}" if h else ""))
    except KeyboardInterrupt:
        stop_reason.append("you pressed Ctrl+C.")
        stop.set()
    try:
        ex.shutdown(wait=not stop.is_set(), cancel_futures=True)
    except TypeError:                        # python older than 3.9
        ex.shutdown(wait=not stop.is_set())
    fh.close()

    rows = [done[p["id"]] for p in papers if p["id"] in done]
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader(); w.writerows(rows)
    errs = sum(r["topic"] == "ERROR" for r in new)
    left = len(papers) - len(rows)
    print(f"\ntagged {len(rows)} of {len(papers)} papers | this run {time.time() - t0:.0f}s | "
          f"total cost ${cost_of(rows):.3f} | failed this run {errs} | retries {sum(hiccups.values())}")
    print(f"saved {out}")
    if stop_reason:
        print("stopped early: " + stop_reason[0])
    if left:
        print(f"{left} papers still to do: run the same command again and it will only do those.")
    else:
        print(f"all done. next: python3 make_report.py {out}")


if __name__ == "__main__":
    main()
