#!/usr/bin/env python3
"""
repost_bot.py — Orchestratore: rileva i nuovi video di un profilo TikTok,
li scarica (TikHub/tikdownloader) e li ripubblica su Instagram e YouTube
(Zernio), tenendo memoria per-piattaforma di cosa ha gia' fatto per non
ripubblicare due volte.

Pensato per girare anche su GitHub Actions (cloud, PC spento).

CHIAVI (da variabili d'ambiente, oppure da argomenti):
  TIKHUB_API_KEY      la tua key TikHub
  ZERNIO_API_KEY      la tua key Zernio
  TIKTOK_USERNAME     profilo da sorvegliare (default: federico.bora)

USO TIPICO (locale, prova sicura senza pubblicare):
  python3 repost_bot.py --tikhub-key 'hZk...' --zernio-key 'sk_...' --dry-run

USO REALE:
  python3 repost_bot.py --tikhub-key 'hZk...' --zernio-key 'sk_...'

PROTEZIONI:
- Primo avvio (nessuno state.json): "semina" gli ID attuali SENZA pubblicare
  nulla, cosi' non riversa tutta la cronologia su Instagram. Da li' in poi
  pubblica solo i video davvero nuovi.
- --max-new: non pubblica mai piu' di N video in una singola esecuzione
  (rete di sicurezza contro pubblicazioni a raffica). Default: 3.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

TIKHUB = "https://api.tikhub.io"
ZERNIO = "https://zernio.com/api/v1"
UA = "repost-bot/1.0"

# Piattaforme di destinazione. Per aggiungerne una in futuro basta metterla qui:
# viene integrata automaticamente nello stato e nel ciclo di pubblicazione.
TARGETS = ("instagram", "youtube")

# curl_cffi e' opzionale: se manca, il bot NON si pianta, ripiega sul 720p TikHub.
try:
    from curl_cffi import requests as _cffi
except ImportError:
    _cffi = None

# tikdownloader.io: via per il 1080p REALE (emula l'app, supera Cloudflare con
# l'impronta TLS di Chrome). Vedi tikdl_fetch_cffi.py per la genesi.
TIKDL_HOME = "https://tikdownloader.io/en"
TIKDL_ENDPOINT = "https://tikdownloader.io/api/ajaxSearch"
TIKDL_HEADERS = {
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
    "origin": "https://tikdownloader.io",
    "referer": TIKDL_HOME,
    "x-requested-with": "XMLHttpRequest",
}
_CF_SIGNS = ("just a moment", "challenge-platform", "cf-mitigated",
             "enable javascript and cookies", "cf_chl_opt")


# --------------------------------------------------------------------------- #
#  HTTP helper                                                                #
# --------------------------------------------------------------------------- #

def _req(url, headers=None, data=None, method="GET", timeout=180):
    req = urllib.request.Request(url, data=data,
                                 headers=headers or {"User-Agent": UA},
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} su {url[:70]} — {body}")


def get_json(url, key):
    return json.loads(_req(url, {"Authorization": f"Bearer {key}", "User-Agent": UA}))


def post_json(url, key, payload):
    return json.loads(_req(
        url, {"Authorization": f"Bearer {key}", "User-Agent": UA,
              "Content-Type": "application/json"},
        data=json.dumps(payload).encode(), method="POST"))


def deep_find(obj, keys):
    if isinstance(obj, dict):
        for k in keys:
            if obj.get(k):
                return obj[k]
        for v in obj.values():
            r = deep_find(v, keys)
            if r:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = deep_find(v, keys)
            if r:
                return r
    return None


# --------------------------------------------------------------------------- #
#  TikHub: rileva + scarica                                                    #
# --------------------------------------------------------------------------- #

def tikhub_sec_uid(key, username):
    url = (f"{TIKHUB}/api/v1/tiktok/web/get_sec_user_id"
           f"?url={urllib.parse.quote(f'https://www.tiktok.com/@{username}', safe='')}")
    data = get_json(url, key)
    sec = deep_find(data, ("sec_user_id", "secUid", "sec_uid"))
    if not sec and isinstance(data.get("data"), str):
        sec = data["data"]
    if not sec:
        raise RuntimeError("Non sono riuscito a risolvere il sec_uid.")
    return sec


def tikhub_recent(key, sec_uid, count):
    # Elenco post via endpoint app V3: piu' stabile dello scraping web.
    # L'endpoint web/fetch_user_post restituisce 400 in modo intermittente;
    # app/v3/fetch_user_post_videos accetta sec_user_id e ritorna aweme_list.
    url = (f"{TIKHUB}/api/v1/tiktok/app/v3/fetch_user_post_videos"
           f"?sec_user_id={urllib.parse.quote(sec_uid)}"
           f"&max_cursor=0&count={count}&sort_type=0")
    data = get_json(url, key)
    # aweme_list = formato app; itemList = formato web (fallback difensivo)
    items = deep_find(data, ("aweme_list",)) or deep_find(data, ("itemList",)) or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        vid = it.get("aweme_id") or it.get("id")
        desc = it.get("desc") or ""
        if vid:
            out.append({"id": str(vid), "desc": desc})
    return out


def tikhub_best_video_url(key, aweme_id):
    """Usa fetch_one_video (app/v3) e sceglie la risoluzione piu' alta."""
    url = f"{TIKHUB}/api/v1/tiktok/app/v3/fetch_one_video?aweme_id={aweme_id}"
    data = get_json(url, key)
    v = None
    try:
        v = data["data"]["aweme_detail"]["video"]
    except Exception:
        pass
    cands = []
    if isinstance(v, dict):
        vw, vh = int(v.get("width") or 0), int(v.get("height") or 0)

        def add(addr, bitrate, codec):
            if not isinstance(addr, dict):
                return
            urls = addr.get("url_list") or []
            if not urls:
                return
            w = int(addr.get("width") or 0) or vw
            h = int(addr.get("height") or 0) or vh
            cands.append((min(w, h) if w and h else 0, int(bitrate or 0),
                          1 if codec == "h264" else 0, urls[0]))

        for br in v.get("bit_rate", []) or []:
            codec = "hevc" if (br.get("is_bytevc1") or br.get("is_h265")) else "h264"
            add(br.get("play_addr"), br.get("bit_rate", 0), codec)
        add(v.get("play_addr"), 0, "?")
        add(v.get("download_addr"), 0, "?")
    if not cands:
        url2 = deep_find(data, ("play_addr", "download_addr"))
        u = deep_find(url2, ("url_list",)) if url2 else None
        if u:
            return u[0]
        raise RuntimeError(f"Nessun URL video per {aweme_id}.")
    cands.sort(reverse=True)
    return cands[0][3]


