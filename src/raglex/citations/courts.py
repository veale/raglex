"""Neutral-citation court registry — the substrate for the snowball (§5, §5a).

A neutral citation has a recognisable *shape* (``[YEAR] COURT NUMBER`` in most
common-law systems, ``YEAR COURT NUMBER`` in Canada/India) but the COURT token is
an open set. We detect the shape generically, then look the token up here:

- a **known** court tells us the jurisdiction (and, eventually, which adapter can
  fetch it) — the citation resolves or queues against the right source;
- an **unknown** court is exactly the snowball signal: the corpus is citing a body
  we don't harvest yet. Those surface in ``snowball`` ranked by frequency, so a
  human (or an agent) sees "47 pending citations to 'EWHC' — no adapter" and knows
  what to build next.

Growing coverage is a data edit here, not a code change. Each entry says the
jurisdiction and whether an adapter exists today (``adapter``), so the worklist
can separate "harvestable now" from "needs a new adapter".

## Three things this registry has to get right

**1. Codes collide across jurisdictions, and bracket style is the disambiguator.**
``FCA`` is both the Federal Court of *Australia* and the Federal Court of Appeal of
*Canada*; ``SCC`` is the Supreme Court of *Canada* and also the dominant Indian law
*reporter* (Supreme Court Cases). The registry therefore allows **several courts per
code** and disambiguates on the citation's shape: Australia writes ``[2020] FCA 1``
(bracketed), Canada writes ``2020 FCA 1`` (bare year first). ``lookup(code)`` keeps the
old single-answer behaviour; ``lookup(code, bracketed=False)`` gets the Canadian one.
Keying a plain dict on ``code`` — as this module used to — silently dropped whichever
entry was declared first, so Canadian Federal Court of Appeal citations were being
reported as Australian.

**2. Jurisdiction is often knowable when the court is not.** Most Commonwealth
medium-neutral citations are ``ISO-country-code + tribunal`` (``KESC``, ``GHASC``,
``FJHC``, ``ZAGPPHC``), so an unrecognised token that *starts* with a known country
prefix still places the citation in a country. ``lookup_prefix`` returns that
jurisdiction's **generic bucket** — a real classification ("New Zealand court
(unidentified)") instead of "unknown", which is what keeps a corpus-wide view honest
while the long tail of tribunal codes fills in.

**3. Court-issued neutral citations are not the same as LII-assigned ones.** AustLII,
NZLII and the Laws.Africa LIIs mint neutral-*looking* identifiers for judgments predating
(or outside) official neutral citation, and Kenya's ``eKLR`` is a database identifier
rather than a citation at all. ``authority`` records which is which, so a "neutral
citation" that is really a database key is never presented as court-issued.

Nothing here has a fetch route outside the UK/Ireland sources — that is deliberate. The
point of registering a court with ``adapter=None`` is to give the citation a home: it
classifies, it counts, and it ranks in the snowball as demand for an adapter that does
not exist yet.
"""

from __future__ import annotations

from dataclasses import dataclass

# Authority of the identifier in the "court" slot.
COURT_ISSUED = "court"   # an official, court-assigned neutral citation
LII_ASSIGNED = "lii"     # AustLII/NZLII/Laws.Africa-style pseudo-neutral, or a database key


@dataclass(frozen=True, slots=True)
class Court:
    code: str
    # CONVENTION: `name` is the natural-language name the UI shows wherever a
    # court/body appears (Explore's courts rail, drill entries, facet sentences)
    # — users see "Immigration & Asylum Tribunal", never "ukaitur". Every court
    # code a future adapter introduces MUST be registered here with a real name;
    # an unregistered code falls back to a prettified slug, which reads wrong.
    # (Reporter series are NOT courts and never belong in this registry.)
    name: str
    jurisdiction: str  # ISO-ish: GB, IE, CA, AU, NZ, IN, EU …
    adapter: str | None = None  # the source adapter that can fetch it, if any
    bracketed: bool = True  # [2024] CODE n  vs  2024 CODE n (CA/IN)
    # court-issued neutral citation vs an LII/database-assigned pseudo-neutral one
    authority: str = COURT_ISSUED
    # True for the per-jurisdiction catch-all rows, which are not real courts
    generic: bool = False


