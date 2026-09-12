<?php

declare(strict_types=1);

namespace EbicsDownloader\Camt;

use DOMDocument;
use DOMElement;
use DOMNode;
use EbicsDownloader\Exceptions\DeliveryRejectedException;

/**
 * Strukturprüfung einer CAMT.053-Datei, PORTIERT aus der bereits
 * unabhängig abgenommenen Python-Logik
 * (`src/mietinkasso/bank/importer.py::_pruefe_stmt_konten` /
 * `_pruefe_und_sammle_direkte_ntry`, Commits fa768be/80a7e9e): dieselbe
 * Local-Name-basierte Traversierung (namensraumunabhängig, wie das
 * Python-Pendant `_localname`/`root.iter()`), dieselben zwei
 * Kernregeln:
 *
 * 1. Jedes `Stmt` darf GENAU EIN direktes `Acct`-Kind mit GENAU EINER
 *    eindeutigen IBAN haben - mehrere `Acct`, eine fehlende IBAN oder
 *    mehrere unterschiedliche IBAN-Angaben im selben `Acct` machen
 *    dieses eine Statement (nicht zwingend die ganze Datei) nicht
 *    zuordenbar.
 * 2. JEDE `Ntry` im gesamten Dokument muss ein DIREKTES Kind eines
 *    (beliebigen) `Stmt` sein. Eine `Ntry` außerhalb jedes `Stmt` oder
 *    tiefer verschachtelt ist ein dokumentweiter Strukturfehler - das
 *    war exakt der in dieser Sitzung zuvor gefundene Fehler (Ntry via
 *    `root.iter()` gefunden statt nur direkte Kinder je geprüftem Stmt)
 *    und wird hier nicht pro Statement, sondern für die GESAMTE Lieferung
 *    hart abgelehnt (siehe `pruefeDokumentstruktur()`).
 *
 * Anders als der Python-Import (der bei jedem Fremdkonto die GANZE Datei
 * ablehnt, weil er nur EIN vorher ausgewähltes Konto kennt) trennt diese
 * Klasse bewusst PRO Statement: eine Kundensammeldatei mit mehreren
 * Konten soll die für die drei freigegebenen Objekte (601/616/617)
 * gültigen Statements weiterreichen können, auch wenn daneben
 * Statements für nicht freigegebene Konten enthalten sind (siehe
 * Auftrag: "Der spätere EBICS-Adapter MUSS pro Stmt/Acct/IBAN trennen").
 * Das Verhalten des bestehenden Ein-Konto-Datei-Imports in
 * `importer.py` bleibt davon unberührt.
 */
final class Camt053StatementValidator
{
    /**
     * @return array<int, array{stmt: DOMElement, iban: string}>
     *
     * @throws DeliveryRejectedException wenn KEIN Stmt existiert oder eine
     *     dokumentweite Ntry-Verschachtelungsverletzung gefunden wird -
     *     in beiden Fällen wird NICHTS aus dieser Lieferung freigegeben.
     */
    public function ermittleStatements(DOMDocument $dom): array
    {
        $stmtElemente = $this->findeElementeMitLocalName($dom, 'Stmt');
        if ($stmtElemente === []) {
            throw new DeliveryRejectedException(
                'CAMT.053-Datei enthält kein Stmt-Element; Kontozugehörigkeit nicht prüfbar - Lieferung abgelehnt.'
            );
        }

        $this->pruefeDokumentstruktur($dom);

        $ergebnis = [];
        foreach ($stmtElemente as $stmt) {
            $ergebnis[] = ['stmt' => $stmt, 'iban' => $this->ermittleIbanOderLeer($stmt)];
        }
        return $ergebnis;
    }

    /**
     * Dokumentweite Prüfung: JEDE im Dokument gefundene Ntry muss
     * direktes Kind EINES Stmt-Elements sein - geprüft über
     * `parentNode->localName`, NICHT über Objektidentität. Ein früherer
     * Ansatz verglich `spl_object_id()` zwischen einer über
     * `direkteKinderMitLocalName()` und einer über
     * `findeElementeMitLocalName()` gewonnenen Knotenliste - PHPs
     * DOM-Erweiterung liefert für denselben zugrundeliegenden libxml-
     * Knoten aber NICHT garantiert dieselbe PHP-Objektinstanz, wenn er
     * über zwei unabhängige Traversierungen erreicht wird (in dieser
     * Sitzung mit einem synthetischen Zwei-Statement-Dokument
     * reproduziert: derselbe Ntry-Knoten erhielt zwei verschiedene
     * `spl_object_id()`-Werte). Der Vergleich über den Elementnamen des
     * Elternknotens ist dagegen unabhängig von der Objektidentität.
     */
    private function pruefeDokumentstruktur(DOMDocument $dom): void
    {
        foreach ($this->findeElementeMitLocalName($dom, 'Ntry') as $ntry) {
            $eltern = $ntry->parentNode;
            if (!($eltern instanceof DOMElement) || $eltern->localName !== 'Stmt') {
                throw new DeliveryRejectedException(
                    'CAMT.053-Datei enthält mindestens eine Ntry außerhalb (oder tiefer verschachtelt als ein '
                    . 'direktes Kind) eines Stmt-Blocks - gesamte Lieferung abgelehnt, keine ungeprüfte '
                    . 'Kontobindung.'
                );
            }
        }
    }

    /**
     * Liefert die normalisierte IBAN eines Stmt, oder '' wenn das
     * Statement strukturell nicht eindeutig zuordenbar ist (kein/mehrere
     * Acct, keine/mehrere IBAN) - der Aufrufer (StatementSplitter)
     * entscheidet, dieses eine Statement zu quarantänieren, statt hier
     * eine Exception für die gesamte Lieferung zu werfen.
     */
    public function ermittleIbanOderLeer(DOMElement $stmt): string
    {
        $accts = $this->direkteKinderMitLocalName($stmt, 'Acct');
        if (count($accts) !== 1) {
            return '';
        }
        $ibans = [];
        foreach ($this->findeElementeMitLocalName($accts[0], 'IBAN') as $ibanElement) {
            $text = trim($ibanElement->textContent);
            if ($text !== '') {
                $ibans[strtoupper(str_replace(' ', '', $text))] = true;
            }
        }
        if (count($ibans) !== 1) {
            return '';
        }
        return array_key_first($ibans);
    }

    /** @return DOMElement[] */
    private function direkteKinderMitLocalName(DOMElement $element, string $localName): array
    {
        $ergebnis = [];
        foreach ($element->childNodes as $kind) {
            if ($kind instanceof DOMElement && $kind->localName === $localName) {
                $ergebnis[] = $kind;
            }
        }
        return $ergebnis;
    }

    /** @return DOMElement[] In Dokumentreihenfolge. */
    private function findeElementeMitLocalName(DOMNode $wurzel, string $localName): array
    {
        $ergebnis = [];
        $this->sammleRekursiv($wurzel, $localName, $ergebnis);
        return $ergebnis;
    }

    /** @param DOMElement[] $ergebnis */
    private function sammleRekursiv(DOMNode $knoten, string $localName, array &$ergebnis): void
    {
        foreach ($knoten->childNodes as $kind) {
            if ($kind instanceof DOMElement) {
                if ($kind->localName === $localName) {
                    $ergebnis[] = $kind;
                }
                $this->sammleRekursiv($kind, $localName, $ergebnis);
            }
        }
    }
}
