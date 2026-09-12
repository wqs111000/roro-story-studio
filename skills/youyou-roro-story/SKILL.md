---
name: youyou-roro-story
description: Create, review, revise, and package age-appropriate original picture-book stories for four-year-old Youyou and Roro. Use for daily-life stories, animal or dinosaur adventures, bedtime stories, storyboards, and approved Roro content exports.
---

# Youyou Roro Story

Create a reviewable family story package in the current Roro story workspace. Treat Codex as the authoring studio, not as a runtime API for the robot.

## Read the relevant context

Always read `profile/youyou.md`, `profile/roro.md`, `profile/family-rules.md`, and `universe/story-bible.md`.

For a new story, also read [references/age-4-content.md](references/age-4-content.md) and [references/story-patterns.md](references/story-patterns.md). For illustration prompts, also read `universe/visual-bible.md`. For approval or export, read `templates/review-checklist.md`.

## Choose the source mode

Use the source mode requested by the parent. If it is not specified, choose from the rotation in `universe/themes.md` without claiming that an invented event happened to Youyou.

- `daily_life`: Transform a supplied real event into a gentle fictional adventure.
- `original_adventure`: Use original animals, dinosaurs, forests, weather, vehicles, or imaginary places.
- `classic_motif`: Borrow only a broad public-domain or genre-level narrative mechanism, then create new characters, wording, sequence, setting, and images.

Never imitate or continue a living author's book, reproduce recognizable protected characters, or closely paraphrase a specific published story. A currently popular work may inform only high-level observations such as repetition, participation, pacing, or emotional reassurance.

## Create the draft

1. Read the newest complete file in `inbox/`, or use the parent's current prompt.
2. Do not invent unknown personal facts, preferences, diagnoses, or real-life events about Youyou.
3. Draft against `templates/story.schema.json`; every story must have exactly 6 numbered pages (1-6), with one main action per page. The cover is separate at page 0 and does not count toward the six story pages.
4. Use short spoken Chinese, gentle repetition, concrete actions, and at most two simple interactions.
5. Keep visual-production facts out of child-facing content. Character form names, atlas versions, scale anchors, prompt language, model controls, and production status belong in storyboards and review notes, not in the synopsis, narration, dialogue, or final audio. Mention a transformation only when the transformation itself is an explicitly intended plot event.
6. Make Roro a companion who notices, asks, models, or helps. Let Youyou or the guest character make the meaningful choice.
7. Keep any learning goal inside the plot. Do not add quizzes, scores, or forced literacy practice unless the parent requests them.
8. Save `story.json`, `story.md`, `storyboard.json`, and `review.md` under `drafts/YYYY-MM-DD-story-slug/`. Extend the existing storyboard: `visual_production` holds shared style, character references, costume, story invariants, scene anchors and scale; each `shots[]` entry records its scene, characters, current state and plot-allowed changes. Keep the existing composition, action, mood, prompt and safety constraints together. Use the extended `templates/storyboard.schema.json`; do not maintain a second independently edited plan.
9. Continue through the complete draft package and Codex internal review without asking the parent to approve intermediate text, storyboard, sample-page, illustration, or audio gates. Keep all work in `drafts/` until the complete candidate reaches the workbench.

### Production binding before image generation

For every new book, select each recurring character's **form and outfit together** from `service/data/characters.json`; read [references/character-outfits.md](references/character-outfits.md) before binding characters. Respect the user's explicit choice, otherwise choose an approved combination suited to the setting, weather, activity and emotional tone. Use `scripts/character_selection.py` to resolve the exact reference, then record `character_id`, `form_id`, `outfit_id`, `selection_reason`, the returned invariants/costume and the actual reference SHA-256 in `visual_production.characters`. Inspect the selected sheet before setting `reference.reviewed: true`. Attach that outfit's sheet when generating, not a generic default portrait. Do not treat a change of clothing as a different species/form. These fields are for production only; do not read them aloud or insert them into children's prose.

