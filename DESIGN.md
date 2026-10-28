---
name: Receipt Bench
description: Evidence-first receipt extraction operations console
colors:
  work-canvas: "#f5f7fa"
  rail-canvas: "#fafbfc"
  office-white: "#ffffff"
  cool-fill: "#f3f6f9"
  fine-rule: "#dfe4eb"
  strong-rule: "#bec7d3"
  ink-navy: "#172033"
  muted-slate: "#596579"
  soft-slate: "#737f90"
  azure-action: "#0f5cc0"
  azure-action-hover: "#0b4da3"
  azure-wash: "rgba(15, 92, 192, 0.08)"
  evidence-positive: "#107c41"
  evidence-caution: "#7a4700"
  evidence-negative: "#b42318"
  nav-deep-navy: "#071b33"
  nav-raised-navy: "#102d4c"
  nav-rule: "#234563"
  nav-text: "#f6f9fc"
  nav-muted: "#b6c5d7"
  positive-wash: "#e8f5ed"
  caution-wash: "#fff3cd"
  negative-wash: "#fdebec"
typography:
  metric:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "20px"
    fontWeight: 650
    lineHeight: 1.16
    letterSpacing: "-0.025em"
  brand:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "15px"
    fontWeight: 650
    lineHeight: 1.16
    letterSpacing: "-0.02em"
  title:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "12px"
    fontWeight: 650
    lineHeight: 1.16
    letterSpacing: "normal"
  body:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "13px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
  control:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "11px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
  label:
    fontFamily: '"Segoe UI", Aptos, Calibri, -apple-system, BlinkMacSystemFont, sans-serif'
    fontSize: "10px"
    fontWeight: 600
    lineHeight: 1.4
    letterSpacing: "normal"
  code:
    fontFamily: 'Consolas, "Courier New", Courier, monospace'
    fontSize: "10px"
    fontWeight: 400
    lineHeight: 1.45
    letterSpacing: "normal"
rounded:
  micro: "0.2rem"
  compact: "0.25rem"
  control: "0.3rem"
  panel: "0.35rem"
  comfortable: "0.625rem"
  pill: "999px"
  round: "50%"
spacing:
  xs: "4px"
  sm: "8px"
  md: "10px"
  lg: "12px"
  xl: "14px"
  section: "16px"
components:
  button-action:
    backgroundColor: "{colors.azure-action}"
    textColor: "{colors.office-white}"
    typography: "{typography.control}"
    rounded: "{rounded.comfortable}"
    padding: "7px 14px"
    height: "38px"
  button-action-hover:
    backgroundColor: "{colors.azure-action-hover}"
  button-toolbar-primary:
    backgroundColor: "{colors.ink-navy}"
    textColor: "{colors.office-white}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "6px 11px"
    height: "32px"
  button-toolbar-secondary:
    backgroundColor: "{colors.office-white}"
    textColor: "{colors.ink-navy}"
    typography: "{typography.control}"
    rounded: "{rounded.control}"
    padding: "6px 11px"
    height: "32px"
  select-compact:
    backgroundColor: "{colors.office-white}"
    textColor: "{colors.ink-navy}"
    typography: "{typography.label}"
    rounded: "{rounded.control}"
    padding: "6px 30px 6px 9px"
    height: "30px"
  nav-item:
    backgroundColor: "{colors.nav-deep-navy}"
    textColor: "{colors.nav-text}"
    typography: "{typography.control}"
    rounded: "{rounded.panel}"
    padding: "7px 9px"
    height: "36px"
  nav-item-active:
    backgroundColor: "{colors.nav-raised-navy}"
    textColor: "{colors.nav-text}"
  panel:
    backgroundColor: "{colors.office-white}"
    textColor: "{colors.ink-navy}"
    rounded: "{rounded.panel}"
    padding: "14px"
  status-positive:
    backgroundColor: "{colors.positive-wash}"
    textColor: "{colors.evidence-positive}"
    typography: "{typography.label}"
    rounded: "{rounded.compact}"
    padding: "3px 9px"
    height: "24px"
---

# Design System: Receipt Bench

