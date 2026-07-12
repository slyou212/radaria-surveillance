#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════╗
║          GARDIEN RADARIA — Agent autonome            ║
║  Mode 1 : Installation  (premier lancement)          ║
║  Mode 2 : Surveillance  (toutes les 5 minutes)       ║
║  Mode 3 : Commandes     (ordres depuis backoffice)   ║
╚══════════════════════════════════════════════════════╝

Usage :
  python gardien_radaria.py              → mode auto (détection)
  python gardien_radaria.py --install    → forcer installation
  python gardien_radaria.py --surveille  → forcer surveillance
  python gardien_radaria.py --diagnostic → rapport complet immédiat
"""

import os, sys, json, time, socket, platform, subprocess, threading
import hashlib, logging, traceback, argparse, urllib.request, urllib.error
from pathlib import Path
from datetime import datetime, timedelta

# ─── Chemins ─────────────────────────────────────────────────────────────────
BASE         = Path(__file__).parent          # dossier du gardien

# RadarIA_PC peut être :
#   - dans le même dossier que le gardien (ZIP extrait à plat)
#   - ou dans le dossier parent (ancienne structure NOUVEAU_PC / RadarIA_PC)
def _trouver_surv_dir():
    candidats = [
        BASE / "RadarIA_PC",          # ZIP extrait à plat : tout dans un dossier
        BASE.parent / "RadarIA_PC",   # ancienne structure : NOUVEAU_PC + RadarIA_PC frères
    ]
    for c in candidats:
        if (c / "surveillance.py").exists():
            return c
    return candidats[0]  # défaut : même dossier

ROOT         = BASE.parent
SURV_DIR     = _trouver_surv_dir()
CONFIG_SURV  = SURV_DIR / "config.json"
CONFIG_GARD  = BASE / "gardien_config.json"
CONNAISSANCE = BASE / "base_connaissance.json"
JOURNAL      = BASE / "journal_gardien.json"
BRIEFING          = BASE / "BRIEFING_CLAUDE.md"
HANDOVER_OUT      = BASE / "HANDOVER_PC_principal.md"
PENDING_REPAIRS   = BASE / "pending_repairs.json"
LAST_DIAGNOSTIC   = BASE / "last_diagnostic.json"
BACKOFFICE        = "https://backoffice.radaria.fr"
GITHUB_SURV_URL = "https://raw.githubusercontent.com/slyou212/radaria-backoffice/main/surveillance.py"

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [GARDIEN] %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(BASE / "gardien.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("gardien")

# ─── Imports optionnels ──────────────────────────────────────────────────────
try:
    import anthropic as _anthropic
    HAS_CLAUDE = True
except ImportError:
    HAS_CLAUDE = False

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

try:
    import cv2
    HAS_CV2 = True
except ImportError:
    HAS_CV2 = False


# ══════════════════════════════════════════════════════════════════════════════
#   UTILITAIRES
# ══════════════════════════════════════════════════════════════════════════════

def charger_json(path, defaut):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return defaut

def sauver_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def maintenant():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def http_post(url, payload, timeout=10):
    try:
        data = json.dumps(payload).encode()
        req  = urllib.request.Request(url, data=data,
               headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.getcode()
    except Exception as e:
        return {"erreur": str(e)}, -1

def http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode()), r.getcode()
    except urllib.error.HTTPError as e:
        return {}, e.code
    except Exception as e:
        return {"erreur": str(e)}, -1

def run_cmd(cmd, timeout=30):
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, encoding="utf-8", errors="replace")
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


# ══════════════════════════════════════════════════════════════════════════════
#   CLAUDE API — Diagnostic intelligent
# ══════════════════════════════════════════════════════════════════════════════

def appeler_claude(probleme: str, contexte: dict) -> dict:
    """Appelle Claude API pour diagnostiquer un problème inconnu."""
    cfg = charger_json(CONFIG_GARD, {})
    api_key = cfg.get("claude_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")

    if not HAS_CLAUDE or not api_key:
        return {
            "diagnostic": "Claude API non disponible — rapport envoyé au backoffice.",
            "fix_code": None,
            "priorite": "haute"
        }

    briefing = ""
    if BRIEFING.exists():
        briefing = BRIEFING.read_text(encoding="utf-8")[:3000]

    prompt = f"""Tu es le Gardien RadarIA, agent de surveillance sur un PC client.
Voici le contexte du système :

{briefing}

PROBLEME DETECTE :
{probleme}

ETAT DU SYSTEME :
{json.dumps(contexte, ensure_ascii=False, indent=2)}

BASE DE CONNAISSANCE (problèmes déjà résolus) :
{json.dumps(charger_json(CONNAISSANCE, {}), ensure_ascii=False, indent=2)[:1000]}