For every new book, select each recurring character's **form and outfit together** from `service/data/characters.json`; read [references/character-outfits.md](references/character-outfits.md) before binding characters. Respect the user's explicit choice, otherwise choose an approved combination suited to the setting, weather, activity and emotional tone. Use `scripts/character_selection.py` to resolve the exact reference, then record `character_id`, `form_id`, `outfit_id`, `selection_reason`, the returned invariants/costume and the actual reference SHA-256 in `visual_production.characters`. Inspect the selected sheet before setting `reference.reviewed: true`. Attach that outfit's sheet when generating, not a generic default portrait. Do not treat a change of clothing as a different species/form. These fields are for production only; do not read them aloud or insert them into children's prose.

Before compiling any page, make `storyboard.json` structurally complete. Every `shots[].characters[]` key must have a matching `visual_production.characters` entry with identity invariants, costume/state constraints, and a reviewed reference. This includes newly introduced animals and recurring guest characters: generate and visually inspect a character sheet first, then bind its real path and SHA-256. If a parent explicitly authorizes reuse of an existing approved character, reuse that character's registry identity and exact approved reference rather than creating a generic duplicate. Do not use a placeholder digest, `reviewed: false`, or an unbound character name to unblock generation.

The storyboard must include a `cover` object with `page: 0` and a valid cover scene. Add a cover scene anchor or make its `anchor_page` 0 so cover compilation does not depend on an unconfirmed page. Every repeated location must have a real scene anchor before its second appearance; the first accepted page establishes that anchor. Compile and generate the representative anchor page first, inspect it and bind its actual reference, then compile page 0 and remaining pages as their dependencies become available. Visual-review image paths are repository-root-relative (`drafts/<story>/images/...`) because `validate_review` runs from the workspace root; input digests remain those emitted by `visual_plan.py`.

## Illustrations and audio

Apply the improvements below within the existing draft → storyboard → illustration → narration → internal review → workbench flow. Keep `storyboard.json` as the visual authoring source; do not create a parallel visual plan or require a separate user workflow. These improvements apply to future books. Do not automatically update, migrate, re-review, or republish existing books or candidates; revise an existing book only when the user explicitly identifies it.

Generate illustrations only when requested for that run. Reuse approved character references when available and repeat invariant appearance constraints in every prompt. Keep story text outside generated artwork except for very short, reviewed labels.

Before generating any scene, read `universe/character-registry.md` and use the newest approved atlas image for every recurring character. Use Roro's approved Warm Sun Form by default. Select another approved Roro form only when the story's setting or emotional tone supports it, record the chosen form and reason in the visual review, and keep that form consistent across the story unless a form change is an explicit approved plot event.

During the existing storyboard step, resolve contradictory costume, prop and action descriptions before generation. Compile page instructions from `storyboard.json` with `scripts/visual_plan.py --page N`; its output is a derived production aid, not another authoring document. Synchronize the compiled prompt back to that shot's `prompt`. Actually attach the listed reference images to ImageGen: a path in text is not an attachment. Preserve page-specific mood and safety constraints, and do not invent unavailable seed or reference-weight controls.

Compile the representative page before generating it; after inspecting and binding its real anchor, compile page 0 and the remaining actual page range before batch-generating those dependent pages. Establish other new-location anchors in the same dependency order. Stop on missing character bindings, missing reviewed references, null required repeated-location anchors, stale SHA-256 values, or a missing cover object; fix the storyboard or create the required character sheet first. Do not require a not-yet-generated anchor to exist before its own first page, and do not bypass the compiler by hand-writing prompts for blocked pages.

Treat canonical heights as starting references, not a rigid cross-story pixel ratio. At visual Gate C, choose one accepted representative page as the story-level scale anchor and record the relative size of every recurring character in that story. On later pages, preserve those relative sizes whenever characters have comparable posture and camera depth; allow only explainable perspective, posture, age/form, or plot effects. Different stories may adopt moderately different relative scale for composition or stylization, so do not force a page to match another story's character ratio and do not use another story as a hard scale rejection gate.

