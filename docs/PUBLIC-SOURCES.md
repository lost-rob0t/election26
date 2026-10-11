# Sources and coverage limits

Checked 2026-10-11 UTC. Access status is a point-in-time probe from the research
computer, not a promise of future access from a phone. Probes supplied no cookies,
login or API key. Default feeds parse as RSS XML and their sampled item links return
HTTP 200. All default feed domains have a compatible robots policy for the exact
collector user agent. Source facts and probe details are in `source-evidence/`.

## Included RSS feeds

1. **GovInfo raw Congressional bill text** (official federal documents)
   https://www.govinfo.gov/rss/bills-bulkdata.xml
   HTTP 200, text/xml, RSS 2.0, 100 items. Feed and linked documents use
   `www.govinfo.gov`, with no observed cross-host redirect. Item links go directly to
   raw bill XML, not merely detail pages. Sample:
   https://www.govinfo.gov/bulkdata/BILLS/119/2/hr/BILLS-119hr10674ih.xml
   Sample returned HTTP 200, root `bill`. Broad federal bills, not just election bills.

2. **GovInfo bill status** (official federal actions and metadata)
   https://www.govinfo.gov/rss/billstatus-bulkdata.xml
   HTTP 200, text/xml, RSS 2.0, 100 items. Same host and redirect behavior. Sample:
   https://www.govinfo.gov/bulkdata/BILLSTATUS/119/hr/BILLSTATUS-119hr9489.xml
   Sample returned HTTP 200, root `billStatus`. This preserves source XML; the current
   collector does not normalize it into a complete legislative relational database.

   Both URLs are published in the official feed directory:
   https://www.govinfo.gov/feeds
   GovInfo says feeds generally retain the 100 most recent documents. It also offers
   custom search feeds; none was guessed or added here. No key is required for these
   static feeds. The separate Congress.gov API requires an API key and is not configured.
   The `/rss/bills.xml` alternative is valid but links mainly to HTML detail pages, so
   the direct-XML bulk feed is a better fit for this collector.

3. **Ballotpedia election news** (secondary reporting)
   https://news.ballotpedia.org/feed/
   HTTP 200 RSS, 20 items, newest October 9; observed entries span October 6–9.
   Feed/item host `news.ballotpedia.org`; no observed redirect. Covers selected federal,
   state, local and ballot-measure reporting. A small recent feed is not the complete
   Ballotpedia database and does not enumerate every race.

4. **DCCC** (Democratic House campaign committee; partisan primary statements)
   https://dccc.org/feed/
   HTTP 200 RSS, 10 items, newest October 10. Feed/item host `dccc.org`.
   Ten observed items covered roughly seven hours of publishing. High churn means a
   slow rotating feed cadence will miss material.

5. **NRCC** (Republican House campaign committee; partisan primary statements)
   https://www.nrcc.org/feed/
   HTTP 200 RSS, 10 items, newest October 10. Feed/item host `www.nrcc.org`.
   Observed items span roughly 41 hours. DCCC/NRCC form a matched House-committee pair;
   this does not make their claims balanced or independently verified.

6. **Libertarian Party** (partisan primary statements)
   https://lp.org/feed/
   HTTP 200 RSS, 10 items, newest October 5. Use apex host `lp.org`:
   `www.lp.org` redirects to it. The configured apex endpoint and sampled article
   required no further host. This does not cover every Libertarian candidate.

7. **Green Party press releases** (partisan primary statements; sparse/stale)
   https://www.gp.org/press_releases.rss
   HTTP 200 RSS, 20 items, newest **May 12, 2026** at this check. Do not treat a quiet
   feed as proof of no later activity. Feed/item host `www.gp.org`; some item links
   are HTTP and redirect to HTTPS on the same hostname.

Except for the explicitly noted Green HTTP-to-HTTPS change, no redirect was observed
for the seven configured feed endpoints or sampled articles. The allowlist names
exact observed hosts; it does not permit all subdomains or linked image/video CDNs.
All successful default-party/Ballotpedia exact-UA feed checks and item-host sets are
recorded in `source-evidence/public-feeds-probe.json`.

## Included manual snapshots

These are **manual_urls**, not feeds. The archive-kit command queues these alongside the rotating RSS feeds.

- FEC announcements: https://www.fec.gov/updates/
  HTTP 200 HTML, no observed redirect. Preserves the current announcements listing;
  it does not follow all announcement links. No current FEC announcement RSS endpoint
  was verified. The historical FEC electronic-filing RSS service is a separate purpose
  and is not silently substituted for announcements or donor-data collection.
- FEC 2026 election dates PDF:
  https://www.fec.gov/resources/cms-content/documents/2026pdates.pdf
  HTTP 200 application/pdf, 226,820 bytes. The current collector treats PDF as document media: the default kit records this link without downloading its body. An explicit per-source document-media policy is required to download it.
- FEC deadline hub:
  https://www.fec.gov/help-candidates-and-committees/dates-and-deadlines/
  HTTP 200 HTML. Same `www.fec.gov` host. Current robots policy requests a 10-second
  crawl delay, which the collector respects.
