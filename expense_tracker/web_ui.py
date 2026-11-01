"""Standalone web dashboard + chat for the Expense Tracker.

Run with:  uv run expense-web   (http://localhost:8080)
"""

from __future__ import annotations

import base64
import os
import secrets
from datetime import datetime

import openpyxl
import uvicorn
from dotenv import load_dotenv

load_dotenv()  # loads GOOGLE_API_KEY (and other vars) from .env before ADK initialises

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai.types import Content, Part
from openpyxl.styles import Alignment, Font, PatternFill
from starlette.middleware.base import BaseHTTPMiddleware

from .agent import root_agent
from .tools import EXPORTS_DIR, _get_connection, _month_bounds

app = FastAPI(title="Expense Tracker")

# ---------------------------------------------------------------------------
# Optional password protection — set APP_PASSWORD env var to enable
# ---------------------------------------------------------------------------
_APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

if _APP_PASSWORD:
    class _BasicAuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Basic "):
                try:
                    decoded = base64.b64decode(auth[7:]).decode()
                    _, pwd = decoded.split(":", 1)
                    if secrets.compare_digest(pwd.encode(), _APP_PASSWORD.encode()):
                        return await call_next(request)
                except Exception:
                    pass
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="Expense Tracker"'},
            )

    app.add_middleware(_BasicAuthMiddleware)

# ---------------------------------------------------------------------------
# ADK runner
# ---------------------------------------------------------------------------
_session_svc = InMemorySessionService()
_runner = Runner(
    agent=root_agent,
    app_name="expense_tracker",
    session_service=_session_svc,
)
_APP = "expense_tracker"
_USER = "web_user"


# ---------------------------------------------------------------------------
# Chat API
# ---------------------------------------------------------------------------

