# Changelog

## 1.0.0

- Gerichteter Abhängigkeitsgraph aus read-only Automation-/Package-Analyse mit Herkunft und Konfidenz
- Konfigurierbare Zustandsautomaten, robuste Betriebszyklus-Erkennung und Prüfung erwarteter Sensorwirkungen
- Geschützte Sofortregeln für explizite Rauch-, Gas-, Wasser-/Feuchte-, Hitze- und Sicherheitsalarme mit Recovery
- Erkennung wiederholter Home-Assistant-Neustarts als deduplizierter System-Incident
- Begrenztes, geschwärztes Log-Template-Clustering mit Erkennung neuer oder stark ansteigender Cluster
- Strukturierte Incident-Analyse über OpenAI Structured Outputs mit strikter Validierung, Audit und deterministischem Offline-Fallback
- Deduplizierte Signal-Incident-Meldungen pro Empfänger mit Eskalation, Cooldown, Ruhezeiten, Wartungs-/Urlaubsmodus und Entwarnung
- Sieben Feedbackarten über Admin-UI/API und bestätigten Signal-Workflow; geschützte Sicherheitsregeln können nicht automatisch unterdrückt werden
- Maschinenlesbare und verständliche Stunden-/Tages-/Wochenzusammenfassungen sowie aktionsfreier JSONL-Replay-Modus
- Erweiterte Admin-Oberfläche und API für Incidents, Anomalien, Profile/Baselines, Abhängigkeiten, Zyklen, Systemmodell, Health und Zusammenfassungen
- SQLite-Migrationen 2–5 mit Graph, Zustandsinstanzen, Zyklen, Feedback, Summaries, indiziertem Lieferstatus, Logclustern, Config-Snapshots, Quelleninvalidierung, LLM-Audit und wiederanlaufbarer Eventverarbeitung
- Sicherheitsalarme werden schon aus dem Start-Snapshot erkannt; Stream-Lücken und verworfene Normalereignisse werden über Zustands-Snapshots abgeglichen
- Korrigierter Incident-Lebenszyklus für Recovery, stabile Langzeitbefunde, Empfängeränderungen und einmalige gegenüber fortbestehenden Anomalien
- Harte LLM-Kontextgrenze, belastbare UTF-8-Audits, konsistente Relations-/Retention-Bereinigung und skalierbare Zeitraumzusammenfassungen
- Korrekte Package-/Automation-Invalidierung ohne Service-als-Entity-Fehlklassifikation; Gesundheitsdaten werden lokal aus Erinnerungen ausgeschlossen
- Deadlock-freie Signal-Zustellwiederholung; bei intelligentem Monitoring beobachtet die Legacy-Anomaliepipeline keine neuen Events, stellt aber noch offene Altmeldungen zu
- Dauerhafte Monitor-Trigger-Outbox, atomare Incident-/Benachrichtigungsübergänge und evidenzgebundene LLM-Analysen verhindern Verluste, doppelte Baselines und veraltete Meldungsabschlüsse
- Gleichartige Monitor-Trigger werden verlustfrei serialisiert; Event-Recovery arbeitet pro Entity chronologisch und analysiert überholte Evidenz ohne Zustandsrücksprung
- Bestätigte und quittierte Incidents behalten ihren Lifecycle; Teilentwarnungen bereinigen Evidenz und Analyse, Incident-Rollover werden als echte SQLite-Transaktion ausgeführt
- Gerätesteuerungsbestätigungen nennen Entity, lokal aufgelösten Dienst, Parameter und Code deterministisch außerhalb der Modellausgabe
- Signal-Chat, persistente Monitore und die optional bestätigte Entity-Steuerung bleiben in ihren bisherigen Sicherheitsgrenzen erhalten

## 0.9.0

- Neue lokale, LLM-unabhängige Monitoring-Pipeline mit validierten und deduplizierten Home-Assistant-Ereignissen
- Semantische Entity-Profile aus State-, Entity-, Device- und Area-Metadaten mit Herkunft und Konfidenz
- Begrenzte Rolling Features sowie globale, saisonale, Tages- und Zeitfenster-Baselines mit robusten Statistiken
- Erklärbare Detektoren für Verfügbarkeit, numerische Abweichungen, Zustandsfrequenz und ausbleibende Updates
- Incident-Gruppierung nach Integration, Gerät, Bereich, Korrelation oder Entity inklusive Kritikalität, Priorität und Recovery
- Eigene versionierte SQLite-Migrationen, Retention, Komponenten-Health, Readiness und Prometheus-Metriken
- Schreibgeschützte Signal-Werkzeuge für Incidents, Profile und Monitoring-Health
- Neue Admin-UI-Konfiguration; bestehender Signal-Chat und bestätigte Entity-Steuerung bleiben unverändert isoliert

## 0.8.0

- Optionaler, standardmäßig deaktivierter Signal-Modus für den eigenen Chat „Notiz an mich“
- Selbst-Chat und ausdrücklich erlaubte externe Absender können gleichzeitig verwendet werden
- Strikte Trennung von eigenen Notizen und sonstigen ausgehenden Unterhaltungen anhand des Signal-Synchronisationsziels
- Gekennzeichnete Agentenantworten werden beim Empfang verworfen und verhindern dauerhafte Antwortschleifen
- Start-, Test-, Monitor- und Anomaliebenachrichtigungen berücksichtigen den aktivierten Selbst-Chat
- UI-Onboarding speichert das per QR-Code erkannte persönliche Konto automatisch, wenn der Selbst-Chat ausgewählt wurde

## 0.7.3

- Speicherschonender nativer Signal-JSON-RPC-Daemon ohne dauerhaft laufende Java-VM
- Behebung des Signal-Starts auf Home Assistant mit nicht ausführbarem `/tmp`-Dateisystem
- Container-Test bildet Home Assistants `noexec`-Schutz für `/tmp` nach und verhindert Rückfälle
- Die Oberfläche zeigt nach einem Start-Timeout die relevante geschwärzte Bridge-Diagnose an

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