- Columbus council calendar:
  https://columbus.legistar.com/Calendar.aspx
  HTTP 200 HTML, no observed redirect. Official Columbus site links to this Legistar
  service at https://www.columbus.gov/Business-Development/Building-Zoning-Services/Zoning
  Calendar is a current-month snapshot; its RSS button was disabled. It does not
  provide automatic bill/ordinance text collection. `robots.txt` returned 404, which
  this collector handles as no robots restrictions.

The collector can capture a known changed page with an explicit `fetch --refresh URL`.
Re-running manual discovery does not itself guarantee a fresh version of an already
captured URL. Keep dated observations; never label a listing snapshot as full coverage.

## Verified alternatives, omitted to keep the initial rotation small

These are expansion options, not automatically enabled. Add only the chosen feed and
its exact item hosts into a new profile by editing your private `archive-config.json` after reviewing the new exact hosts and source policy.

- NPR politics: https://feeds.npr.org/1014/rss.xml
  RSS 200; feed host `feeds.npr.org`, article host `www.npr.org`. Both must be
  allowlisted. Sample article HTTP 200. Feed robots 404 accepted by collector;
  article robots 200 allows the sampled link.
- PBS politics: https://www.pbs.org/newshour/feeds/rss/politics
  RSS 200; feed/articles `www.pbs.org`, sampled article 200; robots allows it with
  a one-second crawl delay.
- Votebeat: https://www.votebeat.org/arc/outboundfeeds/rss/?outputType=xml
  **Atom**, despite the path name, HTTP 200; 100 entries, about 1.05 MB. Uses Atom
  alternate links supported by the collector; feed/articles `www.votebeat.org`.
  Sample article and robots checks passed. Covers election administration and selected
  states, not every state/local contest.
- Ballotpedia ballot-measure subset:
  https://news.ballotpedia.org/tag/ballot-measures/feed/
  RSS 200, 20 items, newest October 7; `news.ballotpedia.org`. Overlaps its main feed.
- DNC: https://democrats.org/feed/ (10, newest October 10; `democrats.org`)
- NRSC: https://www.nrsc.org/feed/ (10, newest October 9; `www.nrsc.org`)
- Working Families: https://workingfamilies.org/feed/ (10, newest October 8;
  `workingfamilies.org`; robots delay 3 seconds)
  These three are partisan primary statements. Candidate-level coverage is selective.
- EAC generic institutional stream: https://www.eac.gov/rss.xml
  RSS 200 and sampled item 200 in the alternative-source check, but recent entries
  were mainly voting-equipment engineering changes; other EAC pages returned 403 in
  our raw checks. This is not a verified press-only feed or complete national results.

Do not add DSCC's empty feed or GOP's blocked feed just to fill a category. CA SOS's
own available-feeds page says “None at this time”:
https://www.sos.ca.gov/administration/multimedia/available-rss-feeds

## Ohio, Franklin County, and all-state expansion gaps

Official URLs below were found through official pages and web research, but raw HTTP
could not establish usable collection in this environment. They are **not enabled**.
Do not work around an access restriction, defeat a challenge, or silently permit new
hosts. A web search service's readable page is not evidence that this raw collector
can fetch it.

- Ohio Legislature status report directory:
  https://www.legislature.ohio.gov/legislation/status-reports
  Raw HTTP 502; official listing advertises General Assembly 136 (2025–2026) HTML
  status reports. No current no-key RSS/Atom endpoint verified.
- Ohio SOS elections: https://www.ohiosos.gov/elections
  Raw HTTP 403. The official trailing-slash URL redirects to this path on the same
  host. Its public pages link to directives, local results and county offices.
- Ohio's 88-county official office directory:
  https://www.ohiosos.gov/directories/county-boards-of-elections
  Web-readable directory; not a raw feed. Not exhaustively probed or crawled.
- Ohio election directives:
  https://www.ohiosos.gov/elections/elections-administration/directives
  Web-readable HTML; not a verified RSS source. Linked files may use other hosts.
- Franklin BOE 2026 election information:
  https://vote.franklincountyohio.gov/election-info/2026
  Raw HTTP 403. Web-readable official page links notices, certified candidates,
  questions/issues and available election results.
- Franklin certified 2026 general questions/issues PDF:
  https://vote.franklincountyohio.gov/getmedia/4001b052-1b90-4f73-b030-2d981e6ec8fa/2026-General-Certified-Questions-and-Issues
  Raw HTTP 403; web research identified application/pdf. Direct URL is given for
  later permitted verification, not enabled automatically.
- EAC official state/local-office directory:
  https://www.eac.gov/voters/register-and-vote-in-your-state
  Raw HTTP 403; useful national research entry point. A captured directory would still
  not mean the collector visited its linked jurisdictions.
- Congress.gov House/Senate floor and presented-to-president RSS endpoints each
  returned raw HTTP 403. Prefer the functioning official GovInfo bulk feeds here.
- Texas SOS RSS was XML-readable but contains hundreds of historical items,
  inconsistent dates and legacy external hosts, including an internal-looking host.
  It is deliberately excluded; never bulk-add those linked hosts.

Remaining work for actual nationwide coverage: verify each jurisdiction's election
calendar, official candidate/ballot list, results publications, state bill sources,
local legislative sources and update mechanics. County-specific raw documents,
candidate websites, social posts, advertising archives and individual donor records
are not supplied by this starter. Never imply that any source count equals coverage
of all U.S. elections, every party, every candidate or all historical changes.
