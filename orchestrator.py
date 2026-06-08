"""End-to-end orchestrator.

Reads campaigns from the DB, picks the most viable one(s), runs the
engine over a source video, schedules posts via Metricool, then submits
the live post URL back to Whop for payout review.

Designed to be called either:
  - From the APScheduler in scheduler.py (24/7 mode)
  - Or once via `python -m orchestrator --campaign <id> --source <url>`
    for a single-shot manual run.

This module is intentionally thin — every step is delegated to the
existing modules (scanner, engine, publisher, submitter). It only owns
the *coordination* and the rules-gate.
"""

from __future__ import annotations

import json

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
from typing import Optional

from loguru import logger

from config import settings
from db import Repository
from engine import EnginePipeline, ProducedClip
from publisher import (
    ApprovalStatus,
    MultiPlatformPublisher,
    TelegramGate,
    validate_against_rules,
)


def pick_top_viable_campaign(repo: Repository, min_pct: Optional[float] = None) -> Optional[dict]:
    """Highest viability_score campaign that passes the budget gate."""
    floor = min_pct if min_pct is not None else settings.min_budget_remaining_pct
    with repo.conn() as c:
        row = c.execute(
            "SELECT * FROM campaigns "
            "WHERE status='active' "
            "AND (budget_remaining_pct IS NULL OR budget_remaining_pct >= ?) "
            "ORDER BY viability_score DESC NULLS LAST LIMIT 1",
            (floor,),
        ).fetchone()
    return dict(row) if row else None


def produce_clips_for_campaign(
    campaign: dict,
    source_url: str,
    n_clips: Optional[int] = None,
    format_mode: str = "smart",
) -> list[ProducedClip]:
    pipeline = EnginePipeline()

    # If this campaign has a queued Brain experiment AND we're in an explore
    # slot, apply its system_params now so they reach the producer modules,
    # not just the prompt. The consume-marker is set in the brain-advice
    # block below; we PEEK here without clearing.
    try:
        from engine.brain.allocator import slot_intent
        from engine.brain.experimenter import get_queued_experiment
        intent = slot_intent(Repository(), campaign["id"])
        if intent.get("mode") == "explore":
            qexp = get_queued_experiment(Repository(), campaign["id"])
            if qexp:
                # Stash on the campaign dict so _ensure_clip_in_db can record
                # the experiment hypothesis + params alongside each produced clip.
                campaign["_active_experiment"] = qexp
                params = qexp.get("system_params") or {}
                if params.get("format_mode") in ("smart", "blur_pad", "crop", "letterbox", "group_focus", "auto"):
                    new_mode = params["format_mode"]
                    if new_mode != format_mode:
                        logger.info(
                            f"[orchestrator] queued experiment overrides format_mode: "
                            f"{format_mode!r} → {new_mode!r}"
                        )
                        format_mode = new_mode
    except Exception as e:
        logger.warning(f"[orchestrator] experiment-params lookup failed (continuing with default): {e}")

    # Don't suggest moments we've already produced (posted) or already
    # rejected against this campaign + source. The user sees fresh clips
    # every time, never the same one twice.
    excluded = _excluded_ranges_for(campaign, source_url)
    if excluded:
        logger.info(
            f"[orchestrator] excluding {len(excluded)} prior moment(s) from scoring: "
            f"{['{:.1f}-{:.1f}'.format(a, b) for a, b in excluded[:5]]}"
            + (f" (+{len(excluded) - 5} more)" if len(excluded) > 5 else "")
        )

    # Brain: pull director's brief + per-campaign patterns + exploit/explore intent.
    brain_advice = None
    try:
        from engine.brain import advice_for_campaign
        from engine.brain.allocator import slot_intent, render_intent_for_prompt
        from engine.brain.director import (
            brief_for_campaign,
            render_brief_for_scorer,
        )
        repo_local = Repository()
        pieces = []
        brief = brief_for_campaign(repo_local, campaign["id"])
        if brief and brief.get("decision") != "no":
            rendered = render_brief_for_scorer(brief)
            if rendered:
                pieces.append(rendered)
        adv = advice_for_campaign(repo_local, campaign["id"])
        if adv:
            pieces.append(adv)
        intent = slot_intent(repo_local, campaign["id"])
        rendered = render_intent_for_prompt(intent)
        if rendered:
            pieces.append(rendered)
        # If this is an explore slot AND we have a queued auto-experiment,
        # inject its hypothesis + params so the scorer aligns with it.
        if intent.get("mode") == "explore":
            from engine.brain.experimenter import get_queued_experiment, clear_queued_experiment
            qexp = get_queued_experiment(repo_local, campaign["id"])
            if qexp:
                pieces.append(
                    f"BRAIN EXPERIMENT (run this clip aligned with these parameters): "
                    f"{qexp.get('action', '?')}. Hypothesis: {qexp.get('hypothesis', '?')}."
                )
                # Mark consumed so the next slot picks fresh.
                clear_queued_experiment(repo_local, campaign["id"])
                logger.info(
                    f"[orchestrator] consumed queued experiment for #{campaign['id']}: "
                    f"{qexp.get('hypothesis', '')[:80]}"
                )
        if pieces:
            brain_advice = "\n\n".join(pieces)
            logger.info(
                f"[orchestrator] brain advice for #{campaign['id']} "
                f"(mode={intent.get('mode')}, target={intent.get('target_style')}, "
                f"brief={bool(brief)})"
            )
    except Exception as e:
        logger.warning(f"[orchestrator] brain advice failed (continuing without): {e}")

    # Diarize when the campaign is podcast/interview content (multiple
    # named speakers). Heuristic: title contains "podcast" / "interview" /
    # "conversation", OR structured_rules.podcast_mode=true.
    title_lc = (campaign.get("title") or "").lower()
    structured_for_diar = _parse_json_obj(campaign.get("structured_rules")) or {}
    is_podcast = (
        bool(structured_for_diar.get("podcast_mode"))
        or any(k in title_lc for k in ("podcast", "interview", "conversation", "huge conversations", "dhar mann"))
    )

    clips = pipeline.run(
        source_url=source_url,
        n_clips=n_clips,
        campaign_title=campaign.get("title"),
        campaign_rules=campaign.get("rules"),
        top_performers=_parse_json_list(campaign.get("top_performers")),
        structured_rules=_parse_json_obj(campaign.get("structured_rules")),
        format_mode=format_mode,
        excluded_ranges=excluded,
        brain_advice=brain_advice,
        diarize=is_podcast,
    )

    # Music overlay — Director's brief picks genre + energy; overlay module
    # looks them up in data/music/<genre>/<energy>/ and mixes at -20dB.
    # Silent no-op if no music library is present.
    try:
        from engine.music_overlay import maybe_overlay_for_brief
        from engine.brain.director import get_brief
        brief = get_brief(Repository(), campaign["id"])
        for c in clips:
            if getattr(c, "final_path", None):
                new_path = maybe_overlay_for_brief(c.final_path, brief)
                if new_path != c.final_path:
                    c.final_path = new_path
                    logger.info(f"[orchestrator] music overlaid → {new_path.name}")
    except Exception as e:
        logger.warning(f"[orchestrator] music overlay skipped: {e}")

    return clips


