from __future__ import annotations

import logging
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import ValidationError

from app.config import settings
from app.models import DiligenceReport, FirmProfile, ParsedDeck

logger = logging.getLogger(__name__)

# Fallback only — the real value comes from settings.anthropic_model (.env).
DEFAULT_MODEL = "claude-sonnet-5"

# Server-side web research tool. Type string and optional params verified against
# the installed SDK's generated types (anthropic/types/web_search_tool_20260209_param.py).
# Do NOT switch to web_search_20260318 — it only adds `response_inclusion`, which
# matters for code-execution nesting we do not use.
WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 14,
}

# The search runs server-side inside a single request, so the loop below is a
# safety net for `pause_turn` / `max_tokens`, not the mechanism that drives search.
MAX_RESEARCH_ITERATIONS = 5
RESEARCH_MAX_TOKENS = 8_000
EXTRACTION_MAX_TOKENS = 16_000
EXTRACTION_ATTEMPTS = 2
_MAX_VALIDATION_ERROR_CHARS = 4_000

# Structured output for the report is delivered as a forced call to this
# client-side tool, NOT via messages.parse(output_format=DiligenceReport).
#
# Why (measured against this API on 2026-08-24, every model tried —
# claude-sonnet-5, claude-opus-5, claude-opus-4-8, claude-opus-4-7):
#   messages.parse(output_format=DiligenceReport)
#     -> 400 invalid_request_error "The compiled grammar is too large, which
#        would cause performance issues. Simplify your tool schemas or reduce
#        the number of strict tools."
# Grammar-constrained output (output_format / a `strict` tool) compiles the whole
# schema graph, and this frozen contract is over the ceiling: bisecting the
# report, company+overview+tam+competitors+founder_profile is accepted and adding
# the `founders` list tips it over. Dropping titles, descriptions, `format: uri`,
# and nullable unions, and marking every property required, all still 400.
# A non-strict tool is not grammar-compiled, so the identical schema is accepted
# there. The model still receives the exact JSON Schema, the SDK hands back
# `tool_use.input` already decoded as a dict, and pydantic validation (including
# `Source`'s cross-field validator) runs on our side — so the corrective retry
# below behaves exactly as the plan intends. No free-text JSON is ever scraped
# out of prose.
REPORT_TOOL_NAME = "submit_diligence_report"
REPORT_TOOL: dict[str, Any] = {
    "name": REPORT_TOOL_NAME,
    "description": (
        "Submit the completed due-diligence report. Call this exactly once. The tool "
        "input IS the report object: its top-level keys are exactly company, "
        "overview, tam_sam_som, competitors, founder_profile, founders, "
        "additional_metrics, key_findings and founder_questions. Do not nest the "
        'report under an outer key such as "report".'
    ),
    "input_schema": DiligenceReport.model_json_schema(),
}


class DiligenceError(RuntimeError):
    """Raised when the agent cannot produce a valid, evidence-backed report.

    Deliberately fatal: a degraded or fabricated report is worse than no report,
    so callers get an exception rather than a plausible-looking placeholder.
    """