Réponds en JSON avec :
{{
  "diagnostic": "explication du problème en 2-3 phrases",
  "fix_code": "code Python à exécuter pour corriger (ou null si pas de fix automatique possible)",
  "fix_description": "description du fix en français",
  "priorite": "basse|moyenne|haute|critique",
  "a_apprendre": true/false
}}"""

    try:
        client = _anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}]
        )
        texte = msg.content[0].text.strip()
        # Extraire JSON
        if "```json" in texte:
            texte = texte.split("```json")[1].split("```")[0].strip()
        elif "```" in texte:
            texte = texte.split("```")[1].split("```")[0].strip()
        return json.loads(texte)
    except Exception as e:
        log.error(f"Erreur Claude API : {e}")
        return {
            "diagnostic": f"Erreur appel Claude : {e}",
            "fix_code": None,
            "priorite": "haute"
        }


# ══════════════════════════════════════════════════════════════════════════════
#   CONNAISSANCE — Apprentissage
# ══════════════════════════════════════════════════════════════════════════════

def apprendre(cle_probleme: str, fix_code: str, fix_desc: str):
    base = charger_json(CONNAISSANCE, {})
    base[cle_probleme] = {
        "fix_code": fix_code,
        "fix_description": fix_desc,
        "date_apprentissage": maintenant(),
        "nb_applications": base.get(cle_probleme, {}).get("nb_applications", 0) + 1
    }
    sauver_json(CONNAISSANCE, base)
    log.info(f"[APPRENTISSAGE] Nouveau fix enregistre : {cle_probleme}")

def fix_connu(cle_probleme: str):
    base = charger_json(CONNAISSANCE, {})
    return base.get(cle_probleme)

def journaliser(type_evt: str, description: str, fix_applique: str = "", priorite: str = "info"):
    journal = charger_json(JOURNAL, [])
    journal.append({
        "date": maintenant(),
        "type": type_evt,
        "description": description,
        "fix_applique": fix_applique,
        "priorite": priorite
    })
    # Garder 500 dernières entrées
    if len(journal) > 500:
        journal = journal[-500:]
    sauver_json(JOURNAL, journal)


# ══════════════════════════════════════════════════════════════════════════════
#   DIAGNOSTICS SYSTEME
# ══════════════════════════════════════════════════════════════════════════════

def process_en_cours(nom):
    if not HAS_PSUTIL:
        code, out, _ = run_cmd(f'tasklist /FI "IMAGENAME eq python.exe" /FO CSV')
        return "python.exe" in out.lower()
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            cmdline = " ".join(p.info.get("cmdline") or [])
            if nom in cmdline:
                return True
        except Exception:
            pass
    return False

def etat_disk():
    if HAS_PSUTIL:
        d = psutil.disk_usage(str(ROOT))
        return {"total_gb": round(d.total/1e9,1),
                "libre_gb": round(d.free/1e9,1),
                "pct_utilise": d.percent}
    code, out, _ = run_cmd('wmic logicaldisk get freespace,size /format:csv')
    return {"detail": out[:200]}

def etat_reseau():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return {"ok": True, "ip_locale": ip}
    except Exception:
        return {"ok": False, "ip_locale": None}

def tester_backoffice(url):
    try:
        start = time.time()
        with urllib.request.urlopen(url + "/login", timeout=8) as r:
            ms = int((time.time()-start)*1000)
            return {"ok": True, "code": r.getcode(), "ms": ms}
    except urllib.error.HTTPError as e:
        return {"ok": True, "code": e.code, "ms": 0}
    except Exception as e:
        return {"ok": False, "detail": str(e)}

def tester_camera_rtsp(rtsp_url: str, timeout=5):
    if not HAS_CV2:
        return None
    try:
        cap = cv2.VideoCapture(rtsp_url)
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, timeout * 1000)
        ok = cap.isOpened()
        if ok:
            ret, _ = cap.read()
            cap.release()
            return ret
        cap.release()
        return False
    except Exception:
        return False

def etat_cameras(cfg):
    cameras = cfg.get("cameras", [])
    if not cameras:
        return []
    # Accepter dict ou list
    if isinstance(cameras, dict):
        cameras = list(cameras.values())
    resultats = []
    for i, cam in enumerate(cameras):
        # Compatibilité ancien format (index/rtsp) et nouveau (id/url)
        cam_id  = cam.get("id") or cam.get("index") or str(i + 1)
        cam_nom = cam.get("nom", "?")
        rtsp    = cam.get("url") or cam.get("rtsp")
        if rtsp:
            ok = tester_camera_rtsp(rtsp)
            resultats.append({"index": cam_id, "nom": cam_nom,
                               "ok": ok, "rtsp": rtsp[:60]+"..."})
        else:
            resultats.append({"index": cam_id, "nom": cam_nom,
                               "ok": None, "info": "pas de RTSP"})
    return resultats

def collecter_etat_complet():
    cfg = charger_json(CONFIG_SURV, {})
    bo_url = cfg.get("backoffice_url", BACKOFFICE)

    return {
        "date": maintenant(),
        "hostname": socket.gethostname(),
        "os": platform.system() + " " + platform.release(),
        "python": sys.version.split()[0],
        "surveillance_active": process_en_cours("surveillance.py"),
        "reseau": etat_reseau(),
        "disk": etat_disk(),
        "backoffice": tester_backoffice(bo_url),
        "cameras": etat_cameras(cfg),
        "license_key": cfg.get("license_key", ""),
        "nom_magasin": cfg.get("nom_magasin", ""),
    }


# ══════════════════════════════════════════════════════════════════════════════
#   CORRECTIONS AUTOMATIQUES
# ══════════════════════════════════════════════════════════════════════════════

def fix_relancer_surveillance():
    """Relance surveillance.py s'il s'est arrêté."""
    if not (SURV_DIR / "surveillance.py").exists():
        return False, "surveillance.py absent de RadarIA_PC"
    log.info("[FIX] Relancement de surveillance.py...")
    subprocess.Popen(
        [sys.executable, str(SURV_DIR / "surveillance.py")],
        cwd=str(SURV_DIR),
        creationflags=subprocess.CREATE_NEW_CONSOLE if os.name == "nt" else 0
    )
    time.sleep(5)
    return process_en_cours("surveillance.py"), "surveillance.py relance"

def fix_liberer_disk():
    """Supprime les anciens snapshots et logs si disk > 85%."""
    supprime = 0
    for dossier in [SURV_DIR / "alertes", SURV_DIR / "videos", SURV_DIR / "visiteurs"]:
        if dossier.exists():
            fichiers = sorted(dossier.glob("*"), key=lambda f: f.stat().st_mtime)
            # Garder les 100 plus récents seulement
            for f in fichiers[:-100]:
                try:
                    f.unlink()
                    supprime += 1
                except Exception:
                    pass
    return supprime > 0, f"{supprime} anciens fichiers supprimes"

def appliquer_fix_code(code_python: str) -> tuple:
    """Exécute du code Python généré par Claude pour corriger un problème."""
    try:
        namespace = {"BASE": BASE, "ROOT": ROOT, "SURV_DIR": SURV_DIR,
                     "CONFIG_SURV": CONFIG_SURV, "log": log,
                     "Path": Path, "subprocess": subprocess,
                     "sys": sys, "os": os, "json": json, "time": time}
        exec(compile(code_python, "<fix_claude>", "exec"), namespace)
        return True, "Fix execute avec succes"
    except Exception as e:
        return False, f"Erreur execution fix : {e}\n{traceback.format_exc()}"


# ══════════════════════════════════════════════════════════════════════════════
#   REPORTING BACKOFFICE
# ══════════════════════════════════════════════════════════════════════════════

def envoyer_status(etat: dict):
    cfg = charger_json(CONFIG_SURV, {})
    license_key = cfg.get("license_key", "")
    backoffice  = cfg.get("backoffice_url", BACKOFFICE)
    if not license_key:
        # Fallback : essayer gardien_config.json
        gard_cfg = charger_json(CONFIG_GARD, {})
        license_key = gard_cfg.get("license_key", "")
        if not license_key:
            log.warning(f"[STATUS] license_key MANQUANTE dans {CONFIG_SURV} — statut non envoye au backoffice")
            log.warning(f"[STATUS] Pour corriger : ajouter license_key dans {CONFIG_SURV}")
            return
        backoffice = gard_cfg.get("backoffice_url", backoffice)
    log.info(f"[STATUS] Envoi statut → {backoffice} (key={license_key[:8]}...)")

    payload = {
        "license_key": license_key,
        "agent_version": "1.0",
        "date": etat["date"],
        "hostname": etat["hostname"],
        "surveillance_active": etat["surveillance_active"],
        "cameras_ok": sum(1 for c in etat.get("cameras",[]) if c.get("ok")),
        "cameras_total": len(etat.get("cameras",[])),
        "disk_libre_gb": etat.get("disk",{}).get("libre_gb"),
        "backoffice_ms": etat.get("backoffice",{}).get("ms"),
        "reseau_ok": etat.get("reseau",{}).get("ok"),
    }
    http_post(f"{backoffice}/api/agent/status", payload)

def envoyer_incident(description: str, diagnostic: str, fix: str, priorite: str):
    cfg = charger_json(CONFIG_SURV, {})
    license_key = cfg.get("license_key", "")
    backoffice  = cfg.get("backoffice_url", BACKOFFICE)
    if not license_key:
        gard_cfg = charger_json(CONFIG_GARD, {})
        license_key = gard_cfg.get("license_key", "")
        if not license_key:
            return
        backoffice = gard_cfg.get("backoffice_url", backoffice)

    payload = {
        "license_key": license_key,
        "date": maintenant(),
        "hostname": socket.gethostname(),
        "description": description,
        "diagnostic": diagnostic,
        "fix_applique": fix,
        "priorite": priorite,
    }
    http_post(f"{backoffice}/api/agent/incident", payload)

def recuperer_commandes():
    cfg = charger_json(CONFIG_SURV, {})
    license_key = cfg.get("license_key", "")
    backoffice  = cfg.get("backoffice_url", BACKOFFICE)
    if not license_key:
        gard_cfg = charger_json(CONFIG_GARD, {})
        license_key = gard_cfg.get("license_key", "")
        if not license_key:
            return []
        backoffice = gard_cfg.get("backoffice_url", backoffice)
    data, code = http_get(f"{backoffice}/api/agent/commandes?license_key={license_key}")
    if code == 200 and isinstance(data, list):
        return data
    return []

# ══════════════════════════════════════════════════════════════════════════════
#   CHAT BACKOFFICE — Dialogue avec l'utilisateur
# ══════════════════════════════════════════════════════════════════════════════

def _get_lk_bo():
    """Retourne (license_key, backoffice_url) depuis la config."""
    cfg = charger_json(CONFIG_SURV, {})
    lk  = cfg.get("license_key","") or charger_json(CONFIG_GARD,{}).get("license_key","")
    bo  = cfg.get("backoffice_url", BACKOFFICE)
    return lk, bo

def recuperer_messages_chat():
    """Récupère les nouveaux messages utilisateur non lus depuis le backoffice."""
    lk, bo = _get_lk_bo()
    if not lk: return []
    data, code = http_get(f"{bo}/api/agent/chat/messages?license_key={lk}")
    return data if code == 200 and isinstance(data, list) else []

def envoyer_message_chat(message: str, msg_type: str = "message",
                          approbation_requise: bool = False, repair_id=None):
    """Envoie une réponse ou demande d'approbation au backoffice."""
    lk, bo = _get_lk_bo()
    if not lk: return None
    payload = {
        "license_key": lk,
        "message": message,
        "type": msg_type,
        "approbation_requise": approbation_requise,
        "repair_id": repair_id,
    }
    resp, code = http_post(f"{bo}/api/agent/chat/respond", payload)
    if code == 200:
        return resp.get("id")
    return None