def _excluded_ranges_for(campaign: dict, source_url: str) -> list[tuple[float, float]]:
    """Time ranges (start_sec, end_sec) of clips already taken against this
    campaign + source. Used to prevent the scorer re-picking the same moment."""
    repo = Repository()
    with repo.conn() as c:
        # Look up the source_videos row for this campaign + source if it exists.
        src = c.execute(
            "SELECT id FROM source_videos WHERE campaign_id = ? AND source_url = ?",
            (campaign["id"], source_url),
        ).fetchone()
        if not src:
            return []
        rows = c.execute(
            "SELECT start_sec, end_sec FROM clips "
            "WHERE source_video_id = ? AND start_sec IS NOT NULL AND end_sec IS NOT NULL",
            (src["id"],),
        ).fetchall()
    return [(float(r["start_sec"]), float(r["end_sec"])) for r in rows]


def _parse_json_list(raw) -> Optional[list[dict]]:
    if not raw:
        return None
    if isinstance(raw, list):
        return raw
    try:
        import json
        v = json.loads(raw)
        return v if isinstance(v, list) and v else None
    except Exception:
        return None


def _parse_json_obj(raw) -> Optional[dict]:
    if not raw:
        return None
    if isinstance(raw, dict):
        return raw
    try:
        import json
        v = json.loads(raw)
        return v if isinstance(v, dict) and v else None
    except Exception:
        return None


