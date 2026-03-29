from __future__ import annotations

import re

from invoice_automation.validation.normalizers import normalize_sender_name


AUTHORITY_SENDER_WHITELIST = tuple(
    normalize_sender_name(value)
    for value in (
        "Finanzamt Oesterreich",
        "Magistrat der Stadt Wien",
        "Magistrat Graz",
        "Stadt Graz",
        "Stadt Wien",
        "Wirtschaftskammer Oesterreich",
        "Wirtschaftskammer Steiermark",
        "Oesterreichische Gesundheitskasse",
        "Sozialversicherung der Selbstaendigen",
        "BVAEB",
        "SVS",
        "Land Steiermark",
    )
)

AUTHORITY_SENDER_PATTERNS = (
    re.compile(r"\bfinanzamt\b"),
    re.compile(r"\bmagistrat\b"),
    re.compile(r"\bstadt\s+(graz|wien)\b"),
    re.compile(r"\b(bezirksgericht|landesgericht|verwaltungsgericht|oberlandesgericht|gericht)\b"),
    re.compile(r"\bwirtschaftskammer\b"),
    re.compile(r"\b(sozialversicherung|gesundheitskasse|oegk|svs|bvaeb)\b"),
    re.compile(r"\bgemeinde\b"),
)

AUTHORITY_DOCUMENT_PATTERNS = (
    re.compile(r"\babgabenkonto\b"),
    re.compile(r"\bzwangsstrafverfuegung\b"),
    re.compile(r"\bnaechtigungsabgabe\b"),
    re.compile(r"\bgemeindeabgaben\b"),
    re.compile(r"\bsaeumniszuschlag\b"),
    re.compile(r"\bmahngebuehr\b"),
)

REMINDER_KEYWORDS = (
    "mahnung",
    "zahlungserinnerung",
)

STORNO_KEYWORDS = (
    "storno",
    "gutschrift",
    "storniert",
)

ORGANSCHAFT_KEYWORDS = (
    "organschaft",
    "innenumsatz",
)

INTERNAL_DEPOSIT_KEYWORDS = (
    "kautionsabrechnung",
    "kaution",
)

INTERNAL_EXPENSE_TRIGGER_KEYWORDS = (
    "abrechnung",
    "spesenabrechnung",
    "auslagenersatz",
    "kostenersatz",
    "kostenrueckerstattung",
)

INTERNAL_EXPENSE_DETAIL_KEYWORDS = (
    "stromkosten",
    "spesen",
    "kostenersatz",
    "wallbox",
    "zaehlerstand",
    "e-auto",
    "betriebliche ausgaben",
    "dienstlich verauslagt",
    "privat vorgestreckt",
    "reisekosten",
    "kilometergeld",
    "belegersatz",
)

INTERNAL_EXPENSE_EMPLOYEE_NAMES = (
    "markus bauer",
    "julia bauer",
)

REVERSE_CHARGE_KEYWORDS = (
    "reverse charge",
    "reverse-charge",
    "uebergang der steuerschuld",
    "steuerschuldnerschaft",
    "13b ustg",
    "19 ustg",
)

SUPPLIER_LEGAL_ENTITY_FORMS = (
    "gmbh",
    "gesellschaft m.b.h",
    "gesmbh",
    "ag",
    "aktiengesellschaft",
    "kg",
    "kommanditgesellschaft",
    "og",
    "offene gesellschaft",
    "e.u",
    "eingetragenes unternehmen",
    "verein",
    "stiftung",
    "anstalt",
    "gmbh & co kg",
    "gmbh und co kg",
    "rechtsanwaltskanzlei",
    "rechtsanwalt",
    "steuerberatung",
    "steuerberater",
    "notar",
    "ziviltechniker",
    "planungsbuero",
    "architekt",
)

