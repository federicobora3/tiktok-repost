#!/usr/bin/env python3
"""
repost_bot.py — Orchestratore: rileva i nuovi video di un profilo TikTok,
li scarica (TikHub) e li ripubblica su Instagram (Zernio), tenendo memoria
di cosa ha gia' fatto per non ripubblicare due volte.

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
    url = (f"{TIKHUB}/api/v1/tiktok/web/fetch_user_post"
           f"?secUid={urllib.parse.quote(sec_uid)}&cursor=0&count={count}")
    data = get_json(url, key)
    items = deep_find(data, ("itemList",)) or []
    out = []
    for it in items:
        vid = it.get("id") or it.get("aweme_id")
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
#  Zernio: carica + pubblica                                                   #
# --------------------------------------------------------------------------- #

def zernio_ig_account(key):
    data = get_json(f"{ZERNIO}/accounts", key)
    accounts = deep_find(data, ("accounts",)) or (data if isinstance(data, list) else [])
    for a in accounts:
        if isinstance(a, dict) and (a.get("platform") or "").lower() == "instagram":
            return a.get("accountId") or a.get("_id") or a.get("id")
    raise RuntimeError("Nessun account Instagram collegato su Zernio.")


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


def zernio_post(key, public_url, caption, account_id):
    payload = {
        "content": caption,
        "mediaItems": [{"type": "video", "url": public_url}],
        "platforms": [{"platform": "instagram", "accountId": account_id}],
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


def load_state(path):
    if not os.path.exists(path):
        return None                      # None = primo avvio
    try:
        with open(path) as f:
            return set(json.load(f).get("posted", []))
    except Exception:
        return set()


def save_state(path, posted):
    with open(path, "w") as f:
        json.dump({"posted": sorted(posted)}, f, indent=2)


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
            state = set()

        # Primo avvio o --seed: registra tutto come "gia' visto", non pubblica.
        if first_run or args.seed:
            if args.dry_run:
                print(f"\n[DRY-RUN] Primo avvio: seminerei {len(recent)} video come "
                      f"gia' visti e pubblicherei 0 (nessuna scrittura).")
                return
            for v in recent:
                state.add(v["id"])
            save_state(args.state, state)
            print(f"\n🌱 Semina completata: {len(state)} video segnati come gia' visti, "
                  f"0 pubblicati.\n   Da ora in poi pubblichero' solo i NUOVI video.")
            return

        # Nuovi = id non ancora nello stato. Ordine cronologico (id crescente).
        new = [v for v in recent if v["id"] not in state]
        new.sort(key=lambda v: int(v["id"]))
        print(f"• Video nuovi da pubblicare: {len(new)}")

        if not new:
            print("\n✅ Niente di nuovo. Tutto a posto.")
            return

        if len(new) > args.max_new:
            print(f"⚠️  Trovati {len(new)} nuovi, ma ne pubblico al massimo {args.max_new} "
                  f"per sicurezza (i restanti al prossimo giro).")
            new = new[:args.max_new]

        if args.dry_run:
            print("\n[DRY-RUN] Pubblicherei questi (nessuna azione reale):")
            for v in new:
                print(f"   {v['id']}  {v['desc'][:60]}")
            return

        ig_account = zernio_ig_account(args.zernio_key)
        print(f"• Account IG su Zernio: {ig_account}")

        published = 0
        for v in new:
            vid, caption = v["id"], v["desc"]
            print(f"\n→ {vid}  «{caption[:45]}»")
            try:
                url = tikhub_best_video_url(args.tikhub_key, vid)
                print("   scarico…")
                blob = download_bytes(url)
                print(f"   {len(blob)/1_000_000:.1f} MB → carico su Zernio…")
                public = zernio_upload(args.zernio_key, blob, f"{vid}.mp4")
                post_id = zernio_post(args.zernio_key, public, caption, ig_account)
                print(f"   ✅ pubblicato (post {post_id})")
                state.add(vid)
                save_state(args.state, state)     # salva subito dopo ogni successo
                published += 1
            except Exception as e:
                print(f"   ❌ errore su {vid}: {e}\n   (riprovero' al prossimo giro)")

        print(f"\n🏁 Fatto: {published} pubblicati, stato aggiornato.")

    except Exception as e:
        print(f"\n❌ Errore: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