def publish_clip_to_all_platforms(
    repo: Repository,
    publisher: MultiPlatformPublisher,
    campaign: dict,
    clip: ProducedClip,
    gate: Optional[TelegramGate] = None,
) -> list[int]:
    """Post the clip to TikTok, YouTube Shorts, and Instagram Reels via
    Playwright. Writes posts rows to the DB. Returns the list of post row ids."""
    structured = _parse_json_obj(campaign.get("structured_rules"))

    # Structured rules can override required platforms.
    rules_platforms = (structured or {}).get("platforms_required") or []
    platforms_required = (
        [p for p in rules_platforms if p in ("tiktok", "youtube", "instagram")]
        or _platforms_from_campaign(campaign)
        or ["tiktok", "youtube", "instagram"]
    )
    # If this is a Vyro campaign with siblings that share the source file,
    # union their platforms_required so a single clip serves all of them.
    if (campaign.get("marketplace") or "").lower() == "vyro":
        try:
            siblings = _find_vyro_siblings(repo, campaign)
            for sib in siblings:
                sib_rules = _parse_json_obj(sib.get("structured_rules")) or {}
                for p in sib_rules.get("platforms_required") or []:
                    if p in ("tiktok", "youtube", "instagram") and p not in platforms_required:
                        platforms_required.append(p)
            if siblings:
                logger.info(
                    f"[orchestrator] vyro sibling union → platforms_required={platforms_required}"
                )
        except Exception as e:
            logger.warning(f"[orchestrator] sibling union failed: {e}")

    # Pre-flight: validate against structured rules before doing any work.
    rule_violations = _check_rules_pre_flight(clip, structured)
    if rule_violations:
        for v in rule_violations:
            logger.error(f"[rules][pre-flight] {v}")
        if gate and gate.enabled:
            gate.notify(
                "<b>❌ Clip skipped — failed campaign-rule check</b>\n\n"
                + "\n".join(f"• {v}" for v in rule_violations)
            )
        return []

    # Brain QA — final gate before approval. Blocks BLOCK-severity issues,
    # warns on WARN-severity, passes silently on FINE.
    try:
        from engine.brain.qa import qa_clip
        qa = qa_clip(
            repo,
            campaign,
            final_caption=clip.moment.caption_text or "",
            hashtags=clip.moment.hashtags or [],
            transcript_excerpt=clip.moment.transcript_excerpt or "",
            duration_sec=clip.moment.duration_sec,
            platforms=platforms_required,
            hook_text=clip.moment.hook_text,
            # When sibling campaigns expand allowed platforms, pass the union
            # so the mechanical check doesn't false-fail "instagram not in
            # #49's allowed list" when #50 covers IG.
            allowed_platforms_override=platforms_required,
            # Pass the final clip path so QA can run Claude vision on
            # sampled frames (catches subject-covered-by-overlay, watermarks,
            # crop issues that text-only QA can't see).
            video_path=str(clip.final_path) if getattr(clip, "final_path", None) else None,
        )
        if qa.severity == "block":
            logger.warning(f"[qa] block detected — attempting auto-revision: {qa.issues}")
            from engine.brain.editor import revise_for_approval
            rev = revise_for_approval(
                repo, campaign,
                caption=clip.moment.caption_text or "",
                hashtags=clip.moment.hashtags or [],
                hook_text=clip.moment.hook_text or "",
                transcript_excerpt=clip.moment.transcript_excerpt or "",
                duration_sec=clip.moment.duration_sec,
                platforms=platforms_required,
                qa_result=qa,
            )
            if rev is None:
                # Auto-revision exhausted. Don't silently kill the clip —
                # escalate to Chris for a human judgment call. The issues
                # may be false-positives (e.g. semantic avoid rules that
                # collide with normal English).
                logger.warning(
                    f"[qa] auto-revision exhausted — escalating to manual approval. "
                    f"Issues: {qa.issues}"
                )
                if gate and gate.enabled:
                    gate.notify(
                        "<b>⚠️ Brain QA couldn't auto-fix all issues — needs your call</b>\n\n"
                        + qa.to_html() +
                        "\n\n<i>Approving below will post anyway. Reject to skip.</i>"
                    )
                # Fall through — proceed to the regular Telegram approval gate
                # with the original clip text. Chris's /approve overrides QA
                # when the issues are deemed false-positive.
            else:
                # Revision succeeded — replace the clip's text fields.
                logger.info(f"[qa] auto-revised in {rev.attempts} attempts: {rev.notes}")
                clip.moment.caption_text = rev.caption
                clip.moment.hashtags = rev.hashtags
                clip.moment.hook_text = rev.hook_text
                qa = rev.final_qa or qa
            if gate and gate.enabled:
                gate.notify(
                    f"<b>✏️ Brain auto-revised this clip ({rev.attempts} attempts)</b>\n"
                    f"<i>{rev.notes}</i>"
                )
        if qa.severity == "warn" and gate and gate.enabled:
            gate.notify(qa.to_html())
    except Exception as e:
        logger.warning(f"[qa] check failed (proceeding without QA): {e}")

    # Telegram approval gate — only post if user hits /approve.
    if gate and gate.enabled:
        gate.send_clip_for_approval(
            video_path=clip.final_path,
            campaign_title=campaign.get("title", ""),
            campaign_payout=campaign.get("payout_per_1k_views"),
            hook_text=clip.moment.hook_text,
            caption_text=clip.moment.caption_text,
            hashtags=clip.moment.hashtags,
            platforms=platforms_required,
            structured_rules=structured,
        )
        verdict = gate.wait_for_verdict(token="", timeout_minutes=30)
        if verdict.status != ApprovalStatus.APPROVED:
            logger.info(f"[orchestrator] not approved ({verdict.status.value}); skipping")
            # Record the rejection so this exact moment doesn't come back in
            # future runs against the same source video.
            try:
                clip_id = _ensure_clip_in_db(repo, campaign, clip)
                repo.set_clip_field(
                    clip_id,
                    status="rejected",
                    error=f"telegram: {verdict.status.value} {verdict.note or ''}",
                )
                logger.info(f"[orchestrator] marked clip #{clip_id} as rejected ({clip.moment.start_sec:.1f}-{clip.moment.end_sec:.1f})")
            except Exception as e:
                logger.warning(f"[orchestrator] could not record rejection: {e}")
            return []

    # Append the campaign's Whop tracking code to the caption so every
    # platform's description carries it. This is what Whop uses to verify
    # the post is yours — required by most Clip Farm campaigns and
    # invisible to the actual viewer when buried at the end of the caption.
    caption_with_code = clip.moment.caption_text
    tracking_code = (campaign.get("tracking_code") or "").strip()
    if tracking_code and tracking_code not in caption_with_code:
        caption_with_code = caption_with_code.rstrip() + "\n\n" + tracking_code

    results = publisher.post_clip(
        video_path=clip.final_path,
        caption=caption_with_code,
        hashtags=clip.moment.hashtags,
        platforms=platforms_required,
        duration_sec=clip.moment.duration_sec,
        campaign_rules=campaign.get("rules"),
        platforms_required=platforms_required,
        min_duration_sec=campaign.get("min_duration_sec"),
        max_duration_sec=campaign.get("max_duration_sec"),
    )

    post_ids: list[int] = []
    for r in results:
        clip_id = _ensure_clip_in_db(repo, campaign, clip)
        post_id = repo.add_post(
            clip_id=clip_id,
            platform=r.platform,
            scheduled_for=r.scheduled_for,
            caption=clip.moment.caption_text,
            hashtags=clip.moment.hashtags,
        )
        repo.mark_post_posted(post_id, platform_post_id=r.platform_post_id, post_url=r.post_url)
        post_ids.append(post_id)
    return post_ids


def _ensure_clip_in_db(repo: Repository, campaign: dict, clip: ProducedClip) -> int:
    # The clips table's source_video_id is a NOT NULL FK to source_videos. Make
    # sure a row exists for this campaign+source before inserting the clip.
    # We use the campaign's current_source_path as the canonical key (or
    # raw_path as a fallback for one-off runs without a registered source).
    source_url = (campaign.get("current_source_path") or "").strip() or str(clip.raw_path)
    source_video_id = repo.add_source_video(
        campaign_id=campaign["id"],
        source_url=source_url,
        title=None,
    )
    # Classify the clip's content style so the Brain can learn from it.
    style_tag: Optional[dict] = None
    try:
        from engine.style_classifier import classify_clip
        style_tag = classify_clip(
            transcript_excerpt=clip.moment.transcript_excerpt or "",
            hook_text=clip.moment.hook_text,
        )
        if style_tag:
            logger.info(
                f"[style] tagged as {style_tag['style']} "
                f"(conf={style_tag.get('confidence', 0):.2f})"
            )
    except Exception as e:
        logger.warning(f"[style] classifier failed (continuing): {e}")

    # Carry the active experiment (if any) onto the clip row for later
    # outcome attribution.
    active_exp = campaign.get("_active_experiment") or {}
    exp_hyp = (active_exp.get("hypothesis") or None) if active_exp else None
    exp_params_json = (
        json.dumps(active_exp.get("system_params") or {})
        if active_exp else None
    )

    return repo.add_clip({
        "source_video_id": source_video_id,
        "campaign_id": campaign["id"],
        "start_sec": clip.moment.start_sec,
        "end_sec": clip.moment.end_sec,
        "duration_sec": clip.moment.duration_sec,
        "transcript_excerpt": clip.moment.transcript_excerpt,
        "ai_score": clip.moment.score,
        "ai_reason": clip.moment.reason,
        "hook_text": clip.moment.hook_text,
        "caption_text": clip.moment.caption_text,
        "suggested_hashtags": clip.moment.hashtags,
        "content_type": style_tag["style"] if style_tag else None,
        "content_type_reason": style_tag["reason"] if style_tag else None,
        "experiment_hypothesis": exp_hyp,
        "experiment_params": exp_params_json,
    })


