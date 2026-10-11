# Bounded U.S. 2026 source starting points

Verified public source references on 2026-10-11. These are references/configuration,
not a claim that data has been collected. The project has nationwide U.S. scope;
this tiny preflight is not representative, complete, or exhaustive coverage.

## Official metadata preflight

`config/us-2026-official-preflight.json` contains exactly two public FEC URLs:

- [FEC 2026 congressional primary dates PDF](https://www.fec.gov/resources/cms-content/documents/2026pdates.pdf)
- [FEC dates and deadlines index](https://www.fec.gov/help-candidates-and-committees/dates-and-deadlines/)

They provide federal election scheduling context, not protest coverage. The
profile allows only `www.fec.gov`, two URLs per invocation, 1 MiB per object,
10 MiB archive capacity, and a 64 MiB free-space reserve. It has no paid API or
notification enablement. Running it still requires choosing private local storage.
Robots rules or host redirects can stop a source; the pipeline does not bypass
those restrictions. No live source request runs in CI.

## Public news feed candidate

[PBS's official feed directory](https://www.pbs.org/newshour/about/pbs-news-rss-feeds)
lists [politics RSS](https://www.pbs.org/newshour/feeds/rss/politics) and
[headlines RSS](https://www.pbs.org/newshour/feeds/rss/headlines). These are useful
candidate discovery sources but are not enabled in the official metadata
preflight. Their content can include international stories and dates outside
2026, so record and review coverage rather than assuming a country/date match.
Media CDN hosts are not silently allowlisted; inspect and approve them explicitly.
A publicly reachable URL does not grant redistribution rights.

## Coverage ledger to maintain locally

Record source URLs, access/robots result, capture time, date/geography relevance,
media coverage, known gaps, and last successful cursor/capture. Status totals
separate capture observations from unique stored objects. A growing byte count
does not measure geographic or editorial completeness.

Next source expansion should add state/local election authority notices and a
balanced set of public newsroom feeds for reported election events and protests,
with a concrete date window and storage budget. Do not convert event coverage
into dossiers, participant identity tracking, facial identification, or private
account collection. All participant-bearing raw material stays private by default.

## Staged nationwide source inventory

Extend the registry across public Republican, Democratic, and third-party
organizations; public advocacy/community organizations and their public subgroups;
election/protest event announcements; and public newsroom/street-event reporting
across the political spectrum. Inventory records should carry the public
organization/event name, publisher, canonical source URL, source kind, geographic
coverage, explicit date scope, refresh cadence, last successful capture, access
restrictions, and known coverage gaps. Source selection must not imply agreement
with a publisher or the truth of a reported allegation.

Work in bounded batches. First verify official organization/event feeds and
public press-release/news pages, then opt in exact hosts and retrieval limits.
Capture originals before optional metadata mapping. Compare source freshness
and coverage gaps across organizations and regions without claiming to capture
“everything.” Public-group coverage is not permission to infer private people's
political affiliations, build participant dossiers, or identify faces. Do not
join unrelated personal/location datasets to political participants.
