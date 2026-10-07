# RADAR LIMA FINAL — MONITOR JEE EN UN SOLO HTML
# Comprueba ONPE cada 30 minutos. Si cambia, intenta identificar directamente las actas resueltas por el delta de votos; si no puede, usa el barrido completo de seguridad.
# No crea snapshots/CSVs/JSONs adicionales.
#
# Uso:
#   python radar_lima_final.py
#   python radar_lima_publico.py --watch 1800 --publish
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


def first_text(obj, keys, default=None):
    for k in keys:
        if k in obj:
            v = obj.get(k)
            if v not in (None, ""):
                return str(v)
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
            updated = first_text(
                totals,
                [
                    "fechaActualizacion",
                    "fechaHoraActualizacion",
                    "fechaActualizacionProceso",
                    "ultimaActualizacion",
                    "fechaCorte",
                    "fechaHora",
                ],
                None,
            )
            return {
                "rp":rp,"ap":ap,"sp":sp,"cont":cont,"total":total,
                "observed":observed,"pending":pending,
                "updated":updated,
            }
        except Exception as e:last=e
    raise RuntimeError(f"No pude obtener resumen provincial: {last}")

STATE_RE=re.compile(r'<script id="radar-state" type="application/json">(.*?)</script>',re.S)


def history_signature(snap):
    """
    Two cuts are the same for public-history purposes when nothing electoral
    changed. Time alone NEVER creates a new cut.
    """
    o = snap.get("official") or {}
    return (
        o.get("rp"),
        o.get("ap"),
        o.get("cont"),
        o.get("observed"),
        o.get("pending"),
        snap.get("unresolved_count"),
        snap.get("unresolved_rp"),
        snap.get("unresolved_ap"),
        snap.get("unresolved_gap"),
        snap.get("raw_final_gap"),
        snap.get("nullification_floor"),
    )


def clean_history(state):
    """
    Always preserve the first/base cut. After that, keep only snapshots whose
    electoral signature differs from the last retained snapshot.
    """
    hist = state.get("history") or []
    if not hist:
        return 0

    kept = [hist[0]]
    last_sig = history_signature(hist[0])

    for snap in hist[1:]:
        sig = history_signature(snap)
        if sig != last_sig:
            kept.append(snap)
            last_sig = sig

    removed = len(hist) - len(kept)
    state["history"] = kept
    return removed


def snapshot_is_meaningful(state, snap):
    hist = state.get("history") or []
    if not hist:
        return True
    return history_signature(hist[-1]) != history_signature(snap)


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
    state = json.loads(html.unescape(m.group(1)))
    removed = clean_history(state)
    if removed:
        print(f"[histórico] Eliminados {removed} cortes de prueba/repetidos.")
    return state

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


def official_vote_delta(previous_official, current_official):
    return {
        "rp": nint(current_official.get("rp")) - nint(previous_official.get("rp")),
        "ap": nint(current_official.get("ap")) - nint(previous_official.get("ap")),
        "sp": nint(current_official.get("sp")) - nint(previous_official.get("sp")),
    }


def candidate_resolution_sets(state, previous_official, current_official, expected_drop):
    """
    Usa únicamente información YA guardada en el radar.

    Si una o dos actas observadas pasan a contabilizadas sin que sus valores
    digitados cambien, el incremento oficial RP/AP/SP debe coincidir exactamente
    con los votos guardados de esa(s) acta(s).

    Devuelve conjuntos candidatos. Nunca modifica el estado.
    """
    delta = official_vote_delta(previous_official, current_official)

    if expected_drop not in (1, 2):
        return [], delta, "solo intento coincidencia exacta cuando salieron 1 o 2 actas"

    # Una resolución normal no debería reducir los votos oficiales.
    if any(delta[k] < 0 for k in ("rp", "ap", "sp")):
        return [], delta, "algún delta de votos fue negativo"

    unresolved = [
        (aid, a)
        for aid, a in state.get("actas", {}).items()
        if a.get("s") == "JEE"
    ]

    target = (delta["rp"], delta["ap"], delta["sp"])

    if expected_drop == 1:
        hits = []
        for aid, a in unresolved:
            sig = (nint(a.get("rp")), nint(a.get("ap")), nint(a.get("sp")))
            if sig == target:
                hits.append([aid])
        return hits[:50], delta, None if hits else "ninguna acta coincide exactamente con el delta"

    # expected_drop == 2. Búsqueda O(n) por complemento.
    by_sig = {}
    for aid, a in unresolved:
        sig = (nint(a.get("rp")), nint(a.get("ap")), nint(a.get("sp")))
        by_sig.setdefault(sig, []).append(aid)

    solutions = set()
    for aid, a in unresolved:
        sig = (nint(a.get("rp")), nint(a.get("ap")), nint(a.get("sp")))
        comp = (target[0]-sig[0], target[1]-sig[1], target[2]-sig[2])
        if min(comp) < 0:
            continue
        for bid in by_sig.get(comp, []):
            if bid == aid:
                continue
            pair = tuple(sorted((str(aid), str(bid))))
            solutions.add(pair)
            if len(solutions) >= 50:
                break
        if len(solutions) >= 50:
            break

    return [list(x) for x in solutions], delta, None if solutions else "ningún par de actas coincide exactamente con el delta"


