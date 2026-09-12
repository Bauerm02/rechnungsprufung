<?php

declare(strict_types=1);

namespace EbicsDownloader\Tests\Camt;

use EbicsDownloader\Camt\Camt053StatementValidator;
use EbicsDownloader\Camt\XmlSafetyGuard;
use EbicsDownloader\Exceptions\DeliveryRejectedException;
use PHPUnit\Framework\TestCase;

final class Camt053StatementValidatorTest extends TestCase
{
    private Camt053StatementValidator $validator;
    private XmlSafetyGuard $xmlGuard;

    protected function setUp(): void
    {
        $this->validator = new Camt053StatementValidator();
        $this->xmlGuard = new XmlSafetyGuard();
    }

    public function testEinzelnesGueltigesStatementWirdErkannt(): void
    {
        $dom = $this->xmlGuard->parseSicher(CamtFixture::einStatement('AT000000000000000601'));
        $ergebnis = $this->validator->ermittleStatements($dom);

        self::assertCount(1, $ergebnis);
        self::assertSame('AT000000000000000601', $ergebnis[0]['iban']);
    }

    public function testMehrereStatementsVerschiedenerKontenWerdenAlleErkannt(): void
    {
        $dom = $this->xmlGuard->parseSicher(
            CamtFixture::zweiStatements('AT000000000000000601', 'AT000000000000000999')
        );
        $ergebnis = $this->validator->ermittleStatements($dom);

        self::assertCount(2, $ergebnis);
        $ibans = array_map(static fn (array $e) => $e['iban'], $ergebnis);
        self::assertSame(['AT000000000000000601', 'AT000000000000000999'], $ibans);
    }

    public function testStatementOhneStmtElementWirdAbgelehnt(): void
    {
        $dom = $this->xmlGuard->parseSicher('<Document xmlns="urn:x"><BkToCstmrStmt><GrpHdr/></BkToCstmrStmt></Document>');

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/kein Stmt-Element/');
        $this->validator->ermittleStatements($dom);
    }

    public function testNtryAusserhalbEinesStmtLehntGesamteLieferungAb(): void
    {
        // Codex-Fund aus der Python-Portierung: eine Ntry außerhalb (hier:
        // als Geschwister) jedes Stmt-Blocks darf NIE mitgelesen werden.
        $dom = $this->xmlGuard->parseSicher(CamtFixture::ntryAusserhalbStmt('AT000000000000000601'));

        $this->expectException(DeliveryRejectedException::class);
        $this->expectExceptionMessageMatches('/außerhalb/');
        $this->validator->ermittleStatements($dom);
    }

    public function testMehrereAcctInEinemStmtMachtDiesesStatementNichtEindeutig(): void
    {
        $dom = $this->xmlGuard->parseSicher(
            CamtFixture::stmtMitMehrerenAcct('AT000000000000000601', 'AT000000000000000999')
        );
        $ergebnis = $this->validator->ermittleStatements($dom);

        self::assertCount(1, $ergebnis);
        self::assertSame('', $ergebnis[0]['iban'], 'Mehrdeutiges Statement muss leere IBAN liefern, nicht die erste.');
    }

    public function testStatementOhneIbanIstNichtEindeutig(): void
    {
        $dom = $this->xmlGuard->parseSicher(
            '<Document xmlns="urn:x"><BkToCstmrStmt><GrpHdr/>'
            . '<Stmt><Id>S1</Id><Acct><Id></Id></Acct>'
            . '<Ntry><Amt Ccy="EUR">1.00</Amt><CdtDbtInd>CRDT</CdtDbtInd><BookgDt><Dt>2026-01-01</Dt></BookgDt></Ntry>'
            . '</Stmt></BkToCstmrStmt></Document>'
        );
        $ergebnis = $this->validator->ermittleStatements($dom);

        self::assertSame('', $ergebnis[0]['iban']);
    }
}
