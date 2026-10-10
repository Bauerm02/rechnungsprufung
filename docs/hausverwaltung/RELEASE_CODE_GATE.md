# Verbindliches Code-Gate für Timer und Web-App

CEO-20261010-CODEAUDIT-HV. Der frühere inkrementelle Dockerfile.reviewed
kopierte nur nach site-packages. Beide Timer-CLIs bevorzugten dagegen einen
alten /app/src-Baum. Das führte zum belegten Modulfehler beim Tagesdienst.

Neue Releases verwenden Dockerfile.release. Genau einen vollständigen,
geprüften Commit an tools/build_runtime_manifest.py übergeben. Dieses Skript
erzeugt den Build-Kontext ausschließlich über git archive, niemals aus der
Arbeitskopie. Manifest- und Archivhash sowie Commit in den Release-Nachweis
übernehmen. Der unveränderliche, aktuell laufende Image-Digest ist BASE_IMAGE.

Build zwingend mit docker build --network=none --pull=false. Das Gate läuft
als mietinkasso nach USER, ohne Produktivvolumes oder Zugangsdaten. Es prüft
den gesamten Paketbaum, beide CLIs, alle auch verschachtelten Fachimporte
und Symbole. Es ruft keine fachliche Funktion auf. Anschließend beide CLIs
zusätzlich in getrennten isolierten Prozessen ohne main importieren, damit
auch modulglobale Initialisierung und die getrennte Herkunft geprüft sind.

Vor Übernahme: vollständige synthetische Testsuite ohne Netzwerk; aktueller
Container-Digest unverändert; User, Entrypoint, Cmd, Env, Mounts, Netzwerk,
Ports und Sicherheits-/Ressourcenlimits identisch. Keine Migration, kein
Seed, keine manuelle Nachholung oder Timer-Ausführung als Test. Codepaket
ohne Datenänderung übernehmen; alter Container ist der Rückweg. Der alte
Container enthält den bekannten Fehler und ist kein fachlich gesunder Stand.

Live-Abnahme erst beim nächsten regulären Tageslauf: Exit 0, richtige
Importherkunft, Zahl der Klärfälle aus dem bestehenden Bericht prüfen. Nur
aktueller Monat; verstrichene/heutige Zahlungstermine bleiben KLAEREN und
werden nicht automatisch nachgeholt. Keine Bankübermittlung/-signatur.

Offen separat: Historie des Schattenbaums/Monatslaufes, Importfehler-Isolation
zwischen Eigentümerlauf und Pflege, Laufbericht-Historie, Schemaerzeugung im
Tagesjob sowie Alarmierung ausgefallener Timer an den bestehenden Owner.