async def verify_candidate_set(state, ids, detail_rps, cooldown):
    """
    Verifica SOLO las actas candidatas. El radar no da por resuelta una acta
    únicamente por coincidencia matemática: confirma su estado actual en ONPE.
    """
    gap_seconds = 1.0 / max(detail_rps, 0.05)
    checked = changed = errors = 0
    confirmed = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR),
            channel="chrome",
            headless=False,
            locale="es-PE",
        )
        pages = context.pages
        page = pages[0] if pages else await context.new_page()

        try:
            await page.goto(SITE + "/main/actas", wait_until="domcontentloaded", timeout=60000)
        except Exception:
            print("[JEE] Chrome no terminó de cargar; sigo con la sesión.")

        for aid in ids:
            detail, page = await fetch_detail(context, page, aid, gap_seconds, cooldown)
            checked += 1

            if detail is None:
                errors += 1
                continue

            new_status, desc = status_from_detail(detail)
            a = state["actas"][str(aid)]

            if new_status != "JEE":
                if a.get("s") == "JEE":
                    changed += 1
                a["s"] = new_status
                a["last"] = {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "desc": desc,
                    "resolution": detail.get("estadoDescripcionActaResolucion"),
                    "sub": detail.get("descripcionSubEstadoActa"),
                }
                confirmed.append(str(aid))

        await context.close()

    return {
        "checked": checked,
        "changed": changed,
        "errors": errors,
        "confirmed": confirmed,
        "complete": changed == len(ids) and errors == 0,
    }


async def reconcile_smart(state, previous_official, current_official, expected_total_jee, detail_rps, cooldown):
    """
    V10.1:
      1) Si salió 1 o 2 actas, intenta identificarlas por el delta exacto RP/AP/SP.
      2) Confirma en ONPE únicamente las candidatas.
      3) Si no hay coincidencia exacta o la verificación falla, vuelve al
         barrido completo anterior.

    No usa los resúmenes distritales que resultaron no comparables con el
    universo provincial de actas observadas.
    """
    current_jee = sum(
        1 for a in state.get("actas", {}).values()
        if a.get("s") == "JEE"
    )

    if expected_total_jee is None:
        print("[delta] ONPE no expuso un total JEE utilizable; uso barrido completo.")
        full = await scan_unresolved(state, expected_total_jee, detail_rps, cooldown)
        full["mode"] = "full_fallback"
        return full

    expected_drop = current_jee - int(expected_total_jee)

    if expected_drop <= 0:
        print(f"[delta] No hay reducción del universo JEE ({current_jee} -> {expected_total_jee}); uso barrido completo.")
        full = await scan_unresolved(state, expected_total_jee, detail_rps, cooldown)
        full["mode"] = "full_fallback"
        return full

    sets, delta, why = candidate_resolution_sets(
        state, previous_official, current_official, expected_drop
    )

    print(
        f"[delta] ONPE: JEE {current_jee} -> {expected_total_jee} "
        f"({expected_drop} acta(s)); votos: "
        f"RP {delta['rp']:+d}, AP {delta['ap']:+d}, SP {delta['sp']:+d}"
    )

    if not sets:
        print(f"[delta] No pude identificar las actas por coincidencia exacta: {why}.")
        print("[delta] Uso barrido completo de seguridad.")
        full = await scan_unresolved(state, expected_total_jee, detail_rps, cooldown)
        full["mode"] = "full_fallback"
        full["vote_delta"] = delta
        return full

    print(f"[delta] Encontré {len(sets)} conjunto(s) candidato(s). Verifico solo esas actas...")

    # Verify candidate sets one by one. Usually there is exactly one.
    for candidate_ids in sets:
        labels = []
        for aid in candidate_ids:
            a = state["actas"].get(str(aid), {})
            labels.append(
                f"{a.get('d','?')} mesa {a.get('m','?')} "
                f"(RP {nint(a.get('rp'))}, AP {nint(a.get('ap'))}, SP {nint(a.get('sp'))})"
            )
        print("[delta] Candidato: " + " + ".join(labels))

        result = await verify_candidate_set(
            state, candidate_ids, detail_rps, cooldown
        )

        if result.get("complete"):
            remaining = sum(
                1 for a in state.get("actas", {}).values()
                if a.get("s") == "JEE"
            )
            if remaining == int(expected_total_jee):
                print(
                    f"[delta] Confirmado. Solo revisé {result['checked']} acta(s); "
                    f"quedan {remaining} observadas."
                )
                return {
                    "mode": "vote_delta_exact",
                    "checked": result["checked"],
                    "changed": result["changed"],
                    "errors": result["errors"],
                    "candidate_ids": candidate_ids,
                    "vote_delta": delta,
                    "target_changed": expected_drop,
                    "stopped_early": True,
                }

        # If a candidate set was false, its verified still-JEE actas have not
        # been mutated. Any resolved acta found is real, so keep that knowledge.
        print("[delta] Ese conjunto no explicó completamente el cambio.")

    print("[delta] Ningún conjunto candidato quedó confirmado; uso barrido completo.")
    full = await scan_unresolved(state, expected_total_jee, detail_rps, cooldown)
    full["mode"] = "full_fallback"
    full["vote_delta"] = delta
    return full


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


