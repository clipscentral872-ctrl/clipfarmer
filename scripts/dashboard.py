"""Generate the clipfarmer dashboard as a self-contained HTML file.

Reads our SQLite DB, computes summary stats and time series, and renders
a 4-page dashboard (Overview / Revenue / System / Competitors) using
Chart.js loaded from CDN. The output is `data/dashboard.html` — open it
in any browser, no server required.

To refresh, just re-run this script (or ask the bot: 'show me the
dashboard'). All cost figures show in R (ZAR), API costs in $.

Usage:
    python scripts/dashboard.py
    python scripts/dashboard.py --no-open       # don't open browser
"""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.repository import Repository


# Manual / configurable economics.
ZAR_PER_USD = 18.5               # rough USD→ZAR conversion for revenue display (update as needed)
CLAUDE_PRO_MONTHLY_ZAR = 380.0   # Chris's Pro membership in ZAR (≈ $20 USD/mo)
API_BALANCE_USD = None           # None until we wire to Anthropic billing


def main() -> int:
    p = argparse.ArgumentParser(prog="dashboard")
    p.add_argument("--no-open", action="store_true", help="Skip opening the browser")
    p.add_argument(
        "--out", type=str, default="data/dashboard.html",
        help="Where to write the HTML",
    )
    args = p.parse_args()

    repo = Repository()
    data = collect_data(repo)
    html = render(data)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"Wrote {out_path.resolve()}  ({len(html):,} bytes)")
    if not args.no_open:
        webbrowser.open(out_path.resolve().as_uri())
    return 0