SUPPLIER_LEGAL_ENTITY_PATTERNS = (
    re.compile(r"\bgmbh\b"),
    re.compile(r"\bgesmbh\b"),
    re.compile(r"\bag\b"),
    re.compile(r"\bkg\b"),
    re.compile(r"\bog\b"),
    re.compile(r"\be\.u\b"),
    re.compile(r"\bverein\b"),
    re.compile(r"\bstiftung\b"),
    re.compile(r"\brechtsanwaltskanzlei\b"),
    re.compile(r"\brechtsanwalt(?:skanzlei)?\b"),
    re.compile(r"\bsteuerberater(?:kanzlei)?\b"),
    re.compile(r"\bnotar\b"),
)

LEGAL_ENTITY_KEYWORDS = (
    "gmbh",
    "gesellschaft m.b.h",
    "gesmbh",
    " ag",
    " kg",
    " og",
    "e.u",
    "eingetragenes unternehmen",
    "verein",
    "stiftung",
    "anstalt",
    "gmbh & co",
    "gmbh und co",
    "rechtsanwaltskanzlei",
    "rechtsanwalt",
    "steuerberater",
    "notar",
)

PRIVATE_PERSON_KEYWORDS = (
    "markus bauer",
    "julia bauer",
)

PRIVATE_DOCUMENT_KEYWORDS = (
    "lottoschein",
    "lotto",
    "privatrechnung",
    "privatausgabe",
    "private ausgabe",
    "persoenliche ausgabe",
    "nicht betrieblich",
    "fuer private zwecke",
    "privat gekauft",
    "privat gebucht",
)

AUSTRIA_HINTS = (
    "oesterreich",
    " a-",
    " austria",
)

TRANSFER_REFERENCE_PATTERNS = (
    re.compile(
        r"(?:verwendungszweck|zahlungsreferenz|payment reference|transfer reference)\s*[:\-]?\s*([a-z0-9][a-z0-9\/\-\s]{3,40})"
    ),
    re.compile(
        r"bei der ueberweisung(?: bitte)?(?: als)?(?: verwendungszweck| referenz)?\s*[:\-]?\s*([a-z0-9][a-z0-9\/\-\s]{3,40})"
    ),
)

AUTO_PAID_VENDORS = tuple(
    normalize_sender_name(value)
    for value in (
        "Amazon Web Services",
        "Dropbox",
        "Close",
        "HeyGen",
        "Google Cloud",
        "Google Workspace",
        "Google One",
        "MessageLayer",
        "Airtable",
        "Paddle",
        "n8n",
        "Zapier",
        "BOE",
        "OpenAI",
        "ImmoMetrica",
        "Manus",
        "Microsoft 365",
        "Rize",
        "PDF.co",
    )
)

TRANSFER_EXCEPTION_VENDORS = tuple(
    normalize_sender_name(value)
    for value in (
        "Google Ads",
        "Google Ireland Limited",
        "Meta Platforms Ireland Limited",
        "Facebook",
        "LinkedIn Ireland Unlimited Company",
        "Kaffee Partner Austria GmbH",
        "Kafee Partner Austria GmbH",
        "DIALOG telekom GmbH",
        "DIALOG telekom GmbH & Co KG",
        "EnergieDirect Austria GmbH",
        "HoT Telekom und Service GmbH",
        "A1 Telekom Austria AG",
        "MANZ'sche Verlags- und Universitaetsbuchhandlung GmbH",
        "MANZ",
        "Wien Energie Vertrieb GmbH & Co KG",
        "Wien Energie",
        "Purkarthofer Hausverwaltung",
    )
)

AUTO_PAID_TEXT_KEYWORDS = (
    "kreditkarte",
    "mastercard",
    "paypal",
    "eingezogen wird",
    "abgebucht wird",
    "lastschrift",
    "sepa-mandat",
    "einzugsermaechtigung",
    "von ihrem konto",
)

STAMP_ALLOWED_PROJECTS = {
    "WSG",
    "7DI",
    "GB",
    "AC3",
    "PW1",
    "PAULUSGASSE",
    "TS",
    "MOON",
}

