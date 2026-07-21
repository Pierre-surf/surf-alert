#!/usr/bin/env python3
"""
Surf Alert Normandie
====================

Vérifie chaque semaine les conditions de surf sur six spots de la côte
normande (Calvados 14 / Seine-Maritime 76) à partir des API publiques
Open-Meteo (Marine + Forecast), et envoie un e-mail uniquement si au
moins un spot remplit simultanément tous les critères définis dans
CRITERIA.

Aucune clé d'API n'est nécessaire. Seuls les identifiants d'envoi de
mail (EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD, et éventuellement
SMTP_SERVER / SMTP_PORT) sont attendus comme variables d'environnement,
injectées en production par GitHub Actions depuis les secrets du dépôt.

Mode test : si la variable d'environnement TEST_MODE vaut "true", le
script saute le garde-fou saisonnier et les appels API, fabrique un
résultat "idéal" avec des valeurs fictives, et envoie un e-mail de test
via la même fonction send_email() que le mode normal.
"""

import os
import sys
import smtplib
import ssl
from datetime import datetime, date, timezone
from email.mime.text import MIMEText

import requests

# ---------------------------------------------------------------------------
# 1. Fenêtre saisonnière
# ---------------------------------------------------------------------------
# Le script ne fait aucun appel réseau en dehors de cette période.
SEASON_START = (5, 15)   # (mois, jour) : 15 mai
SEASON_END = (10, 30)    # (mois, jour) : 30 octobre


def is_in_season(today: date) -> bool:
    start = date(today.year, *SEASON_START)
    end = date(today.year, *SEASON_END)
    return start <= today <= end


# ---------------------------------------------------------------------------
# 2. Spots surveillés
# ---------------------------------------------------------------------------
# `facing` : orientation de côte en degrés (0 = Nord, 90 = Est, 180 = Sud,
# 270 = Ouest). C'est la direction vers laquelle le spot est « ouvert » sur
# la mer. Elle sert à déterminer si un vent est onshore (il vient de cette
# direction, il « rentre » sur la plage) ou offshore (il vient de la
# direction opposée, il repart vers le large).
#
# Les coordonnées et orientations ci-dessous sont des valeurs de départ
# raisonnables mais approximatives : à ajuster selon la configuration
# réelle de chaque plage (caps, digues, embouchures) si les alertes reçues
# ne correspondent pas à l'expérience de terrain.
SPOTS = [
    {
        "name": "Trouville-sur-Mer",
        "department": "14",
        "lat": 49.3667,
        "lon": 0.0833,
        "facing": 340,  # baie de Seine, ouverture Nord/Nord-Ouest
    },
    {
        "name": "Étretat",
        "department": "76",
        "lat": 49.7072,
        "lon": 0.2042,
        "facing": 320,  # Nord-Ouest
    },
    {
        "name": "Antifer / Le Tilleul",
        "department": "76",
        "lat": 49.6975,
        "lon": 0.1594,
        "facing": 320,  # Nord-Ouest, proche du cap d'Antifer
    },
    {
        "name": "Yport",
        "department": "76",
        "lat": 49.7364,
        "lon": 0.3106,
        "facing": 350,  # Nord, plage encaissée entre deux caps
    },
    {
        "name": "Pourville-sur-Mer",
        "department": "76",
        "lat": 49.9270,
        "lon": 1.0210,
        "facing": 350,  # Nord
    },
    {
        "name": "Mers-les-Bains",
        "department": "76",
        "lat": 50.0667,
        "lon": 1.3833,
        "facing": 10,   # Nord/Nord-Est, extrémité orientale de la zone
    },
]

# ---------------------------------------------------------------------------
# 3. Critères de qualité de vagues
# ---------------------------------------------------------------------------
CRITERIA = {
    "min_swell_height_m": 0.7,       # houle primaire minimale
    "max_secondary_swell_m": 0.4,    # houle secondaire maximale (clapot croisé)
    "min_swell_period_s": 6.0,       # période de houle minimale
    "min_water_temp_c": 17.5,        # température de l'eau minimale
    "max_wind_onshore_kn": 10.0,     # vent de mer : tolérance faible
    "max_wind_offshore_kn": 20.0,    # vent de terre : tolérance élevée
    "onshore_sector_deg": 90,        # demi-cercle classé "onshore" autour de facing
}

MARINE_API_URL = "https://marine-api.open-meteo.com/v1/marine"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"
REQUEST_TIMEOUT_S = 15


