#affiliate_growth_agent.py
"""
Affiliate Marketing Social Media Growth Agent — an autonomous subagent that
reports to LITE. Routes through the same shared AI client every other action
uses (core.ai_client.generate_content), so it automatically inherits LITE's
Gemini -> Claude -> Groq -> custom fallback chain and cooldown behaviour —
no separate API key handling here.

Every response follows the agent's fixed persona (SYSTEM_PROMPT below) and
always ends with a "## LITE EXECUTIVE SUMMARY" block (Opportunity /
Recommended Action / Expected ROI / Risk Level / Priority / Next Actions),
which this module parses into a dict so the calling code (or a future
dashboard widget) can act on `priority` / `risk_level` without re-parsing
free text.

Call shape matches every other action module (dev_agent, self_maintain,
flight_finder, ...):

    def affiliate_growth_agent(parameters: dict, player=None, speak=None) -> str

Actions (parameters["action"]):
    rank_offers        - rank affiliate offers for a niche
    research_audience   - target-audience research for a niche
    growth_strategy     - social media growth strategy
    content_calendar    - weekly/monthly content calendar
    post_ideas          - platform-specific post ideas
    video_script         - short-form video script
    email_sequence      - affiliate nurture/conversion email sequence
    landing_page        - landing page copy
    ad_copy             - paid ad copy
    performance_review  - review metrics against the success-metrics ladder
    custom              - free-form task, uses "task" parameter verbatim

Two further actions talk to GAS (Growth Agent Service) — a small Cloudflare
Worker + Supabase deployment, in gas/ — that keeps working affiliate offers
even while your laptop is off:

    assign_job    - hand the agent a standing job (niche/platforms/cadence)
                    it will keep working on its own via a cron trigger
    get_report    - pull the "while you were away" report: drafts written,
                    leads, conversions, earnings delta since you last checked

These two require gas_worker_url / gas_worker_key in
config/api_keys.json (see config/api_keys.example.json). Everything else in
this file works with no extra config, same as before.

See bottom of file for the exact main.py FUNCTION_DECLARATIONS entry and
dispatch snippet needed to wire this in (kept out of main.py itself so this
stays a self-contained, reviewable diff).
"""
import json
import re
import sys
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR     = get_base_dir()
LOG_PATH     = BASE_DIR / "memory" / "affiliate_growth_agent_log.jsonl"
MODEL_NAME   = "gemini-3.5-flash"   # passed through to generate_content; the
                                     # shared client still falls back to
                                     # Claude/Groq/custom if Gemini fails
WORKER_TIMEOUT = 20   # seconds — the cloud service does the slow work async;
                       # these calls should just be reads/writes against Supabase


# ── Persona ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """# AGENT NAME
Affiliate Marketing Social Media Growth Agent

# ROLE
You are an autonomous Social Media Marketing Expert reporting directly to LITE.

Your mission is to build, grow, and optimise profitable affiliate marketing
businesses through strategic social media marketing, audience growth, content
creation, lead generation, conversion optimisation, and performance analytics.

You think like a world-class combination of:
- Affiliate Marketing Director
- Social Media Strategist
- Content Marketing Expert
- Performance Marketer
- Copywriter
- Community Builder
- Digital Growth Consultant

Your sole objective is to maximise long-term affiliate revenue while building
trusted audiences.

# PRIMARY RESPONSIBILITIES
1. Identify profitable affiliate marketing opportunities.
2. Analyse affiliate offers and rank them by potential.
3. Research target audiences.
4. Create social media growth strategies.
5. Design content calendars.
6. Generate high-converting content ideas.
7. Write platform-specific posts.
8. Optimise engagement and conversion rates.
9. Monitor performance metrics.
10. Recommend experiments and improvements.
11. Report findings and recommendations to LITE.

# SUCCESS METRICS
Prioritise, in order:
1. Affiliate revenue
2. Qualified leads generated
3. Conversion rate
4. Click-through rate
5. Audience growth
6. Engagement rate
7. Email list growth
8. Cost efficiency

Never optimise vanity metrics at the expense of revenue.

# OPERATING PRINCIPLES
You must:
- Think strategically before acting.
- Use data-driven reasoning.
- Focus on ROI.
- Continuously test assumptions.
- Look for leverage opportunities.
- Recommend automation whenever practical.
- Prioritise sustainable audience trust.

You must challenge weak ideas and explain why better alternatives exist.
Do not blindly agree with requests.

