#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RadarIA — Générateur de package PC client
==========================================
Usage : python creer_package.py
        (ou lancé par CREER_PACKAGE_CLIENT.bat)

Génère un ZIP complet prêt à déployer sur un nouveau PC client.
Le ZIP contient TOUT : setup, surveillance, modèle IA, templates, config.
"""

import os, sys, json, zipfile, shutil
from pathlib import Path
from datetime import datetime

BASE = Path(__file__).parent
TEMPLATE_DIR = BASE / "_template"
OUTPUT_DIR   = BASE / "packages_generes"

def demander(label, defaut=""):
    val = input(f"  {label}{f' [{defaut}]' if defaut else ''} : ").strip()
    return val if val else defaut

RTSP_FORMATS = {
    "dahua":     "rtsp://{u}:{p}@{h}:{port}/cam/realmonitor?channel={ch}&subtype=1",
    "hikvision": "rtsp://{u}:{p}@{h}:{port}/Streaming/Channels/{ch}01",
    "generique": "rtsp://{u}:{p}@{h}:{port}/ch{ch}/main",
}

def generer_rtsp_cameras(nvr_ip, nvr_user, nvr_pwd, nb, port=554, marque="dahua"):
    noms = ["Entree principale", "Caisse", "Rayon 1", "Rayon 2", "Reserve", "Fond droite",
            "Camera 7", "Camera 8", "Camera 9", "Camera 10"]
    fmt = RTSP_FORMATS.get(marque.lower(), RTSP_FORMATS["dahua"])
    cameras = []
    for i in range(nb):
        ch = i + 1
        cameras.append({
            "index": i,
            "nom": noms[i] if i < len(noms) else f"Camera {ch}",
            "rtsp": fmt.format(u=nvr_user, p=nvr_pwd, h=nvr_ip, port=port, ch=ch)
        })
    return cameras

def main():
    print()
    print("=" * 55)
    print("  RadarIA — Création package nouveau PC client")
    print("=" * 55)
    print()

    # ── Infos client ──────────────────────────────────────────
    print("▶ INFORMATIONS CLIENT")
    nom         = demander("Nom du magasin")
    license_key = demander("License key (backoffice)")
    username    = demander("Username backoffice")
    email       = demander("Email contact")
    tel         = demander("Téléphone")
    adresse     = demander("Adresse")
    prix        = float(demander("Prix mensuel (€)", "80"))

    print()
    print("▶ CAMÉRAS")
    nb_cameras  = int(demander("Nombre de caméras", "4"))
    type_cam    = demander("Type (nvr / usb)", "nvr").lower()

    nvr_ip = nvr_user = nvr_pwd = ""
    cameras = []

    if type_cam == "nvr":
        nvr_ip   = demander("IP du NVR", "192.168.1.x")
        nvr_user = demander("Login NVR", "admin")
        nvr_pwd  = demander("Mot de passe NVR")
        nvr_port = int(demander("Port RTSP", "554"))
        marque   = demander("Marque NVR (dahua / hikvision / generique)", "dahua")
        cameras  = generer_rtsp_cameras(nvr_ip, nvr_user, nvr_pwd, nb_cameras, nvr_port, marque)
    else:
        for i in range(nb_cameras):
            cameras.append({"index": i, "nom": f"Camera {i+1}"})

    # ── Générer config.json ────────────────────────────────────
    config = {
        "nom_magasin":    nom,
        "backoffice_url": "https://backoffice.radaria.fr",
        "license_key":    license_key,
        "username":       username,
        "contact_email":  email,
        "contact_tel":    tel,
        "adresse":        adresse,
        "prix_mensuel":   prix,
        "nb_cameras":     nb_cameras,
        "cameras":        cameras
    }
    if type_cam == "nvr":
        config["nvr_ip"]       = nvr_ip
        config["nvr_user"]     = nvr_user
        config["nvr_password"] = nvr_pwd
        config["nvr_port"]     = nvr_port
        config["nvr_marque"]   = marque

    # ── Créer le ZIP ───────────────────────────────────────────
    OUTPUT_DIR.mkdir(exist_ok=True)
    nom_safe   = nom.replace(" ", "_").replace("/", "-")
    date_str   = datetime.now().strftime("%Y%m%d")
    zip_name   = f"RadarIA_{nom_safe}_{date_str}.zip"
    zip_path   = OUTPUT_DIR / zip_name

    print()
    print(f"▶ Génération du package : {zip_name}")

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        # Parcourir tous les fichiers du template
        for src in TEMPLATE_DIR.rglob("*"):
            if src.is_file() and src.name != "config_template.json":
                arc = src.relative_to(TEMPLATE_DIR)
                # Remplacer {{LICENSE_KEY}} dans RESTART_NUIT.bat
                if src.name == "RESTART_NUIT.bat":
                    content = src.read_text(encoding="utf-8")
                    content = content.replace("{{LICENSE_KEY}}", license_key)
                    z.writestr(str(arc), content.encode("utf-8"))
                else:
                    z.write(src, arc)
                print(f"   + {arc}")

        # Ajouter le config.json généré (remplace celui du template)
        config_bytes = json.dumps(config, ensure_ascii=False, indent=2).encode("utf-8")
        z.writestr("NOUVEAU_PC/config.json", config_bytes)
        z.writestr("RadarIA_PC/config.json", config_bytes)
        print(f"   + NOUVEAU_PC/config.json (généré)")
        print(f"   + RadarIA_PC/config.json (généré)")

    size_mb = zip_path.stat().st_size / 1024 / 1024
    print()
    print("=" * 55)
    print(f"  ✅ Package créé : {zip_path}")
    print(f"     Taille : {size_mb:.1f} MB")
    print()
    print("  CONTENU DU ZIP :")
    print("    NOUVEAU_PC\\         → setup + vérification")
    print("    RadarIA_PC\\         → surveillance + modèle IA")
    print("    RadarIA_PC\\templates → dashboard web")
    print()
    print("  INSTRUCTIONS PC CLIENT :")
    print("  1. Copier le ZIP sur le nouveau PC")
    print("  2. Extraire à la racine C:\\radaria-client\\")
    print("  3. Double-clic NOUVEAU_PC\\SETUP_RADARIA.bat")
    print("=" * 55)
    print()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n  Annulé.")
        sys.exit(0)