# Seed set — common-law neutral-citation courts. Extend freely; the detector
# already finds *unknown* codes, this just classifies the ones we recognise.
COURTS: tuple[Court, ...] = (
    # ---- United Kingdom (Find Case Law covers these) ----------------------
    Court("UKSC", "UK Supreme Court", "GB", adapter="uk-caselaw"),
    Court("UKPC", "Judicial Committee of the Privy Council", "GB", adapter="uk-caselaw"),
    Court("UKHL", "House of Lords", "GB", adapter="uk-caselaw"),
    Court("EWCA", "Court of Appeal (England & Wales)", "GB", adapter="uk-caselaw"),
    Court("EWHC", "High Court (England & Wales)", "GB", adapter="uk-caselaw"),
    Court("EWCOP", "Court of Protection", "GB", adapter="uk-caselaw"),
    Court("EWFC", "Family Court", "GB", adapter="uk-caselaw"),
    Court("UKUT", "Upper Tribunal", "GB", adapter="uk-caselaw"),
    Court("UKFTT", "First-tier Tribunal", "GB", adapter="uk-caselaw"),
    Court("UKAITUR", "Immigration & Asylum Tribunal", "GB", adapter="uk-caselaw"),
    Court("EAT", "Employment Appeal Tribunal", "GB", adapter="uk-caselaw"),  # TNA slug 'eat'
    # UK courts/tribunals recognised but not yet harvestable (on BAILII, not Find Case
    # Law) — classified so they leave the "unknown court" noise and carry a jurisdiction.
    Court("UKEAT", "Employment Appeal Tribunal (pre-2022)", "GB"),
    Court("UKET", "Employment Tribunal", "GB"),
    Court("UKAIT", "Asylum & Immigration Tribunal", "GB"),
    Court("UKIAT", "Immigration Appeal Tribunal", "GB"),
    Court("UKAITUR", "Immigration & Asylum Chamber (BAILII UTIAC dump)", "GB"),
    Court("CAT", "Competition Appeal Tribunal", "GB"),
    Court("SIAC", "Special Immigration Appeals Commission", "GB"),
    # BAILII case-token heads for tribunals kept under their own slug prefix (the slug's
    # first segment) rather than a UKFTT/UKUT chamber path.
    Court("UKVAT", "VAT & Duties Tribunal (pre-2009)", "GB"),
    Court("UKSPC", "Special Commissioners of Income Tax", "GB"),
    Court("UKFSM", "Financial Services & Markets Tribunal", "GB"),
    Court("UKIT", "Information Tribunal", "GB"),
    Court("DRS", "Nominet .uk Domain Dispute Resolution Service", "GB"),
    Court("PBRA", "Parole Board (reconsideration assessments)", "GB"),
    Court("EWCST", "Care Standards Tribunal (England & Wales)", "GB"),
    Court("EWLands", "Lands Tribunal (England & Wales)", "GB"),
    Court("EWPCC", "Patents County Court (England & Wales)", "GB"),
    # Northern Ireland (BAILII)
    Court("NIQB", "NI High Court, Queen's Bench", "GB"),
    Court("NIKB", "NI High Court, King's Bench", "GB"),
    Court("NICA", "NI Court of Appeal", "GB"),
    Court("NICh", "NI High Court, Chancery", "GB"),
    Court("NIFam", "NI High Court, Family", "GB"),
    Court("NIMag", "NI Magistrates' Court", "GB"),
    Court("NICC", "NI Crown Court", "GB"),
    # Scotland (BAILII / Scottish Courts)
    Court("CSOH", "Court of Session, Outer House", "GB"),
    Court("CSIH", "Court of Session, Inner House", "GB"),
    Court("HCJAC", "High Court of Justiciary, Appeal Court", "GB"),
    Court("HCJ", "High Court of Justiciary", "GB"),
    Court("SAC", "Sheriff Appeal Court", "GB"),
    Court("ScotCS", "Court of Session (BAILII legacy code)", "GB"),
    Court("ScotHC", "High Court of Justiciary (BAILII legacy code)", "GB"),
    Court("ScotSAC", "Sheriff Appeal Court (BAILII code)", "GB"),
    Court("ScotSC", "Sheriff Court (BAILII code)", "GB"),
    # NI High Court (BAILII keys its divisions under a NIHC slug head: nihc/qb, nihc/ch…)
    Court("NIHC", "NI High Court", "GB"),
    Court("NIIT", "NI Industrial Tribunal", "GB"),
    Court("NIFET", "NI Fair Employment Tribunal", "GB"),
    Court("NISSCSC", "NI Social Security & Child Support Commissioners", "GB"),

    # ---- Ireland ----------------------------------------------------------
    # BAILII holds them; imported via the BAILII zip/file paths, keyed iehc/2008/56
    # exactly like the citation candidate.
    Court("IESC", "Supreme Court of Ireland", "IE"),
    Court("IESCDET", "Supreme Court of Ireland (determinations)", "IE"),
    Court("IECA", "Court of Appeal of Ireland", "IE"),
    Court("IEHC", "High Court of Ireland", "IE"),
    Court("IECCA", "Court of Criminal Appeal of Ireland", "IE"),
    Court("IECC", "Circuit Court of Ireland", "IE"),
    Court("IEDC", "District Court of Ireland", "IE"),
    Court("IEIC", "Information Commissioner (Ireland)", "IE"),
    Court("IECompA", "Competition Authority (Ireland)", "IE"),
    Court("IEDPC", "Data Protection Commission (Ireland)", "IE"),

    # ---- Crown Dependencies (BAILII /je/, /gg/, /im/) ---------------------
    # BAILII keys Jersey judgments by the source rather than the court: "UR" is the
    # unreported Royal Court series and "JLR" the Jersey Law Reports — the judgment's own
    # citation (JRC / JCA) is recovered from its text and minted as an alias.
    Court("JRC", "Royal Court of Jersey", "JE"),
    Court("JCA", "Jersey Court of Appeal", "JE"),
    Court("UR", "Jersey judgments (Royal Court, BAILII unreported series)", "JE"),
    Court("JLR", "Jersey Law Reports (BAILII series)", "JE"),
    Court("GLR", "Guernsey Law Reports (BAILII series)", "GG"),
    Court("GRC", "Royal Court of Guernsey", "GG"),

    # ---- Offshore & international commercial courts (BAILII /ky/, /ae/, /qa/, /sh/, /io/) --
    Court("GCCI", "Grand Court of the Cayman Islands", "KY"),
    Court("DIFC", "Dubai International Financial Centre Courts", "AE"),
    Court("ADGMCFI", "Abu Dhabi Global Market Courts, Court of First Instance", "AE"),
    Court("ADGMCA", "Abu Dhabi Global Market Courts, Court of Appeal", "AE"),
    Court("QIC", "Qatar International Court", "QA"),
    Court("SHSC", "Supreme Court of St Helena", "SH"),
    Court("SHCA", "Court of Appeal of St Helena", "SH"),
    Court("BIOT", "Court of the British Indian Ocean Territory", "IO"),

    # ---- Australia --------------------------------------------------------
    # Neutral citation adopted 1998–2010; AustLII additionally assigned RETROSPECTIVE
    # neutral-style tags to older judgments which are not court-issued — a pre-adoption
    # "neutral citation" should be read as LII-derived (see module docstring).
    Court("HCA", "High Court of Australia", "AU"),
    Court("FCA", "Federal Court of Australia", "AU"),          # collides with CA FCA
    Court("FCAFC", "Full Court of the Federal Court of Australia", "AU"),
    Court("FamCA", "Family Court of Australia", "AU"),
    Court("FamCAFC", "Family Court of Australia (Full Court)", "AU"),
    Court("FedCFamC1A", "Federal Circuit and Family Court (Div 1 Appellate)", "AU"),
    Court("FedCFamC2F", "Federal Circuit and Family Court (Div 2 Family)", "AU"),
    Court("FCCA", "Federal Circuit Court of Australia", "AU"),
    Court("FMCA", "Federal Magistrates Court of Australia", "AU"),
    Court("FMCAfam", "Federal Magistrates Court (family)", "AU"),
    Court("AATA", "Administrative Appeals Tribunal (Australia)", "AU"),
    Court("ARTA", "Administrative Review Tribunal (Australia)", "AU"),
    Court("ACTCA", "ACT Court of Appeal", "AU"),
    Court("ACTSC", "Supreme Court of the ACT", "AU"),
    Court("ACTMC", "ACT Magistrates Court", "AU"),
    Court("NSWCA", "NSW Court of Appeal", "AU"),
    Court("NSWCCA", "NSW Court of Criminal Appeal", "AU"),
    Court("NSWSC", "NSW Supreme Court", "AU"),
    Court("NSWDC", "NSW District Court", "AU"),
    Court("NSWLEC", "NSW Land and Environment Court", "AU"),
    Court("NSWCATAD", "NSW Civil & Administrative Tribunal (Admin & Equal Opp)", "AU"),
    Court("NSWIRComm", "NSW Industrial Relations Commission", "AU"),
    Court("NTCA", "NT Court of Appeal", "AU"),
    Court("NTCCA", "NT Court of Criminal Appeal", "AU"),
    Court("NTSC", "Supreme Court of the Northern Territory", "AU"),
    Court("QCA", "Queensland Court of Appeal", "AU"),
    Court("QSC", "Supreme Court of Queensland", "AU"),
    Court("QDC", "Queensland District Court", "AU"),
    Court("QCAT", "Queensland Civil and Administrative Tribunal", "AU"),
    Court("SASCA", "SA Court of Appeal", "AU"),
    Court("SASCFC", "Supreme Court of South Australia (Full Court)", "AU"),
    Court("SASC", "Supreme Court of South Australia", "AU"),
    Court("TASCCA", "Tasmania Court of Criminal Appeal", "AU"),
    Court("TASFC", "Supreme Court of Tasmania (Full Court)", "AU"),
    Court("TASSC", "Supreme Court of Tasmania", "AU"),
    Court("VSCA", "Victoria Court of Appeal", "AU"),
    Court("VSC", "Supreme Court of Victoria", "AU"),
    Court("VCC", "County Court of Victoria", "AU"),
    Court("VCAT", "Victorian Civil and Administrative Tribunal", "AU"),
    Court("WASCA", "WA Court of Appeal", "AU"),
    Court("WASC", "Supreme Court of Western Australia", "AU"),
    Court("WADC", "District Court of Western Australia", "AU"),

    # ---- Canada (bracketless: 2024 SCC 1) ---------------------------------
    # Several provinces flipped Queen's Bench → King's Bench in 2022–23, so BOTH the
    # QB and KB forms stay registered: which one is correct depends on the date.
    Court("SCC", "Supreme Court of Canada", "CA", bracketed=False),
    Court("CanLII", "CanLII identifier (court in trailing parentheses)", "CA",
          bracketed=False, authority=LII_ASSIGNED),
    Court("FCA", "Federal Court of Appeal (Canada)", "CA", bracketed=False),
    Court("FC", "Federal Court (Canada)", "CA", bracketed=False),
    Court("TCC", "Tax Court of Canada", "CA", bracketed=False),
    Court("CMAC", "Court Martial Appeal Court of Canada", "CA", bracketed=False),
    Court("ONCA", "Court of Appeal for Ontario", "CA", bracketed=False),
    Court("ONSC", "Ontario Superior Court of Justice", "CA", bracketed=False),
    Court("ONSCDC", "Ontario Divisional Court", "CA", bracketed=False),
    Court("ONCJ", "Ontario Court of Justice", "CA", bracketed=False),
    Court("BCCA", "British Columbia Court of Appeal", "CA", bracketed=False),
    Court("BCSC", "British Columbia Supreme Court", "CA", bracketed=False),
    Court("BCPC", "British Columbia Provincial Court", "CA", bracketed=False),
    Court("ABCA", "Alberta Court of Appeal", "CA", bracketed=False),
    Court("ABQB", "Alberta Court of Queen's Bench", "CA", bracketed=False),
    Court("ABKB", "Alberta Court of King's Bench", "CA", bracketed=False),
    Court("ABPC", "Alberta Provincial Court", "CA", bracketed=False),
    Court("QCCA", "Quebec Court of Appeal", "CA", bracketed=False),
    Court("QCCS", "Quebec Superior Court", "CA", bracketed=False),
    Court("QCCQ", "Court of Quebec", "CA", bracketed=False),
    Court("MBCA", "Manitoba Court of Appeal", "CA", bracketed=False),
    Court("MBQB", "Manitoba Court of Queen's Bench", "CA", bracketed=False),
    Court("MBKB", "Manitoba Court of King's Bench", "CA", bracketed=False),
    Court("SKCA", "Saskatchewan Court of Appeal", "CA", bracketed=False),
    Court("SKQB", "Saskatchewan Court of Queen's Bench", "CA", bracketed=False),
    Court("SKKB", "Saskatchewan Court of King's Bench", "CA", bracketed=False),
    Court("NSCA", "Nova Scotia Court of Appeal", "CA", bracketed=False),
    Court("NSSC", "Nova Scotia Supreme Court", "CA", bracketed=False),
    Court("NSPC", "Nova Scotia Provincial Court", "CA", bracketed=False),
    Court("NBCA", "New Brunswick Court of Appeal", "CA", bracketed=False),
    Court("NBQB", "New Brunswick Court of Queen's Bench", "CA", bracketed=False),
    Court("NBKB", "New Brunswick Court of King's Bench", "CA", bracketed=False),
    Court("NLCA", "Newfoundland & Labrador Court of Appeal", "CA", bracketed=False),
    Court("NLSC", "Newfoundland & Labrador Supreme Court", "CA", bracketed=False),
    Court("PECA", "Prince Edward Island Court of Appeal", "CA", bracketed=False),
    Court("PESC", "Prince Edward Island Supreme Court", "CA", bracketed=False),
    Court("YKCA", "Yukon Court of Appeal", "CA", bracketed=False),
    Court("YKSC", "Yukon Supreme Court", "CA", bracketed=False),
    Court("NWTCA", "Northwest Territories Court of Appeal", "CA", bracketed=False),
    Court("NWTSC", "Northwest Territories Supreme Court", "CA", bracketed=False),
    Court("NUCA", "Nunavut Court of Appeal", "CA", bracketed=False),
    Court("NUCJ", "Nunavut Court of Justice", "CA", bracketed=False),

    # Canadian federal tribunals and boards. The bulk Canadian corpora key these by
    # their own short codes; unregistered, the UI prettifies the slug and a reader
    # sees "Sst", "Rad", "Citt" rather than the body's name (the convention in
    # court_label: every code an adapter introduces needs a name here).
    Court("SST", "Social Security Tribunal of Canada", "CA", bracketed=False),
    Court("RAD", "Refugee Appeal Division (IRB)", "CA", bracketed=False),
    Court("RPD", "Refugee Protection Division (IRB)", "CA", bracketed=False),
    Court("CITT", "Canadian International Trade Tribunal", "CA", bracketed=False),
    Court("FPSLREB", "Federal Public Sector Labour Relations and Employment Board",
          "CA", bracketed=False),
    Court("CIRB", "Canada Industrial Relations Board", "CA", bracketed=False),
    Court("CHRT", "Canadian Human Rights Tribunal", "CA", bracketed=False),
    Court("OHSTC", "Occupational Health and Safety Tribunal Canada", "CA", bracketed=False),
    Court("PSDPT", "Public Servants Disclosure Protection Tribunal", "CA", bracketed=False),
    Court("OIC", "Office of the Information Commissioner of Canada", "CA", bracketed=False),
    Court("RLLR", "Refugee Law Lab Reporter", "CA", bracketed=False),
    Court("NSSM", "Nova Scotia Small Claims Court", "CA", bracketed=False),
    Court("NSFC", "Nova Scotia Family Court", "CA", bracketed=False),
    Court("CT", "Competition Tribunal (Canada)", "CA", bracketed=False),

    # ---- New Zealand ------------------------------------------------------
    Court("NZSC", "Supreme Court of New Zealand", "NZ"),
    Court("NZCA", "Court of Appeal of New Zealand", "NZ"),
    Court("NZHC", "High Court of New Zealand", "NZ"),
    Court("NZDC", "District Court of New Zealand", "NZ"),
    Court("NZFC", "Family Court of New Zealand", "NZ"),
    Court("NZEmpC", "Employment Court of New Zealand", "NZ"),
    Court("NZEnvC", "Environment Court of New Zealand", "NZ"),
    Court("NZERA", "Employment Relations Authority (NZ)", "NZ"),
    Court("NZHRRT", "Human Rights Review Tribunal (NZ)", "NZ"),
    Court("NZWT", "Waitangi Tribunal", "NZ"),
    Court("NZLCDT", "NZ Lawyers and Conveyancers Disciplinary Tribunal", "NZ"),

    # ---- Singapore --------------------------------------------------------
    # The parenthetical suffixes in SGHC(A)/SGHC(I) are part of the court token.
    Court("SGCA", "Singapore Court of Appeal", "SG"),
    Court("SGHC", "Singapore High Court", "SG"),
    Court("SGHC(A)", "Singapore High Court (Appellate Division)", "SG"),
    Court("SGHC(I)", "Singapore International Commercial Court", "SG"),
    Court("SGHCR", "Singapore High Court (Registrar)", "SG"),
    Court("SGHCF", "Singapore High Court (Family Division)", "SG"),
    Court("SGDC", "Singapore District Court", "SG"),
    Court("SGMC", "Singapore Magistrates' Court", "SG"),
    Court("SGFC", "Singapore Family Court", "SG"),
    Court("SICC", "Singapore International Commercial Court (BAILII slug head)", "SG"),

    # ---- Hong Kong (neutral citation from 2018, per Practice Direction 5.5) --
    # Pre-2018 HK cases are cited by REGISTRY CASE NUMBER ("FACV 1/2018"), which is a
    # different animal — see hk_case_number in citations.grammars.
    Court("HKCFA", "Hong Kong Court of Final Appeal", "HK"),
    Court("HKCA", "Hong Kong Court of Appeal", "HK"),
    Court("HKCFI", "Hong Kong Court of First Instance", "HK"),
    Court("HKDC", "Hong Kong District Court", "HK"),
    Court("HKFC", "Hong Kong Family Court", "HK"),
    Court("HKLdT", "Hong Kong Lands Tribunal", "HK"),
    Court("HKCT", "Hong Kong Competition Tribunal", "HK"),
    Court("HKLT", "Hong Kong Labour Tribunal", "HK"),

    # ---- Malaysia ---------------------------------------------------------
    # Neutral citation is patchy; practice leans on the MLJ/CLJ reporters plus a
    # court-in-parentheses, so these tokens are comparatively rare in the wild.
    Court("MYFC", "Federal Court of Malaysia", "MY"),
    Court("MYCA", "Court of Appeal of Malaysia", "MY"),
    Court("MYHC", "High Court of Malaysia", "MY"),

    # ---- India (neutral citation introduced 2023; colon-delimited) --------
    Court("INSC", "Supreme Court of India", "IN", bracketed=False),
    Court("DHC", "Delhi High Court", "IN", bracketed=False),
    Court("BHC", "Bombay High Court", "IN", bracketed=False),
    Court("MHC", "Madras High Court", "IN", bracketed=False),
    Court("CHC", "Calcutta High Court", "IN", bracketed=False),
    Court("KAHC", "Karnataka High Court", "IN", bracketed=False),
    Court("KHC", "Kerala High Court", "IN", bracketed=False),
    Court("AHC", "Allahabad High Court", "IN", bracketed=False),
    Court("GUJHC", "Gujarat High Court", "IN", bracketed=False),
    Court("PHHC", "Punjab & Haryana High Court", "IN", bracketed=False),
    Court("TSHC", "Telangana High Court", "IN", bracketed=False),

    # ---- South Africa -----------------------------------------------------
    # "Z" + ISO country letter: ZA + division code, which can run 6–8 characters.
    Court("ZACC", "Constitutional Court of South Africa", "ZA"),
    Court("ZASCA", "Supreme Court of Appeal of South Africa", "ZA"),
    Court("ZAGPPHC", "High Court, Gauteng Division, Pretoria", "ZA"),
    Court("ZAGPJHC", "High Court, Gauteng Local Division, Johannesburg", "ZA"),
    Court("ZAWCHC", "High Court, Western Cape Division", "ZA"),
    Court("ZAKZDHC", "High Court, KwaZulu-Natal Division, Durban", "ZA"),
    Court("ZAKZPHC", "High Court, KwaZulu-Natal Division, Pietermaritzburg", "ZA"),
    Court("ZAECGHC", "High Court, Eastern Cape Division, Grahamstown", "ZA"),
    Court("ZAFSHC", "High Court, Free State Division", "ZA"),
    Court("ZANWHC", "High Court, North West Division", "ZA"),
    Court("ZALMPPHC", "High Court, Limpopo Division, Polokwane", "ZA"),
    Court("ZANCHC", "High Court, Northern Cape Division", "ZA"),
    Court("ZALC", "Labour Court of South Africa", "ZA"),
    Court("ZALAC", "Labour Appeal Court of South Africa", "ZA"),
    Court("ZALCC", "Land Claims Court of South Africa", "ZA"),
    Court("ZACT", "Competition Tribunal of South Africa", "ZA"),

    # ---- Africa: the Laws.Africa / LII medium-neutral-citation family -----
    # ISO-3166 country code + tribunal abbreviation. On these platforms the MNC is
    # frequently assigned by the LII's Akoma Ntoso pipeline rather than issued by the
    # court, so authority is recorded as LII-assigned unless known otherwise.
    Court("KESC", "Supreme Court of Kenya", "KE", authority=LII_ASSIGNED),
    Court("KECA", "Court of Appeal of Kenya", "KE", authority=LII_ASSIGNED),
    Court("KEHC", "High Court of Kenya", "KE", authority=LII_ASSIGNED),
    Court("KEELRC", "Employment & Labour Relations Court (Kenya)", "KE",
          authority=LII_ASSIGNED),
    Court("KEELC", "Environment & Land Court (Kenya)", "KE", authority=LII_ASSIGNED),
    Court("GHASC", "Supreme Court of Ghana", "GH", authority=LII_ASSIGNED),
    Court("GHACA", "Court of Appeal of Ghana", "GH", authority=LII_ASSIGNED),
    Court("GHAHC", "High Court of Ghana", "GH", authority=LII_ASSIGNED),
    Court("TZCA", "Court of Appeal of Tanzania", "TZ", authority=LII_ASSIGNED),
    Court("TZHC", "High Court of Tanzania", "TZ", authority=LII_ASSIGNED),
    Court("UGSC", "Supreme Court of Uganda", "UG", authority=LII_ASSIGNED),
    Court("UGCA", "Court of Appeal of Uganda", "UG", authority=LII_ASSIGNED),
    Court("UGHC", "High Court of Uganda", "UG", authority=LII_ASSIGNED),
    Court("NGSC", "Supreme Court of Nigeria", "NG", authority=LII_ASSIGNED),
    Court("NGCA", "Court of Appeal of Nigeria", "NG", authority=LII_ASSIGNED),
    Court("ZMSC", "Supreme Court of Zambia", "ZM", authority=LII_ASSIGNED),
    Court("ZMHC", "High Court of Zambia", "ZM", authority=LII_ASSIGNED),
    Court("MWSC", "Supreme Court of Appeal of Malawi", "MW", authority=LII_ASSIGNED),
    Court("MWHC", "High Court of Malawi", "MW", authority=LII_ASSIGNED),
    Court("ZWSC", "Supreme Court of Zimbabwe", "ZW", authority=LII_ASSIGNED),
    Court("ZWCC", "Constitutional Court of Zimbabwe", "ZW", authority=LII_ASSIGNED),
    Court("ZWHHC", "High Court of Zimbabwe, Harare", "ZW", authority=LII_ASSIGNED),
    Court("NASC", "Supreme Court of Namibia", "NA", authority=LII_ASSIGNED),
    Court("NAHC", "High Court of Namibia", "NA", authority=LII_ASSIGNED),
    Court("NAHCMD", "High Court of Namibia, Main Division", "NA", authority=LII_ASSIGNED),
    Court("SZSC", "Supreme Court of Eswatini", "SZ", authority=LII_ASSIGNED),
    Court("SZHC", "High Court of Eswatini", "SZ", authority=LII_ASSIGNED),
    Court("BWCA", "Court of Appeal of Botswana", "BW", authority=LII_ASSIGNED),
    Court("BWHC", "High Court of Botswana", "BW", authority=LII_ASSIGNED),
    Court("MUSC", "Supreme Court of Mauritius", "MU", authority=LII_ASSIGNED),
    Court("SCSC", "Supreme Court of Seychelles", "SC", authority=LII_ASSIGNED),

    # ---- Caribbean --------------------------------------------------------
    # The CCJ is a regional apex court: its (AJ)/(OJ) suffix marks Appellate vs
    # Original jurisdiction and is part of the citation.
    Court("CCJ", "Caribbean Court of Justice", "CARICOM"),
    Court("TTCA", "Court of Appeal of Trinidad & Tobago", "TT"),
    Court("TTHC", "High Court of Trinidad & Tobago", "TT"),
    Court("JMCA", "Court of Appeal of Jamaica", "JM"),
    Court("JMSC", "Supreme Court of Jamaica", "JM"),
    Court("BBCA", "Court of Appeal of Barbados", "BB"),
    Court("BBHC", "High Court of Barbados", "BB"),
    Court("BSSC", "Supreme Court of the Bahamas", "BS"),
    Court("GYCA", "Court of Appeal of Guyana", "GY"),
    Court("BZCA", "Court of Appeal of Belize", "BZ"),

    # ---- Pacific (the PacLII ecosystem) -----------------------------------
    # PacLII identifiers dominate and are frequently platform-assigned rather than
    # court-issued; reporters are sparse across the region.
    Court("FJSC", "Supreme Court of Fiji", "FJ", authority=LII_ASSIGNED),
    Court("FJCA", "Court of Appeal of Fiji", "FJ", authority=LII_ASSIGNED),
    Court("FJHC", "High Court of Fiji", "FJ", authority=LII_ASSIGNED),
    Court("PGSC", "Supreme Court of Papua New Guinea", "PG", authority=LII_ASSIGNED),
    Court("PGNC", "National Court of Papua New Guinea", "PG", authority=LII_ASSIGNED),
    Court("SBCA", "Court of Appeal of Solomon Islands", "SB", authority=LII_ASSIGNED),
    Court("SBHC", "High Court of Solomon Islands", "SB", authority=LII_ASSIGNED),
    Court("VUCA", "Court of Appeal of Vanuatu", "VU", authority=LII_ASSIGNED),
    Court("VUSC", "Supreme Court of Vanuatu", "VU", authority=LII_ASSIGNED),
    Court("WSCA", "Court of Appeal of Samoa", "WS", authority=LII_ASSIGNED),
    Court("WSSC", "Supreme Court of Samoa", "WS", authority=LII_ASSIGNED),
    Court("TOSC", "Supreme Court of Tonga", "TO", authority=LII_ASSIGNED),
    Court("TOCA", "Court of Appeal of Tonga", "TO", authority=LII_ASSIGNED),
    Court("NRSC", "Supreme Court of Nauru", "NR", authority=LII_ASSIGNED),
    Court("CKHC", "High Court of the Cook Islands", "CK", authority=LII_ASSIGNED),
    Court("KIHC", "High Court of Kiribati", "KI", authority=LII_ASSIGNED),
    Court("TVHC", "High Court of Tuvalu", "TV", authority=LII_ASSIGNED),

    # ---- Supranational / regional courts ----------------------------------
    Court("EACJ", "East African Court of Justice", "EAC"),
    Court("AfCHPR", "African Court on Human and Peoples' Rights", "AFRICA"),
    Court("ACtHPR", "African Court on Human and Peoples' Rights (variant)", "AFRICA"),
)