# ----------------------------------------------------------------------
def collect_data(repo: Repository) -> dict:
    with repo.conn() as c:
        campaigns = [dict(r) for r in c.execute(
            "SELECT id, title, payout_per_1k_views, budget_remaining_pct, "
            "viability_score, tracking_code, current_source_path "
            "FROM campaigns WHERE status IS NULL OR status='active'"
        ).fetchall()]
        posts = [dict(r) for r in c.execute(
            "SELECT p.id, p.clip_id, p.platform, p.post_url, p.posted_at, p.status, "
            "cl.caption_text, cl.campaign_id, cl.ai_score, "
            "c.title AS campaign_title, c.payout_per_1k_views "
            "FROM posts p "
            "LEFT JOIN clips cl ON cl.id = p.clip_id "
            "LEFT JOIN campaigns c ON c.id = cl.campaign_id "
            "ORDER BY p.posted_at DESC"
        ).fetchall()]
        analytics = [dict(r) for r in c.execute(
            "SELECT post_id, captured_at, views, likes, comments, shares, saves "
            "FROM analytics ORDER BY captured_at DESC"
        ).fetchall()]
        clips = [dict(r) for r in c.execute(
            "SELECT id, campaign_id, created_at, ai_score, status FROM clips "
            "ORDER BY created_at DESC"
        ).fetchall()]
        submissions = [dict(r) for r in c.execute(
            "SELECT id, post_id, campaign_id, submission_status, submitted_at, "
            "payout_amount, payout_currency FROM submissions"
        ).fetchall()]
        # Top performers per campaign (style-mimicry data already stored)
        top_performers = []
        for camp in campaigns:
            raw = c.execute(
                "SELECT top_performers FROM campaigns WHERE id = ?", (camp["id"],)
            ).fetchone()
            if raw and raw["top_performers"]:
                try:
                    perfs = json.loads(raw["top_performers"])
                    for p in perfs:
                        p["campaign_id"] = camp["id"]
                        p["campaign_title"] = camp["title"]
                        top_performers.append(p)
                except Exception:
                    pass

    # Latest analytics per post
    latest_by_post = {}
    for a in analytics:
        if a["post_id"] not in latest_by_post:
            latest_by_post[a["post_id"]] = a

    # Per-platform totals
    platform_totals = defaultdict(lambda: {"posts": 0, "views": 0, "earnings_est_usd": 0.0})
    per_campaign = defaultdict(lambda: {
        "posts": 0, "views": 0, "earnings_est_usd": 0.0, "title": "",
        "cpm": 0.0,
    })
    revenue_by_day: dict[str, float] = defaultdict(float)

    for post in posts:
        platform = post["platform"]
        a = latest_by_post.get(post["id"])
        views = (a or {}).get("views") or 0
        cpm = post.get("payout_per_1k_views") or 0.0
        earnings = (views / 1000.0) * cpm if cpm else 0.0

        platform_totals[platform]["posts"] += 1
        platform_totals[platform]["views"] += views
        platform_totals[platform]["earnings_est_usd"] += earnings

        cid = post.get("campaign_id")
        if cid:
            per_campaign[cid]["title"] = post.get("campaign_title") or ""
            per_campaign[cid]["cpm"] = cpm
            per_campaign[cid]["posts"] += 1
            per_campaign[cid]["views"] += views
            per_campaign[cid]["earnings_est_usd"] += earnings

        if post.get("posted_at"):
            day = post["posted_at"][:10]
            revenue_by_day[day] += earnings

    total_views = sum(d["views"] for d in platform_totals.values())
    total_earnings_usd = sum(d["earnings_est_usd"] for d in platform_totals.values())
    total_earnings_zar = total_earnings_usd * ZAR_PER_USD

    # 7-day revenue series
    today = datetime.now(timezone.utc).date()
    last_7_days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    revenue_7d = [round(revenue_by_day.get(d, 0.0) * ZAR_PER_USD, 2) for d in last_7_days]

    # System metrics
    now = datetime.now(timezone.utc)
    today_iso = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    week_iso = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month_iso = (now - timedelta(days=30)).isoformat(timespec="seconds")

    clips_24h = sum(1 for c in clips if (c.get("created_at") or "") >= today_iso)
    clips_7d = sum(1 for c in clips if (c.get("created_at") or "") >= week_iso)
    clips_30d = sum(1 for c in clips if (c.get("created_at") or "") >= month_iso)

    post_success_rate = 0
    if posts:
        posted = sum(1 for p in posts if p.get("status") == "posted")
        post_success_rate = round(100.0 * posted / len(posts), 1)

    approval_rate = 0
    if clips:
        # Crude: clips that ended up posted ≈ approved
        clip_ids_posted = {p["clip_id"] for p in posts if p.get("status") == "posted"}
        approved = sum(1 for c in clips if c["id"] in clip_ids_posted)
        approval_rate = round(100.0 * approved / len(clips), 1)

    top_posts = sorted(
        [
            {
                "id": p["id"],
                "platform": p["platform"],
                "campaign": p.get("campaign_title") or "",
                "title": (p.get("caption_text") or "").split("\n", 1)[0][:80],
                "url": p.get("post_url"),
                "views": (latest_by_post.get(p["id"]) or {}).get("views") or 0,
                "earnings_zar": round((((latest_by_post.get(p["id"]) or {}).get("views") or 0)
                                      / 1000.0) * (p.get("payout_per_1k_views") or 0) * ZAR_PER_USD, 2),
            }
            for p in posts
        ],
        key=lambda r: r["views"],
        reverse=True,
    )[:10]

    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "summary": {
            "total_revenue_zar": round(total_earnings_zar, 2),
            "total_revenue_usd": round(total_earnings_usd, 2),
            "claude_pro_monthly_zar": CLAUDE_PRO_MONTHLY_ZAR,
            "api_balance_usd": API_BALANCE_USD,
            "api_balance_warning": (API_BALANCE_USD is not None and API_BALANCE_USD <= 3.0),
            "total_views": total_views,
            "total_posts": len(posts),
            "total_clips": len(clips),
            "total_campaigns_active": len(campaigns),
            "total_submissions": len(submissions),
        },
        "platforms": {
            p: {
                "posts": d["posts"],
                "views": d["views"],
                "earnings_zar": round(d["earnings_est_usd"] * ZAR_PER_USD, 2),
            }
            for p, d in platform_totals.items()
        },
        "per_campaign": [
            {
                "campaign_id": cid,
                "title": d["title"],
                "posts": d["posts"],
                "views": d["views"],
                "cpm_usd": d["cpm"],
                "earnings_zar": round(d["earnings_est_usd"] * ZAR_PER_USD, 2),
            }
            for cid, d in per_campaign.items()
        ],
        "revenue_7d": {"labels": last_7_days, "values_zar": revenue_7d},
        "top_posts": top_posts,
        "system": {
            "clips_24h": clips_24h,
            "clips_7d": clips_7d,
            "clips_30d": clips_30d,
            "post_success_rate_pct": post_success_rate,
            "approval_rate_pct": approval_rate,
        },
        "competitors": top_performers[:30],
        "campaigns": campaigns,
    }


