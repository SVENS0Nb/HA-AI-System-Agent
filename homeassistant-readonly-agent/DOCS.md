# Home Assistant Read-only Agent

Dieses Add-on verbindet einen auf Nur-Lese-Werkzeuge begrenzten OpenAI-Agenten mit Home Assistant und Signal.

## Vor dem Start

1. Eine `signal-cli-rest-api` im privaten Netz bereitstellen und ein Signal-Bot-Konto registrieren/verknüpfen.
2. Einen OpenAI-API-Key anlegen.
3. Das Add-on starten und **Weboberfläche öffnen** wählen.
4. Signal-URL, Bot-Nummer, erlaubte Absender und OpenAI-Key eintragen und speichern.
5. Home Assistant, Signal und OpenAI mit den Schaltflächen einzeln testen.

Eine unvollständige Konfiguration ist zulässig: Dann bleibt der Agent pausiert, während die Einstellungsoberfläche erreichbar ist. Gespeicherte Geheimnisse werden nicht an den Browser zurückgegeben. Änderungen werden automatisch geladen; ein manueller Add-on-Neustart ist nicht erforderlich.

Der Signal-Wrapper ist ein inoffizielles Community-Projekt. Seine HTTP-Schnittstelle darf nicht offen im Internet stehen.

## Grenzen

Das Add-on kann Zustände, Verlauf, Konfigurationsdateien und Home-Assistant-Core-Logs lesen. Es kann eigene Cron-, Entity- und Event-Monitore speichern. Jede dauerhafte Monitoränderung muss im Signal-Chat separat mit `BESTÄTIGEN <Code>` bestätigt werden; `ABBRECHEN` verwirft offene Vorschläge. Es enthält keine Werkzeuge für Service Calls, Zustandsänderungen, Event-Auslösung, Restarts, Shell-Befehle oder Dateiänderungen.

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
