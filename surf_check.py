#!/usr/bin/env python3
"""
Surf Alert Normandie
====================

Ce script est exécuté trois fois par semaine par GitHub Actions, à midi
(heure de Paris), et se comporte différemment selon le jour :

- Le JEUDI : vérification hebdomadaire habituelle. Vérifie les
  PRÉVISIONS de conditions de surf sur six spots de la côte normande
  (Calvados 14 / Seine-Maritime 76) pour le samedi et le dimanche à
  venir, ainsi que les jours fériés fixes (14 juillet, 15 août) si l'un
  d'eux tombe dans les 6 jours suivants.
- Le MARDI : vérifie si ce jour est précisément l'avant-veille du jeudi
  de l'Ascension cette année. Si oui, vérifie les prévisions pour le
  jour de l'Ascension et envoie un e-mail si les conditions sont
  bonnes. Sinon, ne fait rien (aucun appel API).
- Le SAMEDI : même logique pour le lundi de Pentecôte (dont
  l'avant-veille tombe toujours un samedi).

Les autres jours d'exécution éventuels (déclenchement manuel un autre
jour de la semaine) ne déclenchent aucune vérification.

Utilise les API publiques Open-Meteo (Marine + Forecast). Aucune clé
d'API n'est nécessaire. Seuls les identifiants d'envoi de mail
(EMAIL_FROM, EMAIL_TO, EMAIL_PASSWORD, et éventuellement SMTP_SERVER /
SMTP_PORT) sont attendus comme variables d'environnement, injectées en
production par GitHub Actions depuis les secrets du dépôt.

Mode test : si la variable d'environnement TEST_MODE vaut "true", le
script saute le garde-fou saisonnier et les appels API, fabrique un
résultat "idéal" avec des valeurs fictives, et envoie un e-mail de test
via la même fonction send_email() que le mode normal.
"""

import os
import sys
import smtplib
import ssl
from datetime import datetime, date, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

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
# 2. Dates ciblées
# ---------------------------------------------------------------------------
PARIS_TZ = ZoneInfo("Europe/Paris")

# Jours fériés fixes surveillés lors de la vérification hebdomadaire du
# jeudi (label, mois, jour), même s'ils ne tombent pas un week-end.
FIXED_HOLIDAY_MONTH_DAY = [
    ("Fête nationale", 7, 14),
    ("Assomption", 8, 15),
]

# Heures locales (Europe/Paris) vérifiées pour chaque jour cible. Il suffit
# qu'une seule de ces heures remplisse tous les critères pour valider le
# spot ce jour-là.
CHECK_HOURS_LOCAL = [9, 12, 15, 18]


def easter_sunday(year: int) -> date:
    """Calcule la date du dimanche de Pâques pour une année donnée,
    calendrier grégorien (algorithme de Meeus/Jones/Butcher)."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def ascension_date(year: int) -> date:
    """Jeudi de l'Ascension : Pâques + 39 jours."""
    return easter_sunday(year) + timedelta(days=39)


def pentecost_monday_date(year: int) -> date:
    """Lundi de Pentecôte : Pâques + 50 jours."""
    return easter_sunday(year) + timedelta(days=50)


def compute_target_dates(today: date) -> list:
    """Détermine les dates à vérifier pour la vérification hebdomadaire du
    jeudi : le samedi et le dimanche suivants, plus tout jour férié FIXE
    (14 juillet, 15 août — pas les jours mobiles Ascension/Pentecôte, qui
    ont leur propre déclenchement dédié) qui tombe dans les 6 jours
    suivant ce run, même s'il ne tombe pas un week-end."""
    days_until_saturday = (5 - today.weekday()) % 7
    saturday = today + timedelta(days=days_until_saturday)
    sunday = saturday + timedelta(days=1)

    targets = [
        {"label": f"Samedi {saturday.strftime('%d/%m')}", "date": saturday},
        {"label": f"Dimanche {sunday.strftime('%d/%m')}", "date": sunday},
    ]

    existing_dates = {t["date"] for t in targets}
    week_end = today + timedelta(days=6)
    for label, month, day in FIXED_HOLIDAY_MONTH_DAY:
        candidate = date(today.year, month, day)
        if today <= candidate <= week_end and candidate not in existing_dates:
            targets.append({
                "label": f"{label} ({candidate.strftime('%d/%m')})",
                "date": candidate,
            })
            existing_dates.add(candidate)

    return targets


def local_hour_to_utc_naive(target_date: date, hour_local: int) -> datetime:
    """Convertit une date + heure locale (Europe/Paris) en datetime UTC
    naïf, pour retrouver l'échéance correspondante dans les séries
    horaires Open-Meteo (fournies en UTC)."""
    local_dt = datetime(
        target_date.year, target_date.month, target_date.day, hour_local,
        tzinfo=PARIS_TZ,
    )
    return local_dt.astimezone(timezone.utc).replace(tzinfo=None)


