# App windows — tools that stay open while you chat

Status: v1 (2026-11-19). Replaces the split view and the phone return bar shipped
the same morning (`toolsplit.js`, `#tool-return`); they are folded in here.

## 1. The problem, in the owner's words

> "I open Image Studio, then when I go back to chat, it minimizes into a small pill
> on the bottom … Clicking the pill brings the app back up as an overlay, which can
> be resized or docked on one side … on mobile it covers pretty much the entire
> screen, desktop it's like a pop-up … I'm having trouble switching. Set this up as
> a new tool framework for dispatch so it can be applied to any tool."

Switching between a tool and the chat today either destroys the tool (✕, picking a
bot, the phone's Bots page) or needs a mode the user has to discover (split toggle,
the phone's parked chip). The fix is one small window manager that every framed
tool gets for free.

## 2. Research takeaways

| Source | Taken | Rejected |
|---|---|---|
| Gmail compose (minimise to a bottom bar, pop out, full screen; [Workspace Updates 2013](https://workspaceupdates.googleblog.com/2013/07/full-screen-option-in-new-gmail-compose.html)) | Minimise = a titled bar at the bottom; clicking it restores exactly as left; full-screen toggle in the title bar. | Several compose windows visible side by side: fine for small forms, clutter for full apps like Image Studio. |
| Android bubbles + Android 17 bubble windowing mode ([developer.android.com](https://developer.android.com/develop/ui/views/notifications/bubbles)) | Collapsed tool shows a **badge** when something happened; drag to a target to dismiss; system Back collapses, never destroys. | Bubbles destroy the activity on collapse — we keep the page alive (that is the point here). |
| YouTube mini player / Meet & Slack huddle mini windows | Keep running work alive while the main surface is used; a single tap returns to it. | Free-floating draggable mini window on a phone: covers chat content and fights scrolling. Our phone pill sits in a tray instead. |
| Windows Snap / macOS tiling | Drag the title bar to a screen edge → preview → snap to a half; drag to the top → fill; double-click the title bar toggles fill. | Quarter tiling and multi-window layouts: more than a family chat app needs. |
| Material bottom sheet | Phone = a sheet with a grab handle; swipe down to dismiss (here: minimise). | A modal scrim behind it — the sheet is the screen, there is nothing to dim. |
| WAI-ARIA APG: dialog (non-modal) and [window splitter](https://www.w3.org/WAI/ARIA/apg/patterns/windowsplitter/) | Window = `role="dialog"` **without** `aria-modal`, labelled by its title; focus moves in on open and back to the opener/pill on minimise; Esc dismisses (here: minimise, the non-destructive choice). Divider = focusable `separator` with valuenow/min/max, arrows, Home/End. | — |
| WCAG 2.2 [2.5.7 Dragging](https://www.w3.org/WAI/WCAG22/Understanding/dragging-movements.html), 2.5.8 Target size | Every drag (move, resize, snap, divider, swipe-to-close, swipe-to-minimise) has a **single click/tap** alternative: layout buttons, a visible ✕ on each pill, Back to chat. Swipes are never the only way. Targets ≥ 24 px desktop, ≥ 44 px on a phone. | Keyboard-only alternatives to a drag (2.5.7 says that alone is not enough). |

## 3. Interaction model

A **tool window** belongs to one tool (one window per tool — opening it again
restores the one that exists). States:

```
            open (launcher, #tool= link, pill click)
 closed ───────────────────────────────▶ open ◀──────────┐
   ▲                                      │  Back to chat │ pill click /
   │ Close (✕ in bar / ✕ on pill /        │  Esc, phone   │ launcher /
   │ drag pill away; confirm if unsaved)  ▼  Back, swipe  │ #tool= link
   └──────────────────────────────── minimised ───────────┘
```

* **One window visible at a time**, any number minimised. Opening or restoring
  another tool minimises the visible one (it is not closed). Why: DisPatch tools
  are whole apps; one plus the chat is what fits a laptop, and a pill switches in
  one tap. No z-order to manage, no focus fights between two frames.
* **Minimising never unloads the page** (its frame stays in the DOM, hidden), so a
  half-typed prompt, a running picture job or a scrolled list is exactly as left.
  A tool may opt out (`keep_alive: false`) — it is unloaded on minimise and
  reloaded on restore; the pill stays.
* **Close** unloads the page. If the tool declared unsaved work, Close asks first.
* After a **reload**, windows come back as they were (layout, size, position); the
  one that was open loads at once; minimised ones come back as pills and load when
  first restored (no surprise traffic at boot).

### Desktop layouts (chosen in the title bar's layout menu, or by dragging)

```
 float (default)                    dock left / dock right
┌──┬────────────┬──────────────┐   ┌──┬────────────┬┃┬─────────────┐
│R │ threads    │ chat         │   │R │ threads    │ chat         ┃ tool        │
│a │            │  ┌─────────────┐ │a │ (hidden    │              ┃┌───────────┐│
│i │            │  │▤ Studio ⧉ ─ ✕││ │i │  if narrow)│              ┃│▤ Studio … ││
│l │            │  │             ││ │l │            │              ┃│           ││
│  │            │  │   page      ││ │  │            │              ┃│  page     ││
│  │            │  └─────────────◢│ │  │            │ [pill][pill] ┃│           ││
│  │            │ [pill]  composer│ │  │            │ composer     ┃└───────────┘│
└──┴────────────┴──────────────┘   └──┴────────────┴──────────────┴─────────────┘

 full (covers threads + chat; the rail stays)        phone sheet
┌──┬───────────────────────────┐                    ┌───────────────┐
│R │▤ Image Studio   ⧉ ─  ✕    │                    │  ───          │ ← grab: swipe down = minimise
│a │                           │                    │▤ Studio   ⌄ ✕ │
│i │         page              │                    │               │
│l │                           │                    │    page       │
└──┴───────────────────────────┘                    └───────────────┘
```

* **Title bar** (every window): icon, title, a status line the tool can set,
  Refresh (static tools with a refresh script), Open in a new tab, **layout menu**
  (Float · Dock left · Dock right · Full screen), **Back to chat** (minimise),
  **Close**. The title area is the move handle.
* **Float**: drag the title bar to move; the bottom-end corner and the edges
  resize; double-click the title toggles Full. Kept inside the content area (the
  rail is never covered) and re-clamped when the browser window shrinks. Dragging
  the title bar to the content area's left/right edge previews and snaps to a
  dock; to the top edge, Full. Dragging a docked/full window out of place turns it
  back into a float under the pointer.
* **Dock left/right**: the chat column gives up that side; the draggable divider
  (the split view's, same sums and minimums: chat ≥ 320 px, tool ≥ 360 px) sits on
  the seam; the thread list steps aside when there is not room for all three. Too
  narrow for both → the window is shown Full until there is room again (the dock
  choice is remembered). Physical left/right, so the labels mean what they say in
  right-to-left languages too.
* **Full**: covers the thread list and chat; the rail stays.
* The chat under a float or beside a dock is fully usable — non-modal.

### Phone (narrow layout)

* The window is a **sheet** covering the screen below a small top gap (safe-area
  aware), over the bottom tabs ("covers pretty much the entire screen"; Back to
  chat brings them back), with other pills above its lower edge. No float, no
  dock (the layout menu is hidden).
* Bar: grab handle, icon, title/status, **⌄ Back to chat**, ✕ Close (44 px each).
  Swiping the handle/title down ≥ 96 px minimises (the ⌄ button is the tap
  alternative). The phone's **Back** minimises the sheet (Android bubble rule).
* Opening a tool from the phone's Tools sheet never changes the page underneath
  (Bots / threads / a chat). The **⌄ Back to chat** button means it: when a chat
  is open it comes up under the pill (headless check found the label lying
  when the tool was opened from the bot list). The system Back only steps back.

### The pill tray

* One pill per minimised window, in the order they were minimised (newest at the
  end): icon, name, status (e.g. "2 making"), a dot when the tool asked for
  attention while minimised, and a visible ✕.
* Placement: a fixed row at the bottom of the chat column, **directly above the
  composer** when one is showing, else above the phone's bottom tabs, else at the
  bottom edge; safe-area insets respected. The message list gets matching bottom
  padding, so no message hides under it. On a phone the tray hides while the
  on-screen keyboard is up (visualViewport) and returns when it closes.
* Tap a pill = restore. ✕ = close. Drag a pill up/away past a threshold → it turns
  into "Release to close" → release closes (unsaved → confirm). Keyboard: pills are
  a toolbar (arrow keys move between pills, Enter restores, Delete closes).
* A tray with more pills than fit scrolls sideways.

### Focus, Esc, keyboard

* Opening/restoring focuses the window's title bar (screen readers hear "Image
  Studio, dialog"). Minimising returns focus to that tool's pill; closing to the
  previous pill, else the chat composer, else the rail.
* **Esc** with focus on the window chrome minimises (never closes). Esc inside a
  tool's page belongs to the page.
* Move handle (focused title): arrows move a float by 24 px (Shift: 96); with
  Alt+arrows resize. Divider: arrows/Home/End as in the APG splitter.
* `Alt+Shift+M` (not a browser shortcut on any platform we target) toggles the
  most recent window between open and minimised from anywhere in the shell.

### Launcher, deep links, rail

* Rail / Tools menu on a tool that is minimised → restore it; already open →
  focus it. Never a second copy. The rail marks open AND minimised tools (open =
  the active marker, minimised = a small dot).
* `#tool=<id>` opens or restores that window (as today). The hash follows the
  visible window; minimising clears it.

### Built-in panes

The four built-in panes (Harness, StudioForge, Emails, Clients) and an add-on's
native pane (voice tools) are DOM owned by `main.js`, not framed pages; they keep
their full-pane behaviour in v1. Opening one **minimises** the visible tool
window instead of closing it, so switching still works. Hosting them in windows is
a follow-up (an adapter that reparents the view into a window body once).

## 4. Roles

Nothing about gating changes: a window can only hold a tool the server lists to
this session (`GET /api/tools` is already filtered by permission), and stored
window state is filtered against that list on every restore.

| Who | Experience |
|---|---|
| Owner / Admin | Every tool they may use; float / dock / full; pills; state survives reloads on this device. |
| User (Member) | Same, for the tools their permissions list (static/url tools an Admin added, Image Studio where `use_images` is on). |
| Basic (Restricted) / a child on a phone | Only tools granted to that profile (often none: no pill tray at all, nothing new on screen). With a tool: a full-screen sheet with big ⌄ and ✕; Back to chat leaves a pill; tapping it brings the tool back. No drag needed anywhere. |
| Profile switch / lock | Every window is torn down (frames unloaded) before the next person sees the screen. Window state is stored per profile id on this device, so the next profile sees only its own windows; the previous one gets theirs back after signing in again. |
| No-Image Mode | Pills and bars use line icons only. A tool's own pictures follow its own NIM handling (Image Studio already does). |

## 5. State and persistence

Pure module `js/appwin-state.js` (no DOM, node-tested):

```
win = { id, state: 'open'|'min', layout: 'float'|'left'|'right'|'full',
        rect: {x, y, w, h} (float, CSS px, content-area relative),
        ratio (dock: the tool's share, 0..1), order (minimise order),
        status, badge, attention, unsaved }
model = { wins: [win], open: id|null }
```

Operations: `openWin(model, id, opts)`, `minimize`, `restore`, `closeWin`,
`setLayout`, `moveTo`, `resizeTo`, `clampRect(rect, bounds, min)`,
`snapZone(point, bounds)` → 'left'|'right'|'full'|null, `effectiveLayout(win,
{mobile, canDock})`, `serialize` / `parse` (validated, junk dropped, version
key), `filterKnown(model, ids)`, `migrateSplitPref()`.

Storage: `localStorage['dispatch-appwin:v1:<profileId|shared>']` (≤ 8 KB, no
statuses or page content — only ids, layouts, rects, ratio, order, which one was
open). Every access guarded. The prefix joins the privacy wipe list.
Status, badge, attention and unsaved are live-only (a reload resets them).

**Migration:** the morning's split preference (`dispatch-tool-split = 1`) becomes
the default layout of a tool opened for the first time: dock on the inline end
(right in LTR, left in RTL) at the saved ratio (`dispatch-tool-split-ratio`). The
two keys are then removed. The chat-half thread memory (`sessionStorage`) is no
longer needed: the chat is never replaced by a window, so a reload restores the
chat the usual way.

## 6. Framework contract (what a tool author needs)

### Manifest

`apps/<id>/app.yaml` and a `tools.yaml` static/url row may carry:

```yaml
window:
  layout: float        # float | left | right | full — first-open layout on desktop (default float)
  width: 1040          # first-open float size, px (clamped to the screen)
  height: 760
  min_width: 360       # smallest the window may be resized to (default 360, 240..1600)
  min_height: 320      # (default 320, 200..1200)
  dockable: true       # may dock left/right (default true)
  keep_alive: true     # stays loaded while minimised (default true)
```

All keys optional; a tool with none behaves sensibly. Unknown keys are refused at
load (app manifests) or by validation (tools.yaml), like every other key.

### SDK (`/static/js/app-sdk.js`, apps; the same messages work from a static tool's page)

```js
import { win } from '/static/js/app-sdk.js?v=N';
win.setStatus('2 making');      // ≤ 40 chars, shown on the pill and under the title; '' clears
win.setBadge(3);                // a count on the pill (null/0 clears)
win.attention();                // a dot on the pill if minimised; cleared on restore
win.setUnsaved(true);           // Close asks first while true
win.minimize();                 // the app's own "back to chat"
win.onState((s) => {});         // {state:'open'|'min', layout} on every change, once after load
```

Wire format: frame → shell `{type:'dispatch:window', op, value}`; shell → frame
`{type:'dispatch:window-state', state, layout}`. Accepted only from that window's
own frame (`event.source`) with the origin that frame can have — the same rule as
every other app message. Text is drawn with `textContent`.

Existing messages keep working: `dispatch:close` (closes, no confirm — the app
asked), `dispatch:set-title`, `dispatch:open-thread` (opens the thread in the chat
and **minimises** the window on a phone or when Full; beside a float/dock the
window stays; obeyed only from the window on screen — a pill cannot switch the
chat unasked), `dispatch:lightbox`, `dispatch:toast`, `dispatch:open-settings`,
`dispatch:tool-state`. `dispatch:active-thread` now goes to the visible window
when it is floating or docked (it went to the split pane before).

## 7. Security

* Frames are created from a `<template>` whose iframe carries the strict sandbox;
  `tools.js` decides the sandbox per tool **before** `src`, exactly as today (app:
  none; static: scripts/forms/popups; url: + same-origin). No frame is ever moved
  in the DOM (moving reloads it), only re-styled.
* Messages are routed by `event.source` to the window that owns that frame; a
  message from a frame of another window, a closed window, a popup or another
  origin is ignored.
* Lock / profile switch destroys every window. Stored state is per profile id and
  filtered against the server's tool list; it holds no page content.
* No backend route changes. The manifest `window` block is validated and only
  ever echoed as data.

## 8. Tests

* `tests/appwin-state.test.js`: state machine (one open, minimise order, restore
  minimises the other, close), layout coercion on phone / narrow, clamp on shrink,
  snap zones (LTR, RTL), parse of junk/oversized/foreign storage, profile keys,
  migration of the split preference, unknown ids dropped.
* `tests/appwin.test.js` (jsdom): window DOM per tool, frame sandbox before src,
  pill tray, ✕ / restore / Esc / Delete, message routing by source, lock tears
  down, focus moves.
* Source pins: sw.js CACHE, `?v=` importers, the privacy prefix, the strict
  template sandbox.
* Backend: manifest `window` block (valid, defaults, refused shapes), echoed in
  `GET /api/tools` for apps and static/url rows, round-trips through Settings.
* Headless (scratch instance, desktop 1440×900 and phone 390×844): the run in the
  brief — open Image Studio, type, Back to chat, pill, send, restore (form intact),
  move, resize, snap left/right, divider, full, reload, two pills, close by button
  and by drag, keyboard-only, second profile, Restricted profile, Reload last
  generation, no console errors.

## 9. Plan review (independent, before the build) and what changed

An independent review read the owner's words and this note against the code.
Folded in (these override the sections above where they differ):

1. **One fixed window layer; frames never move.** Dock = the chat column reserves
   width through a CSS variable (the split view's mechanism); float/full/sheet are
   styles only.
2. **Per-window panes behind the current `tools.js` exports**; every message is
   routed by `event.source` to the window that owns that frame (app messages and
   the tool-state bridge alike).
3. **Live frames, theme and language go to every live window**, minimised ones
   included (so "2 making" and the attention dot work, and a restored window has
   the current theme). `active-thread` goes to the visible float/dock only.
   Minimised frames get `dispatch:window-state {state:'min'}` so they may slow down.
4. **Phone Back:** the sheet takes a history entry (the shell's `historyPush`);
   ⌄, swipe-down and Back all release it. The old "Back out of a full-page tool"
   popstate path no longer applies to tool windows.
5. **`win.setBusy(n)`** (work running). Close asks while busy **or** unsaved.
   Image Studio reports its running jobs as busy + status; its draft is autosaved,
   so it never claims `unsaved`.
6. **Basic (Restricted) profiles:** no drag-to-close on pills (✕ only, confirm
   when busy). Targets ≥ 44 px on phones for everyone.
7. **At most 3 live frames.** Restoring/opening a fourth unloads the least
   recently used minimised one; its pill stays and it reloads on restore.
8. **After a reload:** desktop reopens the window that was open; a phone brings
   every window back as a pill (a sheet over the chat at boot is a surprise).
9. **No saved window state without a profile id** (no `shared` key).
10. **First float:** at most 60 % of the content width (never under the tool's
    minimum) and ≤ 760 px tall, at the inline-end side, its bottom above the pill
    tray; the tray is drawn above a float.
11. **Alt+Shift+M dropped** (Option+Shift+M types a character on a Mac). **F6**
    cycles focus between the visible window and the chat.
12. **New-tab tools** (`open: window`) never get a window or a pill.
13. **Screen readers:** the tray is a labelled toolbar; pill status text is not a
    live region; "finished while minimised" is announced once, politely.
14. The split view module, its test, the phone return chip and the chat-half
    `sessionStorage` key go in the same change; new modules join the sw.js SHELL.

Kept against the review:
* **"Back to chat" on the phone keeps the owner's wording** (the review suggested
  "Minimise"). It minimises and, when a chat is open underneath, shows that chat.
* **Built-in panes** (Harness, StudioForge, Emails, Clients) were asked to become
  window bodies in v1. They are DOM with their own openers, selection and
  full-pane rules in `main.js`; v1 makes them minimise the visible tool window
  instead of closing it, and hosting them in windows is the next step (section 3,
  "Built-in panes"). Done in this round only if the core lands with time to spare.