def verifier_approbations():
    """Vérifie les approbations accordées par l'utilisateur."""
    lk, bo = _get_lk_bo()
    if not lk: return []
    data, code = http_get(f"{bo}/api/agent/chat/approval_status?license_key={lk}")
    return data if code == 200 and isinstance(data, list) else []

def traiter_message_chat(msg: dict):
    """Interprète un message utilisateur et répond en conséquence."""
    texte = msg.get("message","").lower().strip()
    log.info(f"[CHAT] Message utilisateur : {texte[:80]}")

    # ── Diagnostic / vérification ──────────────────────────────────────────
    if any(kw in texte for kw in ["verif", "diagnos", "état", "etat", "check", "tout", "status"]):
        etat = collecter_etat_complet()
        sauver_json(LAST_DIAGNOSTIC, etat)
        surv    = "✅ Active" if etat["surveillance_active"] else "❌ Arrêtée"
        cam_ok  = sum(1 for c in etat.get("cameras",[]) if c.get("ok"))
        cam_tot = len(etat.get("cameras",[]))
        disk    = etat.get("disk",{})
        bo_info = etat.get("backoffice",{})
        net     = etat.get("reseau",{})

        rapport = (
            f"🤖 Rapport — {etat['date']}\n\n"
            f"📡 Surveillance : {surv}\n"
            f"📷 Caméras : {cam_ok}/{cam_tot} opérationnelles\n"
            f"💾 Disque : {disk.get('libre_gb','?')} Go libres ({disk.get('pct_utilise','?')}% utilisé)\n"
            f"🌐 Réseau : {'✅ Connecté' if net.get('ok') else '❌ Hors ligne'} ({net.get('ip_locale','?')})\n"
            f"🏠 Backoffice : {'✅ OK' if bo_info.get('ok') else '❌ Inaccessible'} ({bo_info.get('ms','?')} ms)"
        )
        problemes = []
        if not etat["surveillance_active"]: problemes.append("surveillance arrêtée")
        if cam_ok < cam_tot: problemes.append(f"{cam_tot-cam_ok} caméra(s) hors ligne")
        if disk.get("pct_utilise",0) > 85: problemes.append("disque presque plein")

        if problemes:
            rapport += f"\n\n⚠️ Problèmes : {', '.join(problemes)}\nRépondez 'réparer' pour que je corrige."
        else:
            rapport += "\n\n✅ Tout fonctionne correctement."

        envoyer_message_chat(rapport, "diagnostic")

    # ── Réparer / corriger ─────────────────────────────────────────────────
    elif any(kw in texte for kw in ["répare", "repare", "repair", "corrige", "fix", "résoudre"]):
        etat = charger_json(LAST_DIAGNOSTIC, None) or collecter_etat_complet()
        sauver_json(LAST_DIAGNOSTIC, etat)
        actions = []
        if not etat.get("surveillance_active"): actions.append("relancer_surveillance")
        if etat.get("disk",{}).get("pct_utilise",0) > 85: actions.append("nettoyer_disk")

        if not actions:
            envoyer_message_chat("✅ Aucun problème à corriger — le système fonctionne bien.", "message")
        else:
            liste = "\n".join(f"• {a.replace('_',' ')}" for a in actions)
            msg_id = envoyer_message_chat(
                f"🔧 Je propose les réparations suivantes :\n{liste}\n\n"
                f"Cliquez ✅ Approuver pour autoriser.",
                "repair_request", approbation_requise=True
            )
            pending = charger_json(PENDING_REPAIRS, [])
            pending.append({"id": msg_id, "actions": actions, "ts": maintenant()})
            sauver_json(PENDING_REPAIRS, pending)

    # ── Relancer surveillance ─────────────────────────────────────────────
    elif any(kw in texte for kw in ["relance", "restart", "redémarre", "redemarr"]):
        msg_id = envoyer_message_chat(
            "🔄 Souhaitez-vous que je relance la surveillance maintenant ?\n\nCliquez ✅ Approuver pour confirmer.",
            "repair_request", approbation_requise=True
        )
        pending = charger_json(PENDING_REPAIRS, [])
        pending.append({"id": msg_id, "actions": ["relancer_surveillance"], "ts": maintenant()})
        sauver_json(PENDING_REPAIRS, pending)

    # ── Nettoyer disque ──────────────────────────────────────────────────
    elif any(kw in texte for kw in ["disk", "disque", "nettoy", "clean", "espace"]):
        msg_id = envoyer_message_chat(
            "🧹 Je peux supprimer les anciens fichiers (snapshots/vidéos — garde les 100 plus récents).\n\nCliquez ✅ Approuver pour autoriser.",
            "repair_request", approbation_requise=True
        )
        pending = charger_json(PENDING_REPAIRS, [])
        pending.append({"id": msg_id, "actions": ["nettoyer_disk"], "ts": maintenant()})
        sauver_json(PENDING_REPAIRS, pending)

    # ── Aide / commandes inconnues ────────────────────────────────────────
    else:
        envoyer_message_chat(
            "🤖 Gardien RadarIA — commandes disponibles :\n"
            "• 'vérifier tout' — diagnostic complet du système\n"
            "• 'réparer' — corriger les problèmes détectés\n"
            "• 'relancer surveillance' — redémarrer surveillance.py\n"
            "• 'nettoyer disque' — libérer de l'espace disque\n\n"
            "Je surveille automatiquement et vous alerterai si quelque chose ne va pas.",
            "message"
        )