def _find_hour_index(time_strings: list, target_utc_naive: datetime):
    """Renvoie l'indice exact correspondant à l'heure UTC visée dans une
    série temporelle Open-Meteo (format ISO 8601), ou None si l'échéance
    est hors de la fenêtre de prévisions renvoyée par l'API."""
    for i, ts in enumerate(time_strings):
        if datetime.fromisoformat(ts) == target_utc_naive:
            return i
    return None


# ---------------------------------------------------------------------------
# 3. Spots surveillés
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
# 4. Critères de qualité de vagues
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

# Horizons maximaux raisonnables demandés aux API. Les dates cibles sont
# toujours proches du jour d'exécution (quelques jours), ces plafonds ne
# sont jamais réellement contraignants en pratique.
MAX_MARINE_FORECAST_DAYS = 10
MAX_FORECAST_FORECAST_DAYS = 16


# ---------------------------------------------------------------------------
# 5. Appels API (séries horaires complètes sur plusieurs jours)
# ---------------------------------------------------------------------------
def fetch_marine_series(lat: float, lon: float, forecast_days: int) -> dict:
    """Récupère la série horaire complète (houle primaire, houle
    secondaire, période, température de l'eau) depuis l'API Marine
    d'Open-Meteo, sur la fenêtre demandée."""
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
        "forecast_days": forecast_days,
    }
    resp = requests.get(MARINE_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


def fetch_forecast_series(lat: float, lon: float, forecast_days: int) -> dict:
    """Récupère la série horaire complète (vitesse et direction du vent à
    10 m) depuis l'API Forecast d'Open-Meteo, sur la fenêtre demandée."""
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "wind_speed_10m,wind_direction_10m",
        "wind_speed_unit": "kn",
        "timezone": "UTC",
        "forecast_days": forecast_days,
    }
    resp = requests.get(FORECAST_API_URL, params=params, timeout=REQUEST_TIMEOUT_S)
    resp.raise_for_status()
    return resp.json()["hourly"]


# ---------------------------------------------------------------------------
# 6. Classification du vent (onshore / offshore)
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
# 7. Évaluation d'un spot pour une date cible
# ---------------------------------------------------------------------------
def evaluate_spot_for_date(spot: dict, target_date: date,
                           marine_series: dict, forecast_series: dict):
    """Parcourt les heures locales surveillées (CHECK_HOURS_LOCAL) pour la
    date cible et renvoie le détail de la première heure qui remplit tous
    les critères pour ce spot, ou None si aucune heure ne convient (ou si
    la date est hors de la fenêtre de prévisions renvoyée par l'API)."""
    for hour_local in CHECK_HOURS_LOCAL:
        target_utc = local_hour_to_utc_naive(target_date, hour_local)
        idx_marine = _find_hour_index(marine_series["time"], target_utc)
        idx_forecast = _find_hour_index(forecast_series["time"], target_utc)
        if idx_marine is None or idx_forecast is None:
            continue

        swell_height = marine_series["swell_wave_height"][idx_marine]
        swell_period = marine_series["swell_wave_period"][idx_marine]
        secondary_swell = marine_series["secondary_swell_wave_height"][idx_marine]
        water_temp = marine_series["sea_surface_temperature"][idx_marine]
        wind_speed = forecast_series["wind_speed_10m"][idx_forecast]
        wind_direction = forecast_series["wind_direction_10m"][idx_forecast]

        wind_category = classify_wind(wind_direction, spot["facing"])
        wind_limit = (
            CRITERIA["max_wind_onshore_kn"]
            if wind_category == "onshore"
            else CRITERIA["max_wind_offshore_kn"]
        )

        checks = {
            "houle_primaire": swell_height > CRITERIA["min_swell_height_m"],
            "houle_secondaire": secondary_swell < CRITERIA["max_secondary_swell_m"],
            "periode_houle": swell_period > CRITERIA["min_swell_period_s"],
            "temperature_eau": water_temp >= CRITERIA["min_water_temp_c"],
            "vent": wind_speed < wind_limit,
        }

        if all(checks.values()):
            return {
                "spot": spot["name"],
                "department": spot["department"],
                "heure_locale": f"{hour_local}h",
                "values": {
                    "houle_primaire_m": round(swell_height, 2),
                    "houle_secondaire_m": round(secondary_swell, 2),
                    "periode_houle_s": round(swell_period, 1),
                    "temperature_eau_c": round(water_temp, 1),
                    "vent_kn": round(wind_speed, 1),
                    "vent_direction_deg": round(wind_direction),
                    "vent_categorie": wind_category,
                },
            }

    return None


