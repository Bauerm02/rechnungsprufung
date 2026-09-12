<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

/**
 * Baut minimale, synthetische CAMT.053-XML-Fragmente für Sicherheitstests.
 * Keine echten Bankdaten, keine realistische Feldvollständigkeit -
 * ausschließlich die für Camt053StatementValidator/StatementSplitter
 * relevante Struktur (Stmt/Acct/IBAN/Ntry-Verschachtelung).
 */
final class CamtFixture
{
    private const NS = 'urn:iso:std:iso:20022:tech:xsd:camt.053.001.02';

    public static function einStatement(string $iban, string $stmtId = 'STMT-001'): string
    {
        return self::dokument(self::stmtBlock($iban, $stmtId));
    }

    public static function zweiStatements(string $ibanA, string $ibanB): string
    {
        return self::dokument(self::stmtBlock($ibanA, 'STMT-A') . self::stmtBlock($ibanB, 'STMT-B'));
    }

    public static function stmtMitMehrerenAcct(string $ibanA, string $ibanB): string
    {
        $ntry = self::ntryBlock();
        return self::dokument(<<<XML
            <Stmt>
                <Id>STMT-MEHRERE-ACCT</Id>
                <Acct><Id><IBAN>{$ibanA}</IBAN></Id></Acct>
                <Acct><Id><IBAN>{$ibanB}</IBAN></Id></Acct>
                {$ntry}
            </Stmt>
            XML);
    }

    public static function ntryAusserhalbStmt(string $iban): string
    {
        $stmt = self::stmtBlock($iban, 'STMT-001');
        $ntry = self::ntryBlock();
        return self::dokument($stmt . $ntry);
    }

    public static function mitDoctype(string $iban): string
    {
        return "<?xml version=\"1.0\"?>\n<!DOCTYPE Document [<!ELEMENT Document ANY>]>\n"
            . '<Document xmlns="' . self::NS . '"><BkToCstmrStmt><GrpHdr><MsgId>MSG1</MsgId></GrpHdr>'
            . self::stmtBlock($iban, 'STMT-DOCTYPE') . '</BkToCstmrStmt></Document>';
    }

    /**
     * Enthält bewusst NUR eine `<!ENTITY`-Deklaration ohne das Wort
     * "DOCTYPE" im Text, um den ENTITY-spezifischen Zweig von
     * `XmlSafetyGuard::pruefeUnsicherenInhaltAb()` isoliert zu testen -
     * ein echter XXE-Angriff enthält syntaktisch fast immer auch
     * `<!DOCTYPE`, das dann bereits vorher greift (siehe `mitDoctype()`).
     */
    public static function mitEntity(string $iban): string
    {
        return "<?xml version=\"1.0\"?>\n"
            . "<!ENTITY xxe SYSTEM \"file:///etc/passwd\">\n"
            . '<Document xmlns="' . self::NS . '"><BkToCstmrStmt>'
            . self::stmtBlock($iban, 'STMT-XXE') . '</BkToCstmrStmt></Document>';
    }

    public static function kaputtesXml(): string
    {
        return '<Document><BkToCstmrStmt><Stmt>';
    }

    private static function dokument(string $stmts): string
    {
        return '<?xml version="1.0" encoding="UTF-8"?>' . "\n"
            . '<Document xmlns="' . self::NS . '">'
            . '<BkToCstmrStmt><GrpHdr><MsgId>MSG1</MsgId></GrpHdr>' . $stmts . '</BkToCstmrStmt>'
            . '</Document>';
    }

    private static function stmtBlock(string $iban, string $stmtId): string
    {
        $ntry = self::ntryBlock();
        return <<<XML
            <Stmt>
                <Id>{$stmtId}</Id>
                <Acct><Id><IBAN>{$iban}</IBAN></Id></Acct>
                {$ntry}
            </Stmt>
            XML;
    }

    private static function ntryBlock(): string
    {
        return <<<XML
            <Ntry>
                <Amt Ccy="EUR">100.00</Amt>
                <CdtDbtInd>CRDT</CdtDbtInd>
                <BookgDt><Dt>2026-01-05</Dt></BookgDt>
            </Ntry>
            XML;
    }
}