def executer_reparations_approuvees():
    """Vérifie les approbations reçues et exécute les réparations autorisées."""
    pending = charger_json(PENDING_REPAIRS, [])
    if not pending: return

    approuves = verifier_approbations()
    # Construire set des id approuvés
    approuve_ids = {a["id"] for a in approuves if a.get("approuve") is True}
    refuse_ids   = {a["id"] for a in approuves if a.get("approuve") is False}

    remaining = []
    for repair in pending:
        rid = repair.get("id")
        if rid in approuve_ids:
            # Exécuter
            resultats = []
            for action in repair.get("actions", []):
                if action == "relancer_surveillance":
                    ok, msg_r = fix_relancer_surveillance()
                    resultats.append(f"{'✅' if ok else '❌'} Surveillance : {msg_r}")
                elif action == "nettoyer_disk":
                    ok, msg_r = fix_liberer_disk()
                    resultats.append(f"{'✅' if ok else '❌'} Disque : {msg_r}")
            texte_resultat = "\n".join(resultats) or "✅ Réparations terminées."
            envoyer_message_chat(
                f"🔧 Réparations effectuées :\n{texte_resultat}",
                "repair_result", repair_id=rid
            )
            journaliser("repair", "Réparations approuvées exécutées", texte_resultat, "info")
        elif rid in refuse_ids:
            envoyer_message_chat("❌ Réparation annulée par l'utilisateur.", "message", repair_id=rid)
        else:
            remaining.append(repair)  # toujours en attente

    sauver_json(PENDING_REPAIRS, remaining)