# SPECIAL EXPERTISE
Affiliate marketing, social media marketing, influencer marketing, personal
branding, community building, content strategy, short-form video marketing,
SEO, email marketing, marketing funnels, conversion optimisation, behavioural
psychology, consumer decision-making, analytics.

# SOCIAL PLATFORMS
Develop strategies for: Facebook, Instagram, TikTok, X, LinkedIn, YouTube,
YouTube Shorts, Pinterest, Threads. Recommend the best platforms based on
audience behaviour rather than defaulting to all of them.

# CONTENT RESPONSIBILITIES
When creating content:
1. Identify audience pain points.
2. Create attention-grabbing hooks.
3. Increase curiosity.
4. Deliver value.
5. Build authority.
6. Generate trust.
7. Include a clear CTA.

Generate as requested: post ideas, reels ideas, video scripts, carousel
posts, lead magnets, email sequences, ads, landing page copy, community
engagement prompts.

# MARKET RESEARCH FRAMEWORK
Before recommending any campaign, analyse: audience demographics, audience
psychology, competitor activity, trending topics, affiliate commission
levels, product-market fit, market saturation, traffic opportunities.
Provide evidence-based conclusions, and flag explicitly when you are
reasoning from general knowledge rather than current data because you have
no live data feed.

# CONTENT CALENDAR FRAMEWORK
When asked to produce a content plan, provide: monthly strategy, weekly
themes, daily content ideas, CTA strategy, content objectives, expected
outcomes.

# DECISION FRAMEWORK
For every recommendation, state:
1. Objective
2. Reasoning
3. Expected benefit
4. Potential risks
5. Success metrics

# REPORTING TO LITE
Every deliverable must end with exactly this block, verbatim headers, so it
can be parsed programmatically:

## LITE EXECUTIVE SUMMARY

Opportunity:
[Summary]

Recommended Action:
[Summary]

Expected ROI:
[Estimate]

Risk Level:
[Low/Medium/High]

Priority:
[Critical/High/Medium/Low]

Next Actions:
1.
2.
3.

# AUTONOMOUS BEHAVIOUR
If information is incomplete:
- Make reasonable assumptions.
- State assumptions clearly, labelled "Assumptions:".
- Continue working.
- Present alternatives where needed.
Do not stop merely because some information is missing.

# AFFILIATE MARKETING OBJECTIVE
The ultimate goal is to build multiple scalable affiliate marketing income
streams that can generate sustainable side income and eventually become a
significant revenue source.

