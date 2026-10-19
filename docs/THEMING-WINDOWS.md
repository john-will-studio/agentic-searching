# Theming and Windows display notes

**Mode:** `ui.theme_mode` is `system`, `light` or `dark`. `system` follows the OS setting via `darkdetect` and falls back to light.

**Schemes:** these work like agent-gui's: a scheme is one **theme color**. Its hue tints the whole app (background, panels, borders, muted text and Sun Valley's grey controls) and sets the accent (buttons, checkboxes, switches, tabs, links, selection). Its saturation sets how strong the tint is, so `gray` is neutral. Body text and the status colors (ok/warn/danger) never change. The accent is darkened or lightened automatically until text on it reaches 4.5:1 contrast.

Built-in schemes: `sun-valley` (neutral, Sun Valley blue), `green`, `blue`, `red`, `brown`, `pink`, `purple`, `orange`, `teal`, `gold`, `gray` (agent-gui's presets). The earlier IDs `violet`, `forest` and `graphite` still load, as Purple, Green and Gray.

Any color: choose **Custom color…** in Appearance, or run `lucid-warrant config theme set custom --color "#336699"` (stored as `ui.theme_color`).

Organization schemes go under `ui.schemes`. Give a `color`, and optionally override individual tokens per mode:

```json
"ui": {
  "scheme": "acme",
  "schemes": {
    "acme": {"label": "Acme red", "color": "#a4161a",
             "light": {"accent": "#8f1316"}}
  }
}
```

**Color tokens:** `bg`, `surface`, `text`, `muted`, `border`, `accent`, `accent_text`, `accent_soft`, `ok`, `warn`, `danger`, `info`, `warn_bg`, `danger_bg`, `info_bg`, `agent_bg`, `agent_border`, `code_bg`. Tokens you don't override come from the scheme's color (or, with no color, the built-in palette for that mode). Keep `ok`, `warn` and `danger` recognizable; don't tie them to your brand color.

**Contrast:** `lucid-warrant config validate` and the Appearance dialog warn when a text/background pair falls below 4.5:1. The pairs checked are text on bg and surface, muted on surface, accent_text on accent, each status on its background, and text on agent_bg.

**How it's applied:** Sun Valley draws its controls from image sprites. The app recolors those sprites (accent pixels to the accent, grey pixels to a faint tint of the hue), loads the tinted background into Sun Valley's own color table (Sun Valley resets every classic widget from it on each theme change), and recolors widgets already on screen. Appearance… changes apply immediately; Cancel restores the previous look.

**Text and density:**
- `ui.base_font_pt` sets the text size; 0 uses the system size.
- `ui.density` (`compact`, `comfortable`, `spacious`) sets table row height.
- Users change these under Appearance… (Ctrl+,); they apply immediately.

**Windows checks before rollout:**
- 100%, 150% and 200% display scaling;
- light, dark and High Contrast modes;
- the smallest supported window (1024×680);
- keyboard-only use: Tab, Space, Return, Ctrl+1–5, F5;
- Narrator or NVDA reading the Compose form and a signing document.