# ---- per-jurisdiction buckets ---------------------------------------------
# The ISO-prefix medium-neutral-citation convention means an UNRECOGNISED court token
# usually still announces its country ("KEELRC" → Kenya). These stand-ins let such a
# citation be classified by jurisdiction rather than dumped in "unknown" — the corpus
# understanding the place precedes it knowing the tribunal.
GENERIC_COURTS: tuple[Court, ...] = tuple(
    Court(f"{prefix}*", f"{name} court (unidentified)", juris, generic=True)
    for prefix, juris, name in (
        ("NZ", "NZ", "New Zealand"), ("SG", "SG", "Singapore"), ("HK", "HK", "Hong Kong"),
        ("IE", "IE", "Irish"), ("ZA", "ZA", "South African"), ("KE", "KE", "Kenyan"),
        ("GHA", "GH", "Ghanaian"), ("TZ", "TZ", "Tanzanian"), ("UG", "UG", "Ugandan"),
        ("NG", "NG", "Nigerian"), ("ZM", "ZM", "Zambian"), ("MW", "MW", "Malawian"),
        ("ZW", "ZW", "Zimbabwean"), ("NA", "NA", "Namibian"), ("SZ", "SZ", "Eswatini"),
        ("BW", "BW", "Botswana"), ("MU", "MU", "Mauritian"),
        ("FJ", "FJ", "Fijian"), ("PG", "PG", "Papua New Guinea"),
        ("SB", "SB", "Solomon Islands"), ("VU", "VU", "Vanuatu"), ("WS", "WS", "Samoan"),
        ("TO", "TO", "Tongan"), ("NR", "NR", "Nauruan"), ("CK", "CK", "Cook Islands"),
        ("KI", "KI", "Kiribati"), ("TT", "TT", "Trinidad & Tobago"),
        ("JM", "JM", "Jamaican"), ("BB", "BB", "Barbadian"), ("BS", "BS", "Bahamian"),
        ("GY", "GY", "Guyanese"), ("BZ", "BZ", "Belizean"), ("MY", "MY", "Malaysian"),
        ("EW", "GB", "England & Wales"), ("UK", "GB", "UK"), ("NI", "GB", "Northern Ireland"),
    )
)