def _check_rules_pre_flight(clip: ProducedClip, structured: Optional[dict]) -> list[str]:
    """Hard guardrails before we post anything. Returns a list of human-readable
    rule violations (empty if all good)."""
    if not structured:
        return []
    import re as _re
    issues: list[str] = []

    excerpt = clip.moment.transcript_excerpt or ""
    caption = clip.moment.caption_text or ""

    def _word_hit(phrase: str, text: str) -> bool:
        try:
            return bool(_re.search(r"\b" + _re.escape(phrase) + r"\b", text, _re.IGNORECASE))
        except _re.error:
            return phrase.lower() in text.lower()

    for phrase in structured.get("forbidden_phrases") or []:
        p = (phrase or "").strip()
        if not p:
            continue
        if _word_hit(p, excerpt) or _word_hit(p, caption):
            issues.append(f"Forbidden phrase '{phrase}' present in clip transcript or caption")

    for tag in structured.get("required_hashtags") or []:
        if tag and tag.lstrip("#").lower() not in caption.replace("#", "").lower():
            issues.append(f"Caption missing required hashtag {tag}")

    for mention in structured.get("required_mentions") or []:
        if mention and mention.lstrip("@").lower() not in caption.replace("@", "").lower():
            issues.append(f"Caption missing required mention {mention}")

    return issues


def _platforms_from_campaign(campaign: dict) -> list[str]:
    raw = campaign.get("platforms_required")
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        import json
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------
def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    parser.add_argument("--campaign", type=int, help="Campaign DB id (default: pick top viable)")
    parser.add_argument("--source", required=True, help="Source video URL to clip from")
    parser.add_argument("--n-clips", type=int, default=None)
    parser.add_argument("--dry-run", action="store_true", help="Produce clips but skip publish")
    parser.add_argument(
        "--auto-submit",
        action="store_true",
        help="After a clip posts successfully, automatically submit it to Whop "
        "via WhopSubmitter (falls back to the manual Telegram summary on failure).",
    )
    parser.add_argument(
        "--sub-campaign",
        type=str,
        default=None,
        help="Sub-campaign title substring (used with --auto-submit).",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=8,
        help="Max produce+approve cycles for this campaign. On rejection we generate a fresh moment "
        "(dedup excludes the rejected one) and retry. Stops on approval or after N misses.",
    )
    parser.add_argument(
        "--format-mode",
        choices=["smart", "crop", "blur_pad", "letterbox"],
        default="smart",
        help="9:16 layout strategy. 'smart' = vision crop on speaker (best for "
        "talking-head / podcasts). 'crop' = centre-fill, no padding (best for "
        "food / demo / action where the action is in the middle of frame). "
        "'blur_pad' = original 16:9 centred with blurred background fill. "
        "'letterbox' = original 16:9 centred with black bars.",
    )
    args = parser.parse_args(argv)

    logger.add(settings.logs_dir / "orchestrator.log", rotation="20 MB", retention=5)
    repo = Repository()

    if args.campaign:
        with repo.conn() as c:
            row = c.execute("SELECT * FROM campaigns WHERE id = ?", (args.campaign,)).fetchone()
        if not row:
            logger.error(f"campaign {args.campaign} not found")
            return 2
        campaign = dict(row)
    else:
        campaign = pick_top_viable_campaign(repo)
        if not campaign:
            logger.error("no viable campaign found in DB")
            return 2

    logger.info(
        f"[orchestrator] target: #{campaign['id']} {campaign['title']} "
        f"(CPM={campaign.get('payout_per_1k_views')}, "
        f"budget left={campaign.get('budget_remaining_pct')}%)"
    )

    if args.dry_run:
        clips = produce_clips_for_campaign(
            campaign, args.source, n_clips=args.n_clips, format_mode=args.format_mode,
        )
        logger.info(f"[orchestrator] dry-run produced {len(clips)} clip(s)")
        for c in clips:
            logger.info(f"  - {c.final_path} (score={c.moment.score})")
        logger.info("[orchestrator] dry-run: skipping publish/submit")
        return 0

    publisher = MultiPlatformPublisher()
    gate = TelegramGate()

    # Persist-until-approval: stick with this ONE campaign, generating a fresh
    # clip on each rejection (dedup excludes previously-rejected moments), until
    # the user approves one OR we hit max_attempts. This way Chris gets the
    # campaign to completion before moving on to the next one.
    max_attempts = args.max_attempts
    approved_post_ids: list[int] = []
    approved_clip = None
    for attempt in range(1, max_attempts + 1):
        logger.info(f"[orchestrator] attempt {attempt}/{max_attempts} for #{campaign['id']}")
        clips = produce_clips_for_campaign(
            campaign, args.source,
            n_clips=args.n_clips or 1,
            format_mode=args.format_mode,
        )
        if not clips:
            logger.warning(
                f"[orchestrator] no more eligible moments for #{campaign['id']} "
                f"(dedup may have exhausted the source); stopping after attempt {attempt - 1}"
            )
            break
        c = clips[0]
        logger.info(f"  - {c.final_path} (score={c.moment.score})")
        try:
            post_ids = publish_clip_to_all_platforms(repo, publisher, campaign, c, gate=gate)
            logger.info(f"[orchestrator] published clip → posts {post_ids}")
            if gate and gate.enabled and post_ids:
                # Build a copy-paste-ready summary so Chris can submit to Whop
                # without retyping anything.
                with repo.conn() as conn:
                    rows = conn.execute(
                        "SELECT platform, post_url FROM posts WHERE id IN (" +
                        ",".join("?" * len(post_ids)) + ")",
                        post_ids,
                    ).fetchall()
                urls_by_platform = {r["platform"]: r["post_url"] for r in rows if r["post_url"]}

                title = c.moment.caption_text.split("\n", 1)[0][:120]
                full_caption = c.moment.caption_text.rstrip()
                if c.moment.hashtags:
                    full_caption += "\n\n" + " ".join("#" + h.lstrip("#") for h in c.moment.hashtags)

                pieces = [
                    "<b>✅ Clip posted — ready to submit to Whop</b>",
                    "",
                    f"<b>Campaign:</b> {campaign.get('title')}",
                    f"<b>Whop submit URL:</b> {campaign.get('submission_url', '')}",
                    "",
                    "<b>Title (copy this):</b>",
                    f"<code>{title}</code>",
                    "",
                    "<b>Live URLs (paste one as Video Link):</b>",
                ]
                for plat, url in urls_by_platform.items():
                    if url:
                        pieces.append(f"  {plat}: <code>{url}</code>")
                pieces += [
                    "",
                    "<b>Demographics Image:</b>",
                    "  data/screenshots/placeholder-demographics.png",
                    "",
                    "<b>Full caption (already on the post):</b>",
                    f"<code>{full_caption}</code>",
                ]
                gate.notify("\n".join(pieces))

                # Optional: auto-submit to Whop in the same run.
                if args.auto_submit:
                    _try_auto_submit(
                        repo,
                        campaign,
                        post_ids,
                        sub_campaign_title=args.sub_campaign,
                        gate=gate,
                    )
                approved_post_ids = post_ids
                approved_clip = c
                break  # campaign complete — stop retrying
            else:
                # Telegram rejected (or timed out). The orchestrator already
                # wrote the clip to DB as 'rejected'; dedup will exclude it on
                # the next attempt's scoring pass. Loop and produce a fresh moment.
                logger.info(
                    f"[orchestrator] clip rejected — generating fresh moment for #{campaign['id']} "
                    f"(attempt {attempt}/{max_attempts})"
                )
        except Exception as e:
            logger.exception(f"[orchestrator] publish failed: {e}")
            # Don't burn an attempt on infrastructure errors — try again.
    if not approved_post_ids:
        logger.warning(
            f"[orchestrator] no approval after {max_attempts} attempt(s) for #{campaign['id']}; "
            f"moving on. Run again later to keep trying."
        )
        if gate and gate.enabled:
            gate.notify(
                f"<b>⚠️ Couldn't get an approval for {campaign.get('title')} after "
                f"{max_attempts} attempts.</b> Try again later or refresh the source."
            )
    return 0