def download_bytes(url):
    return _req(url, {"User-Agent": UA}, timeout=180)


# --------------------------------------------------------------------------- #
#  tikdownloader.io: 1080p reale via curl_cffi (con fallback al 720p TikHub)   #
# --------------------------------------------------------------------------- #

import html as _html
import re as _re


def _looks_blocked(status, text):
    if status in (403, 503):
        return True
    low = (text or "")[:4000].lower()
    return any(s in low for s in _CF_SIGNS)


def _tikdl_extract_html(raw):
    raw = (raw or "").strip()
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict):
            for k in ("data", "result", "html"):
                if isinstance(obj.get(k), str):
                    return obj[k]
    except json.JSONDecodeError:
        pass
    return raw


def _tikdl_video_urls(blob):
    s = _html.unescape((blob or "").replace("\\/", "/"))
    out, seen = [], set()
    for u in _re.findall(r'https?://[^\s"\'<>\\)]+', s):
        u = u.rstrip("\\")
        low = u.lower()
        if not any(t in low for t in (".mp4", "tiktokcdn", "/video/",
                                      "mime_type=video", "dl.", "/dl?",
                                      "rapidcdn", "snapcdn", "tikcdn")):
            continue
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _looks_like_mp4(data):
    # un mp4 valido ha il box 'ftyp' nei primi byte; filtra thumbnail e frammenti
    return bool(data) and b"ftyp" in data[:64]


def fetch_1080_tikdownloader(tiktok_url):
    """Ritorna i BYTES del 1080p via tikdownloader, oppure None se non riesce.

    Sceglie il candidato mp4 valido piu' GRANDE (il 1080p ~112MB straccia il
    540p ~11MB), senza bisogno di ffprobe. Qualsiasi intoppo -> None (fallback).
    """
    if _cffi is None:
        return None
    try:
        s = _cffi.Session(impersonate="chrome")
        try:
            s.get(TIKDL_HOME, timeout=30)
        except Exception:
            pass
        r = s.post(TIKDL_ENDPOINT, data={"q": tiktok_url, "lang": "en"},
                   headers=TIKDL_HEADERS, timeout=60)
        if r.status_code != 200 or _looks_blocked(r.status_code, r.text):
            return None
        cands = _tikdl_video_urls(_tikdl_extract_html(r.text))
        best = None
        for u in cands[:6]:
            try:
                data = s.get(u, timeout=300).content
            except Exception:
                continue
            if not _looks_like_mp4(data):
                continue
            if best is None or len(data) > len(best):
                best = data
        if best and len(best) > 3_000_000:   # >3MB = e' il video vero, non un frammento
            return best
        return None
    except Exception:
        return None