Every recommendation should be evaluated against:
"Will this increase revenue, audience trust, and long-term scalability?"
If not, reject it and propose a better alternative.
"""


# ── LITE EXECUTIVE SUMMARY parsing ──────────────────────────────────────

_SUMMARY_BLOCK_RE = re.compile(r"##\s*LITE EXECUTIVE SUMMARY\s*(.*)", re.IGNORECASE | re.DOTALL)
_FIELD_RE = re.compile(
    r"(Opportunity|Recommended Action|Expected ROI|Risk Level|Priority)\s*:\s*(.*?)"
    r"(?=\n(?:Opportunity|Recommended Action|Expected ROI|Risk Level|Priority|Next Actions)\s*:|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_NEXT_ACTIONS_RE = re.compile(r"Next Actions\s*:\s*(.*)", re.IGNORECASE | re.DOTALL)


def _parse_lite_summary(text: str) -> dict:
    """Extract the mandatory LITE EXECUTIVE SUMMARY block into a dict.
    Tolerant of minor formatting drift — falls back to empty fields rather
    than raising, since a subagent formatting slip should never crash the
    main assistant loop."""
    summary = {
        "opportunity": "", "recommended_action": "", "expected_roi": "",
        "risk_level": "", "priority": "", "next_actions": [],
    }
    block_match = _SUMMARY_BLOCK_RE.search(text)
    block = block_match.group(1) if block_match else text

    key_map = {
        "opportunity": "opportunity",
        "recommended action": "recommended_action",
        "expected roi": "expected_roi",
        "risk level": "risk_level",
        "priority": "priority",
    }
    for match in _FIELD_RE.finditer(block):
        label = match.group(1).strip().lower()
        summary[key_map[label]] = match.group(2).strip()

    actions_match = _NEXT_ACTIONS_RE.search(block)
    if actions_match:
        for ln in actions_match.group(1).splitlines():
            cleaned = re.sub(r"^\d+[\.\)]\s*", "", ln.strip()).strip()
            if cleaned:
                summary["next_actions"].append(cleaned)
    return summary


def _log_run(action: str, task: str, ok: bool, summary: dict, error: str = "") -> None:
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "action": action, "task": task, "ok": ok,
            "summary": summary, "error": error,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except Exception:
        pass  # logging must never break the agent


# ── Task builders (one per PRIMARY RESPONSIBILITY) ──────────────────────

def _task_rank_offers(p: dict) -> str:
    niche  = p.get("niche", "").strip()
    offers = p.get("offers", "")
    return (
        f"Analyse and rank affiliate offers for the '{niche}' niche by "
        f"revenue potential, applying the Market Research Framework "
        f"(commission level, product-market fit, saturation, traffic "
        f"opportunity). Give a ranked shortlist with reasoning.\n\n"
        f"Offers:\n{offers}"
    )


def _task_research_audience(p: dict) -> str:
    niche = p.get("niche", "").strip()
    return f"Research the target audience for the '{niche}' affiliate niche."


def _task_growth_strategy(p: dict) -> str:
    niche     = p.get("niche", "").strip()
    platforms = p.get("platforms", "").strip()
    budget    = p.get("budget_notes", "").strip()
    task = f"Design a social media growth strategy for a '{niche}' affiliate business."
    task += (f" Prioritise these platforms: {platforms}." if platforms
             else " Recommend the best-fit platforms yourself and justify the choice.")
    if budget:
        task += f" Budget/resourcing notes: {budget}"
    return task


def _task_content_calendar(p: dict) -> str:
    niche     = p.get("niche", "").strip()
    weeks     = p.get("weeks", 4)
    platforms = p.get("platforms", "").strip()
    task = (
        f"Produce a {weeks}-week content calendar for the '{niche}' "
        f"affiliate business, following the Content Calendar Framework "
        f"(monthly strategy, weekly themes, daily content ideas, CTA "
        f"strategy, objectives, expected outcomes)."
    )
    if platforms:
        task += f" Platforms: {platforms}."
    return task


def _task_post_ideas(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    platform = p.get("platform", "Instagram").strip()
    count    = p.get("count", 10)
    return f"Generate {count} high-converting {platform} post ideas for the '{niche}' niche."


def _task_video_script(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    platform = p.get("platform", "TikTok").strip()
    angle    = p.get("angle", "").strip()
    return (
        f"Write a short-form video script for {platform} in the '{niche}' "
        f"niche, angle: '{angle}'. Include hook, value, CTA, and an "
        f"estimated runtime."
    )


def _task_email_sequence(p: dict) -> str:
    niche       = p.get("niche", "").strip()
    goal        = p.get("goal", "convert to sale").strip()
    num_emails  = p.get("num_emails", 5)
    return (
        f"Write a {num_emails}-email nurture/conversion sequence for the "
        f"'{niche}' affiliate offer. Goal: {goal}. Include subject lines "
        f"and CTAs for each email."
    )


def _task_landing_page(p: dict) -> str:
    niche = p.get("niche", "").strip()
    offer = p.get("offer", "").strip()
    return f"Write landing page copy for the '{niche}' offer: {offer}."


def _task_ad_copy(p: dict) -> str:
    niche    = p.get("niche", "").strip()
    offer    = p.get("offer", "").strip()
    platform = p.get("platform", "Facebook").strip()
    return f"Write {platform} ad copy (multiple variants) for the '{niche}' offer: {offer}."


def _task_performance_review(p: dict) -> str:
    metrics = p.get("metrics", "")
    return (
        "Review the following performance metrics against the Success "
        "Metrics priority order (revenue > qualified leads > conversion "
        "rate > CTR > audience growth > engagement rate > email list "
        "growth > cost efficiency). Identify what is underperforming, "
        "propose experiments, and flag anything that looks like "
        f"vanity-metric optimisation.\n\nMetrics:\n{metrics}"
    )


def _worker_request(method: str, path: str, json_body: dict = None) -> dict:
    """Talk to the always-on GAS (Growth Agent Service) Cloudflare Worker.
    Raises on any failure — callers turn that into a spoken-friendly error
    rather than a stack trace."""
    from config import get_config
    import requests

    cfg = get_config()
    base_url = (cfg.get("gas_worker_url") or "").rstrip("/")
    key = cfg.get("gas_worker_key") or ""
    if not base_url or not key:
        raise RuntimeError(
            "gas_worker_url / gas_worker_key are not set "
            "in config/api_keys.json — the overnight cloud agent isn't "
            "connected yet."
        )

    resp = requests.request(
        method,
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=json_body,
        timeout=WORKER_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def _handle_assign_job(p: dict) -> str:
    niche = p.get("niche", "").strip()
    if not niche:
        return "What niche should the overnight agent work on, sir?"

    platforms = [s.strip() for s in (p.get("platforms") or "").split(",") if s.strip()] or None
    body = {"niche": niche, "goal": p.get("goal") or "grow leads and affiliate revenue"}
    if platforms:
        body["platforms"] = platforms
    if p.get("cadence_hours"):
        body["cadence_hours"] = int(p["cadence_hours"])
    if p.get("posts_per_run"):
        body["posts_per_run"] = int(p["posts_per_run"])

    try:
        result = _worker_request("POST", "/jobs", body)
    except Exception as e:
        return f"Couldn't reach the overnight agent to assign that job: {e}"

    job = result.get("job", {})
    cadence = job.get("cadence_hours", 6)
    return (
        f"Done, sir. The growth agent will work the '{niche}' niche every "
        f"{cadence} hours while you're away, drafting content for your "
        f"approval and tracking PartnerStack and Exness performance. Ask me "
        f"for a report whenever you're back."
    )


def _handle_get_report(p: dict) -> str:
    try:
        result = _worker_request("GET", "/report/latest")
    except Exception as e:
        return f"Couldn't reach the overnight agent for a report: {e}"

    report = result.get("report")
    if not report:
        return "No overnight report yet, sir — the agent hasn't completed a cycle since you assigned a job."

    text = report.get("summary_text", "").strip()
    pending = report.get("pending_review", 0)
    if pending:
        text += f"\n\n{pending} draft(s) are waiting in your approval queue."
    return text or "Report came back empty — worth checking the Worker logs."


_TASK_BUILDERS = {
    "rank_offers":        _task_rank_offers,
    "research_audience":  _task_research_audience,
    "growth_strategy":    _task_growth_strategy,
    "content_calendar":   _task_content_calendar,
    "post_ideas":         _task_post_ideas,
    "video_script":       _task_video_script,
    "email_sequence":     _task_email_sequence,
    "landing_page":       _task_landing_page,
    "ad_copy":            _task_ad_copy,
    "performance_review": _task_performance_review,
}


# ── Entry point (same call shape as every other LITE action) ───────────

def affiliate_growth_agent(parameters: dict, player=None, speak=None) -> str:
    p = parameters or {}
    action = (p.get("action") or "custom").strip().lower()

    # These two hit the always-on cloud service directly — no LLM call needed here.
    if action == "assign_job":
        text = _handle_assign_job(p)
        if player is not None and hasattr(player, "show_content"):
            try:
                player.show_content("AFFILIATE GROWTH AGENT — JOB ASSIGNED", text)
            except Exception:
                pass
        return text

    if action == "get_report":
        text = _handle_get_report(p)
        if player is not None and hasattr(player, "show_content"):
            try:
                player.show_content("AFFILIATE GROWTH AGENT — OVERNIGHT REPORT", text)
            except Exception:
                pass
        return text

    if action == "custom":
        task = (p.get("task") or "").strip()
        if not task:
            return "Tell me what you want the growth agent to work on, sir."
    else:
        builder = _TASK_BUILDERS.get(action)
        if not builder:
            valid = ", ".join(sorted(_TASK_BUILDERS) | {"custom"})
            return f"Unknown affiliate_growth_agent action '{action}'. Valid actions: {valid}."
        task = builder(p)

    full_prompt = (
        SYSTEM_PROMPT.strip()
        + "\n\n---\n\nTASK:\n" + task
        + "\n\nRespond as the Affiliate Marketing Social Media Growth Agent. "
          "Follow the Decision Framework for any recommendation and end "
          "with the mandatory LITE EXECUTIVE SUMMARY block."
    )

    from core.ai_client import generate_content as _ai_generate

    try:
        response = _ai_generate(full_prompt, model=MODEL_NAME)
        text = response.text.strip()
    except Exception as e:
        error_msg = f"Affiliate growth agent couldn't reach any AI provider: {e}"
        _log_run(action, task, ok=False, summary={}, error=str(e))
        return error_msg

    if "LITE EXECUTIVE SUMMARY" not in text.upper():
        try:
            follow_up = (
                "Your previous response did not include the mandatory "
                "'## LITE EXECUTIVE SUMMARY' block. Reply with ONLY that "
                "block, summarising the response below, using the exact "
                "field labels Opportunity / Recommended Action / Expected "
                "ROI / Risk Level / Priority / Next Actions.\n\n---\n" + text
            )
            addendum = _ai_generate(SYSTEM_PROMPT.strip() + "\n\n" + follow_up, model=MODEL_NAME)
            text = text + "\n\n" + addendum.text.strip()
        except Exception:
            pass  # ship what we have rather than fail the whole call

    summary = _parse_lite_summary(text)
    _log_run(action, task, ok=True, summary=summary)

    if player is not None and hasattr(player, "show_content"):
        try:
            player.show_content("AFFILIATE GROWTH AGENT", text)
        except Exception:
            pass

    return text


# ── main.py wiring (paste manually — kept out of this file on purpose) ──
#
# 1) Import, near the other actions/* imports:
#
#    from actions.affiliate_growth_agent import affiliate_growth_agent
#
# 2) Add to the FUNCTION_DECLARATIONS list, alongside dev_agent/self_maintain:
#
#    {
#        "name": "affiliate_growth_agent",
#        "description": (
#            "Autonomous Affiliate Marketing Social Media Growth subagent "
#            "reporting to LITE. Ranks affiliate offers, researches "
#            "audiences, builds social growth strategies and content "
#            "calendars, writes posts/reels/video scripts/email sequences/"
#            "ad copy/landing page copy, and reviews performance metrics. "
#            "Every response ends with a structured LITE executive summary "
#            "(opportunity, recommended action, expected ROI, risk level, "
#            "priority, next actions). Call this whenever the user asks "
#            "about affiliate marketing, content strategy, social growth, "
#            "or wants marketing content written."
#        ),
#        "parameters": {
#            "type": "OBJECT",
#            "properties": {
#                "action": {
#                    "type": "STRING",
#                    "description": (
#                        "rank_offers | research_audience | growth_strategy | "
#                        "content_calendar | post_ideas | video_script | "
#                        "email_sequence | landing_page | ad_copy | "
#                        "performance_review | assign_job | get_report | "
#                        "custom (default: custom)"
#                    ),
#                },
#                "task":          {"type": "STRING",  "description": "Free-form instruction, used when action=custom."},
#                "niche":         {"type": "STRING",  "description": "The affiliate niche, e.g. 'personal finance apps'."},
#                "goal":          {"type": "STRING",  "description": "assign_job: standing goal for the overnight agent."},
#                "cadence_hours": {"type": "INTEGER", "description": "assign_job: how often it runs a content pass (default 6)."},
#                "posts_per_run": {"type": "INTEGER", "description": "assign_job: pieces drafted per pass (default 3)."},
#                "offers":       {"type": "STRING",  "description": "Affiliate offers to rank (plain text or JSON list)."},
#                "platforms":    {"type": "STRING",  "description": "Comma-separated platforms to prioritise."},
#                "platform":     {"type": "STRING",  "description": "Single platform for post_ideas/video_script/ad_copy."},
#                "budget_notes": {"type": "STRING",  "description": "Budget/resourcing constraints for growth_strategy."},
#                "weeks":        {"type": "INTEGER", "description": "Length of content_calendar in weeks (default 4)."},
#                "count":        {"type": "INTEGER", "description": "Number of post_ideas to generate (default 10)."},
#                "angle":        {"type": "STRING",  "description": "Creative angle for video_script."},
#                "goal":         {"type": "STRING",  "description": "Goal of the email_sequence (default 'convert to sale')."},
#                "num_emails":   {"type": "INTEGER", "description": "Number of emails in email_sequence (default 5)."},
#                "offer":        {"type": "STRING",  "description": "The specific offer for landing_page/ad_copy."},
#                "metrics":      {"type": "STRING",  "description": "Performance metrics to review (plain text or JSON)."},
#            },
#            "required": []
#        }
#    },
#
# 3) Add to the dispatch block, alongside the dev_agent/self_maintain elifs:
#
#    elif name == "affiliate_growth_agent":
#        r = await loop.run_in_executor(
#            None, lambda: affiliate_growth_agent(parameters=args, player=self.ui, speak=self.speak)
#        )
#        result = r or "Done."
