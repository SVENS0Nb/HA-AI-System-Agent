# Changelog

## 0.7.2

- Signal-API und `signal-cli` laufen gemeinsam unter einem einzigen Vordergrund-Supervisor, sodass Neustarts keine verwaisten Prozesse zurücklassen
- Gleichzeitige Start-, Status- und QR-Anfragen warten auf denselben Bridge-Start, statt ihn vorzeitig abzubrechen
- Kontrollierte Wiederherstellung einer später ungesund gewordenen Signal-Bridge nach der Startphase
- Geschwärzte letzte Prozessausgabe in der UI bei Startfehlern statt eines alleinstehenden Exit-Codes
- Verständliche Onboarding-Hinweise für das noch nicht verbundene Bot-Konto und den noch nicht gekoppelten Absender

## 0.7.1

- Signal-Eingänge und vorbereitete Antworten bleiben bis zur bestätigten Zustellung dauerhaft erhalten
- Bestätigte Aktionen werden vor dem externen Effekt atomar verbraucht und niemals doppelt ausgeführt
- Anomalien werden nach Neustarts erneut zugestellt und pro Signal-Empfänger quittiert
- Home-Assistant-Event-Abonnements werden bestätigt; einmalige WebSocket-Aufrufe besitzen feste Zeitlimits
- Nicht mehr vorhandene überwachte Entities gelten als `unavailable`
- Die integrierte Signal-Bridge wird bei lebendem, aber ungesundem Prozess automatisch neu gestartet und weiter überwacht
- Verpflichtende Entity-Freigabeliste bei aktivierter Gerätesteuerung
- Erweiterte Schwärzung für URI-Zugangsdaten, Query-Tokens und gequotete Werte
- Reasoning-Stufen werden für bekannte GPT-5-Modellfähigkeiten kompatibel normalisiert
- Hintergrundarbeiter fangen Speicherfehler ab; Verhaltensspeicherzugriffe blockieren den Event-Loop nicht
- Vollständige CI-Prüfung für Tests, Typen, Lint, Bytecode, statische Sicherheit und bekannte Abhängigkeitsschwachstellen

## 0.7.0

- Standardmäßig deaktivierte Geräte-/Entity-Steuerung als neue UI-Option
- Optionale Allowlist konkreter steuerbarer Entity-IDs
- Feste Domain-/Aktionszuordnung statt allgemeinem oder modellgesteuertem Service Call
- Unterstützte physische Domains: Licht, Schalter, Lüfter, Abdeckung, Klima, Luftbefeuchter, Mediengerät, Staubsauger, Schloss, Sirene, Ventil, Warmwasser sowie Geräte-`number` und -`select`
- Numerische Grenzprüfung und Abgleich von Modi/Optionen mit den aktuellen Entity-Attributen
- Jede Geräteaktion benötigt einen sendergebundenen, zehn Minuten gültigen `BESTÄTIGEN`-Code
- Steuerungsabsicht muss als exakter Ausschnitt der aktuellen authentifizierten Signal-Nachricht vorliegen
- Erneute vollständige Validierung bei der Bestätigung; nachträgliches Ausschalten der UI-Freigabe blockiert offene Aktionen
- Automationen, Skripte, Szenen, Buttons, Helfer, Alarmanlagen, Updates, System-/Add-on-Dienste, Events und Dateien bleiben ausgeschlossen
- Proaktive Monitor- und Lernläufe besitzen kein Steuerungswerkzeug

## 0.6.0

- Persistente, pro Signal-Absender getrennte Wissensbasis für Präferenzen, Korrekturen, normales Geräteverhalten und wichtige Hinweise
- Erinnerungen dürfen ausschließlich exakte Ausschnitte der aktuellen authentifizierten Signal-Nachricht enthalten
- Wichtigkeit und Lebensdauer werden vom Agenten gewählt; abgelaufene Einträge werden automatisch entfernt
- Lokale, kompakte Geräte-Baselines ohne Speicherung einer unbegrenzten Rohdatenhistorie
- Erkennung numerischer Ausreißer, ungewöhnlich häufiger Zustandswechsel und anhaltender `unavailable`-/`unknown`-Zustände
- Automatische Signal-Einordnung auffälligen Verhaltens mit aktueller Zustands- und Verlaufsprüfung
- Abfragewerkzeuge für Erinnerungen, Geräte-Baselines und vergangene Anomalien
- UI-Einstellungen für Lernfunktion, Empfindlichkeit, Aufbewahrung und Speicherlimit
- Log-, Config-, Tool- und Eventinhalte können keine Nutzererinnerungen anlegen oder löschen

## 0.5.0

- Adaptive Reasoning-Steuerung wählt pro Chat oder Monitorlauf lokal zwischen `none`, `low`, `medium` und `high`
- Einfache Statusfragen sparen Reasoning-Tokens; Diagnosen, Konfigurationsprüfungen und Monitoränderungen erhalten höhere Stufen
- Strukturelle Werkzeugfehler und aufwendige Leseoperationen erhöhen die Stufe für den nachfolgenden Modellaufruf
- Manueller Modus mit fester Reasoning-Stufe bleibt vollständig erhalten
- Auswahl ohne zusätzlichen OpenAI-Klassifizierungsaufruf und ohne Auswertung nicht vertrauenswürdiger Log- oder Eventinhalte

## 0.4.1

- Einheitlicher Produktname „HA AI System Agent“ in App-Store, Seitenleiste, UI, Meldungen und Dokumentation
- Zeitzone in der Einstellungsoberfläche als Dropdown mit den verfügbaren IANA-Zeitzonen
- Bestehende gültige Zeitzoneneinstellungen bleiben beim Update erhalten

## 0.4.0

- `signal-cli-rest-api` als interne Multiarch-Bridge direkt in das Add-on integriert
- QR-Code-Onboarding vollständig in der admin-geschützten Ingress-Oberfläche
- Automatische Erkennung des verknüpften Bot-Kontos
- Fünf Minuten gültige Einmalcode-Kopplung für erlaubte Signal-Absender
- Signal-API ausschließlich über Container-Loopback ohne veröffentlichten Port
- Persistente Signal-Geräteschlüssel unter `/data/signal-cli`
- Sicher bestätigtes Entfernen der lokalen Signal-Verknüpfung
- Bestehende benutzerdefinierte Bridge-URLs werden in den externen Expertenmodus migriert

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
