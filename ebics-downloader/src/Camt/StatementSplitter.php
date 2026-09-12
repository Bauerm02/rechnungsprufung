<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

use DOMDocument;
use DOMElement;
use EbicsDownloader\Config\ProfileConfig;

/**
 * Trennt ein bereits sicher geparstes CAMT.053-Dokument (siehe
 * `XmlSafetyGuard`) in einzelne, eigenständige Ein-Konto-Statements und
 * entscheidet je Statement PER IBAN, ob es freigegeben oder quarantiniert
 * wird (siehe Klassendoku von `Camt053StatementValidator` für die
 * Begründung der Pro-Statement-Trennung).
 *
 * "Eigenständig" heißt: ein neues, minimales `Document`/`BkToCstmrStmt`
 * mit demselben Namensraum wie das Original, der (falls vorhanden)
 * kopierten `GrpHdr` und genau diesem EINEN `Stmt` - ein späterer,
 * separat abzunehmender Mietimport bekommt dadurch pro Konto eine für
 * sich gültige, unabhängig prüfbare Datei statt eines Ausschnitts aus
 * der Kundensammeldatei.
 */
final class StatementSplitter
{
    private const NTRY_LOCAL_NAME = 'Ntry';

    public function __construct(private readonly Camt053StatementValidator $validator)
    {
    }

    /** @return StatementOutcome[] */
    public function trenne(DOMDocument $dom, ProfileConfig $config): array
    {
        $statements = $this->validator->ermittleStatements($dom);
        $iso20022Namensraum = $dom->documentElement?->namespaceURI;

        $ergebnisse = [];
        foreach ($statements as $eintrag) {
            /** @var DOMElement $stmt */
            $stmt = $eintrag['stmt'];
            $iban = $eintrag['iban'];
            $auszugsnummer = $this->direkterKindText($stmt, 'Id');

            if ($iban === '') {
                $ergebnisse[] = StatementOutcome::quarantaene(
                    null,
                    $auszugsnummer,
                    $this->baueEigenstaendigesDokument($dom, $stmt, $iso20022Namensraum),
                    'Statement strukturell nicht eindeutig einer einzelnen IBAN zuordenbar '
                    . '(kein/mehrere Acct oder keine/mehrere IBAN-Angaben).'
                );
                continue;
            }

            $objekt = $config->objektFuerIban($iban);
            if ($objekt === null) {
                $ergebnisse[] = StatementOutcome::quarantaene(
                    $iban,
                    $auszugsnummer,
                    $this->baueEigenstaendigesDokument($dom, $stmt, $iso20022Namensraum),
                    'IBAN gehört zu keinem der freigegebenen Objekte (601/616/617) bzw. ist gesperrt (107).'
                );
                continue;
            }

            $ergebnisse[] = StatementOutcome::freigegeben(
                $objekt,
                $iban,
                $auszugsnummer,
                $this->baueEigenstaendigesDokument($dom, $stmt, $iso20022Namensraum)
            );
        }
        return $ergebnisse;
    }

    private function direkterKindText(DOMElement $element, string $localName): ?string
    {
        foreach ($element->childNodes as $kind) {
            if ($kind instanceof DOMElement && $kind->localName === $localName) {
                $text = trim($kind->textContent);
                return $text !== '' ? $text : null;
            }
        }
        return null;
    }

    private function baueEigenstaendigesDokument(DOMDocument $original, DOMElement $stmt, ?string $namensraum): string
    {
        $neu = new DOMDocument('1.0', 'UTF-8');
        $documentElement = $namensraum !== null
            ? $neu->createElementNS($namensraum, 'Document')
            : $neu->createElement('Document');
        $neu->appendChild($documentElement);

        $bkToCstmrStmt = $namensraum !== null
            ? $neu->createElementNS($namensraum, 'BkToCstmrStmt')
            : $neu->createElement('BkToCstmrStmt');
        $documentElement->appendChild($bkToCstmrStmt);

        $grpHdr = $this->findeDirektesGeschwisterVonStmtMitLocalName($stmt, 'GrpHdr');
        if ($grpHdr !== null) {
            $bkToCstmrStmt->appendChild($neu->importNode($grpHdr, true));
        }

        $bkToCstmrStmt->appendChild($neu->importNode($stmt, true));

        $ergebnis = $neu->saveXML();
        return $ergebnis !== false ? $ergebnis : '';
    }

    private function findeDirektesGeschwisterVonStmtMitLocalName(DOMElement $stmt, string $localName): ?DOMElement
    {
        $eltern = $stmt->parentNode;
        if ($eltern === null) {
            return null;
        }
        foreach ($eltern->childNodes as $kind) {
            if ($kind instanceof DOMElement && $kind->localName === $localName) {
                return $kind;
            }
        }
        return null;
    }
}