def executer_commande(cmd: dict):
    action = cmd.get("action", "")
    log.info(f"[COMMANDE] Recue depuis backoffice : {action}")

    if action == "redemarrer_surveillance":
        ok, msg = fix_relancer_surveillance()
        return {"ok": ok, "message": msg}

    elif action == "diagnostic":
        etat = collecter_etat_complet()
        return {"ok": True, "etat": etat}

    elif action == "fix_code":
        code = cmd.get("code", "")
        if code:
            ok, msg = appliquer_fix_code(code)
            return {"ok": ok, "message": msg}
        return {"ok": False, "message": "Pas de code fourni"}

    elif action == "mettre_a_jour":
        # Télécharge la dernière version de surveillance.py depuis GitHub puis redémarre
        url = cmd.get("url", GITHUB_SURV_URL)
        try:
            dest = SURV_DIR / "surveillance.py"
            urllib.request.urlretrieve(url, str(dest))
            log.info(f"[MAJ] surveillance.py mis a jour depuis {url}")
            # Redémarrer surveillance pour appliquer immédiatement
            ok, msg = fix_relancer_surveillance()
            return {"ok": True, "message": f"surveillance.py mis a jour + relance : {msg}"}
        except Exception as e:
            return {"ok": False, "message": str(e)}

    return {"ok": False, "message": f"Commande inconnue : {action}"}