def effective_observed_count(state, official):
    """
    Prefer ONPE's explicit observed count. If the summary endpoint omits it,
    infer the remaining JEE universe as total - contabilizadas, but only because
    our base cut established pending normal = 0 and the election had already
    finished normal counting.
    """
    obs = official.get("observed")
    if obs is not None:
        return nint(obs, None)

    total = official.get("total")
    cont = official.get("cont")
    if total is not None and cont is not None:
        base = (state.get("history") or [{}])[0]
        base_pending = (base.get("official") or {}).get("pending")
        if base_pending == 0:
            return max(0, nint(total) - nint(cont))
    return None


def official_signature(official):
    """
    Time is intentionally excluded. Only electoral data can trigger a new cycle.
    """
    return (
        official.get("rp"),
        official.get("ap"),
        official.get("sp"),
        official.get("cont"),
        official.get("total"),
        official.get("observed"),
        official.get("pending"),
    )


def last_real_official(state):
    hist = state.get("history") or []
    if not hist:
        return None
    return hist[-1].get("official") or None


def should_scan(state, official, args):
    if args.force_scan:
        return True, "barrido forzado"

    prev = last_real_official(state)
    if prev is None:
        return True, "sin corte anterior"

    if official_signature(prev) != official_signature(official):
        changes = []
        labels = {
            "rp": "RP",
            "ap": "AP",
            "sp": "SP",
            "cont": "actas contabilizadas",
            "total": "actas totales",
            "observed": "actas observadas",
            "pending": "actas pendientes",
        }
        for k, label in labels.items():
            if prev.get(k) != official.get(k):
                # Ignore null-vs-null; the condition above already guarantees some difference.
                changes.append(f"{label}: {prev.get(k)}→{official.get(k)}")
        return True, "cambio ONPE detectado (" + "; ".join(changes) + ")"

    return False, "ONPE sin cambios"