def download_best_bytes(tiktok_url, aweme_id, tikhub_key, allow_hd=True):
    """Prima il 1080p (tikdownloader); se fallisce, il 720p nativo di TikHub."""
    if allow_hd:
        data = fetch_1080_tikdownloader(tiktok_url)
        if data:
            return data, "1080p tikdownloader"
        print("   ⚠ HD non disponibile (Cloudflare/curl_cffi) → ripiego sul 720p TikHub")
    url = tikhub_best_video_url(tikhub_key, aweme_id)
    return download_bytes(url), "720p TikHub"


# --------------------------------------------------------------------------- #
#  Zernio: carica + pubblica                                                   #
# --------------------------------------------------------------------------- #

def zernio_account(key, platform):
    """Risolve dinamicamente l'accountId Zernio per la piattaforma indicata
    ('instagram', 'youtube', ...). Niente da hardcodare: lo legge da Zernio,
    quindi non serve nessun nuovo secret per l'account YouTube."""
    data = get_json(f"{ZERNIO}/accounts", key)
    accounts = deep_find(data, ("accounts",)) or (data if isinstance(data, list) else [])
    for a in accounts:
        if isinstance(a, dict) and (a.get("platform") or "").lower() == platform:
            return a.get("accountId") or a.get("_id") or a.get("id")
    raise RuntimeError(f"Nessun account {platform} collegato su Zernio.")


def zernio_upload(key, video_bytes, filename):
    presign = post_json(f"{ZERNIO}/media/presign", key,
                        {"filename": filename, "contentType": "video/mp4"})
    node = presign.get("data") if isinstance(presign.get("data"), dict) else presign
    upload_url = deep_find(node, ("uploadUrl",))
    public_url = deep_find(node, ("publicUrl",))
    if not upload_url or not public_url:
        raise RuntimeError(f"Presign incompleto: {json.dumps(presign)[:200]}")
    # PUT diretto, senza Authorization
    _req(upload_url, {"Content-Type": "video/mp4", "User-Agent": UA},
         data=video_bytes, method="PUT")
    return public_url


def yt_title(caption):
    """YouTube ESIGE un titolo (Instagram no). Prendo la prima riga non vuota
    della caption, ripulita e a max 100 caratteri; se vuota, fallback neutro."""
    for line in (caption or "").splitlines():
        line = line.strip().replace("<", "").replace(">", "")
        if line:
            return line[:100]
    return "Video"


def zernio_post(key, public_url, caption, account_id, platform="instagram", title=None):
    plat = {"platform": platform, "accountId": account_id}
    if platform == "youtube":
        desc = caption or ""
        if "#shorts" not in desc.lower():            # piccola spinta alla feed Shorts
            desc = (desc + "\n\n#Shorts").strip()
        plat["platformSpecificData"] = {
            "title": title or yt_title(caption),
            "description": desc,
            # Zernio documenta 'privacyStatus' (public|unlisted|private). Includo
            # anche 'visibility' come rete di sicurezza: gli extra vengono ignorati.
            # Se mai YouTube rifiutasse, è QUI l'unico punto da ritoccare.
            "privacyStatus": "public",
            "visibility": "public",
            "madeForKids": False,
        }
    payload = {
        "content": caption,
        "mediaItems": [{"type": "video", "url": public_url}],
        "platforms": [plat],
        "publishNow": True,
    }
    res = post_json(f"{ZERNIO}/posts", key, payload)
    node = res.get("post") or res.get("data") or res
    return deep_find(node, ("_id", "id", "postId"))


# --------------------------------------------------------------------------- #
#  Stato (file JSON con gli ID gia' processati)                               #
# --------------------------------------------------------------------------- #

def _is_active_hour(hour, h_from, h_to):
    """True se 'hour' è dentro la finestra [h_from, h_to) (gestisce il wrap)."""
    if h_from <= h_to:
        return h_from <= hour < h_to
    return hour >= h_from or hour < h_to


def within_active_hours(tz_name, h_from, h_to):
    """Controlla l'ora REALE nel fuso dato (es. Europe/Rome), DST inclusa."""
    if ZoneInfo is None:                  # ambiente senza fusi: non bloccare
        return True, None
    now = datetime.now(ZoneInfo(tz_name))
    return _is_active_hour(now.hour, h_from, h_to), now.strftime("%H:%M")


