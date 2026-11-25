"""
Alerte ANT - surveillance pré-marché / séance / after-hours
-----------------------------------------------------------
Vérifie le cours toutes les 60 s (données Yahoo Finance, extended hours inclus)
et envoie une alerte Telegram + email si le cours bouge fortement sur une
fenêtre glissante (10 min par défaut).

Lancer :            python alerte_ant.py
Tester les envois : python alerte_ant.py --test
"""

import json
import os
import smtplib
import sys
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

NY = ZoneInfo("America/New_York")
DOSSIER = Path(__file__).resolve().parent
FICHIER_CONFIG = DOSSIER / "config.json"
FICHIER_LOG = DOSSIER / "alerte_ant.log"


# ---------------------------------------------------------------- utilitaires
def log(msg):
    ligne = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(ligne, flush=True)
    try:
        with open(FICHIER_LOG, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except OSError:
        pass


CONFIG_DEFAUT = {
    "ticker": "ANT",
    "fenetre_minutes": 10,
    "seuil_chute_pct": 3.0,
    "seuil_rebond_pct": 3.0,
    "intervalle_verif_secondes": 60,
    "pause_entre_alertes_minutes": 15,
    "telegram": {"actif": True, "token": "", "chat_id": ""},
    "email": {"actif": False, "smtp_serveur": "smtp.gmail.com", "smtp_port": 465,
              "expediteur": "", "mot_de_passe_application": "", "destinataire": ""},
}


def charger_config():
    """config.json (sur le PC) puis variables d'environnement (secrets GitHub)."""
    cfg = json.loads(json.dumps(CONFIG_DEFAUT))
    if FICHIER_CONFIG.exists():
        with open(FICHIER_CONFIG, encoding="utf-8") as f:
            for cle, val in json.load(f).items():
                if isinstance(val, dict):
                    cfg[cle].update(val)
                else:
                    cfg[cle] = val
    env = os.environ.get
    if env("TELEGRAM_BOT_TOKEN"):
        cfg["telegram"].update(actif=True, token=env("TELEGRAM_BOT_TOKEN").strip(),
                               chat_id=env("TELEGRAM_CHAT_ID", "").strip())
    if env("EMAIL_EXPEDITEUR") and env("EMAIL_MOT_DE_PASSE"):
        cfg["email"].update(actif=True, expediteur=env("EMAIL_EXPEDITEUR").strip(),
                            mot_de_passe_application=env("EMAIL_MOT_DE_PASSE").strip(),
                            destinataire=(env("EMAIL_DESTINATAIRE") or env("EMAIL_EXPEDITEUR")).strip())
    for cle in ("seuil_chute_pct", "seuil_rebond_pct", "fenetre_minutes"):
        if env(cle.upper()):
            cfg[cle] = float(env(cle.upper()))
    return cfg


def session_marche(maintenant_ny):
    """Renvoie 'pre-marché', 'séance', 'after-hours' ou None (marché fermé)."""
    if maintenant_ny.weekday() >= 5:  # samedi / dimanche
        return None
    h = maintenant_ny.hour + maintenant_ny.minute / 60
    if 4 <= h < 9.5:
        return "pré-marché"
    if 9.5 <= h < 16:
        return "séance"
    if 16 <= h < 20:
        return "after-hours"
    return None


# ---------------------------------------------------------------- notifications
def envoyer_telegram(cfg, texte):
    t = cfg.get("telegram", {})
    if not t.get("actif"):
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{t['token']}/sendMessage",
            data={"chat_id": t["chat_id"], "text": texte},
            timeout=15,
        )
        if r.status_code != 200:
            log(f"Telegram : erreur {r.status_code} {r.text[:200]}")
    except Exception as e:
        log(f"Telegram : échec ({e})")


def envoyer_email(cfg, sujet, texte):
    m = cfg.get("email", {})
    if not m.get("actif"):
        return
    try:
        msg = MIMEText(texte, "plain", "utf-8")
        msg["Subject"] = sujet
        msg["From"] = m["expediteur"]
        msg["To"] = m["destinataire"]
        with smtplib.SMTP_SSL(m["smtp_serveur"], int(m["smtp_port"]), timeout=20) as s:
            s.login(m["expediteur"], m["mot_de_passe_application"].replace(" ", ""))
            s.send_message(msg)
    except Exception as e:
        log(f"Email : échec ({e})")


def notifier(cfg, sujet, texte):
    log(f"ALERTE -> {sujet}")
    envoyer_telegram(cfg, f"{sujet}\n\n{texte}")
    envoyer_email(cfg, sujet, texte)


# ---------------------------------------------------------------- analyse
def recuperer_donnees(ticker):
    """Bougies 1 minute des 5 derniers jours, pré-marché et after-hours inclus."""
    df = yf.Ticker(ticker).history(period="5d", interval="1m", prepost=True)
    if df is None or df.empty:
        return None
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    df.index = df.index.tz_convert(NY)
    return df.dropna(subset=["Close"])


def cloture_veille(df):
    """Dernière clôture de séance normale (16h00 NY) avant aujourd'hui."""
    aujourd_hui = df.index[-1].date()
    passe = df[df.index.date < aujourd_hui]
    seance = passe[(passe.index.hour * 60 + passe.index.minute >= 570) &
                   (passe.index.hour < 16)]
    if seance.empty:
        return None
    return float(seance["Close"].iloc[-1])