STAMP_MRG_KEYWORDS = (
    "grundsteuer",
    "wasserversorgung",
    "wasserbezugsgebuehren",
    "kanalgebuehren",
    "kanalbenutzungsgebuehr",
    "kanalbereitstellungsgebuehr",
    "kanalraeumung",
    "muellabfuhr",
    "muellgebuehren",
    "rauchfangkehrer",
    "schaedlingsbekaempfung",
    "hausbetreuung",
    "stiegenhausreinigung",
    "schneeraeumung",
    "gruenflaechenbetreuung",
    "allgemeinstrom",
    "hausstrom",
    "notruftelefon",
    "gebaeudeversicherung",
    "feuer",
    "haftpflicht",
    "leitungswasser",
    "sturm",
    "glasbruch",
    "eich",
    "ablesekosten",
    "verwaltungshonorar",
)

STAMP_TEILANWENDUNG_KEYWORDS = (
    "wartung lift",
    "aufzugspruefung",
    "heizungsanlage",
    "waermepumpe",
    "therme",
    "garagentor",
    "brandschutz",
    "feuerloescher",
    "lueftungsanlage",
    "rechtsschutz",
)

STAMP_TENANT_DAMAGE_KEYWORDS = (
    "mieter-beschaedigung",
    "mieter beschaedigung",
    "durch einen mieter verursacht",
    "vom mieter verursacht",
)

PROJECT_CODE_ALIASES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        [
            ("7 destinations immobilien gmbh", "7Des"),
            ("7d bautraeger gmbh", "7DB"),
            ("7d bautraeger", "7DB"),
            ("ac 3 projekterrichtungs gmbh", "AC3"),
            ("bg7 projekterrichtungs gmbh", "BG7"),
            ("carl g7 immo gmbh", "CRG7"),
            ("dr-hanisch-weg projektgesellschaft mbh", "RW21"),
            ("gutenberg projekt gmbh", "GB"),
            ("gz errichtung gmbh", "GZ"),
            ("steyeregg 217 projektentwicklung gmbh", "Steyer"),
            ("hsst projektentwicklung gmbh", "HSST"),
            ("lo1517 projektentwicklung gmbh", "LO1517"),
            ("mabau beteiligungs gmbh", "MBB"),
            ("mg9 projektentwicklungs gmbh", "MG9"),
            ("perkonigweg 7 gmbh", "JFP7"),
            ("primelweg projekt gmbh", "PW1"),
            ("projekterrichtungsgesellschaft hangstrasse velden am woerthersee gmbh", "HS"),
            ("projekterrichtungsgesellschaft paulusgasse gmbh", "Paulusgasse"),
            ("projektgesellschaft speckbachergasse 25 gmbh", "Speck"),
            ("pschorngasse 48 projektentwicklung gmbh", "Psch"),
            ("pv winterleiten gmbh", "PV"),
            ("pwa projektentwicklungs gmbh", "PWA"),
            ("reinerweg projektgesellschaft mbh", "RW9"),
            ("seeblick velden projektentwicklungs gmbh", "JFP9"),
            ("sieben dorfer immobilien gmbh", "7DI"),
            ("thalstrasse 87-89 projekt gmbh", "TS"),
            ("the moon projektentwicklungs gmbh", "Moon"),
            ("waltendorfer hauptstrasse 8 projekt gmbh", "WH8"),
            ("wiseman saigh gmbh", "WSG"),
            ("hotel villa flora gmbh", "HTV"),
            ("pension redlinghofer gmbh", "HTV"),
            ("pension redlinghofer", "HTV"),
        ],
        key=lambda item: len(item[0]),
        reverse=True,
    )
)

OWN_COMPANY_KEYWORDS = tuple(alias for alias, _code in PROJECT_CODE_ALIASES) + (
    "7d bautraeger gmbh",
    "sieben dorfer immobilien gmbh",
    "carl g7 immo gmbh",
    "mabau beteiligungs gmbh",
    "gz errichtung gmbh",
    "hsst projektentwicklung gmbh",
    "hotel villa flora gmbh",
)