## Overview

**Creative North Star: "The Evidence Ledger"**

The Evidence Ledger treats evaluation as document inspection, not analytics theater. A fixed deep-navy navigation plane anchors a bright office-white work canvas; thin cool-gray rules, dense tables, and small workhorse controls keep the receipt, golden answer, extractor outputs, and operational evidence visually traceable.

Expression is concentrated in state rather than ornament. Azure blue marks actions, selections, and focus; semantic green, amber, and red distinguish evidence outcomes; and every status remains legible through text, borders, or markers as well as color. The system is compact without becoming compressed: tight controls and rows sit inside clearly separated macro regions with persistent dataset and run context.

The default light mapping is the visual authority. The shipped theme override may remap semantic tokens, but new surfaces should preserve the same navigation/canvas contrast, hierarchy, and evidence-first behavior rather than introducing a separate visual language.

**Key Characteristics:**
- Deep-navy navigation framing a bright, document-like work canvas.
- Fine one-pixel rules and cool fills instead of decorative card elevation.
- Azure-blue interaction state with separately labeled evidence semantics.
- A compact Segoe UI/Aptos workhorse ramp with tabular and monospace detail.
- Receipt-level traceability through tables, pinned context, and scrollable inspectors.

## Colors

The default palette combines restrained office neutrals with a single Azure-blue interaction voice and explicit semantic evidence colors.

### Primary
- **Azure Action** (`#0f5cc0`): primary action, selection, link, focus, and active-rule color.
- **Azure Action Hover** (`#0b4da3`): stronger action state for interactive hover.
- **Azure Wash** (`rgba(15, 92, 192, 0.08)`): selected rows, active tabs, and quiet action backgrounds without losing table legibility.

### Secondary
- **Navigation Deep Navy** (`#071b33`): fixed desktop navigation and the branded segment of the top bar.
- **Navigation Raised Navy** (`#102d4c`): hover and active navigation items.
- **Navigation Rule** (`#234563`): internal separators on the navy plane.
- **Navigation Text** (`#f6f9fc`) and **Navigation Muted** (`#b6c5d7`): primary and supporting copy on dark navigation.

### Tertiary
- **Evidence Positive** (`#107c41`) with **Positive Wash** (`#e8f5ed`): completed, matched, and successful evidence.
- **Evidence Caution** (`#7a4700`) with **Caution Wash** (`#fff3cd`): partial, extra, retrying, and first-ranked attention states.
- **Evidence Negative** (`#b42318`) with **Negative Wash** (`#fdebec`): failed, invalid, missing, and wrong evidence.

### Neutral
- **Work Canvas** (`#f5f7fa`), **Rail Canvas** (`#fafbfc`), and **Office White** (`#ffffff`): page, context rail, and primary panel layers.
- **Cool Fill** (`#f3f6f9`): table headers, passive controls, and quiet nested regions.
- **Fine Rule** (`#dfe4eb`) and **Strong Rule** (`#bec7d3`): default and emphasized boundaries.
- **Ink Navy** (`#172033`), **Muted Slate** (`#596579`), and **Soft Slate** (`#737f90`): primary, supporting, and low-priority text.

### Named Rules

**The Navy Boundary Rule.** In the default scheme, deep navy belongs to persistent navigation and branded shell framing; the evidence canvas remains bright and document-like.

**The Evidence Color Rule.** Green, amber, and red communicate evaluation state only, and every use is paired with text, a marker, or a border.

## Typography

**Display Font:** None; the operations console deliberately has no oversized display role.

**Body Font:** Segoe UI, with Aptos, Calibri, and system sans-serif fallbacks.

**Label/Mono Font:** Consolas, with Courier New and monospace fallbacks for identifiers, field paths, and JSON.

**Character:** Familiar desktop productivity typography keeps attention on evidence rather than brand performance. Weight, compact size, and tabular alignment establish hierarchy inside a deliberately narrow ramp.