def _try_auto_submit(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    sub_campaign_title: Optional[str],
    gate: Optional[TelegramGate],
) -> None:
    """Route submission based on campaign marketplace.

    - 'whop' → WhopSubmitter (Playwright web form fill, screenshot upload, etc.)
    - 'clipify' → Telegram-paste the slash command for the user to run in Discord
    - Future: 'clipstake', 'vyro', 'clipaffiliates' etc.

    Failures never abort the orchestrator — the manual Telegram summary already
    fired in the previous step, so the user can submit by hand if anything misses.
    """
    marketplace = (campaign.get("marketplace") or "whop").lower()
    logger.info(f"[auto-submit] routing for marketplace={marketplace}")
    if marketplace == "clipify":
        _submit_via_clipify(repo, campaign, post_ids, gate)
        return
    if marketplace == "vyro":
        _submit_via_vyro(repo, campaign, post_ids, gate)
        return
    if marketplace == "clipstake":
        _submit_via_clipstake(repo, campaign, post_ids, gate)
        return
    if marketplace == "clipaffiliates":
        _submit_via_clipaffiliates(repo, campaign, post_ids, gate)
        return
    if marketplace != "whop":
        logger.warning(f"[auto-submit] no handler for marketplace={marketplace!r}; skipping")
        if gate and gate.enabled:
            gate.notify(
                f"<b>⚠️ No auto-submit handler for marketplace <code>{marketplace}</code> yet.</b>\n"
                f"Submit manually for now."
            )
        return

    # Marketplace == whop → existing flow
    try:
        from scanner.whop_login import WhopSession
        from scanner.whop_submitter import WhopSubmitter, SubmissionInputs
    except Exception as e:
        logger.warning(f"[auto-submit] cannot import submitter: {e}")
        return

    # Whop pays per-platform per-clip, so each platform URL needs its OWN
    # submission. YouTube + Instagram = TWO separate submit-form fills.
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url, caption, platform_post_id "
            "FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    targets = [dict(r) for r in rows]
    if not targets:
        logger.warning("[auto-submit] no post URLs yet; skipping")
        return
    # YouTube first (usually highest CPM and most stable analytics).
    targets.sort(key=lambda r: 0 if r["platform"] == "youtube" else 1)

    # Capture a real YouTube Studio analytics screenshot for the YouTube post,
    # and use it as the demographics image on EVERY platform's submission.
    # Per Chris's rule: no submission goes through without a real screenshot —
    # the placeholder PNG is intentionally only a last-resort fallback.
    demographics_path = _capture_demographics_for_submission(targets, gate)
    if demographics_path is None:
        logger.warning(
            "[auto-submit] no demographics screenshot available — aborting submissions for this clip"
        )
        if gate and gate.enabled:
            urls = "\n".join(f"  {t['platform']}: <code>{t['post_url']}</code>" for t in targets)
            gate.notify(
                "<b>⚠️ Auto-submit paused — couldn't get a YT Studio analytics screenshot.</b>\n\n"
                "Either log into YT Studio (run the bot tool <i>'capture analytics for post N'</i> once "
                "to do the initial sign-in), or submit these manually:\n\n" + urls
            )
        return

    # Re-use one Whop session for all submissions to avoid repeated logins.
    try:
        with WhopSession(headless=True) as session:
            submitter = WhopSubmitter(session.page, debug=True)
            for target in targets:
                _submit_one_post(
                    repo, campaign, target, submitter,
                    sub_campaign_title=sub_campaign_title,
                    demographics_path=demographics_path,
                    gate=gate,
                )
    except Exception as e:
        logger.exception(f"[auto-submit] session crashed: {e}")
        if gate and gate.enabled:
            gate.notify(
                f"<b>⚠️ Whop auto-submit session crashed</b>\n<code>{e}</code>\n"
                f"Submit the remaining posts manually."
            )


