# Recipes

A recipe is turned into a file one section at a time. Write the sections as paragraphs
separated by an empty line, each describing what to write. Optional first lines
`target: FILE` and `mode: code|text` set the file and the kind. Code: `recipe.py`,
`snippets.py`.

```
target: src/shapes.py

area(radius: float) -> float
returns the area of a circle, using math.pi

class Circle
holds a radius; area() -> float uses area()
```

- **Code or text.** `code` writes functions, classes and statements: Python imports are
  gathered at the top, and each Python section is syntax-checked. `text` writes
  Markdown paragraphs, for documents. The mode follows the target (`.md`, `.txt`:
  text), or set it with `mode:`, `text`/`code` on `/write`, `--text`/`--code`, or the
  switch on the web. Each mode has its own short system prompt.
- **Restricted on purpose.** Each section is written by its own clean-context call
  that reads only that section. Its only tool is `risky_write_file`, which you allow
  or deny once, before the first section. A section that fails is retried once with
  the reason, then kept in the file under a comment saying why.
- **One file, rewritten whole.** Every section has its place in the target, in recipe
  order. A run writes only sections that are new, changed or failed (`all` writes them
  all, `only=N,M` just those); edit the recipe, not the output.
- **Unticking sections.** On the web, untick a section (or *untick all*) and the next
  Write writes it again, by the model rather than from the code library.
- **Parallel.** `parallel` writes several sections at once on as many model instances
  as RAM allows.

## The code library
In code mode, every section written is kept as its own file in
`.smallharness/snippets/<language>/`, one library per language (from the target's
extension), indexed with a brief description: the section's first line, with the
whole section text and the code. The target file is built by joining these files in
recipe order. Before a section is written, its language's library is searched:

- a snippet written for a section that says nearly the same thing (by default 85% of
  the same words, `[recipe].reuse_similarity`) is reused as is, with no model call;
- otherwise the most similar snippets (`[recipe].library_k`, default 3) are shown to
  the model, which can reply `REUSE <id>` to use one unchanged, or write new code.

New code joins the library; identical code is kept once and remembers every recipe
that used it. A recipe never reuses its own other sections (each section sees only
itself, and the file would hold the code twice). Take a snippet out and the sections
that used it are written again. Browse with `/library [LANGUAGE [WORDS]]`,
`/library show|remove LANGUAGE ID`, or the Code library panel in the Recipes tab.
`[recipe].library = false` turns it off.

## Commands
- Terminal: `/new NAME [to=FILE] [text|code]`, `/show NAME`, `/edit NAME`,
  `/write NAME|PATH [to=FILE] [text|code] [all] [only=N,M] [parallel]`, `/list`.
- Shell: `smallharness recipe NAME [--to FILE] [--text|--code] [--all] [--only N,M]
  [--parallel] [-y]`.
- Web: the **Recipes** tab. Delete a recipe with the ✕ in the list; the file it wrote
  stays, and is listed under [Relics](web.md#relics).

Saved recipes live in `.smallharness/recipes/<name>.md`.