@app.post("/api/chat")
async def api_chat(request: Request) -> JSONResponse:
    body = await request.json()
    message = (body.get("message") or "").strip()
    session_id = body.get("session_id") or "default"

    if not message:
        return JSONResponse({"error": "empty message"}, status_code=400)

    # Session management — handle both sync and async ADK APIs
    try:
        result = _session_svc.get_session(
            app_name=_APP, user_id=_USER, session_id=session_id
        )
        import inspect
        if inspect.isawaitable(result):
            existing = await result
        else:
            existing = result

        if existing is None:
            result2 = _session_svc.create_session(
                app_name=_APP, user_id=_USER, session_id=session_id
            )
            if inspect.isawaitable(result2):
                await result2
    except Exception:
        pass  # runner will create the session automatically if needed

    user_content = Content(role="user", parts=[Part(text=message)])
    response_text = ""

    try:
        async for event in _runner.run_async(
            user_id=_USER, session_id=session_id, new_message=user_content,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    if hasattr(part, "text") and part.text:
                        response_text += part.text
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    return JSONResponse({
        "response": response_text or "No response — please try again.",
        "session_id": session_id,
    })


# ---------------------------------------------------------------------------
# Summary API  (takes start + end ISO dates)
# ---------------------------------------------------------------------------

@app.get("/api/summary")
def api_summary(start: str, end: str) -> JSONResponse:
    conn = _get_connection()

    expenses = conn.execute(
        """SELECT id, date, amount, currency, category, description
           FROM expenses WHERE date BETWEEN ? AND ?
           ORDER BY date DESC, id DESC""",
        (start, end),
    ).fetchall()

    breakdown = conn.execute(
        """SELECT category, ROUND(SUM(amount), 2) AS total
           FROM expenses WHERE date BETWEEN ? AND ?
           GROUP BY category ORDER BY total DESC""",
        (start, end),
    ).fetchall()

    conn.close()

    bl = [dict(r) for r in breakdown]
    grand_total = round(sum(r["total"] for r in bl), 2)

    return JSONResponse({
        "start": start, "end": end,
        "expenses": [dict(r) for r in expenses],
        "breakdown": bl,
        "grand_total": grand_total,
        "top_category": bl[0]["category"] if bl else None,
    })


# ---------------------------------------------------------------------------
# Export API  (generates .xlsx, streams as download)
# ---------------------------------------------------------------------------

@app.get("/api/export")
def api_export(start: str, end: str) -> FileResponse:
    conn = _get_connection()
    rows = conn.execute(
        """SELECT date, amount, currency, category, description
           FROM expenses WHERE date BETWEEN ? AND ?
           ORDER BY date ASC, id ASC""",
        (start, end),
    ).fetchall()
    conn.close()

    os.makedirs(EXPORTS_DIR, exist_ok=True)
    filename = f"expenses_{start}_to_{end}.xlsx"
    path = os.path.join(EXPORTS_DIR, filename)

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Expenses"

    hf    = Font(bold=True, color="FFFFFF")
    hfill = PatternFill("solid", fgColor="2E75B6")
    cols  = [("Day", 12), ("Date", 14), ("Category", 20), ("Item", 35), ("Cost in euros", 16)]

    for i, (h, w) in enumerate(cols, start=1):
        cell = ws.cell(row=1, column=i, value=h)
        cell.font = hf; cell.fill = hfill
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[cell.column_letter].width = w

    for row in rows:
        d = datetime.strptime(row["date"], "%Y-%m-%d").date()
        ws.append([d.strftime("%A"), d.strftime("%d %b %Y"),
                   row["category"], row["description"], row["amount"]])

    for ri in range(2, ws.max_row + 1):
        ws.cell(row=ri, column=5).number_format = '€#,##0.00'

    ws.freeze_panes = "A2"
    wb.save(path)

    return FileResponse(
        path=path,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    return HTMLResponse(content=_HTML)


_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Expense Tracker</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #f0f4f8; color: #1a1a2e;
      min-height: 100vh; padding-bottom: 88px;
    }

    /* ── Header ── */
    .header {
      background: #2E75B6; color: #fff;
      padding: 14px 20px 0; text-align: center;
      position: sticky; top: 0; z-index: 50;
      box-shadow: 0 2px 10px rgba(0,0,0,.18);
    }
    .header-top { display: flex; align-items: center; justify-content: center; padding-bottom: 12px; }
    .header-top h1 { font-size: 17px; font-weight: 700; }

    /* Period toggle tabs */
    .period-tabs {
      display: flex; border-top: 1px solid rgba(255,255,255,.2);
    }
    .ptab {
      flex: 1; padding: 10px; font-size: 13px; font-weight: 600;
      background: none; border: none; color: rgba(255,255,255,.65);
      cursor: pointer; transition: all .15s; letter-spacing: .3px;
    }
    .ptab.active { color: #fff; border-bottom: 2px solid #fff; }

    /* Period nav */
    .period-nav {
      display: flex; align-items: center; justify-content: center; gap: 14px;
      padding: 10px 20px 14px;
    }
    .nav-btn {
      background: rgba(255,255,255,.2); border: none; color: #fff;
      width: 32px; height: 32px; border-radius: 50%; cursor: pointer;
      font-size: 16px; display: flex; align-items: center; justify-content: center;
      transition: background .15s; flex-shrink: 0;
    }
    .nav-btn:hover { background: rgba(255,255,255,.35); }
    #period-label { font-size: 18px; font-weight: 800; min-width: 200px; text-align: center; }

    /* ── Content ── */
    .content { padding: 16px; max-width: 620px; margin: 0 auto; }
    .card {
      background: #fff; border-radius: 18px; padding: 20px;
      margin-bottom: 14px; box-shadow: 0 1px 6px rgba(0,0,0,.07);
    }
    .card-title { font-size: 15px; font-weight: 700; margin-bottom: 14px; }

    /* ── Summary card ── */
    .summary-card { text-align: center; padding: 22px 20px 20px; }
    .total-amount { font-size: 50px; font-weight: 900; color: #2E75B6; letter-spacing: -2px; }
    .summary-meta { color: #888; font-size: 13px; margin-top: 5px; }
    .top-badge {
      display: inline-block; margin-top: 10px;
      padding: 5px 14px; background: #e8f2fc; color: #2E75B6;
      border-radius: 20px; font-size: 12px; font-weight: 600;
    }

    /* ── Chart + breakdown ── */
    .chart-section {
      display: grid; grid-template-columns: 148px 1fr; gap: 22px; align-items: center;
    }
    .chart-wrapper { position: relative; width: 148px; height: 148px; }
    .chart-center {
      position: absolute; top: 50%; left: 50%;
      transform: translate(-50%,-50%); text-align: center; pointer-events: none;
    }
    .chart-center-num { font-size: 26px; font-weight: 900; color: #1a1a2e; }
    .chart-center-lbl { font-size: 10px; color: #999; margin-top: 2px; }
    .cat-list { display: flex; flex-direction: column; gap: 10px; }
    .cat-item { display: flex; flex-direction: column; gap: 4px; }
    .cat-row { display: flex; justify-content: space-between; align-items: center; }
    .cat-name { display: flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 500; }
    .cat-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
    .cat-amount { font-size: 13px; font-weight: 700; }
    .cat-bar { height: 4px; border-radius: 2px; background: #eee; overflow: hidden; }
    .cat-fill { height: 100%; border-radius: 2px; transition: width .5s ease; }

    /* ── Transactions ── */
    .tx-list { display: flex; flex-direction: column; }
    .tx-row {
      display: flex; justify-content: space-between; align-items: center;
      padding: 11px 0; border-bottom: 1px solid #f2f2f2;
    }
    .tx-row:last-child { border-bottom: none; }
    .tx-left { display: flex; flex-direction: column; gap: 3px; }
    .tx-desc { font-size: 14px; font-weight: 500; }
    .tx-meta { font-size: 12px; color: #999; }
    .tx-amt { font-size: 15px; font-weight: 800; color: #2E75B6; white-space: nowrap; }
    .empty { text-align: center; color: #bbb; padding: 36px 0; font-size: 15px; }

    /* ── CTA bar ── */
    .cta-bar {
      position: fixed; bottom: 0; left: 0; right: 0;
      background: #fff; padding: 10px 16px;
      display: grid; grid-template-columns: 1.2fr 1fr 1fr; gap: 10px;
      box-shadow: 0 -2px 14px rgba(0,0,0,.1); z-index: 50;
    }
    .cta {
      padding: 13px 8px; border: none; border-radius: 13px;
      font-size: 13px; font-weight: 700; cursor: pointer;
      text-decoration: none; text-align: center;
      display: flex; align-items: center; justify-content: center; gap: 6px;
      transition: opacity .15s, transform .1s;
    }
    .cta:active { transform: scale(.97); }
    .cta-chat     { background: #2E75B6; color: #fff; }
    .cta-download { background: #f0f4f8; color: #2E75B6; border: 1.5px solid #c8d9ec; }
    .cta-whatsapp { background: #25D366; color: #fff; }
    .cta.off { opacity: .3; pointer-events: none; }

    /* ── Chat panel ── */
    .chat-overlay {
      position: fixed; inset: 0; z-index: 200;
      display: flex; flex-direction: column; background: #f0f4f8;
      transform: translateY(100%);
      transition: transform .3s cubic-bezier(.4,0,.2,1);
    }
    .chat-overlay.open { transform: translateY(0); }

    .chat-header {
      background: #2E75B6; color: #fff;
      padding: 16px 20px;
      display: flex; align-items: center; justify-content: space-between;
      flex-shrink: 0;
    }
    .chat-hdr-title { font-size: 17px; font-weight: 700; }
    .chat-hdr-sub   { font-size: 12px; opacity: .75; margin-top: 2px; }
    .chat-close {
      background: rgba(255,255,255,.2); border: none; color: #fff;
      width: 34px; height: 34px; border-radius: 50%; cursor: pointer; font-size: 18px;
      display: flex; align-items: center; justify-content: center;
    }
    .chat-close:hover { background: rgba(255,255,255,.35); }

    .chat-messages {
      flex: 1; overflow-y: auto; padding: 16px;
      display: flex; flex-direction: column; gap: 12px;
    }
    .msg { display: flex; flex-direction: column; max-width: 85%; }
    .msg.user  { align-self: flex-end;   align-items: flex-end; }
    .msg.agent { align-self: flex-start; align-items: flex-start; }
    .bubble {
      padding: 11px 15px; border-radius: 18px;
      font-size: 14px; line-height: 1.55; white-space: pre-wrap; word-break: break-word;
    }
    .msg.user  .bubble { background: #2E75B6; color: #fff; border-bottom-right-radius: 4px; }
    .msg.agent .bubble { background: #fff; color: #1a1a2e; border-bottom-left-radius: 4px;
                         box-shadow: 0 1px 4px rgba(0,0,0,.08); }
    .msg-time { font-size: 11px; color: #aaa; margin-top: 4px; padding: 0 4px; }

    .dots { display: flex; align-items: center; gap: 4px; padding: 14px 16px; }
    .dots span {
      width: 7px; height: 7px; border-radius: 50%; background: #bbb;
      animation: bounce .9s infinite;
    }
    .dots span:nth-child(2) { animation-delay: .15s; }
    .dots span:nth-child(3) { animation-delay: .3s; }
    @keyframes bounce {
      0%,60%,100% { transform: translateY(0); }
      30%          { transform: translateY(-6px); }
    }

    .chat-input-row {
      padding: 12px 16px; background: #fff;
      display: flex; gap: 10px; align-items: center;
      border-top: 1px solid #eee; flex-shrink: 0;
    }
    #chat-input {
      flex: 1; padding: 12px 16px;
      border: 1.5px solid #dde1e7; border-radius: 24px;
      font-size: 14px; outline: none; font-family: inherit;
      transition: border-color .15s;
    }
    #chat-input:focus { border-color: #2E75B6; }
    #chat-send {
      width: 44px; height: 44px; border: none; border-radius: 50%;
      background: #2E75B6; color: #fff; cursor: pointer; font-size: 20px;
      display: flex; align-items: center; justify-content: center; flex-shrink: 0;
    }
    #chat-send:hover { background: #2563a8; }
    #chat-send:disabled { background: #ccc; cursor: default; }

    @media (max-width: 400px) {
      .chart-section { grid-template-columns: 1fr; }
      .chart-wrapper { margin: 0 auto 14px; }
      .total-amount  { font-size: 40px; }
    }
  </style>
</head>
<body>

<!-- ── Dashboard ────────────────────────────────────────────── -->
<header class="header">
  <div class="header-top"><h1>💰 Expense Tracker</h1></div>
  <div class="period-tabs">
    <button class="ptab active" id="tab-week"  onclick="setMode('week')">This Week</button>
    <button class="ptab"        id="tab-month" onclick="setMode('month')">This Month</button>
  </div>
  <div class="period-nav">
    <button class="nav-btn" id="prev">&#8592;</button>
    <span id="period-label">…</span>
    <button class="nav-btn" id="next">&#8594;</button>
  </div>
</header>

<div class="content">
  <div class="card summary-card">
    <div class="total-amount" id="total">€0.00</div>
    <div class="summary-meta" id="meta">No expenses</div>
    <div id="top-wrap" style="display:none">
      <span class="top-badge" id="top-text"></span>
    </div>
  </div>

  <div class="card">
    <div class="chart-section">
      <div class="chart-wrapper">
        <canvas id="chart"></canvas>
        <div class="chart-center">
          <div class="chart-center-num" id="cat-count">0</div>
          <div class="chart-center-lbl">categories</div>
        </div>
      </div>
      <div class="cat-list" id="cat-list"></div>
    </div>
  </div>

  <div class="card">
    <div class="card-title">Transactions</div>
    <div class="tx-list" id="tx-list"></div>
  </div>
</div>

<!-- ── CTA bar ──────────────────────────────────────────────── -->
<div class="cta-bar">
  <button class="cta cta-chat" id="btn-chat">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
    Add Expense
  </button>
  <a class="cta cta-download off" id="btn-dl" href="#">
    <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
    Download
  </a>
  <a class="cta cta-whatsapp off" id="btn-wa" href="#" target="_blank" rel="noopener">
    <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor">
      <path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413z"/>
    </svg>
    Share
  </a>
</div>

<!-- ── Chat panel ────────────────────────────────────────────── -->
<div class="chat-overlay" id="chat-panel">
  <div class="chat-header">
    <div>
      <div class="chat-hdr-title">💬 Add & Manage Expenses</div>
      <div class="chat-hdr-sub">Type naturally — "I spent €12 on lunch today"</div>
    </div>
    <button class="chat-close" id="chat-close">✕</button>
  </div>
  <div class="chat-messages" id="chat-messages"></div>
  <div class="chat-input-row">
    <input id="chat-input" type="text"
           placeholder="e.g. I spent €20 on transport yesterday"
           autocomplete="off">
    <button id="chat-send">&#8593;</button>
  </div>
</div>

<script>
// ── Palette & constants ──────────────────────────────────────
const COLORS  = ['#2E75B6','#70AD47','#FF7043','#FFC107','#AB47BC',
                 '#26A69A','#EC407A','#5C6BC0','#29B6F6','#D4E157',
                 '#FF8A65','#78909C','#26C6DA','#FFCA28','#8D6E63','#EF5350'];
const MEDALS  = ['🥇','🥈','🥉'];
const MONTHS  = ['January','February','March','April','May','June',
                 'July','August','September','October','November','December'];

// ── Period state ─────────────────────────────────────────────
let mode  = 'week';          // 'week' | 'month'
let anchor = new Date();     // reference date for current week/month
let donut  = null;
let curData = null;

function toISO(d) {
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
}

function weekBounds(ref) {
  const d = new Date(ref);
  const dow = d.getDay();               // 0=Sun
  const diff = dow === 0 ? -6 : 1-dow;  // shift to Monday
  const mon = new Date(d); mon.setDate(d.getDate() + diff);
  const sun = new Date(mon); sun.setDate(mon.getDate() + 6);
  const label = mon.toLocaleDateString('en-IE',{day:'numeric',month:'short'}) +
                ' – ' +
                sun.toLocaleDateString('en-IE',{day:'numeric',month:'short',year:'numeric'});
  return { start: toISO(mon), end: toISO(sun), label };
}

function monthBounds(ref) {
  const y = ref.getFullYear(), m = ref.getMonth();
  const first = new Date(y, m, 1);
  const last  = new Date(y, m+1, 0);
  return {
    start: toISO(first), end: toISO(last),
    label: MONTHS[m] + ' ' + y,
  };
}

function bounds() {
  return mode === 'week' ? weekBounds(anchor) : monthBounds(anchor);
}

function setMode(m) {
  mode = m;
  document.getElementById('tab-week') .classList.toggle('active', m==='week');
  document.getElementById('tab-month').classList.toggle('active', m==='month');
  loadDash();
}

document.getElementById('prev').onclick = () => {
  if (mode === 'week')  anchor.setDate(anchor.getDate() - 7);
  else                  anchor.setMonth(anchor.getMonth() - 1);
  loadDash();
};
document.getElementById('next').onclick = () => {
  if (mode === 'week')  anchor.setDate(anchor.getDate() + 7);
  else                  anchor.setMonth(anchor.getMonth() + 1);
  loadDash();
};

// ── Dashboard data ───────────────────────────────────────────
function fmt(n) {
  return '€' + Number(n).toLocaleString('en-IE',{minimumFractionDigits:2,maximumFractionDigits:2});
}

async function loadDash() {
  const {start, end, label} = bounds();
  document.getElementById('period-label').textContent = label;
  const r = await fetch(`/api/summary?start=${start}&end=${end}`);
  curData = await r.json();
  curData.start = start; curData.end = end; curData.label = label;
  render(curData);
}

function render(d) {
  document.getElementById('total').textContent = fmt(d.grand_total);
  const c = d.expenses.length;
  document.getElementById('meta').textContent =
    c === 0 ? 'No expenses' : c + ' expense' + (c!==1?'s':'');
  const tw = document.getElementById('top-wrap');
  if (d.top_category) {
    document.getElementById('top-text').textContent = '🏆 Top: ' + d.top_category;
    tw.style.display = 'block';
  } else { tw.style.display = 'none'; }
  renderChart(d); renderBreakdown(d); renderTx(d); renderCTAs(d);
}

function renderChart(d) {
  const ctx = document.getElementById('chart').getContext('2d');
  const labels = d.breakdown.map(r=>r.category);
  const values = d.breakdown.map(r=>r.total);
  const colors = labels.map((_,i)=>COLORS[i%COLORS.length]);
  if (donut) donut.destroy();
  document.getElementById('cat-count').textContent = labels.length||'0';
  if (!labels.length) {
    donut = new Chart(ctx,{type:'doughnut',
      data:{datasets:[{data:[1],backgroundColor:['#eee'],borderWidth:0}]},
      options:{cutout:'72%',plugins:{legend:{display:false},tooltip:{enabled:false}}}});
    return;
  }
  donut = new Chart(ctx,{type:'doughnut',
    data:{labels,datasets:[{data:values,backgroundColor:colors,borderWidth:2,borderColor:'#fff'}]},
    options:{cutout:'72%',plugins:{legend:{display:false},
      tooltip:{callbacks:{label:c=>` ${fmt(c.parsed)} (${Math.round(c.parsed/d.grand_total*100)}%)`}}}}
  });
}

function renderBreakdown(d) {
  const el = document.getElementById('cat-list');
  if (!d.breakdown.length){el.innerHTML='<div style="color:#bbb;font-size:13px">No data yet</div>';return;}
  el.innerHTML = d.breakdown.map((row,i)=>{
    const pct = d.grand_total>0?Math.round(row.total/d.grand_total*100):0;
    const col = COLORS[i%COLORS.length];
    return `<div class="cat-item">
      <div class="cat-row">
        <div class="cat-name"><span class="cat-dot" style="background:${col}"></span>${row.category}</div>
        <div class="cat-amount">${fmt(row.total)}</div>
      </div>
      <div class="cat-bar"><div class="cat-fill" style="width:${pct}%;background:${col}"></div></div>
    </div>`;
  }).join('');
}

function renderTx(d) {
  const el = document.getElementById('tx-list');
  if (!d.expenses.length){el.innerHTML='<div class="empty">No expenses 🎉</div>';return;}
  el.innerHTML = d.expenses.map(e=>{
    const dt = new Date(e.date+'T00:00:00');
    const ds = dt.toLocaleDateString('en-IE',{day:'numeric',month:'short'});
    const dw = dt.toLocaleDateString('en-IE',{weekday:'short'});
    return `<div class="tx-row">
      <div class="tx-left">
        <div class="tx-desc">${e.description}</div>
        <div class="tx-meta">${e.category} · ${dw} ${ds}</div>
      </div>
      <div class="tx-amt">${fmt(e.amount)}</div>
    </div>`;
  }).join('');
}

function renderCTAs(d) {
  const dl = document.getElementById('btn-dl');
  const wa = document.getElementById('btn-wa');
  if (!d.expenses.length){dl.classList.add('off');wa.classList.add('off');return;}
  dl.classList.remove('off');
  dl.href = `/api/export?start=${d.start}&end=${d.end}`;
  const lines = [`📊 ${d.label} — ${fmt(d.grand_total)} total`,''];
  d.breakdown.forEach((r,i)=>lines.push(`${i<3?MEDALS[i]:'   '} ${r.category}  ${fmt(r.total)}`));
  if (d.top_category) lines.push('',`Top spend: ${d.top_category}`);
  wa.classList.remove('off');
  wa.href = 'https://wa.me/?text=' + encodeURIComponent(lines.join('\n'));
}

// ── Chat ─────────────────────────────────────────────────────
let sessionId = sessionStorage.getItem('cid');
if (!sessionId) {
  sessionId = 'w' + Math.random().toString(36).slice(2,11);
  sessionStorage.setItem('cid', sessionId);
}
let busy = false;

document.getElementById('btn-chat')  .onclick = openChat;
document.getElementById('chat-close').onclick = closeChat;
document.getElementById('chat-send') .onclick = send;
document.getElementById('chat-input').addEventListener('keydown', e => {
  if (e.key==='Enter' && !e.shiftKey){e.preventDefault();send();}
});

function openChat() {
  document.getElementById('chat-panel').classList.add('open');
  document.getElementById('chat-input').focus();
  const msgs = document.getElementById('chat-messages');
  if (!msgs.children.length) {
    addMsg('agent',
      "👋 Hi! Tell me about an expense and I'll log it for you.\n\n" +
      "Examples:\n" +
      "• \"I spent €12 on lunch today\"\n" +
      "• \"€45 for groceries yesterday\"\n" +
      "• \"How much did I spend this week?\"\n" +
      "• \"Show my recurring expenses\"");
  }
}
function closeChat() {
  document.getElementById('chat-panel').classList.remove('open');
}

function ts() {
  return new Date().toLocaleTimeString('en-IE',{hour:'2-digit',minute:'2-digit'});
}
function esc(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}
function addMsg(role, text) {
  const msgs = document.getElementById('chat-messages');
  const d = document.createElement('div');
  d.className = 'msg ' + role;
  d.innerHTML = `<div class="bubble">${esc(text)}</div><div class="msg-time">${ts()}</div>`;
  msgs.appendChild(d);
  msgs.scrollTop = msgs.scrollHeight;
}
function showDots() {
  const msgs = document.getElementById('chat-messages');
  const d = document.createElement('div');
  d.className='msg agent'; d.id='dots';
  d.innerHTML='<div class="bubble dots"><span></span><span></span><span></span></div>';
  msgs.appendChild(d); msgs.scrollTop=msgs.scrollHeight;
}
function hideDots() { const e=document.getElementById('dots'); if(e)e.remove(); }

async function send() {
  if (busy) return;
  const inp = document.getElementById('chat-input');
  const txt = inp.value.trim();
  if (!txt) return;
  inp.value = '';
  addMsg('user', txt);
  busy = true;
  document.getElementById('chat-send').disabled = true;
  showDots();
  try {
    const r = await fetch('/api/chat',{
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({message:txt, session_id:sessionId}),
    });
    const data = await r.json();
    hideDots();
    addMsg('agent', data.error || data.response);
    loadDash(); // refresh numbers after any change
  } catch {
    hideDots();
    addMsg('agent','Something went wrong — please try again.');
  }
  busy=false;
  document.getElementById('chat-send').disabled=false;
  document.getElementById('chat-input').focus();
}

// ── Boot ─────────────────────────────────────────────────────
loadDash();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import glob

    port = int(os.environ.get("PORT", 8080))
    is_prod = os.environ.get("RENDER") or os.environ.get("PRODUCTION")

    ssl_kwargs: dict = {}
    if not is_prod:
        # Local dev: use mkcert certificate if available
        certs = sorted(glob.glob("localhost*.pem"))
        key  = next((f for f in certs if "key" in f), None)
        cert = next((f for f in certs if "key" not in f), None)
        if key and cert:
            ssl_kwargs = {"ssl_keyfile": key, "ssl_certfile": cert}
            print(f"🔒 Starting on https://localhost:{port}")
        else:
            print(f"⚠️  Starting on http://localhost:{port}  (no cert — run mkcert to enable HTTPS)")

    if is_prod:
        print(f"🚀 Production mode on port {port}")

    uvicorn.run(
        "expense_tracker.web_ui:app",
        host="0.0.0.0",
        port=port,
        reload=not bool(is_prod),
        **ssl_kwargs,
    )


if __name__ == "__main__":
    main()