class ClaudeDiligenceAgent:
    """Deep-dive diligence agent — the real implementation of the
    `DiligenceAgent` Protocol in app/agent.py (replacing `MockDiligenceAgent`).

    Two Claude calls per deck, per docs/track-b-deep-analysis.md:

      1. Research  — `messages.create` with the server-side web_search tool.
                     Produces a prose transcript plus the concrete list of
                     (title, url) pairs the search actually returned.
      2. Extraction — `messages.create` with one forced client-side tool whose
                     input schema is `DiligenceReport`, fed the deck page map +
                     the transcript + that URL list. See REPORT_TOOL above for
                     why this is not `messages.parse(output_format=...)`.

    No multi-agent orchestration and no vector DB (docs/BUILD_PLAN.md
    "What Not to Build"), and no numeric rating anywhere: every judgement in the
    schema is a status enum plus evidence text.
    """

    def __init__(
        self,
        client: AsyncAnthropic | None = None,
        model: str | None = None,
    ) -> None:
        # Client is built lazily so importing this module (and constructing the
        # agent at app startup) never requires an API key.
        self._client = client
        self._model = model or settings.anthropic_model or DEFAULT_MODEL

    # ---------------------------------------------------------------- public

    async def analyze(self, deck: ParsedDeck, firm: FirmProfile | None) -> DiligenceReport:
        client = self._get_client()

        transcript, cited_sources = await self._research(client, deck, firm)
        if not transcript:
            logger.warning(
                "Research call produced no text for %s — the report will rest on "
                "deck evidence only",
                deck.filename,
            )

        return await self._extract(client, deck, firm, transcript, cited_sources)

    # ---------------------------------------------------------------- client

    def _get_client(self) -> AsyncAnthropic:
        if self._client is not None:
            return self._client

        api_key = (settings.anthropic_api_key or "").strip()
        if not api_key:
            raise DiligenceError(
                "ANTHROPIC_API_KEY is not set, so the diligence agent cannot run. "
                "Add ANTHROPIC_API_KEY=<your key> to .env (see .env.example) or set "
                "it as an environment variable, then retry. Nothing else is missing — "
                "the deck was parsed fine."
            )

        self._client = AsyncAnthropic(api_key=api_key)
        return self._client

    # -------------------------------------------------------------- research

    async def _research(
        self,
        client: AsyncAnthropic,
        deck: ParsedDeck,
        firm: FirmProfile | None,
    ) -> tuple[str, list[tuple[str, str]]]:
        """Runs the web-grounded research call.

        Returns the full prose transcript and the deduplicated (title, url) pairs
        harvested from the search results — the only URLs the extraction call is
        allowed to cite.
        """
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": self._research_user_prompt(deck)}
        ]
        text_chunks: list[str] = []
        # url -> title, dict keeps first-seen order and dedupes on url.
        harvested: dict[str, str] = {}

        for iteration in range(1, MAX_RESEARCH_ITERATIONS + 1):
            response = await client.messages.create(
                model=self._model,
                max_tokens=RESEARCH_MAX_TOKENS,
                system=self._research_system_prompt(firm),
                tools=[WEB_SEARCH_TOOL],
                messages=messages,
            )

            self._collect_blocks(response.content, text_chunks, harvested)

            stop_reason = getattr(response, "stop_reason", None)
            logger.info(
                "Research iteration %s/%s: stop_reason=%s, text_blocks=%s, urls=%s",
                iteration,
                MAX_RESEARCH_ITERATIONS,
                stop_reason,
                len(text_chunks),
                len(harvested),
            )

            if stop_reason == "end_turn":
                break

            # Anything other than end_turn means we hand the turn back. The tool
            # itself runs server-side, so we never build tool_result blocks here.
            messages.append({"role": "assistant", "content": response.content})

            if stop_reason == "pause_turn":
                # Long-running server tool turn: replay it verbatim to resume.
                continue
            if stop_reason == "max_tokens":
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "You ran out of output space. Continue your research notes "
                            "from exactly where you stopped, then finish with the "
                            "WEBSITE: and LINKEDIN: lines. Do not repeat what you "
                            "already wrote."
                        ),
                    }
                )
                continue

            logger.warning(
                "Research loop stopping early on unexpected stop_reason=%r", stop_reason
            )
            break
        else:
            logger.warning(
                "Research loop hit the %s-iteration guard for %s — continuing with "
                "what was gathered",
                MAX_RESEARCH_ITERATIONS,
                deck.filename,
            )

        transcript = "\n\n".join(chunk for chunk in text_chunks if chunk.strip()).strip()
        cited_sources = [(title, url) for url, title in harvested.items()]
        logger.info(
            "Research finished for %s: %s chars of notes, %s citable URLs",
            deck.filename,
            len(transcript),
            len(cited_sources),
        )
        return transcript, cited_sources

    def _collect_blocks(
        self,
        blocks: Any,
        text_chunks: list[str],
        harvested: dict[str, str],
    ) -> None:
        """Walks one response's content blocks defensively.

        Two behaviours matter here and both were observed live:

        * The final answer arrives as MANY small `text` blocks (citation
          interleaved), not one. Every `.text` is appended, in order — taking
          only the first or last block silently truncates the research.
        * Block types we never declared can appear (e.g. `server_tool_use` with a
          code-execution-shaped input, `code_execution_tool_result`). Unknown
          types are skipped at debug level, never assumed to have `.text`.
        """
        for block in blocks or []:
            block_type = getattr(block, "type", None)

            if block_type == "text":
                text = getattr(block, "text", "")
                if text:
                    text_chunks.append(text)
            elif block_type == "web_search_tool_result":
                self._harvest_search_result(block, harvested)
            else:
                # Search results are occasionally routed through another server
                # tool, so look one level down for nested result blocks before
                # discarding the block.
                self._harvest_nested(block, harvested)
                logger.debug("Skipping unhandled content block type %r", block_type)

    @staticmethod
    def _harvest_search_result(block: Any, harvested: dict[str, str]) -> None:
        """Pulls (title, url) out of one `web_search_tool_result` block.

        Shape verified against the installed SDK's generated types
        (anthropic/types/web_search_tool_result_block_content.py): `.content` is
        EITHER a list of `WebSearchResultBlock` — whose only fields are url, title,
        page_age, encrypted_content and type, so there is no text-excerpt field to
        read and `page_age` is opaque human text like "1 month ago" — OR a single
        `WebSearchToolResultError` carrying `.error_code`. Branch on the shape
        before iterating, per docs/track-b-deep-analysis.md.
        """
        content = getattr(block, "content", None)

        if isinstance(content, list):
            for result in content:
                url = getattr(result, "url", None)
                if url and url not in harvested:
                    harvested[url] = getattr(result, "title", None) or url
            return

        error_code = getattr(content, "error_code", "unknown")
        logger.warning(
            "web_search returned an error block (error_code=%r, tool_use_id=%s) — "
            "some research may be missing",
            error_code,
            getattr(block, "tool_use_id", "?"),
        )

    @classmethod
    def _harvest_nested(cls, block: Any, harvested: dict[str, str]) -> None:
        """Looks one level inside an unrecognised block for search results."""
        nested = getattr(block, "content", None)
        if not isinstance(nested, list):
            return
        for item in nested:
            item_type = getattr(item, "type", None)
            if item_type == "web_search_result":
                url = getattr(item, "url", None)
                if url and url not in harvested:
                    harvested[url] = getattr(item, "title", None) or url
            elif item_type == "web_search_tool_result":
                cls._harvest_search_result(item, harvested)

    # ------------------------------------------------------------ extraction

    async def _extract(
        self,
        client: AsyncAnthropic,
        deck: ParsedDeck,
        firm: FirmProfile | None,
        transcript: str,
        cited_sources: list[tuple[str, str]],
    ) -> DiligenceReport:
        """Structured extraction, with one corrective retry.

        Validation runs client-side on `tool_use.input`, so `Source`'s cross-field
        validator (external -> url, deck -> page) — which no JSON Schema can
        express — is enforced here rather than by the model. A `ValidationError` is
        therefore a real possibility, and we feed the exact validator complaint
        back once before giving up.
        """
        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": self._extraction_user_prompt(deck, transcript, cited_sources),
            }
        ]
        last_problem = ""

        for attempt in range(1, EXTRACTION_ATTEMPTS + 1):
            response = await client.messages.create(
                model=self._model,
                max_tokens=EXTRACTION_MAX_TOKENS,
                system=self._extraction_system_prompt(firm),
                messages=messages,
                tools=[REPORT_TOOL],
                tool_choice={"type": "tool", "name": REPORT_TOOL_NAME},
            )
            tool_use = self._find_report_tool_use(response)

            if tool_use is None:
                last_problem = (
                    f"You did not call the {REPORT_TOOL_NAME} tool "
                    f"(stop_reason={getattr(response, 'stop_reason', None)!r}). The "
                    "response was most likely truncated. Be more concise in every "
                    "free-text field and call the tool exactly once with the complete "
                    "report."
                )
                logger.warning(
                    "Extraction attempt %s/%s for %s: %s",
                    attempt,
                    EXTRACTION_ATTEMPTS,
                    deck.filename,
                    last_problem,
                )
                if attempt < EXTRACTION_ATTEMPTS:
                    # No tool_use block to answer, so do not replay the assistant
                    # turn — an unanswered tool_use would be rejected by the API.
                    messages.append(
                        {"role": "user", "content": self._correction_prompt(last_problem)}
                    )
                continue

            try:
                report = DiligenceReport.model_validate(
                    self._unwrap_envelope(tool_use.input)
                )
            except ValidationError as exc:
                last_problem = str(exc)[:_MAX_VALIDATION_ERROR_CHARS]
                logger.warning(
                    "Extraction attempt %s/%s failed schema validation for %s: %s",
                    attempt,
                    EXTRACTION_ATTEMPTS,
                    deck.filename,
                    last_problem,
                )
                if attempt < EXTRACTION_ATTEMPTS:
                    messages.append({"role": "assistant", "content": response.content})
                    messages.append(
                        {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": tool_use.id,
                                    "is_error": True,
                                    "content": self._correction_prompt(last_problem),
                                }
                            ],
                        }
                    )
                continue

            logger.info(
                "Extraction succeeded for %s on attempt %s (%s findings, %s questions)",
                deck.filename,
                attempt,
                len(report.key_findings),
                len(report.founder_questions),
            )
            return report

        raise DiligenceError(
            f"Structured extraction failed {EXTRACTION_ATTEMPTS} times for "
            f"{deck.filename!r}; refusing to return a partial or invented report. "
            f"Last problem: {last_problem}"
        )

    @staticmethod
    def _unwrap_envelope(payload: dict[str, Any]) -> dict[str, Any]:
        """Strips a single wrapper key if the model nested the report inside one.

        Observed live: the tool input came back as `{"report": {...}}` on a first
        attempt. That costs a full retry, so unwrap the obvious envelope instead —
        this only relabels, it never adds or alters report content.
        """
        if len(payload) == 1:
            (only_value,) = payload.values()
            if isinstance(only_value, dict) and "company" in only_value:
                logger.info("Unwrapped report from a %r envelope key", next(iter(payload)))
                return only_value
        return payload

    @staticmethod
    def _find_report_tool_use(response: Any) -> Any | None:
        """First tool_use block that is our report submission, or None."""
        for block in getattr(response, "content", None) or []:
            if (
                getattr(block, "type", None) == "tool_use"
                and getattr(block, "name", None) == REPORT_TOOL_NAME
                and isinstance(getattr(block, "input", None), dict)
            ):
                return block
        return None

    @staticmethod
    def _correction_prompt(problem: str) -> str:
        return (
            "Your previous report was rejected and discarded. The validator "
            f"reported:\n\n{problem}\n\n"
            f"Call {REPORT_TOOL_NAME} again with the COMPLETE report, with that "
            "specific problem fixed. Re-read these rules before you answer:\n"
            '- Every source with type="external" MUST have a `url`, and that URL must '
            "appear verbatim in the citable-URL list in the first message. You have no "
            "web access here, so any other URL is invented.\n"
            '- Every source with type="deck" MUST have the `page` number the evidence '
            "actually appears on.\n"
            "- If a claim has no citable URL, cite the deck page instead or drop the "
            "claim. Never invent, guess or complete a URL.\n"
            "- company.website_url and every founders[*].linkedin_url must be null "
            "unless that exact URL is in the citable list.\n"
            "- At most 5 key_findings and at most 5 founder_questions.\n"
            "- The report goes in the tool input, not in prose."
        )

    # ---------------------------------------------------------------- prompts

    @staticmethod
    def _criteria_block(firm: FirmProfile | None) -> str:
        """Firm emphasis note. Optional by design (docs/BUILD_PLAN.md §10) — with
        no firm profile the agent still produces the full generic report."""
        if not firm or not firm.criteria:
            return ""
        bullets = "\n".join(f"- {criterion}" for criterion in firm.criteria)
        return (
            f"\n\nEMPHASIS FOR THIS INVESTOR — {firm.name} weighs these criteria most "
            f"heavily:\n{bullets}\n"
            "Give these criteria extra attention and make sure the report speaks to "
            "each of them. This changes your emphasis and ordering only: it never "
            "lets you skip a section, soften the evidence rule, or drop a material "
            "finding that falls outside these criteria."
        )

    def _research_system_prompt(self, firm: FirmProfile | None) -> str:
        return (
            "You are a due-diligence research analyst supporting a pre-seed / seed VC "
            "who is deciding whether to take a first meeting with this company. In "
            "this turn you have exactly one job: use the web_search tool to gather "
            "real, citable external evidence. You are not writing the final report "
            "yet.\n\n"
            "Research these areas and only these areas.\n\n"
            "A. TAM / market credibility\n"
            "   - Do independent sources support the deck's TAM/SAM/SOM figures, or is "
            "the market defined far more broadly than the segment the company can "
            "actually reach?\n"
            "   - Is the market growing? Prefer dated third-party figures over "
            "vendor marketing.\n"
            "   - Which market assumptions in the deck have no external support at "
            "all?\n\n"
            "B. Competitive landscape\n"
            "   - Verify the competitors the deck names: do they exist, what have they "
            "raised, what do they actually sell, and to whom?\n"
            "   - Find direct competitors the deck does NOT name — especially obvious "
            "ones and incumbents in the company's own geography and buyer segment.\n"
            "   - Test whether the claimed differentiation is real or trivially "
            "reproducible by a better-funded incumbent.\n\n"
            "C. Founder profile (investment-relevant public professional information "
            "only)\n"
            "   - Verify each named founder's stated employers, roles, tenures and "
            "prior companies.\n"
            "   - Look for founder-market fit evidence, and for material discrepancies "
            "between the deck's claims and the public record.\n"
            "   - Stay strictly professional: no private life, family, health, "
            "politics, finances or anything that is not a credential an investor may "
            "legitimately check.\n\n"
            "D. Two artefacts the downstream CRM record needs verbatim\n"
            "   - The company's own official website URL.\n"
            "   - For each named founder: their LinkedIn profile URL, plus a one-line "
            "professional bio.\n\n"
            "Hard rules:\n"
            "- Search for the exact entities named in the deck. Never present a "
            "same-named or similar company or person as if it were this one. If you "
            "cannot confirm an identity match, say so explicitly in your notes.\n"
            "- Many early-stage companies and people are genuinely not findable. "
            '"Searched N different ways, no result for X" is a correct and valuable '
            "research outcome — write it down plainly. Never substitute a "
            "plausible-looking URL, profile or figure for one you could not actually "
            "find. A fabricated URL is far worse than a missing one.\n"
            "- Attach the full URL to every external fact you report. A fact with no "
            "URL is unusable downstream.\n"
            "- Keep three things separate in your wording: what the deck claims, what "
            "a source says, and what you are inferring.\n\n"
            "Output for this turn: plain prose notes under headings A, B, C, D, with "
            "the source title and full URL under each fact. Then finish with these "
            "explicit lines:\n"
            "  WEBSITE: <url or NOT FOUND>\n"
            "  LINKEDIN <founder name>: <url or NOT FOUND>   (one line per named "
            "founder)\n"
            "Stop once you have covered A-D or exhausted the searches worth running. "
            "Do not output JSON."
            + self._criteria_block(firm)
        )

    def _research_user_prompt(self, deck: ParsedDeck) -> str:
        return (
            f"Pitch deck filename: {deck.filename}\n\n"
            "Research this company. The deck text follows, page by page.\n\n"
            f"{self._deck_page_map(deck)}"
        )

    def _extraction_system_prompt(self, firm: FirmProfile | None) -> str:
        return (
            "You are the analyst writing the final diligence report a pre-seed / seed "
            "VC will read before a first founder meeting. You have no tools in this "
            "turn. Your only inputs are the pitch deck (given page by page) and the "
            "research notes and citable-URL list in the user message. Deliver the "
            f"report by calling the {REPORT_TOOL_NAME} tool exactly once, with the "
            "whole report as the tool input and no prose alongside it.\n\n"
            "THE EVIDENCE RULE — the core product requirement (docs/BUILD_PLAN.md §5), "
            "not a preference:\n"
            '- Every source with type="external" MUST carry a `url`, and that URL MUST '
            "appear verbatim in the citable-URL list in the user message. You have no "
            "web access in this turn, so a URL that is not on that list is invented by "
            "definition. Never invent, guess, complete or reconstruct one. If a claim "
            "has no citable URL, cite the deck page instead or drop the claim.\n"
            '- Every source with type="deck" MUST carry the `page` number the evidence '
            "actually appears on, read off the page map in the user message. Do not "
            "guess page numbers.\n"
            "- `evidence` is a short quote or close paraphrase of what that specific "
            "source says — not your conclusion about it.\n"
            "- Identity matters. Never cite a page about a same-named but different "
            "company or person as if it were this one. If the research could only find "
            "a near-match, you may cite it only when the `evidence` text says plainly "
            "that it is a different entity and why — and it must never be used to fill "
            "in website_url or linkedin_url.\n"
            "- Keep three things separate in your wording: what the deck claims, what "
            "an external source says, and what you conclude. Never state an inference "
            "as a verified fact.\n\n"
            "QUALITATIVE ONLY: never output a number as a judgement of quality — no "
            "rating, grade, percentile, ranking, index or weighted total anywhere, and "
            "never rank this company on a scale. Every judgement in this schema is a "
            "status enum plus evidence text. Numbers appear only when you are quoting "
            "a figure the deck or a source actually states. If you feel the urge to "
            "quantify how good something is, write the reasoning instead.\n\n"
            "WHAT GOES WHERE\n\n"
            "company: the name as used in the deck; `one_liner` is one plain sentence "
            "an investor could paste into a CRM (what it does, for whom); "
            "`website_url` is the company's own site or null.\n\n"
            "overview: a short investment-relevant summary — stage, what it sells, to "
            "whom, where, and what it is raising.\n\n"
            "tam_sam_som:\n"
            "- tam_stated / sam_stated / som_stated: quote the deck's own figures "
            "verbatim including currency, or null where the deck gives none.\n"
            '- tam_methodology: "top_down" for an industry-report figure filtered '
            'down; "bottom_up" for customer count x price built up from the ICP; '
            '"both" when both are shown; "unclear" otherwise. At pre-seed, judge the '
            "quality of the method, not the precision of the number.\n"
            "- som_pct_of_sam_flagged: true when SOM as a share of SAM falls outside "
            "the credible ~1-15% band, and ALSO true when it cannot be derived at all "
            "from what the deck gives.\n"
            "- external_validation_present: true only if the research notes contain at "
            "least one third-party data point that actually bears on this company's "
            "market.\n"
            "- summary: whether the market definition matches the reachable segment, "
            "which assumptions are unsupported, and where an external source "
            "disagrees.\n\n"
            "competitors: one entry per competitor — `name`; `funding_info` (stage or "
            "amount raised, null if unknown, never invented); "
            "`differentiation_claimed` (what the deck claims, or the absence of a "
            "claim); `is_direct` (true for a direct substitute for the same buyer, "
            "false for adjacent or partial); `verified_externally` (true only if a "
            "citable URL confirms the company exists and does what is claimed). "
            "Include obvious direct competitors the deck omits, taken from the "
            "research notes rather than from memory. Set "
            "missing_direct_competitor_flag true when a well-known direct competitor "
            "in this segment or geography is absent from the deck. `why_now_why_us` is "
            "the deck's timing argument (technology shift, regulation, behaviour "
            'change) and whether it is specific or just "big market plus AI"; null if '
            "absent.\n\n"
            "founder_profile:\n"
            "- categories: cover all eight — industry_experience, vision_strategy, "
            "track_record, learning_agility, team_leadership, network_strength, "
            'resilience, execution_strength. Use "supported" when an external source '
            'or a concrete deck fact backs it; "questionable" when the claim is vague '
            'or only self-asserted; "red_flag" when a source contradicts the deck or '
            'something material is off; "unknown" when there is genuinely nothing to '
            'go on. For a founder the research could not verify, "unknown" is the '
            "honest answer — use it instead of inflating or inventing.\n"
            "- founder_market_fit: lived first-hand exposure to this specific problem "
            'plus a network genuinely differentiated for this market -> "strong"; '
            'adjacent or partial exposure -> "moderate"; recent interest only -> '
            '"weak"; not determinable -> "unclear".\n\n'
            "founders: one entry per named founder — `name` as spelled in the deck, "
            "`bio_one_liner` (their relevant professional background in one sentence), "
            "`linkedin_url` (their real profile, or null).\n\n"
            "additional_metrics: use only these names, and include each one the deck "
            "gives you anything to judge — problem_validation (interview counts, LOIs, "
            "waitlist, pilots: presence AND specificity), traction (revenue, usage, "
            "retention, cohorts; say so when a figure is self-reported), "
            "business_model_clarity (is pricing stated, and is the price/ACV used in "
            "the model the same one used in the TAM build), cap_table_legal "
            "(incorporation, founder split, option pool, pre-existing IP or unusual "
            "arrangements), ask_and_use_of_funds (does the ask size and its allocation "
            "make sense against the milestones and the claimed opportunity), "
            "non_obvious_insight (a contrarian, specific market view versus a generic "
            "thesis).\n\n"
            "key_findings: AT MOST 5, and fewer when the evidence does not justify "
            "five — do not force five negatives (docs/BUILD_PLAN.md Step 5). A finding "
            "earns its place only if it would change how the investor runs the first "
            "meeting: a contradiction, an unsupported load-bearing assumption, a red "
            "flag, or an important unknown. Never pad with a generic risk that applies "
            "to every seed company. Give each finding a short stable id (for example "
            '"som-not-derivable") so the questions can reference it.\n\n'
            "founder_questions: AT MOST 5. Each must trace to a specific finding id "
            "via `based_on_finding_ids` (or to an unresolved assumption you name in "
            "the question itself), and must ask for facts this founder actually has. "
            "The test: the VC could paste it into an email unedited, and would not "
            "have known to ask it from reading the deck alone (docs/BUILD_PLAN.md §8).\n"
            '  BAD, never emit: "Who are your competitors?", "How will you scale?", '
            '"What differentiates you?" — generic, answered by the deck, no evidence '
            "behind them.\n"
            '  GOOD: "The deck assumes a three-month enterprise sales cycle, but '
            "comparable products appear to face significantly longer procurement. What "
            "has the sales cycle been for each of your currently paying enterprise "
            'customers, from first contact to signed contract?" — anchored to a '
            "specific finding and asks for a number the founder must already have.\n\n"
            "MISSING LINKS: set company.website_url and any founders[*].linkedin_url "
            "to null when the research did not find a real one. Downstream Slack and "
            "CRM records render a missing link gracefully; a fabricated link corrupts "
            "the record and destroys the investor's trust in every other citation you "
            "give. Returning null is correct behaviour, not a failure."
            + self._criteria_block(firm)
        )

    def _extraction_user_prompt(
        self,
        deck: ParsedDeck,
        transcript: str,
        cited_sources: list[tuple[str, str]],
    ) -> str:
        return (
            f"Pitch deck filename: {deck.filename}\n\n"
            "=== DECK TEXT, PAGE BY PAGE (use these numbers for every deck source) ===\n"
            f"{self._deck_page_map(deck)}\n\n"
            "=== RESEARCH NOTES FROM THE WEB-SEARCH PASS ===\n"
            f"{transcript or '(The research pass returned no notes.)'}\n\n"
            "=== URLS AVAILABLE TO CITE ===\n"
            f"{self._citable_url_block(cited_sources)}\n\n"
            "Now produce the report."
        )

    @staticmethod
    def _citable_url_block(cited_sources: list[tuple[str, str]]) -> str:
        if not cited_sources:
            return (
                "No URLs were retrieved by the research pass. You therefore may NOT "
                'emit any source with type="external" — every source in this report '
                'must be type="deck" with a page number, and anything that would need '
                "external validation must be reported as unverified or unknown."
            )
        lines = "\n".join(
            f"{index}. {title} — {url}"
            for index, (title, url) in enumerate(cited_sources, start=1)
        )
        return (
            "These are the ONLY URLs you may put in an external source. Copy them "
            "character for character. A URL not on this list is fabricated.\n"
            f"{lines}"
        )

    @staticmethod
    def _deck_page_map(deck: ParsedDeck) -> str:
        if not deck.pages:
            return (
                "(No page breakdown was available for this deck — it parsed as one "
                "block, so use page 1 for every deck source and say in the evidence "
                "text that the page is not resolvable. Full text follows.)\n\n"
                f"{deck.full_text}"
            )
        return "\n\n".join(f"[page {page.page}]\n{page.text}" for page in deck.pages)