def render_html(state):
    state_json=json.dumps(state,ensure_ascii=False,separators=(",",":")).replace("</","<\\/")
    template = r'''<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="Radar no oficial dedicado exclusivamente a las actas observadas de la ERM Lima 2026 enviadas al JEE. No reconstruye el escrutinio normal previo.">
<title>Radar de Actas Observadas · ERM Lima 2026</title>
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
.onpe-ref{background:#fff;border:1px solid var(--line);border-radius:16px;padding:14px 16px;box-shadow:var(--shadow);margin:14px 0 16px}
.onpe-ref-top{display:flex;gap:22px;align-items:flex-end;flex-wrap:wrap}
.onpe-pct-wrap{min-width:145px}
.onpe-label{font-size:12px;color:var(--muted);font-weight:700}
.onpe-pct{font-size:36px;font-weight:900;line-height:1;color:#123a8d;margin-top:2px}
.onpe-total{font-size:15px;font-weight:800;margin-bottom:4px}
.onpe-sub{font-size:12px;color:var(--muted)}
.onpe-bar{height:10px;border-radius:999px;overflow:hidden;background:#e9eef3;display:flex;margin:11px 0 9px}
.onpe-bar .cont{background:#0a4c89}
.onpe-bar .jee{background:#6eb7e7}
.onpe-bar .pend{background:#dfe7ef}
.onpe-legend{display:flex;gap:18px;flex-wrap:wrap;justify-content:flex-end;font-size:12px;color:#31445a}
.onpe-legend span{display:inline-flex;align-items:center;gap:6px}
.onpe-dot{width:12px;height:12px;border-radius:50%;display:inline-block;border:1px solid #0a4c89}
.onpe-dot.cont{background:#0a4c89}
.onpe-dot.jee{background:#6eb7e7}
.onpe-dot.pend{background:#fff}
.onpe-time{font-size:11px;color:var(--muted);margin-top:5px;text-transform:uppercase;letter-spacing:.02em}
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
.projection{background:linear-gradient(135deg,#fff8dc 0%,#fff1b8 55%,#ffe59a 100%);border:2px solid #e0a400;border-radius:20px;padding:20px 22px;box-shadow:0 10px 30px rgba(151,103,0,.16);margin:14px 0 18px}
.projection-head{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;flex-wrap:wrap}
.projection-kicker{font-size:12px;font-weight:950;letter-spacing:.09em;text-transform:uppercase;color:#8b5c00}
.projection-title{font-size:clamp(25px,4vw,38px);font-weight:950;letter-spacing:-.025em;margin:4px 0 3px}
.projection-sub{color:var(--muted);font-size:13px;line-height:1.45}
.confidence{display:inline-flex;align-items:center;gap:7px;border-radius:999px;padding:8px 12px;font-size:12px;font-weight:900;background:#dff4e9;color:var(--good);white-space:nowrap}
.confidence.high{background:#fff0cf;color:#8a5a00}
.confidence.open{background:#fde6e3;color:var(--bad)}
.projection-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}
.proj-stat{background:#fff;border:1px solid var(--line);border-radius:13px;padding:13px 14px}
.proj-label{font-size:11px;color:var(--muted);font-weight:750;line-height:1.3}
.proj-value{font-size:22px;font-weight:900;margin-top:4px}
.projection-note{margin-top:14px;font-size:13px;line-height:1.55;color:#33485e}
.projection-link{margin-top:10px;border:0;background:none;color:var(--rp2);font-weight:850;padding:0;cursor:pointer;text-decoration:underline}
@media(max-width:900px){.projection-grid{grid-template-columns:1fr 1fr}}
@media(max-width:560px){.projection-grid{grid-template-columns:1fr}}
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
tfoot th{background:#f3f6f8;border-top:2px solid var(--line);font-weight:900}
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
      <h1>Radar de Actas Observadas · ERM Lima 2026</h1>
      <p>Seguimiento exclusivo de las actas enviadas al JEE. Este panel no reconstruye el escrutinio normal anterior.</p>
    </div>
    <div class="cutbox">
      <label>Ver un corte anterior</label>
      <select id="cut"></select>
    </div>
  </div>

  <div class="onpe-ref">
    <div class="onpe-ref-top">
      <div class="onpe-pct-wrap">
        <div class="onpe-label">Actas contabilizadas</div>
        <div class="onpe-pct" id="onpePct">—</div>
      </div>
      <div>
        <div class="onpe-total">Total de actas: <span id="onpeTotal">—</span></div>
        <div class="onpe-sub" id="onpeSub">—</div>
      </div>
    </div>
    <div class="onpe-bar" aria-label="Distribución de actas">
      <span class="cont" id="barCont"></span>
      <span class="jee" id="barJee"></span>
      <span class="pend" id="barPend"></span>
    </div>
    <div class="onpe-legend">
      <span><i class="onpe-dot cont"></i>Contabilizadas <b id="legendCont">—</b></span>
      <span><i class="onpe-dot jee"></i>Para envío al JEE <b id="legendJee">—</b></span>
      <span><i class="onpe-dot pend"></i>Pendientes <b id="legendPend">—</b></span>
    </div>
    <div class="onpe-time" id="onpeTime">—</div>
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

  <div class="projection" id="projectionBox">
    <div class="projection-head">
      <div>
        <div class="projection-kicker">Proyección del radar</div>
        <div class="projection-title" id="projectionTitle">—</div>
        <div class="projection-sub" id="projectionSub">—</div>
      </div>
      <div class="confidence" id="projectionConfidence">—</div>
    </div>

    <div class="projection-grid">
      <div class="proj-stat">
        <div class="proj-label">Ganador proyectado</div>
        <div class="proj-value" id="projectionWinner">—</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Ventaja proyectada</div>
        <div class="proj-value" id="projectionMargin">—</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Actas observadas aún abiertas</div>
        <div class="proj-value" id="projectionObserved">—</div>
      </div>
      <div class="proj-stat">
        <div class="proj-label">Cambio de votos necesario para que AP pase adelante</div>
        <div class="proj-value" id="projectionNeed">—</div>
      </div>
    </div>

    <div class="projection-note" id="projectionNote">—</div>
    <button class="projection-link" id="projectionMethodLink">¿Cómo se calcula?</button>
  </div>


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
    <div class="info" style="margin-bottom:12px"><b>Alcance del radar:</b> este seguimiento empieza cuando el escrutinio normal ya había terminado y el universo pendiente estaba concentrado en las actas observadas enviadas al JEE. Los resultados normales anteriores no forman parte de este histórico. <b>Cada corte guardado se publica recién después de conciliar las actas observadas</b>, de modo que Resumen, Distritos, Causales e Histórico correspondan al mismo corte.</div>
    <div class="info" id="plainExplanation"></div>
    <div class="grid" style="margin-top:12px">
      <div class="simplecard">
        <div class="question">Actas observadas aún abiertas</div>
        <div class="answer" id="jeeCount">—</div>
        <div class="explain">Se recalculan cuando ONPE cambia y el radar concilia el bloque enviado al JEE.</div>
      </div>
      <div class="simplecard">
        <div class="question">Saldo dentro de esas actas</div>
        <div class="answer" id="jeeLead">—</div>
        <div class="explain">Cómo favorecen hoy a RP o AP según los datos digitados.</div>
      </div>
      <div class="simplecard">
        <div class="question">Para que AP pase adelante tendría que cambiar…</div>
        <div class="answer" id="neededShift">—</div>
        <div class="explain">Diferencia necesaria respecto de los valores que actualmente figuran en las actas observadas.</div>
      </div>
    </div>
  </section>

  <section id="distritos" class="page">
    <h3>Actas observadas aún abiertas, por distrito</h3>
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
      <b>Las dos tablas miden cosas distintas.</b> La de la izquierda reparte cada acta <b>una sola vez</b> en una “causa principal”, para que el total coincida exactamente con el número de actas observadas. La de la derecha cuenta <b>todos los problemas que aparecen</b> en las actas: una misma acta puede tener, por ejemplo, <i>error aritmético + ilegibilidad</i>, y entonces aparece una vez en cada etiqueta. Por eso los números de ambas tablas no tienen que coincidir y la suma de la derecha puede ser mayor que el total de actas.
    </div>
    <div class="grid" style="grid-template-columns:1fr 1fr">
      <div>
        <div class="q" style="margin:0 0 7px 3px">CLASIFICACIÓN EXCLUSIVA · CADA ACTA APARECE UNA VEZ</div>
        <div class="tablewrap"><table id="causeTable"><thead><tr><th>Causa principal asignada</th><th>Actas</th><th>Saldo RP−AP</th></tr></thead><tbody></tbody><tfoot><tr><th>Total de actas</th><th id="causeTotal">—</th><th></th></tr></tfoot></table></div>
      </div>
      <div>
        <div class="q" style="margin:0 0 7px 3px">ETIQUETAS DETECTADAS · UNA ACTA PUEDE APARECER VARIAS VECES</div>
        <div class="tablewrap"><table id="flagTable"><thead><tr><th>Problema encontrado</th><th>Veces que aparece</th></tr></thead><tbody></tbody><tfoot><tr><th>Total de etiquetas</th><th id="flagTotal">—</th></tr></tfoot></table></div>
      </div>
    </div>
    <div class="info" id="causeExplanation" style="margin-top:12px"></div>
  </section>

  <section id="historico" class="page">
    <h3>Cómo ha cambiado la ventaja</h3>
    <div class="legend">
      <span><i class="dot" style="background:#173b8e"></i>Ventaja ya contabilizada</span>
      <span><i class="dot" style="background:#137a4b"></i>Si las observadas quedan como están</span>
      <span><i class="dot" style="background:#b77a14"></i>Peor caso por anulaciones</span>
    </div>
    <div class="chartbox"><canvas id="chart" width="1200" height="340"></canvas></div>
    <h3>Cortes reales: solo cuando cambió algo</h3>
    <div class="tablewrap">
      <table id="historyTable"><thead><tr><th>Fecha</th><th>Ventaja oficial</th><th>Actas vigiladas</th><th>Si quedan como están</th><th>Peor caso anulaciones</th></tr></thead><tbody></tbody></table>
    </div>
  </section>

  <section id="metodo" class="page">
    <div class="info">
      <p><b>1. Alcance:</b> este radar sigue exclusivamente las actas observadas enviadas al JEE. El corte base se tomó después de terminado el escrutinio normal.</p><p><b>2. Ventaja oficial:</b> son los votos que ONPE ya incorporó en cada corte.</p>
      <p><b>3. “Si quedan como están”:</b> toma los números que hoy aparecen digitados en cada acta enviada al JEE y los suma a la ventaja oficial.</p>
      <p><b>4. Peor caso por anulaciones:</b> supone, de forma deliberadamente extrema, que se anulan todas las actas todavía pendientes que favorecen a RP y se conservan todas las que favorecen a AP.</p>
      <p><b>5. Lo que aún puede cambiar:</b> el JEE puede resolver observaciones y, cuando corresponda, ordenar recuentos. Por eso este radar no sustituye el resultado oficial.</p>
      <p><b>6. Proyección del radar:</b> indica qué lista terminaría adelante si las actas observadas que siguen pendientes de resolución conservaran los valores digitados del corte. El nivel “Muy alta” se usa cuando, además, incluso la prueba extrema de anulaciones selectivas mantiene al mismo líder.</p>
      <p><b>7. Causales:</b> la tabla “Causa principal” es una clasificación exclusiva: cada acta aparece una sola vez. La tabla de “Problemas encontrados” no es exclusiva; una misma acta puede aportar a varias etiquetas, por eso sus cantidades no tienen que coincidir.</p>
      <p><b>8. No es una encuesta:</b> el bloque pendiente se calcula con las actas reales publicadas por ONPE, no con promedios de distritos.</p>
    </div>
  </section>

  <div class="footer">
    <div class="source-note">
      <b>Fuentes:</b> datos electorales públicos de ONPE y estados de actas enviados al JEE. El bloque superior reproduce como referencia los indicadores esenciales del resumen de actas. Este sitio es un seguimiento independiente y no oficial.<br>
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
  const total=+o.total||0, cont=+o.cont||0;
  const jee=(c.unresolved_count!==null&&c.unresolved_count!==undefined)?(+c.unresolved_count||0):(+o.observed||0);
  const pend=(o.pending!==null&&o.pending!==undefined)?(+o.pending||0):Math.max(0,total-cont-jee);
  const pctCont=total?100*cont/total:0, pctJee=total?100*jee/total:0, pctPend=total?100*pend/total:0;
  document.getElementById('onpePct').textContent=pctCont.toFixed(3)+' %';
  document.getElementById('onpeTotal').textContent=fmt(total);
  document.getElementById('onpeSub').textContent=pctJee.toFixed(3)+' % de actas para envío al JEE y '+pctPend.toFixed(3)+' % de actas pendientes';
  document.getElementById('barCont').style.width=pctCont+'%';
  document.getElementById('barJee').style.width=pctJee+'%';
  document.getElementById('barPend').style.width=pctPend+'%';
  document.getElementById('legendCont').textContent='('+fmt(cont)+')';
  document.getElementById('legendJee').textContent='('+fmt(jee)+')';
  document.getElementById('legendPend').textContent='('+fmt(pend)+')';
  const officialStamp=o.updated?String(o.updated):null;
  document.getElementById('onpeTime').textContent=officialStamp
    ? 'Actualización reportada por ONPE: '+officialStamp
    : 'Corte conciliado por el radar: '+String(c.ts||'—').replace('T',' ');
  document.getElementById('rpVotes').childNodes[0].nodeValue=fmt(o.rp)+' ';
  document.getElementById('apVotes').childNodes[0].nodeValue=fmt(o.ap)+' ';
  document.getElementById('officialLead').textContent=signed(gap)+' RP';
  document.getElementById('projectedLead').textContent=signed(raw)+' RP';
  document.getElementById('worstLead').textContent=signed(worst)+' RP';
  document.getElementById('jeeCount').textContent=fmt(c.unresolved_count);
  document.getElementById('jeeLead').textContent=signed(c.unresolved_gap)+' RP';
  document.getElementById('neededShift').textContent=fmt(need)+' votos';

  // PROYECCIÓN DEL RADAR
  const projTitle=document.getElementById('projectionTitle');
  const projSub=document.getElementById('projectionSub');
  const projConf=document.getElementById('projectionConfidence');
  const projWinner=document.getElementById('projectionWinner');
  const projMargin=document.getElementById('projectionMargin');
  const projObserved=document.getElementById('projectionObserved');
  const projNeed=document.getElementById('projectionNeed');
  const projNote=document.getElementById('projectionNote');

  let winner='Elección abierta', conf='Abierta', confClass='open';

  if(raw>0){
    winner='Renovación Popular';
    if(worst>0){
      conf='Muy alta';
      confClass='';
    }else{
      conf='Alta';
      confClass='high';
    }
  }else if(raw<0){
    winner='Avanza País';
    conf='Alta';
    confClass='high';
  }

  projWinner.textContent=winner;
  projObserved.textContent=fmt(c.unresolved_count);
  projNeed.textContent=fmt(need)+' votos';
  projConf.textContent='Confianza: '+conf;
  projConf.className='confidence '+confClass;

  if(raw>0){
    projTitle.textContent='RP terminaría adelante si las actas observadas mantienen los valores digitados';
    projMargin.textContent='RP +'+fmt(raw);
    projMargin.className='proj-value plus';
    projSub.textContent='Proyección basada en los valores digitados de las actas observadas del corte seleccionado; no es una encuesta.';
    if(worst>0){
      projNote.innerHTML='Con los valores digitados del corte, RP terminaría con una ventaja de <b>'+fmt(raw)+' votos</b>. Incluso en la prueba extrema que anula todas las observadas favorables a RP y conserva todas las favorables a AP, RP seguiría arriba por <b>'+fmt(worst)+' votos</b>. Para que AP pase adelante, las resoluciones o recuentos tendrían que cambiar el resultado en aproximadamente <b>'+fmt(need)+' votos a favor de AP</b> respecto de los valores actualmente digitados.';
    }else{
      projNote.innerHTML='Con los valores digitados del corte, RP terminaría con una ventaja de <b>'+fmt(raw)+' votos</b>. Sin embargo, la prueba extrema de anulaciones ya puede poner el resultado en zona sensible, por lo que el nivel de confianza baja.';
    }
  }else if(raw<0){
    projTitle.textContent='AP terminaría adelante si las actas observadas mantienen los valores digitados';
    projMargin.textContent='AP +'+fmt(-raw);
    projMargin.className='proj-value minus';
    projSub.textContent='Proyección basada en los valores digitados de las actas observadas del corte seleccionado; no es una encuesta.';
    projNote.innerHTML='Los valores digitados del corte favorecen a AP en la proyección. El resultado oficial todavía depende de las resoluciones del JEE.';
  }else{
    projTitle.textContent='Proyección prácticamente empatada';
    projMargin.textContent='Empate';
    projMargin.className='proj-value';
    projSub.textContent='La diferencia proyectada es prácticamente nula en este corte.';
    projNote.innerHTML='El bloque observado deja el resultado en una situación abierta.';
  }

  const st=document.getElementById('status');
  if(gap>0 && raw>0 && worst>0){
    st.className='status';
    st.innerHTML=`<span class="badge">● Ventaja muy difícil de revertir</span><h2>RP va adelante por <span class="biglead">${fmt(gap)} votos</span> en lo ya contabilizado.</h2><p>Las actas observadas aún abiertas, tomadas tal como están digitadas en el corte conciliado, <b>no recortan esa ventaja: la aumentan</b>. El escenario extremo de anulaciones selectivas tampoco alcanza por sí solo para poner a AP adelante.</p>`;
  }else if(gap>0 && raw>0){
    st.className='status warn';
    st.innerHTML=`<span class="badge" style="background:#fff1d4;color:#9a5b00">● Ventaja, pero quedan escenarios sensibles</span><h2>RP sigue adelante por ${fmt(gap)} votos.</h2><p>Las actas pendientes aún pueden ser relevantes y conviene seguirlas de cerca.</p>`;
  }else{
    st.className='status bad';
    st.innerHTML=`<span class="badge" style="background:#fde5e3;color:#b42318">● Corte muy abierto</span><h2>Este corte necesita atención.</h2><p>La ventaja actual no es suficientemente robusta bajo los escenarios del radar.</p>`;
  }

  let plain='';
  if(gap>0&&raw>0&&worst>0){
    plain=`<b>En castellano:</b> hoy RP tiene ${fmt(gap)} votos de ventaja que ya están en el cómputo. Si las ${fmt(c.unresolved_count)} actas observadas aún abiertas terminaran valiendo exactamente lo que aparece digitado en ellas, la ventaja subiría a <b>${fmt(raw)} votos</b>. Incluso en un ejercicio extremo donde se anulan todas las pendientes favorables a RP y se conservan todas las favorables a AP, RP seguiría arriba por <b>${fmt(worst)}</b>. Para cambiar el líder haría falta que recuentos o correcciones muevan al menos <b>${fmt(need)} votos netos hacia AP</b> respecto de lo que actualmente muestran esas actas.`;
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
  const c=cut();
  const rows=Object.entries(c.causes||{}).map(([name,v])=>({name,n:v[0],gap:v[3]})).sort((a,b)=>b.n-a.n);
  const flags=Object.entries(c.flags||{}).sort((a,b)=>b[1]-a[1]);
  const causeTotal=rows.reduce((s,x)=>s+(+x.n||0),0);
  const flagTotal=flags.reduce((s,x)=>s+(+x[1]||0),0);

  document.querySelector('#causeTable tbody').innerHTML=rows.map(x=>`<tr><td>${esc(x.name)}</td><td>${fmt(x.n)}</td><td class="${cls(x.gap)}">${x.gap>=0?'RP +'+fmt(x.gap):'AP +'+fmt(-x.gap)}</td></tr>`).join('');
  document.querySelector('#flagTable tbody').innerHTML=flags.map(([k,v])=>`<tr><td>${esc(k)}</td><td>${fmt(v)}</td></tr>`).join('');

  document.getElementById('causeTotal').textContent=fmt(causeTotal);
  document.getElementById('flagTotal').textContent=fmt(flagTotal);

  const extras=Math.max(0,flagTotal-causeTotal);
  document.getElementById('causeExplanation').innerHTML=
    `En este corte hay <b>${fmt(causeTotal)} actas observadas</b>. La tabla izquierda suma exactamente ${fmt(causeTotal)} porque cada acta se clasifica una sola vez. La tabla derecha suma <b>${fmt(flagTotal)} etiquetas</b>: son ${fmt(extras)} apariciones adicionales porque algunas actas tienen más de un problema simultáneamente. Por ejemplo, un acta clasificada a la izquierda como <b>“Sin firmas”</b> también puede contar a la derecha dentro de <b>“Error aritmético”</b> si presenta ambos problemas.`;
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
document.getElementById('projectionMethodLink').onclick=()=>{
  const btn=[...document.querySelectorAll('.tab')].find(x=>x.dataset.page==='metodo');
  if(btn) btn.click();
  document.getElementById('metodo').scrollIntoView({behavior:'smooth',block:'start'});
};

document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.page').forEach(x=>x.classList.remove('active'));b.classList.add('active');document.getElementById(b.dataset.page).classList.add('active');if(b.dataset.page==='historico')drawChart()});
document.getElementById('districtSearch').oninput=renderDistricts;
document.querySelectorAll('#districtTable th').forEach(th=>th.onclick=()=>{const k=th.dataset.k;if(!k)return;if(dSort.k===k)dSort.dir*=-1;else{dSort.k=k;dSort.dir=k==='name'?1:-1}renderDistricts()});
const sel=document.getElementById('cut');sel.innerHTML=cuts.map((c,i)=>`<option value="${i}">${esc(c.ts)}</option>`).join('');sel.value=currentIndex;sel.onchange=e=>{currentIndex=+e.target.value;render()};
render();
setTimeout(()=>location.reload(),5*60*1000);
</script>
<!-- Cloudflare Web Analytics --><script type='module' src='https://static.cloudflareinsights.com/beacon.min.js' data-cf-beacon='{"token": "ba8a7f01a9c9455eaad2bfc6d82bc94a"}'></script><!-- End Cloudflare Web Analytics -->
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
        msg="Actualización Radar actas observadas "+datetime.now().strftime("%Y-%m-%d %H:%M")
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
    official_start = get_current_official()

    do_scan, reason = should_scan(state, official_start, args)

    if not do_scan:
        print("[ONPE] Sin cambios respecto del último corte real.")
        print("[JEE] No se escanean actas.")
        print("[histórico] No se crea corte.")
        print("[publicación] No se hace commit ni push.")
        last = (state.get("history") or [{}])[-1]
        print("\n=== RADAR DE ACTAS OBSERVADAS · ERM LIMA 2026 ===")
        print(f"Oficial sin cambios: RP {official_start['rp']} | AP {official_start['ap']} | brecha {official_start['rp']-official_start['ap']:+d}")
        print(f"Último corte real: {last.get('ts')}")
        print("Próxima comprobación según la cadencia del monitor.")
        return

    print("[ONPE]", reason)

    # This phase has no normal pending actas in the base snapshot. When ONPE's
    # summary omits 'observed', total-cont gives the exact JEE remainder.
    target_observed = effective_observed_count(state, official_start)
    if target_observed is not None:
        print(f"[JEE] Universo observado esperado tras el cambio: {target_observed}")

    previous_official = last_real_official(state) or {}
    scan_meta = await reconcile_smart(
        state,
        previous_official,
        official_start,
        target_observed,
        args.detail_rps,
        args.cooldown,
    )
    scan_meta["ran"] = True
    scan_meta["reason"] = reason
    scan_meta["target_observed"] = target_observed

    # Read ONPE again AFTER the acta reconciliation so headline and district tabs
    # belong to the same as-close-as-possible cut.
    official_end = get_current_official()

    # If ONPE moved again while we were scanning, try one extra reconciliation.
    # This still respects the hourly outer cadence; it is part of the SAME cut.
    if official_signature(official_end) != official_signature(official_start):
        print("[ONPE] Hubo movimiento durante el escaneo. Hago una segunda conciliación del mismo corte...")
        target2 = effective_observed_count(state, official_end)
        scan2 = await reconcile_smart(
            state,
            official_start,
            official_end,
            target2,
            args.detail_rps,
            args.cooldown,
        )
        scan_meta["second_pass"] = scan2
        scan_meta["target_observed_after"] = target2
        official_end = get_current_official()

    state["last_full_scan"] = datetime.now().isoformat(timespec="seconds")
    ts = datetime.now().isoformat(timespec="seconds")
    snap = build_snapshot(
        state,
        ts,
        official_end,
        "Cambio detectado en ONPE; corte guardado después de conciliar las actas observadas.",
        scan_meta,
    )

    # Avoid a false cut if ONPE returned to exactly the previous electoral state.
    if not snapshot_is_meaningful(state, snap):
        print("[histórico] Tras conciliar, el estado electoral coincide con el último corte. No se guarda ni publica.")
        return

    state.setdefault("history", []).append(snap)
    clean_history(state)
    atomic_write(HTML_PATH, render_html(state))
    print("[histórico] Cambio real conciliado: nuevo corte completo guardado.")

    if getattr(args, "publish", False):
        publish_github()

    print("\n=== RADAR DE ACTAS OBSERVADAS · ERM LIMA 2026 ===")
    print(f"Oficial conciliado: RP {official_end['rp']} | AP {official_end['ap']} | brecha {official_end['rp']-official_end['ap']:+d}")
    print(f"Actas observadas aún abiertas: {snap['unresolved_count']}")
    print(f"Diferencia RP−AP dentro de las actas observadas: {snap['unresolved_gap']:+d}")
    print(f"Proyección conciliada: {snap['raw_final_gap']:+d}")
    print(f"Piso por anulaciones: {snap['nullification_floor']:+d}")
    print("HTML público:", HTML_PATH)

def run_once(args):asyncio.run(run_once_async(args))

def main():
    ap = argparse.ArgumentParser(description="Radar ERM Lima 2026 — publicación en subruta")
    ap.add_argument("--watch", type=int, default=0, metavar="SEGUNDOS")
    ap.add_argument("--force-scan", action="store_true",
                    help="Fuerza un barrido completo de las actas JEE.")
    ap.add_argument("--detail-rps", type=float, default=0.8)
    ap.add_argument("--cooldown", type=int, default=45)
    ap.add_argument("--render-only", action="store_true",
                    help="Regenera el HTML sin consultar ONPE ni abrir Chrome.")
    ap.add_argument("--publish", action="store_true",
                    help="Después de actualizar index.html, hace git commit y git push.")
    args = ap.parse_args()

    args.detail_rps = max(.15, min(args.detail_rps, 2))
    args.cooldown = max(20, min(args.cooldown, 300))

    if args.render_only:
        state = load_state()
        atomic_write(HTML_PATH, render_html(state))
        print("HTML regenerado SIN consultar ONPE y con histórico depurado:", HTML_PATH)
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
