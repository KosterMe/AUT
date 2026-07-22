# Bundled fonts

These fonts are burned onto auto-clip headlines and covers by libass (via ffmpeg's
`subtitles` filter, pointed here with `fontsdir`). Bundling them keeps the on-screen
look identical on Windows and in the Linux containers, neither of which ships a
punchy display font with Cyrillic coverage.

- **Oswald-Bold.ttf** — Oswald, Bold. Condensed, high-impact headline font with full
  Cyrillic support. SIL Open Font License 1.1 (see `OFL.txt`).
  Source: https://github.com/googlefonts/OswaldFont

Override the headline font with the `AUTOCLIPS_TITLE_FONT` env var (the family name
must match a font available here or system-wide). Override this directory with
`AUTOCLIPS_FONTS_DIR`.