# ══════════════════════════════════════════════════════════════════════════════
#   MODE SURVEILLANCE — Boucle principale
# ══════════════════════════════════════════════════════════════════════════════

def cycle_surveillance():
    """Un cycle complet de vérification. Appelé toutes les 5 minutes."""
    log.info("--- Cycle de surveillance ---")
    etat = collecter_etat_complet()
    problemes = []

    # ── 1. Surveillance.py en cours ? ────────────────────────────────────────
    if not etat["surveillance_active"]:
        cle = "surveillance_arretee"
        fix = fix_connu(cle)
        if fix:
            log.info(f"[FIX CONNU] {cle}")
            exec_ok, msg = fix_relancer_surveillance()
            journaliser("auto_fix", "surveillance.py relancee (fix connu)", msg, "moyenne")
            envoyer_incident("surveillance.py arretee", "Processus non detecte", msg, "moyenne")
        else:
            ok, msg = fix_relancer_surveillance()
            if ok:
                apprendre(cle, "# fix integre — voir fix_relancer_surveillance()", "Relancer surveillance.py")
                journaliser("auto_fix", "surveillance.py relancee", msg, "moyenne")
            else:
                problemes.append(("surveillance_arretee", "surveillance.py arrete et non relance"))

    # ── 2. Caméras ───────────────────────────────────────────────────────────
    cameras_ko = [c for c in etat.get("cameras", []) if c.get("ok") is False]
    if cameras_ko:
        noms = ", ".join(c["nom"] for c in cameras_ko)
        problemes.append(("cameras_deconnectees", f"Cameras perdues : {noms}"))
        journaliser("alerte", f"Cameras deconnectees : {noms}", "", "haute")

    # ── 3. Disk ──────────────────────────────────────────────────────────────
    disk = etat.get("disk", {})
    if disk.get("pct_utilise", 0) > 85:
        cle = "disk_presque_plein"
        fix = fix_connu(cle)
        if fix:
            appliquer_fix_code(fix["fix_code"])
            journaliser("auto_fix", "Disk nettoye (fix connu)", fix["fix_description"], "haute")
        else:
            ok, msg = fix_liberer_disk()
            if ok:
                apprendre(cle, "# fix integre — voir fix_liberer_disk()", "Supprimer anciens fichiers alertes/videos")
                journaliser("auto_fix", "Disk nettoye", msg, "haute")

    # ── 4. Backoffice inaccessible ────────────────────────────────────────────
    if not etat["backoffice"].get("ok"):
        detail = etat["backoffice"].get("detail", "timeout")
        journaliser("alerte", f"Backoffice inaccessible : {detail}", "", "haute")
        # Pas de fix possible côté PC — escalade seulement

    # ── 5. Problèmes inconnus → Claude API ───────────────────────────────────
    for cle, description in problemes:
        fix = fix_connu(cle)
        if fix and fix.get("fix_code"):
            log.info(f"[FIX CONNU] Application : {cle}")
            ok, msg = appliquer_fix_code(fix["fix_code"])
            journaliser("auto_fix", description, msg, "moyenne")
            envoyer_incident(description, fix.get("fix_description",""), msg, "moyenne")
            base = charger_json(CONNAISSANCE, {})
            if cle in base:
                base[cle]["nb_applications"] = base[cle].get("nb_applications", 0) + 1
                sauver_json(CONNAISSANCE, base)
        else:
            log.warning(f"[INCONNU] {description} — appel Claude API...")
            rep = appeler_claude(description, etat)
            fix_code = rep.get("fix_code")
            fix_desc = rep.get("fix_description", rep.get("diagnostic",""))
            priorite = rep.get("priorite", "haute")

            fix_msg = "Aucun fix automatique — escalade"
            if fix_code:
                ok, fix_msg = appliquer_fix_code(fix_code)
                if ok and rep.get("a_apprendre"):
                    apprendre(cle, fix_code, fix_desc)

            journaliser("incident_claude", description, fix_msg, priorite)
            envoyer_incident(description, rep.get("diagnostic",""), fix_msg, priorite)

    # ── 6. Commandes depuis backoffice ────────────────────────────────────────
    commandes = recuperer_commandes()
    for cmd in commandes:
        resultat = executer_commande(cmd)
        log.info(f"[COMMANDE] {cmd.get('action')} → {resultat}")

    # ── 7. Heartbeat status ──────────────────────────────────────────────────
    envoyer_status(etat)

    # ── 8. Messages chat utilisateur ──────────────────────────────────────────
    msgs_chat = recuperer_messages_chat()
    for msg in msgs_chat:
        try:
            traiter_message_chat(msg)
        except Exception as e:
            log.error(f"[CHAT] Erreur traitement message : {e}")

    # ── 9. Réparations approuvées ─────────────────────────────────────────────
    try:
        executer_reparations_approuvees()
    except Exception as e:
        log.error(f"[CHAT] Erreur réparations approuvées : {e}")

    # ── 10. Alertes proactives via chat ───────────────────────────────────────
    # Si problème critique détecté → demande d'approbation automatique via chat
    if not etat["surveillance_active"]:
        # Vérifier si une repair_request est déjà en attente
        pending = charger_json(PENDING_REPAIRS, [])
        has_surv = any("relancer_surveillance" in r.get("actions",[]) for r in pending)
        if not has_surv:
            msg_id = envoyer_message_chat(
                "🚨 Alerte automatique : la surveillance est arrêtée !\n"
                "Dois-je la relancer ? Cliquez ✅ Approuver pour confirmer.",
                "repair_request", approbation_requise=True
            )
            if msg_id:
                pending.append({"id": msg_id, "actions": ["relancer_surveillance"], "ts": maintenant()})
                sauver_json(PENDING_REPAIRS, pending)

    log.info(f"Cycle termine. Surveillance={'OK' if etat['surveillance_active'] else 'KO'}, "
             f"Cameras={sum(1 for c in etat.get('cameras',[]) if c.get('ok'))}/{len(etat.get('cameras',[]))}")