# ---------------------------------------------------------------------------
# 4. Appels API
# ---------------------------------------------------------------------------
def fetch_marine_data(lat: float, lon: float) -> dict:
    """Récupère houle primaire, houle secondaire, période et température
    de l'eau depuis l'API Marine d'Open-Meteo, pour l'heure la plus proche
    de maintenant."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": ",".join([
            "swell_wave_height",
            "swell_wave_period",
            "secondary_swell_wave_height",
            "sea_surface_temperature",
        ]),
        "timezone": "UTC",
        "forecast_days": 2,
    }
    resp = requests.get(MARINE_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    idx = _nearest_hour_index(data["hourly"]["time"])
    hourly = data["hourly"]
    return {
        "swell_height_m": hourly["swell_wave_height"][idx],
        "swell_period_s": hourly["swell_wave_period"][idx],
        "secondary_swell_m": hourly["secondary_swell_wave_height"][idx],
        "water_temp_c": hourly["sea_surface_temperature"][idx],
    }


def fetch_forecast_data(lat: float, lon: float) -> dict:
    """Récupère la vitesse et la direction du vent à 10 m depuis l'API
    Forecast d'Open-Meteo, pour l'heure la plus proche de maintenant."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_days": 2,
    }
    resp = requests.get(FORECAST_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    data = resp.json()
    idx = _nearest_hour_index(data["hourly"]["time"])
    hourly = data["hourly"]
    return {
        "wind_speed_kn": hourly["wind_speed_10m"][idx],
        "wind_direction_deg": hourly["wind_direction_10m"][idx],
    }


def _nearest_hour_index(time_strings: list) -> int:
    """Renvoie l'indice de l'échéance horaire la plus proche de l'heure
    UTC actuelle dans une série temporelle Open-Meteo (format ISO 8601)."""
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    now_naive = now.replace(tzinfo=None)
    best_idx, best_delta = 0, None
    for i, ts in enumerate(time_strings):
        t = datetime.fromisoformat(ts)
        delta = abs((t - now_naive).total_seconds())
        if best_delta is None or delta < best_delta:
            best_idx, best_delta = i, delta
    return best_idx


# ---------------------------------------------------------------------------
# 5. Classification du vent (onshore / offshore)
# ---------------------------------------------------------------------------
def angular_difference(a: float, b: float) -> float:
    """Écart angulaire absolu entre deux directions en degrés, ramené à
    l'intervalle [0, 180]."""
    d = abs(a - b) % 360
    return d if d <= 180 else 360 - d


def classify_wind(wind_direction_deg: float, facing_deg: float) -> str:
    """Classe le vent en 'onshore' (vient de la mer, dégrade les vagues)
    ou 'offshore' (vient de la terre, lisse les vagues), en comparant sa
    direction d'origine à l'orientation de côte du spot (facing)."""
    diff = angular_difference(wind_direction_deg, facing_deg)
    return "onshore" if diff <= CRITERIA["onshore_sector_deg"] else "offshore"


# ---------------------------------------------------------------------------
# 6. Évaluation d'un spot
# ---------------------------------------------------------------------------
def evaluate_spot(spot: dict) -> dict:
    """Interroge les deux API pour un spot donné, applique les cinq
    critères, et renvoie un dictionnaire de résultat détaillé (utile pour
    les logs et pour le corps de l'e-mail)."""
    marine = fetch_marine_data(spot["lat"], spot["lon"])
    forecast = fetch_forecast_data(spot["lat"], spot["lon"])

    wind_category = classify_wind(forecast["wind_direction_deg"], spot["facing"])
    wind_limit = (
        CRITERIA["max_wind_onshore_kn"]
        if wind_category == "onshore"
        else CRITERIA["max_wind_offshore_kn"]
    )

    checks = {
        "houle_primaire": marine["swell_height_m"] > CRITERIA["min_swell_height_m"],
        "houle_secondaire": marine["secondary_swell_m"] < CRITERIA["max_secondary_swell_m"],
        "periode_houle": marine["swell_period_s"] > CRITERIA["min_swell_period_s"],
        "temperature_eau": marine["water_temp_c"] >= CRITERIA["min_water_temp_c"],
        "vent": forecast["wind_speed_kn"] < wind_limit,
    }

    result = {
        "spot": spot["name"],
        "department": spot["department"],
        "ok": all(checks.values()),
        "checks": checks,
        "values": {
            "houle_primaire_m": round(marine["swell_height_m"], 2),
            "houle_secondaire_m": round(marine["secondary_swell_m"], 2),
            "periode_houle_s": round(marine["swell_period_s"], 1),
            "temperature_eau_c": round(marine["water_temp_c"], 1),
            "vent_kn": round(forecast["wind_speed_kn"], 1),
            "vent_direction_deg": round(forecast["wind_direction_deg"]),
            "vent_categorie": wind_category,
        },
    }
    return result


# ---------------------------------------------------------------------------
# 7. Notification par e-mail
# ---------------------------------------------------------------------------
def build_email_body(good_spots: list) -> str:
    lines = [
        "Surf Alert Normandie — au moins un spot remplit tous les critères :",
        "",
    ]
    for r in good_spots:
        v = r["values"]
        lines.append(f"• {r['spot']} ({r['department']})")
        lines.append(
            f"    Houle : {v['houle_primaire_m']} m primaire / "
            f"{v['houle_secondaire_m']} m secondaire, période {v['periode_houle_s']} s"
        )
        lines.append(
            f"    Eau : {v['temperature_eau_c']} °C — "
            f"Vent : {v['vent_kn']} nds ({v['vent_categorie']}, {v['vent_direction_deg']}°)"
        )
        lines.append("")
    lines.append("Généré automatiquement par surf_check.py (GitHub Actions).")
    return "\n".join(lines)


def send_email(subject: str, body: str) -> None:
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]
    email_password = os.environ["EMAIL_PASSWORD"]
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    context = ssl.create_default_context()
    with smtplib.SMTP(smtp_server, smtp_port, timeout=REQUEST_TIMEOUT_S) as server:
        server.starttls(context=context)
        server.login(email_from, email_password)
        server.sendmail(email_from, [email_to], msg.as_string())