def _capture_demographics_for_submission(
    targets: list[dict],
    gate: Optional["TelegramGate"],
) -> Optional[Path]:
    """Open YT Studio for the YouTube post and screenshot its analytics.

    Returns the PNG path on success. Returns None if the capture fails — the
    caller should NOT submit without a real screenshot (per Chris's rule).
    """
    yt_target = next((t for t in targets if t.get("platform") == "youtube"), None)
    if not yt_target:
        logger.warning("[auto-submit] no YouTube post in this batch — can't capture YT Studio analytics")
        return None
    video_id = yt_target.get("platform_post_id")
    if not video_id:
        logger.warning(f"[auto-submit] YouTube post #{yt_target['id']} missing platform_post_id")
        return None

    try:
        from scanner.youtube_studio import YouTubeStudioCapture
    except Exception as e:
        logger.warning(f"[auto-submit] cannot import YouTubeStudioCapture: {e}")
        return None

    try:
        with YouTubeStudioCapture() as cap:
            png = cap.screenshot_audience(video_id)
    except Exception as e:
        logger.exception(f"[auto-submit] YT Studio capture crashed: {e}")
        return None
    if not png or not Path(png).exists():
        return None
    logger.info(f"[auto-submit] using YT Studio screenshot for demographics: {png}")
    return Path(png)


def _submit_via_clipify(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    gate: Optional[TelegramGate],
) -> None:
    """Fire `/clips add` directly in the streamer's Discord server using
    the burner DiscordSession. Falls back to a copy-paste Telegram message
    only if the burner isn't configured.

    Clipify's `/clips add` takes one platform per call, so we group URLs
    by platform and fire one command per group."""
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    if not rows:
        logger.warning("[clipify] no post URLs to submit")
        return

    from scanner.clipify_submitter import PLATFORM_ALIAS

    by_platform: dict[str, list[str]] = {}
    by_platform_postids: dict[str, list[int]] = {}
    for r in rows:
        plat = PLATFORM_ALIAS.get((r["platform"] or "").lower(), r["platform"])
        by_platform.setdefault(plat, []).append(r["post_url"])
        by_platform_postids.setdefault(plat, []).append(r["id"])

    server = (campaign.get("marketplace_server") or "").strip() or campaign.get("title", "")
    channel = "commands"  # Clipify's standard submission channel

    # Burner Discord configured? → fire directly.
    from config import settings as _settings
    burner_ready = bool(_settings.discord_burner_email and _settings.discord_burner_password)

    if not burner_ready:
        logger.warning("[clipify] burner Discord creds missing — falling back to paste message")
        _submit_via_clipify_paste(repo, campaign, by_platform, by_platform_postids, rows, gate, server)
        return

    from scanner.clipify_submitter import ClipifySubmitter

    results: dict[str, dict] = {}
    try:
        with ClipifySubmitter() as sub:
            results = sub.submit_posts(
                server_name=server,
                channel_name=channel,
                urls_by_platform=by_platform,
            )
    except Exception as e:
        logger.error(f"[clipify] burner submission failed ({e}); falling back to paste")
        if gate and gate.enabled:
            gate.notify(
                f"<b>⚠️ Clipify auto-submit failed for <code>{_esc_html(server)}</code>:</b> "
                f"<code>{_esc_html(str(e))}</code>\nFalling back to paste."
            )
        _submit_via_clipify_paste(repo, campaign, by_platform, by_platform_postids, rows, gate, server)
        return

    # Record submissions for any platform group that fired (even if Clipify's
    # ack was missed — the command was sent).
    succeeded_platforms = [p for p, res in results.items() if res.get("ok") or res.get("error") is None]
    for plat in succeeded_platforms:
        for pid in by_platform_postids.get(plat, []):
            try:
                row = next(r for r in rows if r["id"] == pid)
                repo.add_submission(
                    post_id=pid, campaign_id=campaign["id"],
                    submitted_url=row["post_url"],
                )
            except Exception as e:
                logger.warning(f"[clipify] couldn't record submission for post #{pid}: {e}")

    if gate and gate.enabled:
        lines = [f"<b>✅ Clipify auto-submitted to <code>{_esc_html(server)}</code></b>", ""]
        for plat, res in results.items():
            count = len(by_platform.get(plat, []))
            reply = (res.get("reply") or "").strip().splitlines()[0:1]
            tag = "✅" if res.get("ok") else "❓"
            line = f"{tag} <b>{plat}</b> · {count} URL{'s' if count != 1 else ''}"
            if reply:
                line += f" — <i>{_esc_html(reply[0][:140])}</i>"
            lines.append(line)
        gate.notify("\n".join(lines))
    logger.info(f"[clipify] fired {len(results)} platform group(s) at {server}")


