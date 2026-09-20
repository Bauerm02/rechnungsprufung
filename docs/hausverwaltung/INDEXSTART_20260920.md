# Mietbeginn und Indexbasis

Auftrag HV-20260920-LIVE-INDEXSTART: Unterschrift im Juni bei Mietbeginn im
Dezember erlaubt keinen Indexlauf vor Dezember. Der ursprüngliche Mietbeginn
wird getrennt von der technischen Buchhaltungslaufzeit geführt. Für belegte
gewerbliche Kalender-/Intervallklauseln zählt der ursprüngliche Mietbeginn,
wenn er dokumentiert ist. Eine Verwaltungsübernahme startet das Intervall
nicht erneut. Ohne gesonderten Mietbeginn bleibt die technische Laufzeit als
bisheriger Fallback bestehen; sie ist kein Beleg für eine Indexbasis.

Einzelaufrufe vor Miet-/Verwaltungsbeginn brechen nach Autorisierung und vor
dem Perioden-Claim ab. Der spätere Aufruf im selben Monat bleibt möglich.
Der Batch überspringt solche Verträge. Änderungen des dokumentierten
Mietbeginns entwerten die quellgebundene Rechtsprofilfreigabe.

Explizite vertragliche VPI-Basis, tatsächlich verwendete Anpassungsbasis und
Abschlussdatum bleiben unverändert. Die Formularansicht zeigt ursprünglichen
Mietbeginn und Buchhaltungsbeginn separat. Keine automatische Ableitung eines
Abschlussdatums oder Überschreibung der VPI-Basis.

Grenzen: keine neuen Rechtsprofilfreigaben, keine nachträglichen Buchungen,
keine Änderung der Versand-/Soll-Aktivierung. Der monatliche Bericht bleibt
deterministisch. Fehlende Quellen, letzte tatsächlich angewandte Mietbasis,
Mietbestandteile und gesetzliche Einzelfallparameter müssen vor Ausführung
geklärt werden. Diese Änderung ist keine vollständige Freigabe aller Verträge.

Claude lieferte den begrenzten Guard-/Formularentwurf; Codex hat ihn nach
Prüfung der Tabellenbedeutung um die getrennte ursprüngliche Mietlaufzeit
und die Freigabebindung ergänzt. Testdaten bleiben synthetisch.
