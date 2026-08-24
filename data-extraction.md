# We're now building the due diligence outreach and have a strong plan in place. My responsibility is to use the extracted data and structure it so that the AI agent can read it, score it, and create an in-depth analytics report on it.

Then, map this into metrics that can be passed on to a dashboard.

1. TAM
2. Competitors
3. Founders Profile
4. Further relevant metrics that actually help evaluating the pitchdeck.

Here's a structured schema you can hand to the AI agent for extraction, scoring, and dashboard mapping, built on top of the founder-market-fit-first evaluation model appropriate for pre-seed.

## 1. TAM (and SAM/SOM)

Pre-seed market-size review is judged on directional credibility, not precision — a ±50% margin is considered acceptable at this stage, so the agent should score *methodology quality* over precision. Structure the extraction as:[^1]

- **TAM**: total potential customers × average annual revenue per customer, captured via top-down (industry report figure) and bottom-up (customer count × ACV) so the agent can flag if only one method was used.[^2][^3]
- **SAM**: TAM filtered by the startup's actual reachable segment (geography, ICP, channel).[^4][^5]
- **SOM**: realistic 3–5 year capture, ideally as % of SAM — flag anything outside the credible 1–15% range as a red flag.[^6][^1]
- **Methodology tag**: top-down / bottom-up / capacity-based, plus source citations used in the deck.

Score this dimension on: is a bottom-up calculation present (not just a Gartner/Statista slide), is SOM logically derived from SAM, and is there at least one external validating data point.[^3][^1]

## 2. Competitors

Extract into a structured table rather than free text: competitor name, funding stage/amount raised, differentiation claimed by the founder, and a flag for whether the deck lists direct vs. adjacent competitors only (a common red flag is omitting a well-known direct competitor). The agent should also compute a **"why now / why us"** field — is there a credible timing argument (tech shift, regulatory change, behavioral shift) distinct from just "big market + AI". Cross-reference founder claims against public info you can pull (e.g., a quick market/news check) to flag unverified or exaggerated differentiation claims.[^7]

## 3. Founder Profile

Use a weighted scorecard modeled on the VCII Founder Evaluation framework, which weights eight categories:[^8]


| Category | Weight | What the agent extracts |
| :-- | :-- | :-- |
| Industry experience | 20% | Years/roles directly in this market |
| Vision \& strategy | 15% | Clarity and specificity of stated strategy |
| Entrepreneurial track record | 15% | Prior startups, outcomes, lessons |
| Learning agility | 15% | Evidence of pivots, fast iteration |
| Team leadership | 10% | Prior team-building/hiring experience |
| Network strength | 10% | Access to customers, advisors, capital [^9] |
| Resilience/risk handling | 10% | Evidence of overcoming setbacks |
| Execution strength | 5% | Shipped products, demos, prior launches |

Layer founder-market fit on top as the single highest-weighted qualitative signal at pre-seed: does the founder have lived, personal exposure to the problem (not just recent interest), and is their network genuinely differentiated for this specific market.[^9][^10][^11]

## 4. Additional Metrics Worth Capturing

Beyond the three you named, these round out a pre-seed-appropriate scoring model:

- **Problem/customer validation signals**: customer interview count, LOIs, waitlist size, pilot conversations — presence and specificity, since assertion without evidence is the most common pre-seed weakness.[^12][^13]
- **Traction proxies**: even pre-revenue, look for cohort or usage data if any exists, and technical founder-market fit signals from GitHub (repo activity, prior shipped projects).[^7]
- **Business model clarity**: pricing model stated, ACV/ARPU assumption used consistently between the deck and the TAM calculation (a common inconsistency to flag).[^5]
- **Cap table \& legal hygiene**: incorporation status, founder equity split, any pre-existing IP or legal complications.[^12]
- **Ask \& use of funds**: amount raised vs. TAM/SOM scale — does the ask size make sense relative to the claimed opportunity.[^14]
- **Non-obvious insight check**: does the founder articulate a contrarian or non-obvious view of the market, versus a generic thesis.[^7]


## Suggested Dashboard Schema

Each pitch deck maps to one record with nested sub-scores (1–5 or 0–100) per pillar, an overall weighted composite score, and a flat list of extracted red flags with severity tags (e.g., "SOM inconsistent with SAM," "no direct competitor named," "founder background unverifiable"). Feed the four pillars — TAM/SAM/SOM, Competitors, Founder Profile, Traction/Validation — as separate dashboard widgets, each with its own drill-down into the source evidence the agent cited, so judges/investors can audit *why* a score was given rather than trusting a black-box number.
<span style="display:none">[^15][^16][^17][^18][^19][^20]</span>

<div align="center">⁂</div>

[^1]: https://www.growthjockey.com/blogs/tam-sam-som

[^2]: https://www.icanpitch.com/blog/tam-sam-som-market-sizing-guide

[^3]: https://waveup.com/blog/tam-sam-som/

[^4]: https://wise.com/gb/blog/tam-sam-vs-som

[^5]: https://www.rho.co/blog/tam-vs-sam-vs-som

[^6]: https://www.antler.co/blog/tam-sam-som

[^7]: https://financeinterviewprep.com/blog/how-vcs-evaluate-founders-product-moats-traction

[^8]: https://www.vciinstitute.com/blog/the-vcii-founder-evaluation-scorecard-an-ai-enhanced-tool-for-comprehensive-founder-assessment

[^9]: https://www.allied.vc/tools/founder-market-fit-scorecard

[^10]: https://www.linkedin.com/pulse/founder-market-fit-real-reason-pre-seed-investors-bet-choudhury-3ebcf

[^11]: https://startupfundraising.com/founder-market-fit

[^12]: https://www.evalyze.ai/blog/startup-due-diligence

[^13]: https://www.8minutes.in/post/due-diligence-in-pre-seed-stage-of-startup-funding-by-venture-capital-firms

[^14]: https://www.spectup.com/resource-hub/pre-seed-funding-stage-of-startups

[^15]: https://techcrunch.com/2022/03/09/how-to-calculate-your-startups-tam-sam-and-som/

[^16]: https://advertising.amazon.com/library/guides/tam-sam-som

[^17]: https://www.seerinteractive.com/insights/marketing-sizing-with-tam-sam-som

[^18]: https://www.spectup.com/resource-hub/tam-som-sam

[^19]: https://upmetrics.co/blog/tam-sam-som-market-size-metrics

[^20]: https://foundationinc.co/lab/tam-sam-som

