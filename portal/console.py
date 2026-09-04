"""Local test console for amd-portal-mcp flows.

Aaron-only, localhost-only FastAPI app (127.0.0.1:8811). Runs the same
flow code as the MCP server in-process, with AMD_PORTAL_CAPTURE=1 so a
screenshot is saved per checkpoint under runtime/console/<run_id>/.

This console IS allowed to display PHI (screenshots, scraped field
values) to Aaron in his browser; that is its purpose. It must never
write PHI anywhere except runtime/ (gitignored). The verdict log
(runtime/console/verdicts.jsonl) stores a sha256 short hash of the
patient search string, never the string itself, and per-field presence
booleans plus a verdict, never field values.

Not an MCP surface: the MCP server (server.py) stays unchanged apart
from the additive result keys from the runner.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from . import browser
from .flows import insurance
from .flows._runner import Checkpoints, run_flow
from .flows.session import portal_session_status as _session_status_flow

log = logging.getLogger("amd_portal_mcp.console")

_REPO_ROOT = Path(__file__).resolve().parents[2]
CONSOLE_DIR = _REPO_ROOT / "runtime" / "console"
VERDICTS_PATH = CONSOLE_DIR / "verdicts.jsonl"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

app = FastAPI(title="amd-portal-mcp console")

_flow_lock = asyncio.Lock()
# run_id -> {"state": "running"|"done", "cp": Checkpoints,
#            "result": dict|None, "patient": str}
_runs: dict[str, dict] = {}
_run_order: list[str] = []


class RunRequest(BaseModel):
    patient: str
    insurance_index: int = 1


class VerdictRequest(BaseModel):
    run_id: str
    patient: str
    field: str
    value_present: bool
    verdict: str  # match | mismatch | cant-tell


def _patient_ref(patient: str) -> str:
    return hashlib.sha256(patient.strip().lower().encode()).hexdigest()[:12]


def _serialize_run(rec: dict) -> dict:
    return {
        "state": rec["state"],
        "checkpoints": rec["cp"].as_dict(),
        "run_id": rec["cp"].run_id,
        "result": rec["result"],
        "kind": rec.get("kind", "insurance"),
    }


async def _do_run(rec: dict, patient: str, insurance_index: int) -> None:
    async with _flow_lock:
        try:
            page = await browser.get_page()
            result = await run_flow(
                "get_insurance_details",
                insurance.get_insurance_details,
                page,
                patient=patient,
                insurance_index=insurance_index,
                checkpoints=rec["cp"],
            )
        except Exception as exc:  # defensive; run_flow shouldn't raise
            result = {
                "ok": False,
                "flow": "get_insurance_details",
                "error": type(exc).__name__,
                "message": type(exc).__name__,
            }
        rec["result"] = result
        rec["state"] = "done"


@app.post("/api/run")
async def start_run(req: RunRequest):
    if _flow_lock.locked():
        return JSONResponse(
            {"busy": True, "message": "A flow is already running; "
             "wait for it to finish."},
            status_code=409,
        )
    cp = Checkpoints(capture=True)
    rec = {"state": "running", "cp": cp, "result": None,
           "patient": req.patient, "kind": "insurance"}
    _runs[cp.run_id] = rec
    _run_order.append(cp.run_id)
    asyncio.create_task(_do_run(rec, req.patient, req.insurance_index))
    return {"run_id": cp.run_id}


@app.get("/api/run/{run_id}")
async def run_status(run_id: str):
    rec = _runs.get(run_id)
    if rec is None:
        return JSONResponse({"error": "unknown run_id"}, status_code=404)
    return _serialize_run(rec)


class BatchRequest(BaseModel):
    patients: list
    insurance_index: int = 1


@app.post("/api/batch")
async def run_batch(req: BatchRequest):
    """Batch smoke path: run get_insurance_details_batch over one warm
    session. Returns the summary and per-item PRESENCE booleans only
    (never field values), so console-driven live verification stays
    within the whitelist and never surfaces PHI in this response.
    """
    if _flow_lock.locked():
        return JSONResponse(
            {"busy": True, "message": "A flow is already running; "
             "wait for it to finish."},
            status_code=409,
        )
    async with _flow_lock:
        page = await browser.get_page()
        result = await run_flow(
            "get_insurance_details_batch",
            insurance.get_insurance_details_batch,
            page,
            patients=req.patients,
            insurance_index=req.insurance_index,
        )
    # Reduce to summary + per-item presence booleans (no field values).
    if result.get("ok") and isinstance(result.get("data"), dict):
        data = result["data"]
        items = []
        for it in data.get("results", []):
            items.append({
                "index": it.get("index"),
                "ok": it.get("ok"),
                "session_reestablished": bool(it.get("session_reestablished")),
                "field_presence": {
                    f: bool(it.get(f))
                    for f in (insurance.FIELDS + insurance.ELIGIBILITY_FIELDS)
                } if it.get("ok") else None,
                "diagnosis": it.get("diagnosis") if not it.get("ok") else None,
            })
        return {"summary": data.get("summary"), "results": items}
    return result


@app.post("/api/session-status")
async def session_status():
    if _flow_lock.locked():
        return JSONResponse(
            {"busy": True, "message": "A flow is already running; "
             "wait for it to finish."},
            status_code=409,
        )
    async with _flow_lock:
        page = await browser.get_page()
        result = await run_flow(
            "portal_session_status", _session_status_flow, page
        )
    return result


@app.post("/api/verdict")
async def post_verdict(req: VerdictRequest):
    if req.verdict not in ("match", "mismatch", "cant-tell"):
        return JSONResponse({"error": "bad verdict"}, status_code=400)
    CONSOLE_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "run_id": req.run_id,
        "patient_ref": _patient_ref(req.patient),
        "field": req.field,
        "value_present": req.value_present,
        "verdict": req.verdict,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with VERDICTS_PATH.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    return {"ok": True, "tally": _tally()}


def _tally() -> dict:
    counts = {"match": 0, "mismatch": 0, "cant-tell": 0}
    if VERDICTS_PATH.exists():
        for line in VERDICTS_PATH.read_text().splitlines():
            try:
                v = json.loads(line).get("verdict")
            except json.JSONDecodeError:
                continue
            if v in counts:
                counts[v] += 1
    judged = counts["match"] + counts["mismatch"]
    counts["total"] = judged + counts["cant-tell"]
    counts["accuracy"] = (
        round(100.0 * counts["match"] / judged, 1) if judged else None
    )
    return counts


@app.get("/api/verdicts/tally")
async def verdicts_tally():
    return _tally()


@app.get("/shots/{run_id}/{filename}")
async def get_screenshot(run_id: str, filename: str):
    if not (_SAFE_NAME.match(run_id) and _SAFE_NAME.match(filename)):
        return JSONResponse({"error": "bad path"}, status_code=400)
    path = CONSOLE_DIR / run_id / filename
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(str(path), media_type="image/png")


@app.get("/", response_class=HTMLResponse)
async def index():
    return _PAGE


_PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>amd-portal-mcp console</title>
<style>
  :root { --bg:#f6f7f9; --card:#fff; --ink:#1c2330; --mut:#6b7280;
          --line:#e3e6ea; --ok:#177245; --bad:#b3261e; --run:#8a6d00; }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
         font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI",
         Helvetica, Arial, sans-serif; }
  header { padding:14px 24px; background:var(--card);
           border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:18px; flex-wrap:wrap; }
  h1 { font-size:16px; margin:0; }
  .pill { background:var(--bg); border:1px solid var(--line);
          border-radius:999px; padding:3px 12px; color:var(--mut); }
  main { max-width:1180px; margin:0 auto; padding:20px 24px; }
  .card { background:var(--card); border:1px solid var(--line);
          border-radius:10px; padding:16px 18px; margin-bottom:18px; }
  form { display:flex; gap:10px; align-items:end; flex-wrap:wrap; }
  label { display:block; color:var(--mut); font-size:12px;
          margin-bottom:3px; }
  input { font:inherit; padding:7px 10px; border:1px solid var(--line);
          border-radius:6px; }
  button { font:inherit; padding:8px 16px; border:1px solid var(--line);
           border-radius:6px; background:var(--ink); color:#fff;
           cursor:pointer; }
  button.secondary { background:var(--card); color:var(--ink); }
  button:disabled { opacity:.5; cursor:default; }
  #msg { color:var(--bad); margin-left:8px; }
  .ladder { display:flex; flex-direction:column; gap:6px; }
  .stage { display:flex; align-items:center; gap:12px; padding:6px 8px;
           border-radius:6px; }
  .stage .name { width:180px; font-family:ui-monospace,Menlo,monospace; }
  .stage .dur { width:70px; color:var(--mut); text-align:right; }
  .stage .st { width:80px; font-weight:600; }
  .st.pass { color:var(--ok); } .st.fail { color:var(--bad); }
  .st.running { color:var(--run); } .st.pending { color:var(--mut); }
  .thumb { height:44px; border:1px solid var(--line); border-radius:4px;
           cursor:zoom-in; }
  .cols { display:grid; grid-template-columns: 1fr 1fr; gap:18px; }
  table { border-collapse:collapse; width:100%; }
  td, th { border-bottom:1px solid var(--line); padding:6px 8px;
           text-align:left; vertical-align:top; }
  th { color:var(--mut); font-weight:600; font-size:12px; }
  td.val { font-family:ui-monospace,Menlo,monospace; word-break:break-all; }
  .vbtn { padding:2px 8px; margin-right:4px; font-size:12px;
          background:var(--card); color:var(--ink);
          border:1px solid var(--line); }
  .vbtn.sel-match { background:var(--ok); color:#fff; }
  .vbtn.sel-mismatch { background:var(--bad); color:#fff; }
  .vbtn.sel-cant-tell { background:var(--mut); color:#fff; }
  .fullshot { width:100%; border:1px solid var(--line); border-radius:6px; }
  .diag { border-left:4px solid var(--bad); padding:10px 14px;
          background:#fdf1f0; border-radius:6px; margin-bottom:14px; }
  #lightbox { position:fixed; inset:0; background:rgba(0,0,0,.75);
              display:none; align-items:center; justify-content:center;
              cursor:zoom-out; z-index:10; }
  #lightbox img { max-width:95vw; max-height:95vh; }
  pre { background:var(--bg); padding:10px; border-radius:6px;
        overflow:auto; }
  .hide { display:none; }
</style>
</head>
<body>
<header>
  <h1>amd-portal-mcp console</h1>
  <span class="pill">localhost only; displays PHI</span>
  <span class="pill" id="tally">verdicts: -</span>
</header>
<main>
  <div class="card">
    <form id="runform">
      <div><label>Patient search string</label>
        <input id="patient" size="28" placeholder="last, first or chart #"
               required></div>
      <div><label>Insurance index</label>
        <input id="idx" type="number" value="1" min="1" style="width:70px">
      </div>
      <button id="runbtn" type="submit">Run</button>
      <button id="statusbtn" type="button" class="secondary">
        Session status</button>
      <span id="msg"></span>
    </form>
    <pre id="sessionout" class="hide"></pre>
  </div>

  <div class="card hide" id="laddercard">
    <h3 style="margin-top:0">Checkpoints <span class="pill"
        id="runid"></span></h3>
    <div class="ladder" id="ladder"></div>
  </div>

  <div class="card hide" id="failcard"></div>

  <div class="card hide" id="resultcard">
    <div class="cols">
      <div>
        <h3 style="margin-top:0">Scraped fields</h3>
        <table id="fields"><thead><tr><th>Field</th><th>Value</th>
          <th>Verdict</th></tr></thead><tbody></tbody></table>
      </div>
      <div>
        <h3 style="margin-top:0">Insurance card screenshot</h3>
        <img id="cardshot" class="fullshot">
      </div>
    </div>
  </div>
</main>
<div id="lightbox" onclick="this.style.display='none'"><img></div>
<script>
const STAGES = ["logged_in","app_ready","scheduler_open","patient_found",
  "patient_info_open","insurance_card_open","fields_scraped",
  "eligibility_details_open"];
let currentRun = null, currentPatient = null, pollTimer = null;

const $ = id => document.getElementById(id);

async function refreshTally() {
  const t = await (await fetch("/api/verdicts/tally")).json();
  $("tally").textContent = "verdicts: " + t.match + " match / " +
    t.mismatch + " mismatch / " + t["cant-tell"] + " cant-tell" +
    (t.accuracy === null ? "" : " | accuracy " + t.accuracy + "%");
}

function shotUrl(rec) {
  if (!rec || !rec.screenshot) return null;
  const parts = rec.screenshot.split("/");
  return "/shots/" + parts[parts.length-2] + "/" + parts[parts.length-1];
}

function renderLadder(cps) {
  const el = $("ladder"); el.innerHTML = "";
  for (const st of STAGES) {
    const rec = cps[st];
    const status = rec ? rec.status : "pending";
    const dur = rec && rec.duration_s != null ? rec.duration_s + "s" : "";
    const row = document.createElement("div");
    row.className = "stage";
    row.innerHTML = '<span class="name">' + st + '</span>' +
      '<span class="st ' + status + '">' + status + '</span>' +
      '<span class="dur">' + dur + '</span>';
    const url = shotUrl(rec);
    if (url) {
      const img = document.createElement("img");
      img.src = url; img.className = "thumb";
      img.onclick = () => { $("lightbox").style.display = "flex";
        $("lightbox").querySelector("img").src = url; };
      row.appendChild(img);
    }
    el.appendChild(row);
  }
}

function verdictCell(field, value) {
  const td = document.createElement("td");
  for (const v of ["match","mismatch","cant-tell"]) {
    const b = document.createElement("button");
    b.textContent = v; b.className = "vbtn";
    b.onclick = async () => {
      await fetch("/api/verdict", {method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({run_id: currentRun, patient: currentPatient,
          field: field, value_present: !!value, verdict: v})});
      td.querySelectorAll("button").forEach(x => x.className = "vbtn");
      b.className = "vbtn sel-" + v;
      refreshTally();
    };
    td.appendChild(b);
  }
  return td;
}

function renderResult(run) {
  const r = run.result;
  if (r.ok) {
    $("resultcard").classList.remove("hide");
    $("failcard").classList.add("hide");
    const tb = $("fields").querySelector("tbody"); tb.innerHTML = "";
    for (const [k, v] of Object.entries(r.data)) {
      if (k === "patient" || k === "insurance_index") continue;
      const tr = document.createElement("tr");
      const tdk = document.createElement("td"); tdk.textContent = k;
      const tdv = document.createElement("td"); tdv.className = "val";
      tdv.textContent = v === "" ? "(empty)" : v;
      tr.appendChild(tdk); tr.appendChild(tdv);
      tr.appendChild(verdictCell(k, v));
      tb.appendChild(tr);
    }
    const url = shotUrl(r.checkpoints["insurance_card_open"]) ||
                shotUrl(r.checkpoints["fields_scraped"]);
    if (url) $("cardshot").src = url;
  } else {
    $("resultcard").classList.add("hide");
    const fc = $("failcard"); fc.classList.remove("hide");
    let failShot = null;
    for (const st of STAGES) {
      const rec = r.checkpoints && r.checkpoints[st];
      if (rec && rec.status === "fail") failShot = shotUrl(rec);
    }
    fc.innerHTML = '<div class="diag"><b>Failed: ' + (r.diagnosis||r.error) +
      '</b><br>' + (r.message||"") + '<br><b>Next action:</b> ' +
      (r.next_action||"") + ' (retryable: ' + r.retryable + ')</div>';
    if (failShot) {
      const img = document.createElement("img");
      img.src = failShot; img.className = "fullshot";
      fc.appendChild(img);
    }
  }
}

async function poll() {
  const run = await (await fetch("/api/run/" + currentRun)).json();
  renderLadder(run.checkpoints || {});
  if (run.state === "done") {
    clearInterval(pollTimer); pollTimer = null;
    $("runbtn").disabled = false;
    renderResult(run);
  }
}

$("runform").onsubmit = async (e) => {
  e.preventDefault();
  $("msg").textContent = "";
  const body = {patient: $("patient").value,
    insurance_index: parseInt($("idx").value || "1")};
  const resp = await fetch("/api/run", {method:"POST",
    headers:{"Content-Type":"application/json"},
    body: JSON.stringify(body)});
  const j = await resp.json();
  if (resp.status === 409) { $("msg").textContent = j.message; return; }
  currentRun = j.run_id; currentPatient = body.patient;
  $("runid").textContent = j.run_id;
  $("laddercard").classList.remove("hide");
  $("resultcard").classList.add("hide");
  $("failcard").classList.add("hide");
  $("runbtn").disabled = true;
  renderLadder({});
  pollTimer = setInterval(poll, 1000);
};

$("statusbtn").onclick = async () => {
  $("msg").textContent = "";
  const resp = await fetch("/api/session-status", {method:"POST"});
  const j = await resp.json();
  if (resp.status === 409) { $("msg").textContent = j.message; return; }
  const out = $("sessionout"); out.classList.remove("hide");
  out.textContent = JSON.stringify(j, null, 2);
};

refreshTally();
</script>
</body>
</html>
"""


def main() -> None:
    import uvicorn

    os.environ["AMD_PORTAL_CAPTURE"] = "1"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.warning(
        "amd-portal-console displays PHI in the browser; it binds "
        "127.0.0.1 only and must stay on localhost. Artifacts go to "
        "runtime/ (gitignored) only."
    )
    uvicorn.run(app, host="127.0.0.1", port=8811, log_level="info")


if __name__ == "__main__":
    main()
