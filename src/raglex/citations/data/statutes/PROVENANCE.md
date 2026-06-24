# Statute-title gazetteers

Title → legislation.gov.uk URI lists for UK primary legislation (ukpga, asp, nia,
anaw, ukla, ukcm, apni, mwa, mnia) plus short-form abbreviations (`ukpga_short`).

**Source:** the legislation.gov.uk **GATE Legislation Amendments / eMarkup pipeline**
(`LegislationAmendments/gazetteer/des_legislation_*.lst`), © Crown copyright 2023,
released under the LGPL-3.0. These are the gazetteer lists the official pipeline uses to
resolve statute names to URIs when generating the Table of Effects.

We vendor only the **primary-legislation** lists (the SI list, ~59k entries, is omitted
for size — add `uksi.lst` the same way if SI-by-name resolution is needed).

Used by [statute_gazetteer.py](../../statute_gazetteer.py) to resolve "the X Act YYYY"
(and abbreviations like "ICTA") to a stable_id offline, with no network lookup.
