# RADAR LIMA FINAL — MONITOR JEE EN UN SOLO HTML
# Mantiene TODO el historial dentro de radar-ERM-Lima-2026/index.html.
# No crea snapshots/CSVs/JSONs adicionales.
#
# Uso:
#   python radar_lima_final.py
#   python radar_lima_final.py --watch 3600
#
# Dependencias:
#   python -m pip install curl_cffi playwright

import argparse
import asyncio
import html
import json
import re
import time
import subprocess
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode

from curl_cffi import requests
from playwright.async_api import async_playwright

BASE = "https://resultadoelectoral.onpe.gob.pe/presentacion-backend"
SITE = "https://resultadoelectoral.onpe.gob.pe"
ID_ELECCION = 3
DEP = "140000"
PROV = "140100"

ROOT = Path(__file__).resolve().parent
PUBLIC_SLUG = "radar-ERM-Lima-2026"
PUBLIC_DIR = ROOT / PUBLIC_SLUG
PUBLIC_DIR.mkdir(parents=True, exist_ok=True)
HTML_PATH = PUBLIC_DIR / "index.html"
LEGACY_HTML_PATH = ROOT / "index.html"
PROFILE_DIR = ROOT.parent / "radar_lima_starter" / "radar_lima" / "chrome_profile_hibrido"
if not PROFILE_DIR.exists():
    PROFILE_DIR = ROOT / "radar_lima" / "chrome_profile_hibrido"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)

RP_HINT = "RENOVACION POPULAR"
AP_HINT = "AVANZA PAIS"
SP_HINT = "SOMOS PERU"

HEADERS = {
    "accept":"application/json, text/plain, */*",
    "content-type":"application/json",
    "referer":SITE + "/main/resumen",
    "origin":SITE,
    "user-agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
}

def norm(s):
    s=str(s or "")
    return "".join(c for c in unicodedata.normalize("NFD",s) if unicodedata.category(c)!="Mn").upper().strip()

def nint(v,default=0):
    try:return int(v)
    except Exception:
        try:return int(float(v))
        except Exception:return default

def first_num(obj,keys,default=None):
    for k in keys:
        if k in obj and obj.get(k) is not None:
            try:return int(round(float(obj[k])))
            except Exception:pass
    return default

class ONPE:
    def __init__(self):
        self.s=requests.Session(impersonate="chrome124")
        self.s.headers.update(HEADERS)
        try:self.s.get(SITE+"/",timeout=20)
        except Exception:pass
    def get(self,path,params=None,retries=5):
        url=BASE+path;last=None
        for i in range(retries):
            try:
                r=self.s.get(url,params=params,timeout=30)
                txt=r.text or "";ctype=(r.headers.get("content-type") or "").lower()
                if r.status_code==200 and ("json" in ctype or txt.lstrip().startswith(("{","["))):
                    obj=r.json()
                    if isinstance(obj,dict) and obj.get("success") is False:last=RuntimeError(str(obj))
                    else:return obj.get("data") if isinstance(obj,dict) and "data" in obj else obj
                else:last=RuntimeError(f"HTTP {r.status_code}: {txt[:120]!r}")
            except Exception as e:last=e
            time.sleep(1.2*(i+1))
        raise RuntimeError(f"No pude leer {url}: {last}")

def province_params():
    return [
      {"idEleccion":ID_ELECCION,"idAmbitoGeografico":1,"tipoFiltro":"ubigeo_nivel_02","ubigeoNivel01":DEP,"ubigeoNivel02":PROV},
      {"idEleccion":ID_ELECCION,"idAmbitoGeografico":1,"tipoFiltro":"ubigeo_nivel_02","idUbigeoDepartamento":DEP,"idUbigeoProvincia":PROV},
    ]

def get_current_official():
    api=ONPE();last=None
    for p in province_params():
        try:
            parts=api.get("/resumen-general/participantes",p)
            totals=api.get("/resumen-general/totales",p)
            if not isinstance(parts,list) or not parts:continue
            rp=ap=sp=0
            for x in parts:
                name=str(x.get("nombreAgrupacionPolitica") or x.get("descripcion") or x.get("organizacionPolitica") or x.get("nombre") or "")
                votes=first_num(x,["totalVotosValidos","nvotos","totalVotos","votos"],0) or 0
                nn=norm(name)
                if RP_HINT in nn:rp=votes
                elif AP_HINT in nn:ap=votes
                elif SP_HINT in nn:sp=votes
            if rp<=0 or ap<=0:continue
            total=first_num(totals,["totalActas","actasTotal","nTotalActas"])
            cont=first_num(totals,["contabilizadas","actasContabilizadasNumero","nActasContabilizadas"])
            pct=first_num(totals,["actasContabilizadas","porcentajeActasContabilizadas"])
            if total and (cont is None or cont<=100) and pct is not None and 0<=pct<=100:
                cont=int(round(total*pct/100.0))
            observed=first_num(totals,["observadas","observada","actasObservadasNumero","nActasObservadas","totalActasObservadas"],None)
            pending=first_num(totals,["pendientes","pendiente","actasPendientesNumero","nActasPendientes"],None)
            return {"rp":rp,"ap":ap,"sp":sp,"cont":cont,"total":total,"observed":observed,"pending":pending}
        except Exception as e:last=e
    raise RuntimeError(f"No pude obtener resumen provincial: {last}")

STATE_RE=re.compile(r'<script id="radar-state" type="application/json">(.*?)</script>',re.S)

