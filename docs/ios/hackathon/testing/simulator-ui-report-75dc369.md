# List-only Agent and Live Activity retest

**Revision:** `75dc369dffe4aa65ef07d8bd1142f030fb32fd7c` (combined sizing and PR39 polling).
**Environment:** iPhone 17 Pro, iOS 26.5 (23F73), Xcode 26.6. Rebuilt and installed locally; fresh confirmed Pro test account retained.

## Results

- **PASS — required nonterminal starting state:** fresh research task appeared as **Working · 0:17** in Sessions → Agent list. Its detail screen was never opened.
- **PASS — autonomous progression:** observed **Working → Done · 1:39** in that list, with no external status request, parent polling or detail entry. A documented start request seeded the task and an initial UI pull-to-refresh discovered it; no additional refresh gesture was used after the nonterminal starting state.
- **PARTIAL — queued → running → completed:** start response was **queued**; the first visible list state was already **Working**. The Queued label was not observed in the list and is not claimed as covered.
- **PASS — Live Activity actually ends:** observed the active task's expanded Working activity on Home, then after list completion returned Home and confirmed the island had no activity, including after long-press. ActivityKit system logs independently record the same activity ending, not merely updating to pinned Done.
- **PASS — keyboard regression:** switched to actual Gusan extension in Messages and hid/showed it with Cmd+K. Full strip and four rows remained bounded.
- **No new product failure reproduced** in the approved longer-task run.

The initial short example.com task completed before nonterminal list discovery, so it could not meet the required starting state. Per explicit approval, retried once with a longer read-only Apple-documentation research task. That retry is the primary evidence here. No demo task, preview host, runtime mutation, source edit or manual status polling was used.

## Task and timeline

Task **`38b196b1-b7dd-4596-a6c8-ab87e08fccbb`**:

| Event | UTC, 2026-11-08 |
|---|---|
| Start request sent | 08:55:23.021 |
| Backend created_at from start response | 08:55:24.128 |
| Queued start response received | 08:55:25.825 |
| First nonterminal list observation | approximately 08:55:41, row timer 0:17 |
| Working screenshot saved | 08:56:01 |
| Active Live Activity screenshot saved | 08:56:40 |
| ActivityKit ending event | **08:57:03.677** |
| Done list screenshot saved | 08:57:31; row duration **1:39** |

Research request: official Apple documentation for UIInputViewController, UIInputView.allowsSelfSizing, Full Access privacy and ActivityKit ending policies, with a concise linked comparison and three implementation pitfalls. Explicitly prohibited sign-in, form submission, edits and purchases.

App remained on Sessions list for progression except brief Home visits to inspect Dynamic Island. Returned using the app icon, never the activity's Open action or a task row.

## Lifecycle evidence

Activity ID **`264818B4-D632-4E87-B6F6-5C69FBADF815`**, created **08:55:40 UTC**:

```text
00:57:03.677 liveactivitiesd: Ending activity 264818B4-D632-4E87-B6F6-5C69FBADF815 ... com.aram2k.gusan
00:57:03.687 liveactivitiesd: Ephemeral activity ended 264818B4-D632-4E87-B6F6-5C69FBADF815
00:57:03.688 liveactivitiesd: Momentary activity ended 264818B4-D632-4E87-B6F6-5C69FBADF815
```

System log timestamps use VM local time UTC−8. These ending events align with the 1:39 task duration and disappeared island UI.

| Working in Sessions list | Done in the same list, detail never opened |
|---|---|
| ![List Working](https://app.devin.ai/attachments/cea26540-966c-41a1-b189-90eb79b5a8af/gusan-75dc369-list-working.png) | ![List Done](https://app.devin.ai/attachments/00b0f2d6-86b0-4816-ac27-c973b5771483/gusan-75dc369-list-done.png) |

| Active task activity | Activity gone after completion |
|---|---|
| ![Active Live Activity](https://app.devin.ai/attachments/46134374-299f-49b2-97f2-f37d08ca192b/gusan-75dc369-activity-active.png) | ![No pinned activity remains](https://app.devin.ai/attachments/1110d220-f826-4a68-bef9-d96188f5832e/gusan-75dc369-activity-gone.png) |

## Artifacts and gaps

- Primary recording: `/Users/devin/screencasts/rec-c19118af-1ce3-4bf0-abbd-e893f530afa2/rec-c19118af-1ce3-4bf0-abbd-e893f530afa2-edited.mp4`
- Start metadata: `/Users/devin/gusan-75dc369-longer-task-seed.json`
- Full lifecycle log: `/Users/devin/gusan-75dc369-activity-lifecycle.log`
- Ending excerpt: `/Users/devin/gusan-75dc369-activity-ended-excerpt.txt`
- Build log: `/Users/devin/gusan-75dc369-build.log`
- Regression screenshot: `/Users/devin/screenshots/gusan-75dc369-keyboard-regression.png`

The list's Queued label was not observed. Audio input, real voice insertion and Undo remain outside this retest and were previously blocked by the VM having no audio devices. Calendar changes at newer revision 0a8094f have not been tested here. No user credentials or intervention needed.