For every location that appears on multiple pages, establish a scene anchor from the first accepted page: spatial layout, path or room direction, landmark positions, doors/windows/furniture, persistent props, camera axis, and baseline lighting. Reuse that anchor as an image reference on every later page in the same location. Change only character actions, framing, and plot-explicit state changes. If time, weather, lighting, damage, or object placement changes in the story, list the before/after state in the visual review; if the story moves to a new location, establish a new anchor.

Within Gate C, produce and inspect a representative page first, then use that accepted scene and scale reference for later pages. Prefer one main action and a readable medium shot; simplify complex holding or crossed limbs before repeated regeneration. Plan reverse camera angles explicitly. Never rely solely on an unreviewed chain of previous images. A scale reference from a different location provides proportions and style only, not its background. Scene changes remain normal whenever the plot changes location; in the same location preserve only elements not changed by the story.

Create every master page illustration as full-bleed scene art. Explicitly require the scene to extend naturally to all four edges and prohibit text, borders, banners, caption panels, pale bottom strips, blank text areas, and layout placeholders. Do not use phrases such as “底部留白”, “正文区”, “排字区”, or a percentage reserved for text. Text belongs to the player/PDF layout layer; if a later print layout needs text on the page, derive it from the untouched full-bleed master.

Before presenting illustrations for parent review, inspect a whole-book contact sheet, then inspect only flagged pages at full size; reuse prior observations for unchanged images. Compare repeated locations side by side against their scene anchor, and compare recurring characters against that story's scale anchor. Reject unexplained within-story size drift at comparable depth or posture, but do not reject a page solely because another story uses a different scale relationship. Also reject any page with a uniform pale strip, abruptly fading ground texture, unused caption zone, malformed anatomy, wrong character identity, unexplained relocation of a landmark or fixed prop, or another continuity break. File existence and matching dimensions are not visual acceptance.

Converge the book with a bounded visual loop:

1. Before generation, freeze the story invariants: title, main theme, cause and effect, emotional turn, required character choice, safety boundary, and cross-page setup/payoff. These cannot be changed merely to excuse an image.
2. Generate one initial image per page after establishing its necessary references. Review the whole book once as a contact sheet, then open only flagged pages at full size. Reuse the recorded verdict and hashes; do not spend another image-understanding pass on unchanged files.
3. Classify each page as `accept`, `text-adapt`, or `repair`. Use `text-adapt` when the image is visually sound and the mismatch is limited to a nonessential action, prop, count, expression, or environmental detail. Update `story.json`, `story.md`, `narration.json`, `storyboard.json`, and `review.md` together so child-facing text, audible narration, and production notes stay aligned. Regenerate audio only for affected pages.
4. Use `repair` for wrong identity or form, malformed anatomy, unsafe action, random text, bottom caption bands, missing plot-critical action or object, or broken story-level scale/scene continuity. Text must never be distorted to hide these failures.
5. Default to one targeted repair after the initial generation. Allow a second repair only when the first clearly improved one named blocking defect and the remaining correction is deterministic. Otherwise re-plan the composition or stop the page instead of making repeated near-duplicate generations.

Record the existing visual review as structured supporting evidence in `visual-review.json`: cover page 0 and exactly six story pages, image hashes, compiled input digests, verdict, reason, reviewer and attempt count. A changed reference or input requires checking the affected relationship before renewing the record; do not blindly copy acceptance or regenerate unaffected art. A third attempt needs a concrete second-repair reason. The machine-readable record supports `review.md`; it is not another approval gate for the user.

When writing `visual-review.json`, store image paths relative to the repository root, not relative to the draft directory, and verify every row by resolving it from the same root used by `visual_plan.py`. The record must include page 0 for the cover and exactly one row for every compiled storyboard page.

