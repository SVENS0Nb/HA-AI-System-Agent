# HA AI System Agent

Dieses Add-on verbindet einen überwiegend auf Nur-Lese-Werkzeuge begrenzten OpenAI-Agenten mit Home Assistant und Signal. Eine separat aktivierbare, bestätigungspflichtige Geräte-/Entity-Steuerung ist verfügbar; Konfiguration und System bleiben unveränderbar.

## Vor dem Start

1. Einen OpenAI-API-Key bereithalten. Empfohlen ist ein eigenes Signal-Konto; alternativ kann das persönliche Konto ausschließlich über „Notiz an mich“ genutzt werden.
2. Das Add-on starten und **Weboberfläche öffnen** wählen.
3. Unter Signal **Integriert – automatisch** auswählen und **Signal-Konto verbinden** anklicken.
4. Den QR-Code in der Signal-App des gewünschten Kontos unter **Einstellungen → Verknüpfte Geräte** scannen.
5. Entweder **Eigenen Chat „Notiz an mich“ verwenden** aktivieren oder den angezeigten `KOPPELN …`-Befehl von einem anderen Signal-Konto senden. Beide Wege können gleichzeitig aktiv sein.
6. OpenAI-Key speichern und die drei Verbindungstests ausführen.

Die **Reasoning-Steuerung** steht standardmäßig auf **Automatisch – je nach Aufgabe**. Kurze Statusfragen verwenden `none` oder `low`, Monitoraufträge und Diagnosen mindestens `medium`, und umfangreiche Ursachen- oder Konfigurationsanalysen `high`. Die Einstufung erfolgt lokal ohne einen zusätzlichen OpenAI-Aufruf. Unter **Feste Stufe** kann dieses Verhalten bei Bedarf deaktiviert werden.

Unter **Lernen und Wissensbasis** lässt sich die lokale Verhaltensanalyse ein- oder ausschalten. **Ausgewogen** ist der empfohlene Startwert. **Konservativ** braucht mehr Beobachtungen und meldet seltener, **Empfindlich** reagiert früher und kann mehr Fehlalarme erzeugen. Aufbewahrungsdauer und Anzahl der Erinnerungen pro Signal-Absender sind begrenzt und direkt in der Oberfläche einstellbar.

Unter **Geräte- und Entity-Steuerung** kann die standardmäßig ausgeschaltete Steuerfunktion aktiviert werden. Dort müssen die exakt erlaubten Entity-IDs eingetragen werden. Eine leere Liste erlaubt keine Geräteaktion; Konfigurationen, Automationen, Skripte und Systemdateien bleiben immer schreibgeschützt.

Eine unvollständige Konfiguration ist zulässig: Dann bleibt der Agent pausiert, während die Einstellungsoberfläche erreichbar ist. Gespeicherte Geheimnisse werden nicht an den Browser zurückgegeben. Änderungen werden automatisch geladen; ein manueller Add-on-Neustart ist nicht erforderlich.

Der Signal-Wrapper ist ein inoffizielles Community-Projekt. Seine integrierte HTTP-Schnittstelle ist ausschließlich über Loopback im Add-on erreichbar und veröffentlicht keinen Port. Eine eigene externe Bridge bleibt als Expertenmodus verfügbar.

Die Signal-Geräteschlüssel liegen persistent unter `/data/signal-cli`. Dadurch übersteht die Verknüpfung Neustarts; Home-Assistant-Backups mit diesen Daten müssen vertraulich gespeichert werden.

Der Selbst-Chat ist standardmäßig deaktiviert. Bei Aktivierung akzeptiert der Agent nur `syncMessage`-Nachrichten, die eindeutig vom verbundenen Konto an genau dieses Konto adressiert sind. Ausgehende Agentenantworten werden gekennzeichnet und beim Empfang verworfen, um Antwortschleifen auch nach einem Neustart zu verhindern. Normale Nachrichten bleiben unabhängig davon auf ausdrücklich erlaubte Absender begrenzt.

## Grenzen

