# Changelog

## 0.3.1

- Neues Markenlogo im Home-Assistant-App-Store und in der Einstellungsoberfläche
- Quadratisches 128-Pixel-App-Icon für die Store- und Supervisor-Darstellung

## 0.3.0

- Serverseitige Adminprüfung für jeden Ingress-Aufruf
- Zweistufige Bestätigung aller dauerhaften Monitoränderungen
- Lokale Schwärzung typischer Tokens, Kennwörter und privater Schlüssel
- Begrenzte OpenAI-Laufzeit, Ausgabe, Parallelität, Speicher- und Monitoranzahl
- Persistente Signal-Deduplizierung und Verarbeitung aller Nachrichten in Batch-Antworten
- Entkoppelte Monitor-Queue mit Wiederholungen und regelmäßigem Statusabgleich
- Erfolgreiche Zustellung wird erst danach als Cooldown-Lauf markiert
- Getrennte UI-Assets ohne `unsafe-inline` und Rücksetzen von UI-Überschreibungen
- Aktuelles Home-Assistant-Buildformat mit direktem Basis-Image

## 0.2.0

- Eigene, nur über Home Assistant Ingress erreichbare Einstellungsoberfläche
- Signal-, OpenAI- und Home-Assistant-Verbindungstests
- Automatischer Agent-Neustart nach UI-Änderungen
- Geheimnisse werden nicht an den Browser zurückgegeben
- Deutsche und englische Bezeichnungen für die native Add-on-Konfiguration

## 0.1.0

- Initialer MVP
- Read-only Entity-, History-, Config- und Log-Werkzeuge
- Signal-Senden und -Empfangen mit Absender-Whitelist
- Persistente Cron-, Entity- und Event-Monitore
- Lokaler Chat-Kontext und Schutz sensibler Konfigurationsdateien