# Longest-first so "GHA" beats "GH"-less alternatives and "NZ" can't shadow a longer one.
_PREFIXES: tuple[tuple[str, Court], ...] = tuple(sorted(
    ((c.code.rstrip("*"), c) for c in GENERIC_COURTS),
    key=lambda pair: len(pair[0]), reverse=True))

# All courts indexed by code — a code may map to SEVERAL courts (FCA is both Australian
# and Canadian), which is why this is a tuple per code rather than a single entry.
COURTS_BY_CODE: dict[str, tuple[Court, ...]] = {}
for _court in COURTS:
    COURTS_BY_CODE.setdefault(_court.code.upper(), ())
    COURTS_BY_CODE[_court.code.upper()] += (_court,)

# Back-compatible single-answer view: the FIRST registration for each code wins, which
# keeps existing callers (taxonomy, snowball) working unchanged. Use ``lookup`` with a
# bracket hint when a code is ambiguous.
KNOWN_COURTS: dict[str, Court] = {code: courts[0]
                                  for code, courts in COURTS_BY_CODE.items()}

# Codes registered for more than one jurisdiction — the set a caller must disambiguate
# by bracket style or surrounding context rather than trusting a bare lookup.
AMBIGUOUS_CODES: frozenset[str] = frozenset(
    code for code, courts in COURTS_BY_CODE.items()
    if len({c.jurisdiction for c in courts}) > 1)