def _empty_state():
    return {p: set() for p in TARGETS}


def load_state(path):
    """Stato per-piattaforma: {'instagram': {..id..}, 'youtube': {..id..}}.

    Migra DA SOLO il vecchio formato {'posted': [...]} (solo IG): quegli id
    vengono considerati 'gia' fatti' su TUTTE le piattaforme, cosi' aggiungere
    YouTube NON riversa la cronologia sul canale nuovo — solo i video nuovi.
    """
    if not os.path.exists(path):
        return None                      # None = primo avvio
    try:
        with open(path) as f:
            raw = json.load(f)
    except Exception:
        return _empty_state()
    state = _empty_state()
    if isinstance(raw, dict) and "posted" in raw and not any(p in raw for p in TARGETS):
        seen = set(raw.get("posted", []))            # vecchio formato → migrazione
        for p in TARGETS:
            state[p] = set(seen)
    else:                                            # formato nuovo
        for p in TARGETS:
            state[p] = set((raw or {}).get(p, []))
    return state


def save_state(path, state):
    with open(path, "w") as f:
        json.dump({p: sorted(state.get(p, ())) for p in TARGETS}, f, indent=2)


# --------------------------------------------------------------------------- #
#  Main                                                                        #
# --------------------------------------------------------------------------- #