# ----------------------------------------------------------------------
def render(data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return _TEMPLATE.replace("__DATA_JSON__", payload)


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>clipfarmer · dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #0f1226; color: #e6e7ee;
  }
  .header { padding: 18px 28px; background: #181b35; border-bottom: 1px solid #232652; display: flex; align-items: center; gap: 18px; }
  .header h1 { font-size: 18px; margin: 0; font-weight: 700; }
  .header .gen { color: #7d83a3; font-size: 12px; margin-left: auto; }
  .tabs { display: flex; padding: 0 28px; background: #181b35; border-bottom: 1px solid #232652; }
  .tab { padding: 14px 18px; cursor: pointer; color: #9ea3c8; border-bottom: 2px solid transparent; font-size: 14px; }
  .tab.active { color: #fff; border-bottom-color: #ff7a28; }
  .alert { margin: 14px 28px 0; padding: 12px 16px; border-radius: 8px; background: #5a1f1f; border: 1px solid #c44a4a; color: #ffd8d8; font-size: 14px; }
  .alert.warn { background: #4a3a1a; border-color: #d49a3a; color: #ffe8b8; }
  .alert.success { background: #1d4a2c; border-color: #2faa66; color: #c6f0d4; }
  .container { padding: 22px 28px; max-width: 1200px; margin: 0 auto; }
  .grid { display: grid; gap: 18px; grid-template-columns: repeat(4, 1fr); }
  .card { background: #181b35; border: 1px solid #232652; border-radius: 12px; padding: 18px; }
  .card .label { color: #9ea3c8; font-size: 12px; text-transform: uppercase; letter-spacing: 1px; }
  .card .num { font-size: 26px; font-weight: 700; margin-top: 6px; }
  .card .sub { color: #7d83a3; font-size: 12px; margin-top: 4px; }
  .row { display: grid; grid-template-columns: 2fr 1fr; gap: 18px; margin-top: 22px; }
  .panel { background: #181b35; border: 1px solid #232652; border-radius: 12px; padding: 18px; }
  .panel h2 { font-size: 14px; margin: 0 0 14px; color: #e6e7ee; }
  .panel .desc { color: #9ea3c8; font-size: 13px; margin-top: 12px; line-height: 1.5; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 6px; border-bottom: 1px solid #232652; }
  th { color: #9ea3c8; font-weight: 500; text-transform: uppercase; letter-spacing: 0.5px; font-size: 11px; }
  td a { color: #ff9c5a; text-decoration: none; }
  td a:hover { text-decoration: underline; }
  .pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; background: #232652; color: #c5cae9; }
  .pill.tt { background: #2c1f3a; color: #d8b4fe; }
  .pill.yt { background: #3a1f1f; color: #fecaca; }
  .pill.ig { background: #3a2a1f; color: #fde68a; }
  .selector { background: #232652; color: #fff; padding: 8px 12px; border: none; border-radius: 6px; font-size: 13px; }
  .toggle { display: inline-flex; background: #232652; border-radius: 6px; padding: 3px; gap: 0; }
  .toggle button { padding: 6px 12px; background: transparent; border: none; color: #9ea3c8; cursor: pointer; font-size: 12px; border-radius: 4px; }
  .toggle button.active { background: #ff7a28; color: #fff; }
  canvas { max-height: 280px; }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .empty { color: #7d83a3; font-style: italic; padding: 12px; text-align: center; }
</style>
</head>
<body>

<div class="header">
  <h1>clipfarmer</h1>
  <span class="gen" id="gen-at"></span>
</div>

<div class="tabs">
  <div class="tab active" data-tab="overview">Overview</div>
  <div class="tab" data-tab="revenue">Revenue</div>
  <div class="tab" data-tab="system">System</div>
  <div class="tab" data-tab="competitors">Competitors</div>
</div>

<div id="alerts"></div>

<!-- =================== OVERVIEW =================== -->
<div class="tab-content active" id="tab-overview">
  <div class="container">
    <div class="grid">
      <div class="card">
        <div class="label">Total revenue (est.)</div>
        <div class="num" id="kpi-revenue">R0.00</div>
        <div class="sub">From cached view counts × CPM</div>
      </div>
      <div class="card">
        <div class="label">Net profit (est.)</div>
        <div class="num" id="kpi-profit">R0.00</div>
        <div class="sub">Revenue − Claude Pro (R<span id="pro-cost"></span>/mo)</div>
      </div>
      <div class="card">
        <div class="label">Total spend</div>
        <div class="num" id="kpi-spend">R<span id="pro-cost2"></span></div>
        <div class="sub">Claude Pro membership (API spend not yet wired)</div>
      </div>
      <div class="card">
        <div class="label">API balance (Anthropic)</div>
        <div class="num" id="kpi-balance">—</div>
        <div class="sub">Wire to Anthropic billing when public API exists</div>
      </div>
    </div>

    <div class="row">
      <div class="panel">
        <h2>Revenue · last 7 days</h2>
        <canvas id="chart-revenue-7d"></canvas>
        <div class="desc" id="desc-revenue-7d"></div>
      </div>
      <div class="panel">
        <h2>Top performing posts</h2>
        <table>
          <thead><tr><th>Campaign</th><th>Plat.</th><th>Views</th><th>Est.</th></tr></thead>
          <tbody id="tbl-top-posts"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- =================== REVENUE =================== -->
<div class="tab-content" id="tab-revenue">
  <div class="container">
    <div class="row" style="grid-template-columns: 1fr;">
      <div class="panel">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px;">
          <h2 style="margin: 0;">Revenue · time series</h2>
          <div class="toggle" id="period-toggle">
            <button data-period="daily" class="active">Daily</button>
            <button data-period="weekly">Weekly</button>
            <button data-period="monthly">Monthly</button>
          </div>
        </div>
        <canvas id="chart-revenue-series"></canvas>
        <div class="desc" id="desc-revenue-series"></div>
      </div>
    </div>
    <div class="row">
      <div class="panel">
        <h2>Per platform</h2>
        <canvas id="chart-platform-revenue"></canvas>
        <div class="desc" id="desc-platforms"></div>
      </div>
      <div class="panel">
        <h2>Per campaign</h2>
        <table>
          <thead><tr><th>Campaign</th><th>Posts</th><th>Views</th><th>Est.</th></tr></thead>
          <tbody id="tbl-per-campaign"></tbody>
        </table>
      </div>
    </div>
  </div>
</div>

<!-- =================== SYSTEM =================== -->
<div class="tab-content" id="tab-system">
  <div class="container">
    <div class="grid">
      <div class="card"><div class="label">Clips · 24h</div><div class="num" id="sys-24h">0</div></div>
      <div class="card"><div class="label">Clips · 7d</div><div class="num" id="sys-7d">0</div></div>
      <div class="card"><div class="label">Clips · 30d</div><div class="num" id="sys-30d">0</div></div>
      <div class="card"><div class="label">Active campaigns</div><div class="num" id="sys-camp">0</div></div>
    </div>
    <div class="row">
      <div class="panel">
        <h2>Approval & post success</h2>
        <table>
          <tr><td>Approval rate</td><td id="sys-approval">—</td></tr>
          <tr><td>Post success rate</td><td id="sys-post">—</td></tr>
          <tr><td>Total clips produced</td><td id="sys-clips-total">—</td></tr>
          <tr><td>Total platform posts</td><td id="sys-posts-total">—</td></tr>
          <tr><td>Total Whop submissions</td><td id="sys-subs-total">—</td></tr>
        </table>
        <div class="desc">Approval rate = clips that reached 'posted' / total clips produced. Post success rate = posted / all posts attempted.</div>
      </div>
      <div class="panel">
        <h2>Posts per platform</h2>
        <canvas id="chart-platform-posts"></canvas>
        <div class="desc" id="desc-system-platforms"></div>
      </div>
    </div>
  </div>
</div>

<!-- =================== COMPETITORS =================== -->
<div class="tab-content" id="tab-competitors">
  <div class="container">
    <div class="panel">
      <h2>Top performing competitor clips (from each campaign's brief)</h2>
      <table>
        <thead><tr><th>Campaign</th><th>Title / hook</th><th>Views</th><th>Platform</th></tr></thead>
        <tbody id="tbl-competitors"></tbody>
      </table>
      <div class="desc" id="desc-competitors"></div>
    </div>
  </div>
</div>

<script>
const DATA = __DATA_JSON__;

// ---------------------- helpers ----------------------
const fmtZAR = (n) => "R" + (Number(n || 0)).toLocaleString("en-ZA", {minimumFractionDigits: 2, maximumFractionDigits: 2});
const fmtNum = (n) => Number(n || 0).toLocaleString();
const platformPill = (p) => `<span class="pill ${p === 'tiktok' ? 'tt' : p === 'youtube' ? 'yt' : p === 'instagram' ? 'ig' : ''}">${p || '?'}</span>`;

// ---------------------- alerts ----------------------
(function alerts() {
  const box = document.getElementById('alerts');
  if (DATA.summary.api_balance_warning) {
    box.innerHTML += `<div class="alert">⚠️ Anthropic API balance is $${DATA.summary.api_balance_usd?.toFixed?.(2) || '?'} — top up soon.</div>`;
  }
  // Best-performing clip alert (>10x median)
  const top = DATA.top_posts[0];
  if (top && top.views >= 50000) {
    box.innerHTML += `<div class="alert success">🚀 One of your clips just crossed ${fmtNum(top.views)} views — <a href="${top.url}" target="_blank" style="color: #c6f0d4;">${top.title}</a></div>`;
  }
})();

// ---------------------- header ----------------------
document.getElementById('gen-at').textContent = "updated " + new Date(DATA.generated_at).toLocaleString();

// ---------------------- tabs ----------------------
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('tab-' + t.dataset.tab).classList.add('active');
}));

// ---------------------- OVERVIEW ----------------------
(function overview() {
  const s = DATA.summary;
  document.getElementById('kpi-revenue').textContent = fmtZAR(s.total_revenue_zar);
  document.getElementById('kpi-profit').textContent = fmtZAR(s.total_revenue_zar - s.claude_pro_monthly_zar);
  document.getElementById('pro-cost').textContent = s.claude_pro_monthly_zar;
  document.getElementById('pro-cost2').textContent = s.claude_pro_monthly_zar.toFixed(2);
  document.getElementById('kpi-balance').textContent = s.api_balance_usd != null ? ("$" + s.api_balance_usd.toFixed(2)) : "—";

  new Chart(document.getElementById('chart-revenue-7d'), {
    type: 'bar',
    data: {
      labels: DATA.revenue_7d.labels.map(d => d.slice(5)),
      datasets: [{ label: 'Revenue (R)', data: DATA.revenue_7d.values_zar, backgroundColor: '#ff7a28' }]
    },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#9ea3c8' } }, x: { ticks: { color: '#9ea3c8' } } } }
  });
  const sum7 = DATA.revenue_7d.values_zar.reduce((a, b) => a + b, 0);
  document.getElementById('desc-revenue-7d').innerHTML =
    `Last 7 days: <b>${fmtZAR(sum7)}</b> in estimated revenue from cached view counts.`;

  const tbl = document.getElementById('tbl-top-posts');
  if (!DATA.top_posts.length) {
    tbl.innerHTML = `<tr><td colspan=4 class="empty">No posts yet</td></tr>`;
  } else {
    tbl.innerHTML = DATA.top_posts.map(p => `
      <tr>
        <td>${p.campaign || '—'}</td>
        <td>${platformPill(p.platform)}</td>
        <td>${fmtNum(p.views)}</td>
        <td>${fmtZAR(p.earnings_zar)} ${p.url ? `· <a href="${p.url}" target="_blank">open</a>` : ''}</td>
      </tr>
    `).join('');
  }
})();

// ---------------------- REVENUE ----------------------
let revenueChart;
function drawRevenueSeries(period) {
  const ctx = document.getElementById('chart-revenue-series');
  const labels = DATA.revenue_7d.labels;
  const values = DATA.revenue_7d.values_zar;
  if (revenueChart) revenueChart.destroy();
  revenueChart = new Chart(ctx, {
    type: 'line',
    data: { labels: labels.map(d => d.slice(5)), datasets: [{ label: 'Revenue (R)', data: values, borderColor: '#ff7a28', backgroundColor: 'rgba(255,122,40,0.2)', fill: true, tension: 0.3 }] },
    options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#9ea3c8' } }, x: { ticks: { color: '#9ea3c8' } } } }
  });
  const total = values.reduce((a, b) => a + b, 0);
  document.getElementById('desc-revenue-series').innerHTML =
    `<b>${period[0].toUpperCase() + period.slice(1)}</b> view: total ${fmtZAR(total)} estimated. ` +
    `Weekly / monthly aggregation will fill in once data accumulates beyond 7 days.`;
}
drawRevenueSeries('daily');
document.querySelectorAll('#period-toggle button').forEach(b => b.addEventListener('click', () => {
  document.querySelectorAll('#period-toggle button').forEach(x => x.classList.remove('active'));
  b.classList.add('active');
  drawRevenueSeries(b.dataset.period);
}));

(function platforms() {
  const labels = Object.keys(DATA.platforms);
  const values = labels.map(p => DATA.platforms[p].earnings_zar);
  if (!labels.length) {
    document.getElementById('chart-platform-revenue').replaceWith(Object.assign(document.createElement('div'), {className: 'empty', textContent: 'No platform data yet'}));
  } else {
    new Chart(document.getElementById('chart-platform-revenue'), {
      type: 'doughnut',
      data: { labels, datasets: [{ data: values, backgroundColor: ['#ff7a28', '#5b8def', '#36c98c', '#d8b4fe'] }] },
      options: { plugins: { legend: { position: 'bottom', labels: { color: '#9ea3c8' } } } }
    });
  }
  const desc = labels.map(p => `<b>${p}</b>: ${DATA.platforms[p].posts} posts, ${fmtNum(DATA.platforms[p].views)} views, ${fmtZAR(DATA.platforms[p].earnings_zar)}`).join(' · ');
  document.getElementById('desc-platforms').innerHTML = desc || 'No data yet.';
})();

(function perCampaign() {
  const tbl = document.getElementById('tbl-per-campaign');
  if (!DATA.per_campaign.length) {
    tbl.innerHTML = `<tr><td colspan=4 class="empty">No campaigns with posts yet</td></tr>`;
    return;
  }
  tbl.innerHTML = DATA.per_campaign
    .sort((a, b) => b.earnings_zar - a.earnings_zar)
    .map(c => `
      <tr>
        <td>${c.title} <span class="pill">#${c.campaign_id}</span></td>
        <td>${c.posts}</td>
        <td>${fmtNum(c.views)}</td>
        <td>${fmtZAR(c.earnings_zar)}</td>
      </tr>`).join('');
})();

// ---------------------- SYSTEM ----------------------
(function system() {
  const s = DATA.system;
  document.getElementById('sys-24h').textContent = s.clips_24h;
  document.getElementById('sys-7d').textContent = s.clips_7d;
  document.getElementById('sys-30d').textContent = s.clips_30d;
  document.getElementById('sys-camp').textContent = DATA.summary.total_campaigns_active;
  document.getElementById('sys-approval').textContent = s.approval_rate_pct + "%";
  document.getElementById('sys-post').textContent = s.post_success_rate_pct + "%";
  document.getElementById('sys-clips-total').textContent = DATA.summary.total_clips;
  document.getElementById('sys-posts-total').textContent = DATA.summary.total_posts;
  document.getElementById('sys-subs-total').textContent = DATA.summary.total_submissions;

  const labels = Object.keys(DATA.platforms);
  const values = labels.map(p => DATA.platforms[p].posts);
  if (labels.length) {
    new Chart(document.getElementById('chart-platform-posts'), {
      type: 'bar',
      data: { labels, datasets: [{ label: 'Posts', data: values, backgroundColor: '#5b8def' }] },
      options: { plugins: { legend: { display: false } }, scales: { y: { ticks: { color: '#9ea3c8' } }, x: { ticks: { color: '#9ea3c8' } } } }
    });
    document.getElementById('desc-system-platforms').innerHTML =
      labels.map(p => `<b>${p}</b>: ${DATA.platforms[p].posts}`).join(' · ');
  }
})();

// ---------------------- COMPETITORS ----------------------
(function competitors() {
  const tbl = document.getElementById('tbl-competitors');
  if (!DATA.competitors.length) {
    tbl.innerHTML = `<tr><td colspan=4 class="empty">No top-performer data yet. Run <code>auto_extract_briefs</code> + <code>scrape_top_performers</code> per campaign to populate.</td></tr>`;
    document.getElementById('desc-competitors').textContent = '';
    return;
  }
  tbl.innerHTML = DATA.competitors.map(c => `
    <tr>
      <td>${c.campaign_title || c.campaign_id} <span class="pill">#${c.campaign_id}</span></td>
      <td>${c.title || c.hook || c.notes || '—'}</td>
      <td>${c.views || '?'}</td>
      <td>${platformPill(c.platform || '')}</td>
    </tr>`).join('');
  document.getElementById('desc-competitors').innerHTML =
    `<b>${DATA.competitors.length}</b> top-performer clips across <b>${DATA.summary.total_campaigns_active}</b> campaigns. Use these as style signal for our scorer — clip in the energy / angle that's already winning.`;
})();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sys.exit(main())