When repairing evidence paths or bindings, verify the resolved file and its actual hash, and preserve the distinction between previously observed acceptance and unchecked content. A helper may normalize paths or validate records, but must not infer `accept` or `passed` from file existence, page counts, or matching digests. If the underlying inspection cannot be substantiated, leave that check incomplete. For recovery examples, read `docs/PRODUCTION_LESSONS.md`, section “2026-09-06 补充：制作前置条件和证据恢复”. Updating this skill does not authorize migration or re-review of existing books.

This bounded loop is also the traffic budget: one batch contact-sheet review, full-size inspection only for flagged pages, and no re-review of unchanged assets. A minor text adjustment is cheaper and more coherent than repeated image generation only when it preserves the frozen story invariants.

For audio, produce an emotion-tagged narration script. Every non-narrator segment must be audibly attributed: use a natural `speech_cue` when useful, otherwise synthesize a safe default such as “佑佑说”. The rendered audio must never rely only on the visual `speaker` label. Local Roro TTS may render it after approval; do not claim audio exists until a playable file is present and checked. Emotion-token mapping, silence normalization, ASR leakage checks, and encoding details are internal production evidence and must not be displayed as parent review items.

Render the cover title and final narration as separately cached segments for every offered voice: first speak `《绘本标题》。` while the cover remains visible, leave a short pause, then render page 1 onward page by page. Concatenate the segments with a voice-specific cover boundary and page timeline. Cache against the exact title/page TTS input so unchanged segments are reused and a text adjustment regenerates only the affected pages. The player must use that voice's exact page boundaries; proportional duration scaling is only a backward-compatibility fallback for legacy audio. When changing voices, preserve the current cover/page and page-relative progress.

Before TTS, run the narration alignment validator. Keep the cover title out of page 1 narration; page 0 title is rendered by the cover segment. Remove quoted dialogue from the narrator's prose when the same dialogue has a speaker segment, add every dialogue exactly once with an audible cue, and add interaction prompts as actual spoken segments whose `text` exactly matches the story's `interaction` field. If text is adapted, update `story.json`, `story.md`, `narration.json`, `storyboard.json`, and `review.md` together before regenerating affected audio.

## Review and publish

Use `http://127.0.0.1:8877/workbench` as the review surface. The current default is development mode, which does not require parent PIN verification. Codex must first review text, character consistency, story-level scale, scene anchors, full-size page images, narration, and audio; autonomously repair blocking problems; then generate an explicit candidate manifest and a digest-bound `ai-review.json`. Use `templates/review-checklist.md` for this internal review. In the existing candidate preparation step, use `prepare-review-candidate.py --inspect` to inspect selected assets and the digest, record actual checks in `internal-review.json`, then finalize with the same script. It validates storyboard references and visual evidence before producing `ai-review.json`; file presence must never manufacture a passed review.

Treat production evidence, Codex internal approval, a development workbench action, and verified parent approval as separate states. A generated file, automatic check, `story.json` status, or `ai-review.json` is not a publish action. In development mode, a deliberate workbench decision bound to the displayed revision and package digest is sufficient for the local shelf, but it must be recorded as unverified local development activity rather than verified parent approval.

Use the former A-D gates as lightweight Codex internal checkpoints, not user interruptions. Present one complete package in the workbench. “通过并上架” accepts the exact complete local story package and atomically publishes an immutable release to the shelf; “退回修改” records issue tags and notes, after which Codex creates a new revision and repeats internal review. If a published image later fails visual QA, preserve the published release and create a new versioned candidate under `drafts/`.

After the workbench persistently records “通过并上架”, publish only the manifest-listed assets as an immutable `approved/<story-id>/releases/<revision>/` and atomically update `current.json`. Record whether the action came from development mode or PIN-protected parent mode. Never overwrite an older release. Export PDF or an external-library package only when separately requested, and verify each produced artifact.