### Hierarchy
- **Metric** (650, `18–20px`, `1.16`): KPI values and the strongest numerical evidence; use tabular numerals where values align.
- **Brand** (650, `15px`, `1.16`): product identity in the fixed top bar.
- **Title** (600–650, `12–14px`, `1.16`): section, rail-card, and inspector headings.
- **Body** (400, `13px`, `1.45`): operational explanations and ordinary workspace copy.
- **Control** (550–700, `10–12px`, approximately `1.4`): buttons, selectors, table values, and compact navigation.
- **Label** (600, `9–10px`, normal to `0.04em`): table headers, state notes, metadata, and navigation kickers; uppercase is reserved for structural kickers and headers.
- **Code** (400, `9–10px`, `1.45`): run IDs, field paths, raw responses, and JSON.

### Named Rules

**The Workhorse Ramp Rule.** Keep console typography within the observed `9–20px` range; create hierarchy with weight, alignment, and spacing before increasing size.

## Layout

The desktop shell is a fixed-context grid: a `58px` top bar spans a `218px` navigation column, a `224px` dataset/run context rail, and the flexible work canvas. The navigation and context rail remain viewport-height and independently scrollable. The work canvas is capped at `1680px`, centered, and uses `14px` side gutters with `16px` section intervals.

Within the evaluation surface, the visual sequence is operational: slim section navigation and actions, five independent KPI cards, a compact extractor table, then the receipt inspector. The inspector keeps the source receipt beside comparison evidence; sticky field and golden-answer columns retain meaning while extractor outputs scroll horizontally.

Responsive behavior is progressive rather than a wholesale redesign:
- At `1220px`, shell columns tighten to `205px / 218px / 1fr`, and KPIs reflow to three columns.
- At `1100px`, the context rail leaves the canvas and recent runs return to primary navigation.
- At `900px`, navigation becomes a focus-trapped drawer, the top bar becomes a light mobile shell, KPIs use two columns, and the inspector may scroll horizontally.
- At `640px`, toolbar actions stack, KPI cards become a horizontal snap strip, receipt and comparison regions stack, and wide evidence tables remain intentionally scrollable.

The recurring spacing rhythm is `4px`, `8px`, `10px`, `12px`, `14px`, and `16px`. Use the tighter steps inside controls and tables; reserve the larger steps for section framing and panel padding.

## Elevation & Depth

The system is flat by default. Primary panels explicitly remove ambient shadows and gain depth from white-on-cool layering, one-pixel rules, sticky positioning, and selective background washes. The top bar uses only a hairline sheen (`0 1px 0 rgba(255, 255, 255, 0.55)`), while pinned comparison columns use a one-pixel lateral rule (`1px 0 0 #dfe4eb`) to signal overlap.

### Shadow Vocabulary
- **Top-Bar Hairline** (`0 1px 0 rgba(255, 255, 255, 0.55)`): separates the fixed shell without making it float.
- **Pinned-Column Rule** (`1px 0 0 #dfe4eb`): preserves the edge of sticky golden-answer columns during horizontal scroll.

### Named Rules

**The Rules Before Shadows Rule.** Separate operational regions with cool fills and one-pixel borders first; shadows are structural hairlines, not card decoration.

## Shapes

Outer operational panels use gently squared corners (`0.35rem`), while compact controls use `0.3rem` and status or segmented-control interiors use `0.25rem`. Generic controls outside the dense evaluation workspace may use the established comfortable corner (`0.625rem`).

Tables and comparison matrices keep square internal geometry (`0`) so rules line up and sticky regions read as one ledger. Full pills (`999px`) are limited to progress tracks, and circles (`50%`) are limited to icon buttons, rank markers, and status dots.

## Components

### Buttons
- **Shape:** generic primary actions are `38px` high with `7px 14px` padding and the comfortable corner; evaluation-toolbar actions compact to `32px`, `6px 11px`, and the control corner.
- **Primary:** Azure blue with white text is the reusable action treatment. The evaluation toolbar's report/export action uses Ink Navy with white text to remain distinct from selected-state blue.
- **Hover / Focus:** blue actions deepen to Azure Action Hover; secondary actions shift to Cool Fill with a Strong Rule. All interactive elements use a `2px` Azure Action focus outline with `2px` offset.
- **Secondary:** Office White with Ink Navy copy and a Fine Rule; Azure may appear in its icon.
- **Disabled:** retain the component shape and reduce opacity to `0.48`; do not remove the control from the layout.