# ---------------------------------------------------------------------------
# 8. Notification par e-mail
# ---------------------------------------------------------------------------
def build_email_body(good_results: list) -> str:
    lines = [
        "Surf Alert Normandie — bonnes conditions détectées :",
        "",
    ]

    labels_order = []
    grouped = {}
    for r in good_results:
        grouped.setdefault(r["target_label"], []).append(r)
        if r["target_label"] not in labels_order:
            labels_order.append(r["target_label"])

    for label in labels_order:
        lines.append(f"=== {label} ===")
        for r in grouped[label]:
            v = r["values"]
            lines.append(
                f"• {r['spot']} ({r['department']}) — meilleure fenêtre : {r['heure_locale']}"
            )
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
# 9. Évaluation générique (spots x dates cibles) + notification
# ---------------------------------------------------------------------------
def evaluate_and_notify(today: date, targets: list) -> int:
    """Fonction commune : interroge les API pour chaque spot, évalue
    chaque date cible fournie, et envoie un e-mail groupé si au moins un
    créneau remplit tous les critères. Utilisée à la fois par la
    vérification hebdomadaire du jeudi et par les vérifications dédiées
    Ascension / Pentecôte."""
    if not targets:
        print("Aucune date à vérifier.")
        return 0

    max_target_date = max(t["date"] for t in targets)
    days_needed = max(1, (max_target_date - today).days + 2)
    marine_days = max(1, min(days_needed, MAX_MARINE_FORECAST_DAYS))
    forecast_days = max(1, min(days_needed, MAX_FORECAST_FORECAST_DAYS))

    good_results = []
    for spot in SPOTS:
        try:
            marine_series = fetch_marine_series(spot["lat"], spot["lon"], marine_days)
            forecast_series = fetch_forecast_series(spot["lat"], spot["lon"], forecast_days)
        except (requests.RequestException, KeyError) as exc:
            print(f"  ! {spot['name']} : erreur lors de la récupération des données ({exc})")
            continue

        for target in targets:
            result = evaluate_spot_for_date(
                spot, target["date"], marine_series, forecast_series
            )
            if result:
                result["target_label"] = target["label"]
                good_results.append(result)
                print(f"  [OK] {spot['name']} — {target['label']} à "
                      f"{result['heure_locale']} : {result['values']}")
            else:
                print(f"  [—] {spot['name']} — {target['label']} : "
                      f"aucune heure ne remplit tous les critères")

    if good_results:
        labels = ", ".join(sorted(set(r["target_label"] for r in good_results)))
        print(f"Conditions réunies pour : {labels}. Envoi de l'e-mail...")
        send_email(
            subject=f"Surf Alert Normandie — {len(good_results)} créneau(x) exploitable(s)",
            body=build_email_body(good_results),
        )
        print("E-mail envoyé.")
    else:
        print("Aucun créneau ne remplit tous les critères pour cette période. "
              "Aucun e-mail envoyé.")

    return 0


# ---------------------------------------------------------------------------
# 10. Vérifications dédiées Ascension / Pentecôte (envoi à l'avant-veille)
# ---------------------------------------------------------------------------
def check_movable_holiday(today: date, label: str, holiday_date: date) -> int:
    """N'agit que si `today` est précisément l'avant-veille (J-2) de la
    date fournie. Sinon, ne fait rien (aucun appel API). Ce garde-fou
    permet de brancher ce contrôle sur un cron hebdomadaire fixe (tous
    les mardis, ou tous les samedis) tout en ne déclenchant réellement
    la vérification que la semaine où le jour férié approche."""
    target_send_date = holiday_date - timedelta(days=2)
    if today != target_send_date:
        print(f"[{today.isoformat()}] Pas l'avant-veille de {label} cette année "
              f"({label} tombe le {holiday_date.strftime('%d/%m/%Y')}). Aucun appel API.")
        return 0

    print(f"[{today.isoformat()}] Avant-veille de {label} "
          f"({holiday_date.strftime('%d/%m/%Y')}). Vérification des prévisions...")
    targets = [{
        "label": f"{label} ({holiday_date.strftime('%d/%m')})",
        "date": holiday_date,
    }]
    return evaluate_and_notify(today, targets)


# ---------------------------------------------------------------------------
# 11. Mode test
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
        "target_label": "Samedi (test)",
        "heure_locale": "12h",
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
# 12. Point d'entrée
# ---------------------------------------------------------------------------
def main() -> int:
    test_mode = os.environ.get("TEST_MODE", "false").lower() == "true"
    if test_mode:
        return run_test_mode()

    today = datetime.now(timezone.utc).astimezone(PARIS_TZ).date()

    if not is_in_season(today):
        print(f"[{today.isoformat()}] Hors saison (fenêtre : {SEASON_START} -> "
              f"{SEASON_END}). Aucun appel API effectué.")
        return 0

    weekday = today.weekday()  # Lundi=0 ... Dimanche=6

    if weekday == 3:  # Jeudi : vérification hebdomadaire (week-end + jours fixes)
        targets = compute_target_dates(today)
        print(f"[{today.isoformat()}] Vérification hebdomadaire pour : "
              + ", ".join(t["label"] for t in targets))
        return evaluate_and_notify(today, targets)

    if weekday == 1:  # Mardi : avant-veille possible de l'Ascension
        return check_movable_holiday(today, "Ascension", ascension_date(today.year))

    if weekday == 5:  # Samedi : avant-veille possible du lundi de Pentecôte
        return check_movable_holiday(
            today, "Lundi de Pentecôte", pentecost_monday_date(today.year)
        )

    print(f"[{today.isoformat()}] {today.strftime('%A')} : jour d'exécution non "
          f"prévu pour une vérification automatique. Aucun appel API.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