def analyser(df, fenetre_min, seuil_chute, seuil_rebond):
    """Renvoie une liste d'alertes (type, sujet, détail) sur la fenêtre glissante."""
    dernier = df.index[-1]
    fenetre = df[df.index > dernier - timedelta(minutes=fenetre_min)]
    if len(fenetre) < 2:
        return [], {}

    prix = float(fenetre["Close"].iloc[-1])
    haut = float(fenetre["High"].max())
    bas = float(fenetre["Low"].min())
    t_haut = fenetre["High"].idxmax()
    t_bas = fenetre["Low"].idxmin()
    ecart_haut = (prix - haut) / haut * 100   # négatif si on a chuté
    ecart_bas = (prix - bas) / bas * 100      # positif si on a rebondi

    veille = cloture_veille(df)
    var_jour = (prix - veille) / veille * 100 if veille else None

    vol_fenetre = float(fenetre["Volume"].sum())
    jour = df[df.index.date == dernier.date()]
    nb_fen = max(len(jour) / max(len(fenetre), 1), 1)
    vol_moyen = float(jour["Volume"].sum()) / nb_fen if len(jour) else 0
    ratio_vol = vol_fenetre / vol_moyen if vol_moyen > 0 else None

    infos = {
        "prix": prix, "haut": haut, "bas": bas, "var_jour": var_jour,
        "ratio_vol": ratio_vol, "heure": dernier,
    }

    alertes = []
    # Chute rapide : le plus haut de la fenêtre arrive avant la baisse
    if ecart_haut <= -seuil_chute:
        retournement = var_jour is not None and var_jour > 0
        titre = "RETOURNEMENT BAISSIER" if retournement else "CHUTE RAPIDE"
        alertes.append(("chute", titre,
                        f"{ecart_haut:+.2f} % depuis le plus haut des {fenetre_min} min "
                        f"({haut:.2f} $ à {t_haut.strftime('%H:%M')})"))
    # Rebond rapide : le plus bas de la fenêtre arrive avant la hausse
    if ecart_bas >= seuil_rebond:
        retournement = var_jour is not None and var_jour < 0
        titre = "RETOURNEMENT HAUSSIER" if retournement else "REBOND RAPIDE"
        alertes.append(("rebond", titre,
                        f"{ecart_bas:+.2f} % depuis le plus bas des {fenetre_min} min "
                        f"({bas:.2f} $ à {t_bas.strftime('%H:%M')})"))
    return alertes, infos


def formater(ticker, session, titre, detail, infos):
    lignes = [
        f"Prix : {infos['prix']:.2f} $   ({session})",
        detail,
    ]
    if infos["var_jour"] is not None:
        lignes.append(f"Variation vs clôture veille : {infos['var_jour']:+.2f} %")
    if infos["ratio_vol"] is not None:
        lignes.append(f"Volume de la fenêtre : x{infos['ratio_vol']:.1f} la moyenne du jour")
    lignes.append(f"Dernière cotation : {infos['heure'].strftime('%H:%M')} heure de New York")
    if session != "séance":
        lignes.append("Hors séance : liquidité faible, utilise un ordre à cours limité.")
    return f"{ticker} - {titre}", "\n".join(lignes)


# ---------------------------------------------------------------- boucle
def main():
    cfg = charger_config()
    ticker = cfg["ticker"]

    if "--test" in sys.argv:
        notifier(cfg, f"{ticker} - TEST", "Si tu lis ce message, les alertes fonctionnent.")
        log("Test envoyé. Vérifie Telegram et ta boîte mail (et les spams).")
        return

    fenetre = int(cfg["fenetre_minutes"])
    seuil_chute = float(cfg["seuil_chute_pct"])
    seuil_rebond = float(cfg["seuil_rebond_pct"])
    intervalle = int(cfg["intervalle_verif_secondes"])
    pause = timedelta(minutes=int(cfg["pause_entre_alertes_minutes"]))
    derniere_alerte = {}  # type -> datetime

    log(f"Surveillance de {ticker} : fenêtre {fenetre} min, "
        f"chute {seuil_chute} %, rebond {seuil_rebond} %. Ctrl+C pour arrêter.")

    # Sur GitHub : chaque exécution s'arrête seule après DUREE_MAX_MINUTES
    duree_max = os.environ.get("DUREE_MAX_MINUTES")
    fin = datetime.now(NY) + timedelta(minutes=int(duree_max)) if duree_max else None

    while True:
        try:
            maintenant = datetime.now(NY)
            if fin and maintenant >= fin:
                log("Durée maximale atteinte, fin de cette exécution.")
                break
            session = session_marche(maintenant)
            if session is None:
                if fin and (maintenant.weekday() >= 5 or maintenant.hour >= 20):
                    log("Fin de journée de cotation, arrêt.")
                    break
                log("Marché fermé (ouvre à 4h00 heure de New York, lun-ven). Pause 5 min.")
                time.sleep(300)
                continue

            df = recuperer_donnees(ticker)
            if df is None:
                log("Pas de données reçues, nouvel essai.")
                time.sleep(intervalle)
                continue

            age = maintenant - df.index[-1]
            if age > timedelta(minutes=fenetre):
                log(f"Aucune transaction depuis {int(age.total_seconds() // 60)} min, on attend.")
                time.sleep(intervalle)
                continue

            alertes, infos = analyser(df, fenetre, seuil_chute, seuil_rebond)
            log(f"{session} | {infos.get('prix', 0):.2f} $ | "
                f"haut/bas {fenetre} min : {infos.get('haut', 0):.2f} / {infos.get('bas', 0):.2f}")

            for type_, titre, detail in alertes:
                precedente = derniere_alerte.get(type_)
                if precedente and maintenant - precedente < pause:
                    continue
                sujet, texte = formater(ticker, session, titre, detail, infos)
                notifier(cfg, sujet, texte)
                derniere_alerte[type_] = maintenant

        except KeyboardInterrupt:
            log("Arrêt demandé.")
            break
        except Exception as e:
            log(f"Erreur : {e}")
        time.sleep(intervalle)


if __name__ == "__main__":
    main()