def load_state():
    if not HTML_PATH.exists():
        if LEGACY_HTML_PATH.exists():
            # Migración automática desde la antigua raíz al nuevo subdirectorio.
            HTML_PATH.write_text(LEGACY_HTML_PATH.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[migración] Estado copiado a {HTML_PATH}")
        else:
            raise RuntimeError(f"No encuentro {HTML_PATH} ni el antiguo index.html.")
    txt=HTML_PATH.read_text(encoding="utf-8")
    m=STATE_RE.search(txt)
    if not m:
        raise RuntimeError("El HTML público no contiene radar-state.")
    return json.loads(html.unescape(m.group(1)))

def atomic_write(path,text):
    tmp=path.with_suffix(path.suffix+".tmp");tmp.write_text(text,encoding="utf-8");tmp.replace(path)

def is_unresolved(a):return a.get("s") not in ("RESUELTA","ANULADA")

def cause_main(c):
    n=norm(c)
    if "ACTA SIN FIRMAS" in n:return "Sin firmas"
    if "ACTA ILEGIBLE" in n:return "Ilegibilidad"
    if "ACTA SIN DATOS" in n:return "Sin datos"
    if "ACTA INCOMPLETA" in n:return "Incompleta"
    if "ACTA CON ERROR ARITMETICO" in n:return "Error aritmético"
    if "ACTA IMPUGNADA" in n or "SOLICITUD DE NULIDAD" in n:return "Impugnación / nulidad"
    return "Otra"

def build_snapshot(state,ts,official,note="",scan_meta=None):
    unresolved=[a for a in state["actas"].values() if is_unresolved(a)]
    raw_rp=sum(nint(a.get("rp")) for a in unresolved);raw_ap=sum(nint(a.get("ap")) for a in unresolved)
    raw_gap=raw_rp-raw_ap;off_gap=nint(official.get("rp"))-nint(official.get("ap"));raw_final=off_gap+raw_gap
    null_floor=off_gap+sum(min(nint(a.get("rp"))-nint(a.get("ap")),0) for a in unresolved)
    req=max(raw_final+1,0)
    districts={};causes={}
    flags={k:0 for k in ["Error aritmético","Impugnada","Ilegible","Sin firmas","Sin datos","Incompleta","Solicitud de nulidad"]}
    pats={"Error aritmético":"ACTA CON ERROR ARITMETICO","Impugnada":"ACTA IMPUGNADA","Ilegible":"ACTA ILEGIBLE","Sin firmas":"ACTA SIN FIRMAS","Sin datos":"ACTA SIN DATOS","Incompleta":"ACTA INCOMPLETA","Solicitud de nulidad":"SOLICITUD DE NULIDAD"}
    for a in unresolved:
        gap=nint(a.get("rp"))-nint(a.get("ap"))
        d=districts.setdefault(a["d"],[0,0,0,0]);d[0]+=1;d[1]+=nint(a.get("rp"));d[2]+=nint(a.get("ap"));d[3]+=gap
        cm=a.get("cm") or cause_main(a.get("c"))
        c=causes.setdefault(cm,[0,0,0,0]);c[0]+=1;c[1]+=nint(a.get("rp"));c[2]+=nint(a.get("ap"));c[3]+=gap
        nc=norm(a.get("c"))
        for label,pat in pats.items():
            if pat in nc:flags[label]+=1
    return {"ts":ts,"official":official,"unresolved_count":len(unresolved),"unresolved_rp":raw_rp,"unresolved_ap":raw_ap,"unresolved_gap":raw_gap,"raw_final_gap":raw_final,"nullification_floor":null_floor,"required_shift":req,"avg_shift_per_unresolved":req/len(unresolved) if unresolved else 0,"districts":districts,"causes":causes,"flags":flags,"note":note,"scan":scan_meta or {},"reconciled":True,"analysis_at":ts}

async def page_fetch(page,url):
    js = "async (url) => { try { const r=await fetch(url,{method:'GET',credentials:'include',headers:{'Accept':'application/json, text/plain, */*'}}); const text=await r.text(); return {status:r.status,contentType:r.headers.get('content-type')||'',text}; } catch(e){ return {status:0,contentType:'',text:'',error:String(e)}; } }"
    return await page.evaluate(js,url)

async def recover_page(context):
    pages=[p for p in context.pages if not p.is_closed()];page=pages[0] if pages else await context.new_page()
    try:
        if "resultadoelectoral.onpe.gob.pe" not in (page.url or ""):await page.goto(SITE+"/main/actas",wait_until="commit",timeout=15000)
    except Exception:pass
    return page

async def fetch_detail(context,page,aid,gap_seconds,cooldown):
    url=BASE+f"/actas/{aid}";current=page
    for attempt in range(1,9):
        try:
            r=await page_fetch(current,url);status=r.get("status");txt=(r.get("text") or "").strip()
            if status==200 and txt:
                obj=json.loads(txt);data=obj.get("data") if isinstance(obj,dict) and "data" in obj else obj
                await asyncio.sleep(gap_seconds);return data,current
            human=status in (403,405,429) or "human verification" in txt.lower()
            if human:
                print(f"[JEE] protección ONPE ({status}); pausa {cooldown}s...")
                await asyncio.sleep(cooldown);current=await recover_page(context);continue
        except Exception:current=await recover_page(context)
        await asyncio.sleep(min(2*attempt,12))
    return None,current

def status_from_detail(detail):
    if not detail:return "UNKNOWN",""
    desc=str(detail.get("descripcionEstadoActa") or "");code=str(detail.get("codigoEstadoActa") or "");n=norm(desc)
    if code=="E" or "JEE" in n:return "JEE",desc
    if code=="P" or "PENDIENTE" in n:return "PENDIENTE",desc
    if "ANUL" in n:return "ANULADA",desc
    if "CONTABIL" in n:return "RESUELTA",desc
    return "RESUELTA",desc or code or "Estado final distinto de JEE"

async def scan_unresolved(state,official_observed,detail_rps,cooldown):
    ids=[aid for aid,a in state["actas"].items() if a.get("s")=="JEE"]
    if not ids:return {"checked":0,"changed":0,"errors":0,"stopped_early":False}
    current_jee=len(ids);target_changed=None
    if official_observed is not None and 0<=official_observed<=current_jee:target_changed=current_jee-official_observed
    cursor=nint(state.get("scan_cursor"),0)%len(ids);ordered=ids[cursor:]+ids[:cursor]
    checked=changed=errors=0;gap_seconds=1.0/max(detail_rps,0.05)
    async with async_playwright() as p:
        print(f"[JEE] revisando hasta {len(ordered)} actas aún marcadas JEE...")
        context=await p.chromium.launch_persistent_context(user_data_dir=str(PROFILE_DIR),channel="chrome",headless=False,locale="es-PE")
        pages=context.pages;page=pages[0] if pages else await context.new_page()
        try:await page.goto(SITE+"/main/actas",wait_until="domcontentloaded",timeout=60000)
        except Exception:print("[JEE] Chrome no terminó de cargar; sigo con la sesión.")
        for idx,aid in enumerate(ordered,1):
            detail,page=await fetch_detail(context,page,aid,gap_seconds,cooldown);checked+=1
            if detail is None:errors+=1;continue
            new_status,desc=status_from_detail(detail);a=state["actas"][aid]
            if new_status!="JEE":
                if a.get("s")=="JEE":changed+=1
                a["s"]=new_status
                a["last"]={"ts":datetime.now().isoformat(timespec="seconds"),"desc":desc,"resolution":detail.get("estadoDescripcionActaResolucion"),"sub":detail.get("descripcionSubEstadoActa")}
            if idx%50==0:print(f"[JEE] {idx}/{len(ordered)} | cambios={changed} | errores={errors}")
            if target_changed is not None and changed>=target_changed:
                state["scan_cursor"]=(cursor+idx)%max(len(ids),1);await context.close()
                return {"checked":checked,"changed":changed,"errors":errors,"stopped_early":checked<len(ordered),"target_changed":target_changed}
        state["scan_cursor"]=(cursor+checked)%max(len(ids),1);await context.close()
    return {"checked":checked,"changed":changed,"errors":errors,"stopped_early":False,"target_changed":target_changed}

def parse_iso(s):
    try:return datetime.fromisoformat(s)
    except Exception:return None


def last_reconciled_snapshot(state):
    hist = state.get("history") or []
    for snap in reversed(hist):
        if snap.get("reconciled") is True:
            return snap
        # Backwards compatibility: the seeded first cut is a complete 1,924-acta read.
        if snap.get("unresolved_count") == 1924 and snap.get("unresolved_rp") == 108434 and snap.get("unresolved_ap") == 104222:
            return snap
    return hist[-1] if hist else None


def build_quick_snapshot(state, ts, official, note=""):
    """
    Hourly/light update.
    IMPORTANT: it does NOT combine today's official total with a stale JEE basket.
    It preserves the projection from the last complete reconciliation and updates
    only the official count. This avoids double-counting actas that may already
    have moved from JEE into the official total.
    """
    ref = last_reconciled_snapshot(state)
    if not ref:
        return build_snapshot(
            state, ts, official, note,
            {"ran": False, "reason": "sin corte conciliado"}
        )

    snap = {
        "ts": ts,
        "official": official,
        "unresolved_count": ref.get("unresolved_count", 0),
        "unresolved_rp": ref.get("unresolved_rp", 0),
        "unresolved_ap": ref.get("unresolved_ap", 0),
        "unresolved_gap": ref.get("unresolved_gap", 0),
        "raw_final_gap": ref.get("raw_final_gap", 0),
        "nullification_floor": ref.get("nullification_floor", 0),
        "required_shift": ref.get("required_shift", 0),
        "avg_shift_per_unresolved": ref.get("avg_shift_per_unresolved", 0),
        "districts": ref.get("districts", {}),
        "causes": ref.get("causes", {}),
        "flags": ref.get("flags", {}),
        "note": note,
        "scan": {"ran": False, "reason": "actualización rápida; sin barrido JEE"},
        "reconciled": False,
        "analysis_at": ref.get("analysis_at") or ref.get("ts"),
    }
    return snap


def should_scan(state, official, args):
    if args.force_scan:
        return True, "barrido forzado"

    hist = state.get("history") or []
    last = hist[-1] if hist else None
    if not last:
        return True, "sin corte anterior"

    # If ONPE exposes the count of observed/JEE actas and it changed,
    # that is a meaningful trigger for reconciliation.
    prev_off = last.get("official") or {}
    old_obs = prev_off.get("observed")
    new_obs = official.get("observed")
    if old_obs is not None and new_obs is not None and old_obs != new_obs:
        return True, f"actas observadas {old_obs}→{new_obs}"

    # Otherwise, hourly updates remain LIGHT. A movement in RP/AP can simply mean
    # JEE resolutions are being incorporated; scanning all 1,924 immediately would
    # be wasteful and can hit ONPE protection.
    last_scan = parse_iso(state.get("last_full_scan"))
    if last_scan is None:
        # The seeded 12:43 cut is already a complete reconciliation.
        seed = last_reconciled_snapshot(state)
        if seed:
            state["last_full_scan"] = seed.get("analysis_at") or seed.get("ts")
            last_scan = parse_iso(state["last_full_scan"])

    if last_scan is None:
        return True, "sin barrido completo conocido"

    if datetime.now() - last_scan >= timedelta(hours=args.full_scan_hours):
        return True, f"barrido periódico {args.full_scan_hours:g}h"

    return False, "actualización rápida; no hace falta reescanear actas"

def render_html(state):
    state_json=json.dumps(state,ensure_ascii=False,separators=(",",":")).replace("</","<\\/")
    template = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Radar no oficial de la elección municipal de Lima 2026 basado en datos públicos de ONPE y actas enviadas al JEE.">
<title>Radar Lima 2026</title>
<style>
:root{
  --bg:#f3f6f8; --ink:#17212b; --muted:#6f7d8c; --card:#fff; --line:#dfe5ea;
  --rp:#0d7f9c; --rp2:#045b73; --ap:#173b8e; --ap2:#d6293e;
  --good:#137a4b; --goodbg:#eaf7f0; --warn:#9a5b00; --warnbg:#fff6e8; --bad:#b42318; --badbg:#fdecec;
  --shadow:0 8px 28px rgba(20,34,48,.08);
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif}
.wrap{max-width:1240px;margin:auto;padding:20px}
.topbar{display:flex;justify-content:space-between;align-items:flex-start;gap:16px;flex-wrap:wrap;margin-bottom:16px}
.brand h1{margin:0;font-size:clamp(28px,5vw,46px);letter-spacing:-.035em}
.brand p{margin:6px 0 0;color:var(--muted);font-size:15px}
.cutbox{background:#fff;border:1px solid var(--line);border-radius:14px;padding:10px 12px;box-shadow:var(--shadow)}
.cutbox label{font-size:12px;color:var(--muted);display:block;margin-bottom:5px}
select,input{border:1px solid var(--line);background:#fff;border-radius:9px;padding:9px 11px;color:var(--ink)}
.hero{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}
.candidate{min-height:240px;border-radius:22px;overflow:hidden;position:relative;box-shadow:var(--shadow);background:#fff}
.candidate .photo{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;object-position:center 20%}
.candidate:after{content:"";position:absolute;inset:0;background:linear-gradient(90deg,rgba(8,18,28,.88) 0%,rgba(8,18,28,.64) 42%,rgba(8,18,28,.12) 76%,rgba(8,18,28,.02) 100%)}
.candidate.rp:before,.candidate.ap:before{content:"";position:absolute;left:0;top:0;bottom:0;width:9px;z-index:3}
.candidate.rp:before{background:var(--rp)}
.candidate.ap:before{background:linear-gradient(var(--ap),var(--ap2))}
.candidate .content{position:relative;z-index:2;padding:22px;color:#fff;max-width:66%}
.party{font-size:12px;letter-spacing:.08em;text-transform:uppercase;font-weight:800;opacity:.88}
.name{font-size:clamp(24px,3vw,34px);font-weight:850;line-height:1.02;margin:5px 0 10px}
.role{font-size:12px;opacity:.83;line-height:1.35}
.vote{font-size:clamp(28px,4vw,42px);font-weight:900;margin-top:18px}
.vote small{display:block;font-size:12px;font-weight:500;opacity:.85}
.status{border-radius:18px;padding:18px 20px;margin:14px 0;box-shadow:var(--shadow);border:1px solid #b9d7c7;background:var(--goodbg)}
.status h2{margin:0 0 5px;font-size:clamp(23px,4vw,34px)}
.status p{margin:6px 0;line-height:1.55}
.status.warn{background:var(--warnbg);border-color:#ecd09c}
.status.bad{background:var(--badbg);border-color:#efbbb7}
.biglead{font-weight:900;color:var(--good)}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:14px 0}
.simplecard{background:#fff;border:1px solid var(--line);border-radius:16px;padding:17px;box-shadow:var(--shadow)}
.simplecard .question{font-size:13px;color:var(--muted);font-weight:700;line-height:1.35}
.simplecard .answer{font-size:clamp(23px,3vw,32px);font-weight:900;margin:7px 0}
.simplecard .explain{font-size:13px;color:var(--muted);line-height:1.4}
.tabs{display:flex;gap:7px;border-bottom:1px solid var(--line);overflow:auto;margin-top:22px}
.tab{border:0;background:transparent;padding:12px 14px;font-weight:800;color:var(--muted);cursor:pointer;white-space:nowrap}
.tab.active{color:var(--rp2);border-bottom:3px solid var(--rp)}
.page{display:none;padding-top:12px}.page.active{display:block}
h3{font-size:20px;margin:14px 0 9px}
.info{background:#fff;border:1px solid var(--line);border-radius:15px;padding:16px 18px;box-shadow:var(--shadow);line-height:1.6}
.info strong{color:#0d4862}
.tablewrap{max-height:630px;overflow:auto;border:1px solid var(--line);border-radius:14px;background:#fff}
table{width:100%;border-collapse:collapse}
th,td{padding:9px 11px;border-bottom:1px solid #edf1f4;text-align:right;font-size:13px}
th{position:sticky;top:0;background:#f8fafb;z-index:2;cursor:pointer}
th:first-child,td:first-child{text-align:left}
.plus{color:var(--good);font-weight:850}.minus{color:var(--bad);font-weight:850}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin:8px 0 12px}
.dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:5px}
.chartbox{background:#fff;border:1px solid var(--line);border-radius:15px;padding:10px;box-shadow:var(--shadow)}
canvas{width:100%;height:330px}
.footer{margin:30px 0 10px;color:var(--muted);font-size:11px;line-height:1.55}
.source-note{border-top:1px solid var(--line);padding-top:12px}
.badge{display:inline-flex;align-items:center;gap:6px;border-radius:999px;padding:6px 10px;font-size:12px;font-weight:800;background:#e9f6ef;color:var(--good)}
@media(max-width:800px){
  .hero{grid-template-columns:1fr}
  .grid{grid-template-columns:1fr}
  .candidate{min-height:215px}
  .candidate .content{max-width:72%;padding:18px}
}
</style>
</head>
<body>
<div class="wrap">
  <div class="topbar">
    <div class="brand">
      <h1>Radar Lima 2026</h1>
      <p>Una forma simple de entender qué falta y cuánto podría cambiar el resultado.</p>
    </div>
    <div class="cutbox">
      <label>Ver un corte anterior</label>
      <select id="cut"></select>
    </div>
  </div>

  <div class="hero">
    <div class="candidate rp">
      <img class="photo" src="https://commons.wikimedia.org/wiki/Special:Redirect/file/Aliaga.jpg?width=700" alt="Rafael López Aliaga">
      <div class="content">
        <div class="party">Renovación Popular</div>
        <div class="name">Rafael López Aliaga</div>
        <div class="role">Figura principal de la lista · formalmente figura como primer regidor.</div>
        <div class="vote" id="rpVotes">—<small>votos ya contabilizados</small></div>
      </div>
    </div>
    <div class="candidate ap">
      <img class="photo" src="https://commons.wikimedia.org/wiki/Special:Redirect/file/Francis_Allison.jpg?width=700" alt="Francis Allison">
      <div class="content">
        <div class="party">Avanza País</div>
        <div class="name">Francis Allison</div>
        <div class="role">Candidato a alcalde provincial de Lima.</div>
        <div class="vote" id="apVotes">—<small>votos ya contabilizados</small></div>
      </div>
    </div>
  </div>

  <div id="status" class="status"></div>

  <div class="grid">
    <div class="simplecard">
      <div class="question">¿Cuánto lleva de ventaja RP en lo ya contado?</div>
      <div class="answer" id="officialLead">—</div>
      <div class="explain">Esta es la diferencia que ONPE ya tiene incorporada oficialmente.</div>
    </div>
    <div class="simplecard">
      <div class="question">¿Qué pasaría si las observadas quedan como están digitadas hoy?</div>
      <div class="answer" id="projectedLead">—</div>
      <div class="explain">Sumamos literalmente los números que hoy figuran en esas actas; no es una encuesta ni una extrapolación.</div>
    </div>
    <div class="simplecard">
      <div class="question">¿Y en un peor caso de anulaciones contra RP?</div>
      <div class="answer" id="worstLead">—</div>
      <div class="explain">Anulamos todas las observadas que hoy favorecen a RP y dejamos todas las que favorecen a AP. No incluye cambios por recuento.</div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" data-page="resumen">Qué significa</button>
    <button class="tab" data-page="distritos">Distrito por distrito</button>
    <button class="tab" data-page="causas">Por qué están observadas</button>
    <button class="tab" data-page="historico">Cómo va cambiando</button>
    <button class="tab" data-page="metodo">Cómo se calcula</button>
  </div>

  <section id="resumen" class="page active">
    <div class="info" id="plainExplanation"></div>
    <div class="grid" style="margin-top:12px">
      <div class="simplecard">
        <div class="question">Actas que todavía sigue vigilando el radar</div>
        <div class="answer" id="jeeCount">—</div>
        <div class="explain">Son las que aún no detectamos como resueltas.</div>
      </div>
      <div class="simplecard">
        <div class="question">Saldo dentro de esas actas</div>
        <div class="answer" id="jeeLead">—</div>
        <div class="explain">Cómo favorecen hoy a RP o AP según los datos digitados.</div>
      </div>
      <div class="simplecard">
        <div class="question">Para que AP pase adelante tendría que cambiar…</div>
        <div class="answer" id="neededShift">—</div>
        <div class="explain">Votos netos respecto de lo que actualmente dicen las actas.</div>
      </div>
    </div>
  </section>

  <section id="distritos" class="page">
    <h3>Las actas observadas que quedan, por distrito</h3>
    <div style="margin-bottom:10px"><input id="districtSearch" placeholder="Buscar distrito…"></div>
    <div class="tablewrap">
      <table id="districtTable">
        <thead><tr><th data-k="name">Distrito</th><th data-k="n">Actas</th><th data-k="rp">RP</th><th data-k="ap">AP</th><th data-k="gap">Quién gana ahí</th></tr></thead>
        <tbody></tbody>
      </table>
    </div>
  </section>

  <section id="causas" class="page">
    <h3>¿Por qué esas actas fueron enviadas al JEE?</h3>
    <div class="info" style="margin-bottom:12px">
      Una misma acta puede tener más de un problema. Por eso abajo hay una tabla de <b>causal principal</b> y otra de <b>etiquetas</b> que pueden superponerse.
    </div>
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div class="tablewrap"><table id="causeTable"><thead><tr><th>Causal principal</th><th>Actas</th><th>Saldo RP−AP</th></tr></thead><tbody></tbody></table></div>
      <div class="tablewrap"><table id="flagTable"><thead><tr><th>Problema detectado</th><th>Actas</th></tr></thead><tbody></tbody></table></div>
    </div>
  </section>

  <section id="historico" class="page">
    <h3>Cómo ha cambiado la ventaja</h3>
    <div class="legend">
      <span><i class="dot" style="background:#173b8e"></i>Ventaja ya contabilizada</span>
      <span><i class="dot" style="background:#137a4b"></i>Si las observadas quedan como están</span>
      <span><i class="dot" style="background:#b77a14"></i>Peor caso por anulaciones</span>
    </div>
    <div class="chartbox"><canvas id="chart" width="1200" height="340"></canvas></div>
    <h3>Cortes guardados dentro de este mismo archivo</h3>
    <div class="tablewrap">
      <table id="historyTable"><thead><tr><th>Fecha</th><th>Ventaja oficial</th><th>Actas vigiladas</th><th>Si quedan como están</th><th>Peor caso anulaciones</th></tr></thead><tbody></tbody></table>
    </div>
  </section>

  <section id="metodo" class="page">
    <div class="info">
      <p><b>1. Ventaja oficial:</b> son los votos que ONPE ya incorporó.</p>
      <p><b>2. “Si quedan como están”:</b> toma los números que hoy aparecen digitados en cada acta enviada al JEE y los suma a la ventaja oficial.</p>
      <p><b>3. Peor caso por anulaciones:</b> supone, de forma deliberadamente extrema, que se anulan todas las actas todavía pendientes que favorecen a RP y se conservan todas las que favorecen a AP.</p>
      <p><b>4. Lo que aún puede cambiar:</b> el JEE puede resolver observaciones y, cuando corresponda, ordenar recuentos. Por eso este radar no sustituye el resultado oficial.</p>
      <p><b>5. No es una encuesta:</b> el bloque pendiente se calcula con las actas reales publicadas por ONPE, no con promedios de distritos.</p>
    </div>
  </section>

  <div class="footer">
    <div class="source-note">
      <b>Fuentes:</b> datos electorales públicos de ONPE y estados de actas enviados al JEE. Este sitio es un seguimiento independiente y no oficial.<br>
      Foto Rafael López Aliaga: “Aliaga.jpg”, Vox España, CC0, vía Wikimedia Commons.
      Foto Francis Allison: Ministerio de la Producción del Perú, marcada como dominio público en Wikimedia Commons.
    </div>
  </div>
</div>

<script id="radar-state" type="application/json">__STATE__</script>
<script>
const STATE=JSON.parse(document.getElementById('radar-state').textContent);
const cuts=STATE.history||[]; let currentIndex=Math.max(0,cuts.length-1); let dSort={k:'gap',dir:-1};
const fmt=n=>(n===null||n===undefined||Number.isNaN(+n))?'—':Math.round(+n).toLocaleString('es-PE');
const signed=n=>(n===null||n===undefined)?'—':((+n>0?'+':'')+fmt(n));
const cls=n=>(+n>0?'plus':(+n<0?'minus':''));
const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
const cut=()=>cuts[currentIndex]||{};

function render(){
  const c=cut(),o=c.official||{},gap=(+o.rp||0)-(+o.ap||0),raw=+c.raw_final_gap||0,worst=+c.nullification_floor||0,need=+c.required_shift||0;
  document.getElementById('rpVotes').childNodes[0].nodeValue=fmt(o.rp)+' ';
  document.getElementById('apVotes').childNodes[0].nodeValue=fmt(o.ap)+' ';
  document.getElementById('officialLead').textContent=signed(gap)+' RP';
  document.getElementById('projectedLead').textContent=signed(raw)+' RP';
  document.getElementById('worstLead').textContent=signed(worst)+' RP';
  document.getElementById('jeeCount').textContent=fmt(c.unresolved_count);
  document.getElementById('jeeLead').textContent=signed(c.unresolved_gap)+' RP';
  document.getElementById('neededShift').textContent=fmt(need)+' votos';

  const st=document.getElementById('status');
  if(gap>0 && raw>0 && worst>0){
    st.className='status';
    st.innerHTML=`<span class="badge">● Ventaja muy difícil de revertir</span><h2>RP va adelante por <span class="biglead">${fmt(gap)} votos</span> en lo ya contabilizado.</h2><p>Las actas que faltan resolver, tomadas tal como están digitadas hoy, <b>no recortan esa ventaja: la aumentan</b>. El escenario extremo de anulaciones selectivas tampoco alcanza por sí solo para poner a AP adelante.</p>`;
  }else if(gap>0 && raw>0){
    st.className='status warn';
    st.innerHTML=`<span class="badge" style="background:#fff1d4;color:#9a5b00">● Ventaja, pero quedan escenarios sensibles</span><h2>RP sigue adelante por ${fmt(gap)} votos.</h2><p>Las actas pendientes aún pueden ser relevantes y conviene seguirlas de cerca.</p>`;
  }else{
    st.className='status bad';
    st.innerHTML=`<span class="badge" style="background:#fde5e3;color:#b42318">● Corte muy abierto</span><h2>Este corte necesita atención.</h2><p>La ventaja actual no es suficientemente robusta bajo los escenarios del radar.</p>`;
  }

  let plain='';
  if(gap>0&&raw>0&&worst>0){
    plain=`<b>En castellano:</b> hoy RP tiene ${fmt(gap)} votos de ventaja que ya están en el cómputo. Si las ${fmt(c.unresolved_count)} actas que el radar sigue vigilando terminaran valiendo exactamente lo que hoy aparece digitado en ellas, la ventaja subiría a <b>${fmt(raw)} votos</b>. Incluso en un ejercicio extremo donde se anulan todas las pendientes favorables a RP y se conservan todas las favorables a AP, RP seguiría arriba por <b>${fmt(worst)}</b>. Para cambiar el líder haría falta que recuentos o correcciones muevan al menos <b>${fmt(need)} votos netos hacia AP</b> respecto de lo que actualmente muestran esas actas.`;
  }else{
    plain=`Este corte todavía tiene suficiente incertidumbre como para no resumirlo en una sola frase. Mira las pestañas de distrito e histórico.`;
  }
  document.getElementById('plainExplanation').innerHTML=plain;
  renderDistricts(); renderCauses(); renderHistory();
}

function districtRows(){
  const q=(document.getElementById('districtSearch').value||'').toUpperCase();
  let rows=Object.entries(cut().districts||{}).map(([name,v])=>({name,n:v[0],rp:v[1],ap:v[2],gap:v[3]}));
  if(q)rows=rows.filter(x=>x.name.toUpperCase().includes(q));
  rows.sort((a,b)=>typeof a[dSort.k]==='string'?dSort.dir*a[dSort.k].localeCompare(b[dSort.k]):dSort.dir*((+a[dSort.k])-(+b[dSort.k])));
  return rows;
}
function renderDistricts(){
  document.querySelector('#districtTable tbody').innerHTML=districtRows().map(x=>{
    const who=x.gap>0?`RP +${fmt(x.gap)}`:(x.gap<0?`AP +${fmt(-x.gap)}`:'Empate');
    return `<tr><td>${esc(x.name)}</td><td>${fmt(x.n)}</td><td>${fmt(x.rp)}</td><td>${fmt(x.ap)}</td><td class="${cls(x.gap)}">${who}</td></tr>`;
  }).join('');
}
function renderCauses(){
  const rows=Object.entries(cut().causes||{}).map(([name,v])=>({name,n:v[0],gap:v[3]})).sort((a,b)=>b.n-a.n);
  document.querySelector('#causeTable tbody').innerHTML=rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${fmt(x.n)}</td><td class="${cls(x.gap)}">${x.gap>=0?'RP +'+fmt(x.gap):'AP +'+fmt(-x.gap)}</td></tr>`).join('');
  document.querySelector('#flagTable tbody').innerHTML=Object.entries(cut().flags||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<tr><td>${esc(k)}</td><td>${fmt(v)}</td></tr>`).join('');
}
function renderHistory(){
  document.querySelector('#historyTable tbody').innerHTML=[...cuts].reverse().map((c,i)=>`<tr data-idx="${cuts.length-1-i}" style="cursor:pointer"><td>${esc(c.ts)}</td><td>${signed((c.official.rp||0)-(c.official.ap||0))}</td><td>${fmt(c.unresolved_count)}</td><td>${signed(c.raw_final_gap)}</td><td>${signed(c.nullification_floor)}</td></tr>`).join('');
  document.querySelectorAll('#historyTable tbody tr').forEach(tr=>tr.onclick=()=>{currentIndex=+tr.dataset.idx;document.getElementById('cut').value=currentIndex;render()});
  drawChart();
}
function drawChart(){
  const cv=document.getElementById('chart'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height;ctx.clearRect(0,0,W,H);ctx.fillStyle='#fff';ctx.fillRect(0,0,W,H);
  if(!cuts.length)return;
  const vals=[];cuts.forEach(c=>vals.push((+c.official.rp||0)-(+c.official.ap||0),+c.raw_final_gap||0,+c.nullification_floor||0));
  let min=Math.min(...vals,0),max=Math.max(...vals,1),pad=(max-min)*.12||1000;min-=pad;max+=pad;
  const x=i=>55+(W-90)*(cuts.length===1?.5:i/(cuts.length-1)),y=v=>20+(H-50)*(1-(v-min)/(max-min));
  ctx.strokeStyle='#e1e6ea';ctx.lineWidth=1;for(let j=0;j<5;j++){let yy=20+j*(H-50)/4;ctx.beginPath();ctx.moveTo(55,yy);ctx.lineTo(W-35,yy);ctx.stroke()}
  function line(fn,color){ctx.strokeStyle=color;ctx.lineWidth=4;ctx.beginPath();cuts.forEach((c,i)=>{const xx=x(i),yy=y(fn(c));i?ctx.lineTo(xx,yy):ctx.moveTo(xx,yy)});ctx.stroke()}
  line(c=>(+c.official.rp||0)-(+c.official.ap||0),'#173b8e');line(c=>+c.raw_final_gap||0,'#137a4b');line(c=>+c.nullification_floor||0,'#b77a14');
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.page).classList.add('active');if(b.dataset.page==='historico')drawChart()});
document.getElementById('districtSearch').oninput=renderDistricts;
document.querySelectorAll('#districtTable th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(!k)return;if(dSort.k===k)dSort.dir*=-1;else{dSort.k=k;dSort.dir=k==='name'?1:-1}renderDistricts()});
const sel=document.getElementById('cut');sel.innerHTML=cuts.map((c,i)=>`<option value="${i}">${esc(c.ts)}</option>`).join('');sel.value=currentIndex;sel.onchange=e=>{currentIndex=+e.target.value;render()};
render();
setTimeout(()=>location.reload(),5*60*1000);
</script>
</body>
</html>'''
    return template.replace("__STATE__", state_json)


def publish_github():
    """Commit/push index.html if this folder is already a Git repository."""
    try:
        probe=subprocess.run(["git","rev-parse","--is-inside-work-tree"],cwd=str(ROOT),capture_output=True,text=True,timeout=15)
        if probe.returncode!=0:
            print("[publicación] Esta carpeta todavía no está conectada a un repositorio Git.")
            return False
        subprocess.run(["git","add",f"{PUBLIC_SLUG}/index.html"],cwd=str(ROOT),check=True,timeout=20)
        diff=subprocess.run(["git","diff","--cached","--quiet"],cwd=str(ROOT),timeout=20)
        if diff.returncode==0:
            print("[publicación] No hubo cambios para publicar.")
            return True
        msg="Actualización Radar Lima "+datetime.now().strftime("%Y-%m-%d %H:%M")
        subprocess.run(["git","commit","-m",msg],cwd=str(ROOT),check=True,timeout=60)
        subprocess.run(["git","push"],cwd=str(ROOT),check=True,timeout=120)
        print("[publicación] Sitio enviado a GitHub.")
        return True
    except FileNotFoundError:
        print("[publicación] Git no está instalado todavía.")
    except Exception as e:
        print("[publicación] No pude publicar:",e)
    return False


async def run_once_async(args):
    state = load_state()
    print("Leyendo resumen provincial ONPE...")
    official = get_current_official()

    do_scan, reason = should_scan(state, official, args)

    if do_scan:
        print("Revisión JEE activada:", reason)
        scan_meta = await scan_unresolved(
            state,
            official.get("observed"),
            args.detail_rps,
            args.cooldown,
        )
        scan_meta["ran"] = True
        scan_meta["reason"] = reason
        state["last_full_scan"] = datetime.now().isoformat(timespec="seconds")

        # Releer resumen después de un barrido largo.
        try:
            official = get_current_official()
        except Exception:
            pass

        ts = datetime.now().isoformat(timespec="seconds")
        snap = build_snapshot(
            state, ts, official,
            f"Revisión completa de actas JEE: {reason}.",
            scan_meta,
        )
    else:
        print("Actualización rápida:", reason)
        ts = datetime.now().isoformat(timespec="seconds")
        snap = build_quick_snapshot(
            state, ts, official,
            "Corte horario del cómputo oficial. La proyección de observadas corresponde al último barrido completo.",
        )

    state.setdefault("history", []).append(snap)
    atomic_write(HTML_PATH, render_html(state))

    if getattr(args, "publish", False):
        publish_github()

    print("\n=== RADAR LIMA PÚBLICO ===")
    print(f"Oficial actual: RP {official['rp']} | AP {official['ap']} | brecha {official['rp']-official['ap']:+d}")
    if snap.get("reconciled"):
        print(f"JEE conciliadas en este corte: {snap['unresolved_count']}")
    else:
        print(f"Análisis JEE reutilizado del corte: {snap.get('analysis_at')}")
    print(f"Proyección del último barrido completo: {snap['raw_final_gap']:+d}")
    print(f"Piso por anulaciones del último barrido: {snap['nullification_floor']:+d}")
    print("HTML público:", HTML_PATH)

def run_once(args):asyncio.run(run_once_async(args))

def main():
    ap = argparse.ArgumentParser(description="Radar ERM Lima 2026 — publicación en subruta")
    ap.add_argument("--watch", type=int, default=0, metavar="SEGUNDOS")
    ap.add_argument("--force-scan", action="store_true",
                    help="Fuerza un barrido completo de las actas JEE.")
    ap.add_argument("--full-scan-hours", type=float, default=12.0,
                    help="Horas entre barridos completos preventivos. Default: 12.")
    ap.add_argument("--detail-rps", type=float, default=0.8)
    ap.add_argument("--cooldown", type=int, default=45)
    ap.add_argument("--render-only", action="store_true",
                    help="Regenera el HTML sin consultar ONPE ni abrir Chrome.")
    ap.add_argument("--publish", action="store_true",
                    help="Después de actualizar index.html, hace git commit y git push.")
    args = ap.parse_args()

    args.detail_rps = max(.15, min(args.detail_rps, 2))
    args.cooldown = max(20, min(args.cooldown, 300))
    args.full_scan_hours = max(1, min(args.full_scan_hours, 48))

    if args.render_only:
        state = load_state()
        atomic_write(HTML_PATH, render_html(state))
        print("HTML regenerado SIN consultar ONPE:", HTML_PATH)
        return

    if not args.watch:
        run_once(args)
        return

    args.watch = max(300, args.watch)
    print(f"Monitor activo. Cadencia objetivo: {args.watch}s. Ctrl+C para detener.")
    while True:
        started = time.monotonic()
        try:
            run_once(args)
        except Exception as e:
            print("[ERROR]", e)
        elapsed = time.monotonic() - started
        sleep_for = max(0, args.watch - elapsed)
        print(f"Próximo corte en {sleep_for/60:.1f} min.")
        time.sleep(sleep_for)

if __name__=="__main__":
    try:main()
    except KeyboardInterrupt:print("\nMonitor detenido. Todo el historial queda dentro de index.html.")