def main():
    ap = argparse.ArgumentParser(description="Riposta i nuovi TikTok su Instagram")
    ap.add_argument("--tikhub-key", default=os.environ.get("TIKHUB_API_KEY"))
    ap.add_argument("--zernio-key", default=os.environ.get("ZERNIO_API_KEY"))
    ap.add_argument("--username", default=os.environ.get("TIKTOK_USERNAME", "federico.bora"))
    ap.add_argument("--sec-uid", default=os.environ.get("TIKTOK_SEC_UID"),
                    help="sec_uid del profilo (fisso): se dato, salta la chiamata di risoluzione")
    ap.add_argument("--state", default="state.json")
    ap.add_argument("--count", type=int, default=15, help="quanti video recenti controllare")
    ap.add_argument("--max-new", type=int, default=3, help="max pubblicazioni per esecuzione")
    ap.add_argument("--dry-run", action="store_true", help="mostra cosa farebbe, senza pubblicare")
    ap.add_argument("--seed", action="store_true", help="forza la semina dello stato senza pubblicare")
    ap.add_argument("--backfill-youtube", type=int, default=0, metavar="N",
                    help="UNA TANTUM (avvio manuale): ripubblica su YouTube gli ultimi N "
                         "video gia' su IG, per popolare il canale nuovo. Default 0 = nessuno.")
    ap.add_argument("--no-hd", action="store_true",
                    default=os.environ.get("NO_HD", "").lower() in ("1", "true", "yes"),
                    help="salta il 1080p tikdownloader e usa direttamente il 720p TikHub")
    ap.add_argument("--tz", default=os.environ.get("TZ_NAME", "Europe/Rome"),
                    help="fuso orario per la finestra attiva (default: Europe/Rome)")
    ap.add_argument("--active-from", type=int,
                    default=int(os.environ.get("ACTIVE_FROM", "12")),
                    help="ora di inizio finestra attiva (default: 12)")
    ap.add_argument("--active-to", type=int,
                    default=int(os.environ.get("ACTIVE_TO", "24")),
                    help="ora di fine finestra attiva (default: 24 = mezzanotte)")
    ap.add_argument("--ignore-hours", action="store_true",
                    default=os.environ.get("IGNORE_ACTIVE_HOURS", "").lower()
                    in ("1", "true", "yes"),
                    help="ignora la finestra oraria (per test/avvii manuali)")
    args = ap.parse_args()

    if not args.tikhub_key or not args.zernio_key:
        print("❌ Mancano le chiavi (TIKHUB_API_KEY / ZERNIO_API_KEY o --tikhub-key/--zernio-key).",
              file=sys.stderr)
        sys.exit(1)

    # Finestra oraria: salta (a costo zero) fuori dagli orari attivi italiani.
    # Non si applica a --dry-run (ispezione) né con --ignore-hours (test/manuale).
    if not args.dry_run and not args.ignore_hours:
        active, now = within_active_hours(args.tz, args.active_from, args.active_to)
        if not active:
            print(f"⏸️  Ora {args.tz} {now}: fuori dalla finestra attiva "
                  f"({args.active_from:02d}:00–{args.active_to:02d}:00). "
                  f"Esco senza alcun controllo (zero costi).")
            return

    try:
        print(f"• Profilo: @{args.username}")
        if args.sec_uid:
            sec = args.sec_uid
            print("• sec_uid: da cache (nessuna chiamata di risoluzione)")
        else:
            sec = tikhub_sec_uid(args.tikhub_key, args.username)
        recent = tikhub_recent(args.tikhub_key, sec, args.count)
        print(f"• Video recenti recuperati: {len(recent)}")

        state = load_state(args.state)
        first_run = state is None
        if first_run:
            state = _empty_state()

        # Primo avvio o --seed: segna tutto come "gia' fatto" su TUTTE le
        # piattaforme, senza pubblicare nulla.
        if first_run or args.seed:
            if args.dry_run:
                print(f"\n[DRY-RUN] Primo avvio: seminerei {len(recent)} video su "
                      f"{list(TARGETS)} e pubblicherei 0 (nessuna scrittura).")
                return
            for v in recent:
                for p in TARGETS:
                    state[p].add(v["id"])
            save_state(args.state, state)
            print(f"\n🌱 Semina completata: {len(recent)} video segnati come gia' fatti "
                  f"su {list(TARGETS)}, 0 pubblicati.\n   Da ora pubblichero' solo i NUOVI.")
            return

        # Backfill una-tantum su YouTube: "dimentica" gli ultimi N su YT cosi'
        # vengono ripubblicati per popolare il canale nuovo (IG resta intatto).
        if args.backfill_youtube > 0:
            latest = sorted((v["id"] for v in recent), key=int, reverse=True)[:args.backfill_youtube]
            for vid in latest:
                state["youtube"].discard(vid)
            print(f"• Backfill YouTube: {len(latest)} video rimessi in coda per il canale.")

        # Per ogni video, quali piattaforme mancano ancora.
        def missing(vid):
            return [p for p in TARGETS if vid not in state[p]]

        todo = [v for v in recent if missing(v["id"])]
        todo.sort(key=lambda v: int(v["id"]))        # ordine cronologico (id crescente)
        print(f"• Video con almeno una piattaforma da fare: {len(todo)}")

        if not todo:
            print("\n✅ Niente di nuovo. Tutto a posto.")
            return

        if len(todo) > args.max_new:
            print(f"⚠️  Trovati {len(todo)}, ma ne lavoro al massimo {args.max_new} "
                  f"per sicurezza (i restanti al prossimo giro).")
            todo = todo[:args.max_new]

        if args.dry_run:
            print("\n[DRY-RUN] Lavorerei questi (nessuna azione reale):")
            for v in todo:
                print(f"   {v['id']}  → {', '.join(missing(v['id']))}  {v['desc'][:50]}")
            return

        accounts = {p: zernio_account(args.zernio_key, p) for p in TARGETS}
        print("• Account Zernio: " + ", ".join(f"{p}={accounts[p]}" for p in TARGETS))

        published = 0
        for v in todo:
            vid, caption = v["id"], v["desc"]
            targets = missing(vid)
            print(f"\n→ {vid}  «{caption[:45]}»  manca: {', '.join(targets)}")
            # Scarico + carico UNA VOLTA SOLA: lo stesso publicUrl serve IG e YT.
            try:
                tiktok_url = f"https://www.tiktok.com/@{args.username}/video/{vid}"
                print("   scarico…")
                blob, source = download_best_bytes(
                    tiktok_url, vid, args.tikhub_key, allow_hd=not args.no_hd)
                print(f"   [{source}] {len(blob)/1_000_000:.1f} MB → carico su Zernio…")
                public = zernio_upload(args.zernio_key, blob, f"{vid}.mp4")
            except Exception as e:
                print(f"   ❌ download/upload fallito: {e}\n   (riprovo al prossimo giro)")
                continue

            # Pubblico su ogni piattaforma mancante in modo indipendente: se una
            # fallisce, l'altra va comunque e quella fallita si riprova al giro dopo.
            for p in targets:
                try:
                    title = yt_title(caption) if p == "youtube" else None
                    post_id = zernio_post(args.zernio_key, public, caption,
                                          accounts[p], platform=p, title=title)
                    print(f"   ✅ {p}: pubblicato (post {post_id})")
                    state[p].add(vid)
                    save_state(args.state, state)     # salva subito dopo OGNI successo
                    published += 1
                except Exception as e:
                    print(f"   ❌ {p}: {e}\n   (riprovo al prossimo giro)")

        print(f"\n🏁 Fatto: {published} pubblicazioni totali, stato aggiornato.")

    except Exception as e:
        print(f"\n❌ Errore: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