Das Add-on kann Zustände, Verlauf, Konfigurationsdateien und Home-Assistant-Core-Logs lesen. Es kann eigene Cron-, Entity- und Event-Monitore speichern. Jede dauerhafte Monitoränderung muss im Signal-Chat separat mit `BESTÄTIGEN <Code>` bestätigt werden; `ABBRECHEN` verwirft offene Vorschläge.

Bei aktivierter Gerätesteuerung kann es fest definierte Aktionen für `light`, `switch`, `fan`, `cover`, `climate`, `humidifier`, `media_player`, `vacuum`, `lock`, `siren`, `valve`, `water_heater`, `number` und `select` vorschlagen. Auch diese werden erst nach `BESTÄTIGEN <Code>` ausgeführt. Die aktuelle Signal-Nachricht muss die Steuerungsabsicht selbst enthalten. Automationen, Skripte, Szenen, Buttons, Helfer, Alarmanlagen, Updates, allgemeine Service Calls, Event-Auslösung, Restarts, Shell-Befehle und Dateiänderungen bleiben gesperrt.

Bei aktiviertem Lernen werden Home-Assistant-Zustandsereignisse lokal zu begrenzten Baselines zusammengefasst. Nach einer Aufwärmphase kann der Agent auffällige numerische Abweichungen, ungewöhnlich viele Zustandswechsel und anhaltende Ausfälle mit aktuellem Zustand und Verlauf abgleichen. Das Ergebnis ist eine statistische Einschätzung und keine sichere Defektdiagnose.

Die Wissensbasis enthält nur exakte Aussagen aus der aktuellen Nachricht eines erlaubten Signal-Absenders. Inhalte aus Logs, Konfigurationen, Events oder Agentenantworten dürfen keine Erinnerung erzeugen. Erinnerungen sind pro Absender getrennt, laufen automatisch ab und können auf ausdrücklichen Wunsch gelöscht werden. Gespeicherte Baselines, Erinnerungen und Auffälligkeiten ändern Home Assistant nicht; sie liegen ausschließlich in der eigenen Datenbank unter `/data`.

`allow_sensitive_config` ist standardmäßig ausgeschaltet und blockiert insbesondere `.storage`, `.cloud` und `secrets.yaml`. Erkannte Tokens, Kennwörter und Schlüssel werden unabhängig davon lokal geschwärzt. Das ist absichtlich so, weil gelesene Inhalte zur Analyse an die OpenAI API übertragen werden können.

## Erste Tests

Nach der Startnachricht per Signal senden:

```text
Liste fünf Sensoren mit "Temperatur" im Namen auf.
```

Danach beispielsweise:

```text
Überwache sensor.beispiel. Melde dich, wenn er drei Minuten unavailable oder unknown ist.
```

Der Agent schlägt den Monitor anschließend vor. Sende den ausgegebenen `BESTÄTIGEN`-Befehl als zweite Nachricht, um ihn wirklich anzulegen.

Mit `Zeige meine Monitore` lassen sich die gespeicherten Regeln kontrollieren.

Für die Wissensbasis eignen sich zum Beispiel:

```text
Merke dir: Die Gartenbewässerung läuft im Sommer normalerweise jeden zweiten Abend.
```

```text
Was weißt du über die Gartenbewässerung und war ihr Verhalten heute ungewöhnlich?
```

```text
Vergiss die alte Notiz zur Gartenbewässerung; sie ist nicht mehr gültig.
```

Das Speichern erfolgt nur, wenn der Agent die Aussage als langfristig nützlich einstuft. Mit `Was hast du dir über … gemerkt?` lässt sich die Wissensbasis kontrollieren.

Nach Aktivierung der Gerätesteuerung beispielsweise:

```text
Schalte light.wohnzimmer ein.
```

Der Agent liest zunächst die Entity, schlägt die konkrete Aktion vor und nennt einen Code. Erst die zweite Nachricht führt sie aus:

```text
BESTÄTIGEN 1a2b3c4d
```

Die UI-Freigabe und alle Grenzen werden beim Bestätigen erneut geprüft. Wird die Funktion vorher ausgeschaltet oder die Entity aus der Allowlist entfernt, schlägt die Ausführung fehl.