def _submit_via_clipstake(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    gate: Optional[TelegramGate],
) -> None:
    """Fire ClipStake submissions for each post URL via Playwright."""
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    if not rows:
        return
    try:
        from scanner.clipstake_submitter import ClipStakeSubmitter
    except Exception as e:
        logger.warning(f"[clipstake-submit] cannot import: {e}")
        return
    target_identifier = campaign.get("submission_url") or campaign.get("title") or ""
    results = []
    try:
        with ClipStakeSubmitter() as sub:
            for r in rows:
                res = sub.submit_url_for_campaign(target_identifier, r["post_url"])
                results.append((r, res))
                if res.get("ok"):
                    try:
                        repo.add_submission(post_id=r["id"], campaign_id=campaign["id"],
                                            submitted_url=r["post_url"])
                    except Exception:
                        pass
    except Exception as e:
        logger.exception(f"[clipstake-submit] failed: {e}")
        if gate and gate.enabled:
            gate.notify(f"<b>⚠️ ClipStake submitter crashed:</b> {e}\nManual submit needed.")
        return
    if gate and gate.enabled:
        lines = [f"<b>📤 ClipStake submissions — #{campaign['id']}</b>", ""]
        for r, res in results:
            tag = "✅" if res.get("ok") else "❌"
            lines.append(f"{tag} {r['platform']} — {r['post_url'][:60]}")
            if not res.get("ok"):
                lines.append(f"   <i>{res.get('error', '?')}</i>")
        gate.notify("\n".join(lines))


def _submit_via_clipaffiliates(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    gate: Optional[TelegramGate],
) -> None:
    """Fire ClipAffiliates submissions for each post URL via Playwright."""
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    if not rows:
        return
    try:
        from scanner.clipaffiliates_submitter import ClipAffiliatesSubmitter
    except Exception as e:
        logger.warning(f"[clipaffiliates-submit] cannot import: {e}")
        return
    title = campaign.get("title") or ""
    title_substr = title.split("—")[0].split("-")[0].strip()[:30] or title
    results = []
    try:
        with ClipAffiliatesSubmitter() as sub:
            for r in rows:
                res = sub.submit_url_for_campaign(title_substr, r["post_url"])
                results.append((r, res))
                if res.get("ok"):
                    try:
                        repo.add_submission(post_id=r["id"], campaign_id=campaign["id"],
                                            submitted_url=r["post_url"])
                    except Exception:
                        pass
    except Exception as e:
        logger.exception(f"[clipaffiliates-submit] failed: {e}")
        if gate and gate.enabled:
            gate.notify(f"<b>⚠️ ClipAffiliates submitter crashed:</b> {e}\nManual submit needed.")
        return
    if gate and gate.enabled:
        lines = [f"<b>📤 ClipAffiliates submissions — #{campaign['id']}</b>", ""]
        for r, res in results:
            tag = "✅" if res.get("ok") else "❌"
            lines.append(f"{tag} {r['platform']} — {r['post_url'][:60]}")
            if not res.get("ok"):
                lines.append(f"   <i>{res.get('error', '?')}</i>")
        gate.notify("\n".join(lines))


def _submit_via_vyro(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    gate: Optional[TelegramGate],
) -> None:
    """Fire one Vyro submission per post URL via VyroSubmitter.

    Routes per platform: a single clip posted to YT + IG submits the YT
    URL to the TT/YT-platform campaign and the IG URL to the IG-platform
    campaign. We discover sibling vyro campaigns sharing the same source
    file."""
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    if not rows:
        return

    # Discover sibling Vyro campaigns that share the same source video.
    siblings = _find_vyro_siblings(repo, campaign)
    # Include self in the lookup table.
    all_camps = [campaign] + siblings

    try:
        from scanner.vyro_submitter import VyroSubmitter
    except Exception as e:
        logger.warning(f"[vyro-submit] cannot import submitter: {e}")
        if gate and gate.enabled:
            gate.notify(f"<b>⚠️ Vyro submitter unavailable. Manual submit needed for #{campaign['id']}.</b>")
        return

    results = []
    try:
        with VyroSubmitter() as sub:
            for r in rows:
                # Pick the campaign whose platforms_required includes this platform.
                target_camp = _pick_target_camp_for_platform(all_camps, r["platform"]) or campaign
                title_substr = _title_substr_for_vyro(target_camp.get("title") or "")
                res = sub.submit_url_for_campaign(title_substr, r["post_url"])
                results.append((r, target_camp, res))
                if res.get("ok"):
                    try:
                        repo.add_submission(
                            post_id=r["id"], campaign_id=target_camp["id"],
                            submitted_url=r["post_url"],
                        )
                    except Exception as e:
                        logger.warning(f"[vyro-submit] couldn't record submission for post #{r['id']}: {e}")
    except Exception as e:
        logger.exception(f"[vyro-submit] session-level failure: {e}")
        if gate and gate.enabled:
            gate.notify(f"<b>⚠️ Vyro submitter crashed: {e}</b>\nManual submit needed.")
        return

    if gate and gate.enabled:
        lines = [f"<b>📤 Vyro submissions</b>", ""]
        for r, tc, res in results:
            tag = "✅" if res.get("ok") else "❌"
            lines.append(f"{tag} {r['platform']} → #{tc['id']} {tc.get('title', '')[:30]}")
            lines.append(f"   {r['post_url'][:60]}")
            if not res.get("ok"):
                lines.append(f"   <i>{res.get('error', '?')}</i>")
        gate.notify("\n".join(lines))


def _find_vyro_siblings(repo: Repository, campaign: dict) -> list[dict]:
    """Other Vyro campaigns sharing this campaign's source file."""
    src = (campaign.get("current_source_path") or "").strip()
    if not src:
        return []
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE marketplace='vyro' "
            "AND current_source_path = ? AND id != ?",
            (src, campaign["id"]),
        ).fetchall()
    return [dict(r) for r in rows]