# Court tokens that are really *divisions*, valid only after a parent court code
# (so "[2024] EWCA Civ 1" is one citation, not court "Civ").
DIVISIONS = {
    "Civ", "Crim", "Admin", "Fam", "Ch", "QB", "KB", "Pat", "TCC", "Comm",
    "Admlty", "Mercantile", "IPEC", "SCCO", "Costs",
}


def lookup(code: str, *, bracketed: bool | None = None) -> Court | None:
    """Resolve a court token, optionally disambiguated by the citation's bracket style.

    ``bracketed`` is the decisive signal for the cross-jurisdiction code collisions:
    ``[2020] FCA 1`` is the Federal Court of Australia while ``2020 FCA 1`` is the
    Federal Court of Appeal of Canada, and only the brackets say which. When the hint
    is omitted, or no registered court matches it, the primary registration is returned
    so existing single-answer callers keep working.
    """
    candidates = COURTS_BY_CODE.get((code or "").upper())
    if not candidates:
        return None
    if bracketed is not None:
        match = next((c for c in candidates if c.bracketed is bracketed), None)
        if match is not None:
            return match
    return candidates[0]


def lookup_prefix(code: str) -> Court | None:
    """An unrecognised court token → its jurisdiction's generic bucket, if the token
    begins with a known country prefix.

    This is what turns "[2023] KEELRC 1142" from an unknown token into Kenyan case law
    even before that tribunal is registered: the medium-neutral-citation convention puts
    the ISO country code first, so the country is recoverable when the tribunal is not.
    Returns None when nothing matches, which keeps the token in the snowball as a genuine
    unknown rather than mislabelling it.
    """
    token = (code or "").upper()
    if not token:
        return None
    for prefix, court in _PREFIXES:
        if token.startswith(prefix.upper()):
            return court
    return None


def classify(code: str, *, bracketed: bool | None = None) -> Court | None:
    """The registry's full answer for a token: the exact court if registered, else the
    jurisdiction bucket, else None. This is the entry point callers should prefer."""
    return lookup(code, bracketed=bracketed) or lookup_prefix(code)


# The Irish senior courts' slug heads (lowercase) — the set the importers use to key a
# case as Irish (``source="ie-caselaw"``) and the extraction stage uses to gate UK
# statute-name heuristics ("Companies Act 1963" in an IEHC judgment is Irish law).
# Generic buckets are excluded: they are classifications, not fetchable court slugs.
IRISH_COURTS: frozenset[str] = frozenset(
    c.code.lower() for c in COURTS if c.jurisdiction == "IE")