### Inputs / Fields
- **Style:** compact selectors are `30px` high with Office White fill, a Fine Rule, the control corner, and `6px 30px 6px 9px` padding.
- **Focus:** the global Azure Action outline is always visible without relying on border color alone.
- **Labels:** field labels and hidden accessible labels remain structurally associated; visible micro-labels use the Label role.

### Navigation
- **Style:** desktop items are `36px` high with `7px 9px` padding on Navigation Deep Navy. Hover and active items move to Navigation Raised Navy; active items also gain a `2px` Azure Action rule and stronger weight.
- **Mobile:** below `900px`, the same navigation becomes a maximum `286px` focus-trapped drawer with scrim, Escape dismissal, and focus restoration.

### Chips
- **Style:** compact run-state chips use semantic ink over the matching wash, `3px 9px` padding, and the compact corner.
- **State:** copy and iconography remain present; animation is restricted to running indicators and is removed for reduced-motion preferences.

### Cards / Containers
- **Corner Style:** operational panels and context cards use the panel corner (`0.35rem`).
- **Background:** Office White on Work Canvas or Rail Canvas.
- **Shadow Strategy:** none at rest; follow the Rules Before Shadows rule.
- **Border:** a single Fine Rule on the outer edge and between internal regions.
- **Internal Padding:** `14px` is the standard compact card inset; KPI cards use `14px 15px`.

### KPI Strip
Five independent cards keep quality, exactness, failure, and cost evidence separate. Each card has a minimum height of `112px`; an `11px` title anchors the top, an `18–20px` value forms the evidence center, and `10px` provenance copy sits at the bottom.

### Evidence Tables
Performance rows are `36px` high with `6px 10px` cell padding, Cool Fill headers, one-pixel grid rules, and tabular numbers. Hover and selected rows use Azure Wash. The comparison matrix keeps a sticky `104px` field column and `158px` golden-answer column; extractor columns are `155px` wide, and cell state is reinforced by a one-pixel semantic left rule plus explicit state copy.

**The Horizontal Ledger Rule.** Every horizontally scrollable table owns an isolated scroll container, preserves native `table-cell` display on every `th` and `td`, and gives each sticky track identical `width`, `min-width`, and `max-width` values. Grid or flex layout belongs inside a cell wrapper, never on the table cell itself. Pinned cells must have opaque backgrounds, explicit stacking order, clipped overflow, and a visible trailing rule. Validate rendered text and child bounds at both `scrollLeft = 0` and a non-zero scroll position for every horizontal table before shipping.

### Receipt Inspector
The inspector toolbar is `49px` high. Its desktop body pairs a `220px` receipt source column with flexible comparison evidence; the receipt viewport is `322px` high, and image controls remain compact. At the narrow breakpoint the source stacks above the comparison while the matrix retains horizontal scrolling.

## Do's and Don'ts

### Do:
- **Do** preserve the deep-navy navigation and bright office-white canvas relationship while keeping action and selection emphasis Azure blue.
- **Do** use one-pixel cool-gray rules and subtle fills to separate dense evidence before adding elevation.
- **Do** keep quality, cost, latency, and failure evidence in separate labeled columns, cards, or regions.
- **Do** keep horizontal-table column tracks fixed and move grid/flex composition into wrappers inside native table cells.
- **Do** pair status color with text labels, markers, or borders.
- **Do** keep long comparison data in scrollable tables with sticky field and golden-answer context.
- **Do** collapse the context rail before converting primary navigation to a drawer at the observed breakpoints.

### Don't:
- **Don't** turn the default work canvas into a uniformly dark, high-gloss dashboard; its shipped identity is bright and document-like.
- **Don't** replace evidence tables with decorative charts or a single composite score.
- **Don't** round every internal matrix cell; square internal geometry preserves ledger alignment.
- **Don't** use shadows to make every operational panel float.
- **Don't** introduce oversized marketing typography into the evaluation console.