# ══════════════════════════════════════════════════════════════════════════════
#   MODE INSTALLATION
# ══════════════════════════════════════════════════════════════════════════════

def mode_installation():
    """Premier lancement : vérifie, installe, configure, génère le HANDOVER."""
    print("\n" + "="*58)
    print("  GARDIEN RADARIA — Mode Installation")
    print("="*58 + "\n")

    rapport = {
        "date": maintenant(),
        "hostname": socket.gethostname(),
        "os": platform.system() + " " + platform.release(),
        "etapes": [],
        "problemes": [],
        "actions_restantes": [],
    }

    def etape(nom, ok, detail=""):
        statut = "[OK]" if ok else "[ERREUR]"
        print(f"  {statut} {nom}")
        if detail:
            print(f"         {detail}")
        rapport["etapes"].append({"nom": nom, "ok": ok, "detail": detail})
        if not ok:
            rapport["problemes"].append(nom)

    # ── Python ───────────────────────────────────────────────────────────────
    py_ok = sys.version_info >= (3, 8)
    etape("Python", py_ok, sys.version.split()[0])

    # ── Dépendances ──────────────────────────────────────────────────────────
    print("\n  Installation dependances...")
    deps = ["opencv-python", "requests", "psutil", "flask",
            "ultralytics", "anthropic"]
    for dep in deps:
        code, _, err = run_cmd(f'pip install {dep} --quiet --no-warn-script-location', timeout=120)
        etape(f"pip {dep}", code == 0, err[:80] if err else "")

    # ── Fichiers surveillance ─────────────────────────────────────────────────
    surv_py = SURV_DIR / "surveillance.py"
    yolo    = SURV_DIR / "yolov8n.pt"
    cfg_ok  = False

    etape("surveillance.py present", surv_py.exists(), str(SURV_DIR))
    etape("yolov8n.pt present", yolo.exists(), str(SURV_DIR))

    if CONFIG_SURV.exists():
        try:
            cfg = json.loads(CONFIG_SURV.read_text(encoding="utf-8"))
            cfg_ok = bool(cfg.get("license_key"))
            etape("config.json valide", cfg_ok,
                  f"license_key={'OK' if cfg_ok else 'MANQUANTE'} / {cfg.get('nom_magasin','?')}")
        except Exception as e:
            etape("config.json valide", False, str(e))
            rapport["actions_restantes"].append("Corriger config.json (JSON invalide)")
    else:
        etape("config.json present", False, str(CONFIG_SURV))
        rapport["actions_restantes"].append("Déposer config.json dans RadarIA_PC\\")

    # ── Réseau ────────────────────────────────────────────────────────────────
    net = etat_reseau()
    etape("Reseau connecte", net["ok"], net.get("ip_locale",""))

    # ── Backoffice ────────────────────────────────────────────────────────────
    cfg = charger_json(CONFIG_SURV, {})
    bo  = tester_backoffice(cfg.get("backoffice_url", BACKOFFICE))
    etape("Backoffice accessible", bo["ok"],
          f"HTTP {bo.get('code','?')} en {bo.get('ms','?')} ms")

    # ── Caméras ──────────────────────────────────────────────────────────────
    print("\n  Test cameras (peut prendre 30s)...")
    cams = etat_cameras(cfg)
    nb_ok = sum(1 for c in cams if c.get("ok"))
    nb_total = len(cams)
    etape(f"Cameras ({nb_ok}/{nb_total})", nb_ok == nb_total and nb_total > 0,
          " | ".join(f"{c['nom']} {'[OK]' if c.get('ok') else '[ECHEC]'}" for c in cams))

    if nb_ok < nb_total:
        rapport["actions_restantes"].append(
            f"Vérifier flux RTSP des caméras KO : "
            + ", ".join(c["nom"] for c in cams if not c.get("ok"))
        )

    # ── Lancer surveillance ──────────────────────────────────────────────────
    if surv_py.exists() and cfg_ok and net["ok"]:
        print("\n  Lancement surveillance.py...")
        ok, msg = fix_relancer_surveillance()
        etape("Surveillance lancee", ok, msg)
        if not ok:
            rapport["actions_restantes"].append("Lancer manuellement : python surveillance.py")
    else:
        etape("Surveillance lancee", False, "prerequis manquants")
        rapport["actions_restantes"].append("Résoudre les erreurs ci-dessus puis relancer le Gardien")

    # ── Démarrage auto ────────────────────────────────────────────────────────
    startup = (Path(os.environ.get("APPDATA","")) /
               "Microsoft/Windows/Start Menu/Programs/Startup")
    gardien_bat = startup / "RadarIA_Gardien.bat"
    if startup.exists() and not gardien_bat.exists():
        bat_content = f"""@echo off
timeout /t 30 /nobreak >nul
start "Gardien RadarIA" /min python "{Path(__file__).resolve()}" --surveille
"""
        gardien_bat.write_text(bat_content, encoding="utf-8")
        etape("Demarrage auto installe", True, str(gardien_bat))
    elif gardien_bat.exists():
        etape("Demarrage auto installe", True, "deja present")
    else:
        etape("Demarrage auto installe", False, "dossier Startup introuvable")
        rapport["actions_restantes"].append("Installer manuellement RadarIA_Gardien.bat dans le dossier Démarrage")

    # ── Générer HANDOVER ─────────────────────────────────────────────────────
    generer_handover(rapport)
    print(f"\n  [OK] Rapport genere : {HANDOVER_OUT}")

    problemes_total = len(rapport["problemes"])
    print("\n" + "="*58)
    if problemes_total == 0:
        print("  INSTALLATION COMPLETE — Systeme operationnel")
    else:
        print(f"  INSTALLATION PARTIELLE — {problemes_total} point(s) a resoudre")
        for a in rapport["actions_restantes"]:
            print(f"    -> {a}")
    print("="*58 + "\n")

    # Sauvegarder l'état de base
    sauver_json(BASE / "etat_installation.json", rapport)


