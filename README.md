# Stromer-Datensammler

Liest die Daten des eigenen Stromer-Bikes aus der Stromer-Cloud (gleiche API wie die offizielle App „Stromer OMNI“) und speichert sie in SQLite. Eine kleine Webseite zeigt, ob es funktioniert.

Zweck: herausfinden, ob die Cloud-Daten für eine Verbrauchsauswertung pro Fahrt reichen (Sweetspot für Stufe 2), ohne Handy-App.

> Inoffiziell. Kein Produkt von Stromer. Nur für das eigene Konto.

## Was gesammelt wird

| Was | Endpunkt | Intervall |
|---|---|---|
| Zustand (Akku, km, Wh-Zähler, Temperaturen, Schloss) | `bike/{id}/state/?cached=true` | 5 min |
| Position | `bike/{id}/position/?cached=true` | 5 min |
| Tuning Stufe 2, Sensoren | `bike/{id}/settings/` | 30 min, nur Änderungen |
| Tagesstatistik 400 Tage, Gesamtstatistik | `bike/statistics/` | 6 h |

- Nur lesen. Keine Befehle ans Bike.
- Immer `cached=true`: Das Bike wird nie live abgefragt, seine Mobilfunkverbindung wird nicht belastet.
- Ein neuer Datenstand wird nur gespeichert, wenn das Bike wirklich neue Daten geschickt hat (Zeitstempel `rcvts`).

Webseite: `http://192.168.0.2:8095`
- Status, aktuelle Werte, Tuning
- **Wie oft schickt das Bike Daten?** Anzahl neuer Datenstände, Abstände
- Verlauf 7 Tage (Akku, km, Wh)
- **Erkannte Fahrten:** Kilometerstand steigt zwischen zwei Datenständen. Wh aus dem Energiezähler, Akku-%, Tuning zum Fahrtbeginn, Start- und Zielposition. Die Dauer ist die Zeit zwischen den Datenständen, bei seltenen Uploads also zu lang.
- Tagesstatistik mit Wh/km
- Protokoll und Rohdaten

## Installation auf dem Pi

```bash
cd /home/pi/mynas-git/stromer
cp .env.example .env
chmod 600 .env
sudo mkdir -p /mnt/raid/docker/stromer/data
sudo chown 1000:1000 /mnt/raid/docker/stromer/data
docker compose up -d --build
```

## Erster Start: Login

Stromer erlaubt nach dem Login nur die Rücksprungadresse `stromer://auth`. Eine Webseite kann die nicht empfangen, deshalb einmal von Hand:

1. `http://192.168.0.2:8095/login` öffnen, „Login öffnen“, bei Stromer anmelden.
2. Der Browser will `stromer://auth?code=…` öffnen und scheitert. Die Adresse kopieren (Chrome: vorher F12 → Netzwerk, Eintrag „authorize“, Header „Location“).
3. Innerhalb von etwa 60 Sekunden ins Feld einfügen.

Danach erneuert der Sammler den Zugang selbst (Refresh-Token). Der Token liegt in `DATA_LOCATION/token.json` (nur für uid 1000 lesbar).

## Hinweise

- Nur im Heimnetz: Die Webseite hat kein eigenes Login. Port 8095 nicht in der Fritz!Box freigeben.
- Updates: `git pull && docker compose up -d --build`
- Backup: `DATA_LOCATION` enthält alles (`collector.db`, `token.json`).
- Rohantworten: die letzten 200 pro Art, ausgewertete Werte unbegrenzt.
