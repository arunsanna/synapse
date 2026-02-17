# Synapse UI Revamp Guide

## 1. Current Frontend Model
- Stack: FastAPI server-rendered HTML in `gateway/src/main.py` (`_DASHBOARD_HTML` string).
- Styling: Inline CSS in the same template string.
- Interactivity: Vanilla JS in the same template (`refreshHealth`, `refreshModels`, load/unload actions).
- Constraint: No React/Vue/Tailwind runtime in gateway image, so design system must remain lightweight and self-contained.

## 2. Design System Tokens
Use CSS variables in `:root` as the single source of truth.

- Core colors:
  - `--background: #0a0a0f`
  - `--foreground: #e0e0e0`
  - `--card: #12121a`
  - `--muted: #1c1c2e`
  - `--muted-foreground: #8a93a3`
  - `--accent: #00ff88`
  - `--accent-secondary: #ff00ff`
  - `--accent-tertiary: #00d4ff`
  - `--border: #2a2a3a`
  - `--ring: #00ff88`
  - `--destructive: #ff3366`
- Effects:
  - `--shadow-neon`, `--shadow-neon-sm`, `--shadow-neon-lg`
  - `--shadow-neon-secondary`, `--shadow-neon-tertiary`
- Shape primitives:
  - `--chamfer-sm`, `--chamfer-md` clip-path tokens for corner cuts.

## 3. Typography Rules
- Headings: `Orbitron` with uppercase and wide tracking.
- System/body text: `JetBrains Mono`.
- UI labels + endpoint rows: `Share Tech Mono`.
- Maintain terminal-style casing and letter spacing on metadata, chips, and controls.

## 4. Layout Architecture
- Hero section (60/40 split) with:
  - Glitched H1 + chromatic aberration.
  - Operational metadata chips (URL, uptime, backend count).
  - Terminal feed panel with blinking cursor.
- Main content:
  - Two-column operational grid for backend health and model control.
  - Backend API inspector is embedded directly under backend health and driven by backend-card selection.
  - Capability matrix table.
  - Operator quick links panel.
- Responsive behavior:
  - Collapse to single-column below `1120px`.
  - Preserve 44px minimum tap targets.

## 5. Component Contract
- Panels:
  - Classes: `.cyber-panel`, `.panel`.
  - Visuals: chamfer clip-path, neon edge, hover signal.
- Buttons:
  - Class: `.btn`, variants via `.load` and `.unload`.
  - Default neon green, magenta variant for unload.
- Status primitives:
  - Gateway LED: `.hdr-dot`.
  - Backend LED: `.status-dot`.
  - Model state: `.status-pill` (`loaded`, `loading`, `unloaded`, `failed`, `unknown`).
- Data surfaces:
  - Tables wrapped in `.table-wrap` for consistent border + overflow behavior.
  - Backend selector cards are interactive (`role="button"`, `tabindex="0"`) and highlight active selection.
  - Model registry rows expose a metadata modal (`.model-modal`) showing parsed fields, runtime args, and raw JSON payload.

## 6. Motion + Effects
- Mandatory effects applied:
  - Global scanline overlay (`body::after`).
  - Grid + gradient mesh atmosphere (`body::before`).
  - Glitch headline (`.cyber-glitch` + pseudo-elements).
  - Scan sweep animation (`.scanline-sweep`).
  - Cursor blink (`.cursor`, subtitle caret).
- Accessibility:
  - `prefers-reduced-motion` disables animation-heavy effects.
  - Neon focus-visible ring on buttons/links.

## 7. Behavioral Integration
- Keep IDs unchanged for JS bindings:
  - `#overall-dot`, `#uptime`, `#refresh-info`, `#models-refresh-info`,
  - `#model-action-status`, `#models-table-body`, `#refresh-models-btn`,
  - `#term-health`, `#term-backends`, `#term-models`, `#term-age`, `#term-bus`,
  - `#model-modal`, `#model-modal-title`, `#model-modal-content`.
- Keep backend card selectors unchanged:
  - `data-backend`, `.status-dot`, `.backend-status`.
- Keep existing API actions unchanged:
  - `GET /health`, `GET /models`, `POST /models/load`, `POST /models/unload`.
- Truth-first rules:
  - Terminal telemetry values must be derived from live API state, never hardcoded.
  - On refresh failure, mark health/model telemetry as stale and show stale visuals.
  - Polling must ignore stale responses (request token/abort protection).

## 8. Path and Access Rules
- Dashboard routes:
  - `GET /` direct dashboard (primary).
  - `GET /ui` alias.
  - `GET /dashboard` compatibility route.
- Requirement: root path must return `text/html` with direct template body.

## 9. Next Refactor (Optional)
- Move dashboard template into `gateway/src/templates/dashboard.html`.
- Split CSS into a dedicated static asset and keep token block as a shared include.
- Convert capability and endpoint lists into structured Python data for easier updates.