def _pick_target_camp_for_platform(camps: list[dict], platform: str) -> Optional[dict]:
    """Of the given Vyro campaigns, return the one whose platforms_required
    includes `platform`. None if no match."""
    pl = (platform or "").lower()
    for camp in camps:
        rules = _parse_json_obj(camp.get("structured_rules")) or {}
        req = [str(p).lower() for p in (rules.get("platforms_required") or [])]
        if pl in req:
            return camp
        # Fallback: check the top-level platforms_required column (JSON)
        top = _parse_json_list(camp.get("platforms_required")) or []
        if pl in [str(p).lower() for p in top]:
            return camp
    return None


def _title_substr_for_vyro(title: str) -> str:
    """Use the parenthetical (TT/YT) or (IG) tag if present — Vyro's
    cards differ only by that tag."""
    import re as _re
    m = _re.search(r"\([^)]+\)", title or "")
    if m:
        return m.group(0)
    # Fallback to the first word.
    return (title or "").split()[0] if title else ""


def _record_passive_submissions(
    repo: Repository,
    campaign: dict,
    post_ids: list[int],
    marketplace: str,
    gate: Optional[TelegramGate],
) -> None:
    """For marketplaces that track via connected socials (Vyro, ClipStake,
    ClipAffiliates), the *post itself* IS the submission. We just record
    the submission row + Telegram-confirm."""
    with repo.conn() as conn:
        rows = conn.execute(
            "SELECT id, platform, post_url FROM posts WHERE id IN ("
            + ",".join("?" * len(post_ids)) + ") AND post_url IS NOT NULL",
            post_ids,
        ).fetchall()
    if not rows:
        return
    for r in rows:
        try:
            repo.add_submission(
                post_id=r["id"], campaign_id=campaign["id"],
                submitted_url=r["post_url"],
            )
        except Exception as e:
            logger.warning(f"[passive-submit] couldn't record submission for post #{r['id']}: {e}")
    if gate and gate.enabled:
        lines = [
            f"<b>✅ {marketplace.capitalize()} auto-tracked — "
            f"{len(rows)} post URL{'s' if len(rows) != 1 else ''}</b>",
            f"<i>{marketplace.capitalize()} tracks via your connected socials, "
            "so the post itself is the submission. Views/earnings will appear "
            "in your dashboard within ~24h.</i>",
        ]
        gate.notify("\n".join(lines))
    logger.info(f"[passive-submit] recorded {len(rows)} {marketplace} submission(s)")


def _submit_via_clipify_paste(
    repo: Repository,
    campaign: dict,
    by_platform: dict[str, list[str]],
    by_platform_postids: dict[str, list[int]],
    rows,
    gate: Optional[TelegramGate],
    server: str,
) -> None:
    """Fallback: Telegram-send copy-paste-ready commands."""
    commands = []
    for plat, urls in by_platform.items():
        urls_str = " ".join(urls)
        commands.append(f"/clips add platform:{plat} urls:{urls_str}")

    for plat, ids in by_platform_postids.items():
        for pid in ids:
            try:
                row = next(r for r in rows if r["id"] == pid)
                repo.add_submission(
                    post_id=pid, campaign_id=campaign["id"],
                    submitted_url=row["post_url"],
                )
            except Exception as e:
                logger.warning(f"[clipify] couldn't record submission for post #{pid}: {e}")

    msg_lines = [
        f"<b>📋 Paste in <code>{_esc_html(server)}</code> #commands:</b>",
        "",
    ]
    for cmd in commands:
        msg_lines.append(f"<code>{_esc_html(cmd)}</code>")
    if gate and gate.enabled:
        gate.notify("\n".join(msg_lines))
    logger.info(f"[clipify] paste fallback: emitted {len(commands)} command(s) for {server}")


def _esc_html(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _submit_one_post(
    repo: Repository,
    campaign: dict,
    target: dict,
    submitter,
    *,
    sub_campaign_title: Optional[str],
    demographics_path: Optional[Path],
    gate: Optional[TelegramGate],
) -> None:
    from scanner.whop_submitter import SubmissionInputs

    caption = (target.get("caption") or "").split("\n", 1)[0].strip()
    title = caption[:120] or "Submission"
    platform_label = (target.get("platform") or "?").lower()
    inputs = SubmissionInputs(
        title=title,
        video_url=target["post_url"],
        demographics_image=demographics_path,
    )

    try:
        result = submitter.submit(
            program_title=campaign["title"],
            sub_campaign_title=sub_campaign_title,
            inputs=inputs,
            community_id=campaign.get("community_id"),
        )
    except Exception as e:
        logger.exception(f"[auto-submit] {platform_label} crashed: {e}")
        if gate and gate.enabled:
            gate.notify(
                f"<b>⚠️ Whop auto-submit ({platform_label}) crashed</b>\n"
                f"<code>{e}</code>\nSubmit manually: {target['post_url']}"
            )
        return

    if result.ok:
        repo.add_submission(
            post_id=target["id"], campaign_id=campaign["id"],
            submitted_url=target["post_url"],
        )
        logger.info(f"[auto-submit] ✅ submitted post #{target['id']} ({platform_label})")
        if gate and gate.enabled:
            gate.notify(
                f"<b>✅ Whop auto-submitted</b> {platform_label.upper()} "
                f"→ {campaign['title']}\n<code>{target['post_url']}</code>"
            )
        return

    logger.warning(f"[auto-submit] ❌ {platform_label}: {result.message}")
    if gate and gate.enabled:
        msg_lower = (result.message or "").lower()
        if "platform verification" in msg_lower or "verify" in msg_lower:
            gate.notify(
                f"<b>🔐 Whop needs platform verification ({platform_label})</b>\n\n"
                f"{result.message}\n\n"
                "Paste the code in the platform bio / description, save, then "
                "trigger a clip again."
            )
        else:
            gate.notify(
                f"<b>⚠️ Whop auto-submit ({platform_label}) failed:</b> {result.message}\n"
                f"Manual submission URL: <code>{target['post_url']}</code>"
            )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