# ---------------------------------------------------------------------------
# 8. Mode test
# ---------------------------------------------------------------------------
def run_test_mode() -> int:
    """Fabrique un résultat idéal avec des valeurs fictives qui remplissent
    volontairement tous les critères, puis envoie un e-mail de test via la
    même fonction send_email() que le mode normal (donc avec la même
    chaîne SMTP, les mêmes secrets, le même serveur). Ne fait aucun appel
    réseau vers les API Open-Meteo."""
    print("MODE TEST activé")

    fake_result = {
        "spot": "Étretat (données simulées)",
        "department": "76",
        "ok": True,
        "checks": {
            "houle_primaire": True,
            "houle_secondaire": True,
            "periode_houle": True,
            "temperature_eau": True,
            "vent": True,
        },
        "values": {
            "houle_primaire_m": 1.4,
            "houle_secondaire_m": 0.2,
            "periode_houle_s": 9.5,
            "temperature_eau_c": 19.2,
            "vent_kn": 8.0,
            "vent_direction_deg": 320,
            "vent_categorie": "offshore",
        },
    }

    subject = "Surf Alert Normandie — E-MAIL DE TEST (données simulées)"
    body = (
        "E-MAIL DE TEST (données simulées) — ceci est un test du mode "
        "TEST_MODE, aucune donnée météo réelle n'a été utilisée.\n\n"
        + build_email_body([fake_result])
    )

    print("Envoi de l'e-mail de test...")
    send_email(subject=subject, body=body)
    print("E-mail de test envoyé avec succès")
    return 0


# ---------------------------------------------------------------------------
# 9. Point d'entrée
# ---------------------------------------------------------------------------
def main() -> int:
    test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
    if test_mode:
        return run_test_mode()

    today = datetime.now(timezone.utc).date()

    if not is_in_season(today):
        print(f"[{today.isoformat()}] Hors saison (fenêtre : {SEASON_START} -> "
              f"{SEASON_END}). Aucun appel API effectué.")
        return 0

    print(f"[{today.isoformat()}] Vérification des {len(SPOTS)} spots...")

    results = []
    for spot in SPOTS:
        try:
            result = evaluate_spot(spot)
        except (requests.RequestException, KeyError, IndexError) as exc:
            print(f"  ! {spot['name']} : erreur lors de la récupération des données ({exc})")
            continue
        results.append(result)
        status = "OK" if result["ok"] else "—"
        print(f"  [{status}] {spot['name']} : {result['values']}")

    good_spots = [r for r in results if r["ok"]]

    if good_spots:
        names = ", ".join(r["spot"] for r in good_spots)
        print(f"Conditions réunies pour : {names}. Envoi de l'e-mail...")
        send_email(
            subject=f"Surf Alert Normandie — {len(good_spots)} spot(s) exploitable(s)",
            body=build_email_body(good_spots),
        )
        print("E-mail envoyé.")
    else:
        print("Aucun spot ne remplit tous les critères cette semaine. Aucun e-mail envoyé.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