def generer_handover(rapport: dict):
    """Génère le fichier HANDOVER à transmettre à Rachid."""
    cfg = charger_json(CONFIG_SURV, {})
    cams = etat_cameras(cfg)

    lignes = [
        f"# RAPPORT INSTALLATION — {cfg.get('nom_magasin', 'Client')}",
        f"### Genere automatiquement par le Gardien RadarIA",
        f"",
        f"**Date :** {rapport['date']}",
        f"**PC :** {rapport['hostname']} ({rapport['os']})",
        f"",
        f"---",
        f"",
        f"## Etat final",
        f"",
        f"| Element | Statut |",
        f"|---|---|",
    ]
    for e in rapport["etapes"]:
        statut = "OK" if e["ok"] else "ERREUR"
        detail = f" — {e['detail']}" if e.get("detail") else ""
        lignes.append(f"| {e['nom']} | {statut}{detail} |")

    lignes += [
        f"",
        f"---",
        f"",
        f"## Problemes rencontres",
        f"",
    ]
    if rapport["problemes"]:
        for p in rapport["problemes"]:
            lignes.append(f"- {p}")
    else:
        lignes.append("Aucun — installation complete")

    lignes += [
        f"",
        f"## Actions restantes",
        f"",
    ]
    if rapport["actions_restantes"]:
        for a in rapport["actions_restantes"]:
            lignes.append(f"- {a}")
    else:
        lignes.append("Aucune — systeme operationnel")

    lignes += [
        f"",
        f"---",
        f"",
        f"*Transmis automatiquement par le Gardien RadarIA*",
        f"*Contact : rachid_sliman@yahoo.fr | {BACKOFFICE}*",
    ]

    HANDOVER_OUT.write_text("\n".join(lignes), encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════════════
#   POINT D'ENTREE
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Gardien RadarIA")
    parser.add_argument("--install",    action="store_true", help="Forcer mode installation")
    parser.add_argument("--surveille",  action="store_true", help="Forcer mode surveillance (1 cycle)")
    parser.add_argument("--daemon",     action="store_true", help="Boucle infinie toutes les 5 min")
    parser.add_argument("--diagnostic", action="store_true", help="Rapport complet immédiat")
    args = parser.parse_args()

    if args.install:
        mode_installation()

    elif args.diagnostic:
        etat = collecter_etat_complet()
        print(json.dumps(etat, ensure_ascii=False, indent=2))

    elif args.daemon:
        log.info("Gardien RadarIA — mode daemon (toutes les 5 min)")
        while True:
            try:
                cycle_surveillance()
            except Exception as e:
                log.error(f"Erreur cycle : {e}\n{traceback.format_exc()}")
            time.sleep(300)  # 5 minutes

    elif args.surveille:
        cycle_surveillance()

    else:
        # Auto-détection du mode
        etat_install = BASE / "etat_installation.json"
        if not etat_install.exists():
            log.info("Premier lancement detecte → mode installation")
            mode_installation()
        else:
            log.info("Installation deja effectuee → mode surveillance")
            cycle_surveillance()


if __name__ == "__main__":
    main()
