# Resume changes: Software Engineer Intern (Summer 2027), Figma

Source: `input/master.tex`. Output: `output/resume.tex`.

Posting terms this tailoring targets: clean code in a general-purpose language (Python, Java, C++, JavaScript), data structures and algorithms, systems "from infrastructure to UI", full-stack user-facing features, tests, collaboration and feedback.

No bullets were removed or cut. The master resume already fits one page with room to spare, so nothing was trimmed. Section order is unchanged.

## Reworded

1. **Collaborative Whiteboard, bullet 1**
   - New text: "Built a real-time multi-user canvas over WebSockets with CRDT-based conflict resolution for 10 concurrent users."
   - Source: `master.tex:L25`
   - Reason: The posting asks for user-facing features and systems work "from infrastructure to UI." This bullet is the clearest example of both. "Over WebSockets" comes from the project header on the same entry (`master.tex:L23`), so it is not new content.

## Reordered

2. **Skills, language list**
   - New text: "Python, Java, TypeScript, Go, C; PostgreSQL, Redis; Git, Docker, Linux"
   - Source: `master.tex:L33`
   - Reason: The posting's required languages are Python and Java, so they lead the list. TypeScript moves up as the full-stack language, from the Whiteboard project. The same items appear, with no additions.

## Moved

None. Bullet order within every section is unchanged from `master.tex`.

## Unchanged (kept as written)

- `master.tex:L14`, `master.tex:L15`, `master.tex:L16` (intern bullets: caching layer, billing tests, cron migration). These stay as written. The test bullet (`L15`) matches the posting's "build, document, and maintain tests."
- `master.tex:L20` (Teaching Assistant, CS 61B). Kept for the collaboration and communication requirement.
- `master.tex:L26` (Canvas API, 60 fps) and `master.tex:L30` (Tiny Database, B-tree and WAL).
- Header, education, and preamble (`master.tex:L1-L10`) are unchanged.

## Not added

- The posting's nice-to-have, "AI-assisted development tools," does not appear in `input/master.tex`, so it was not added.
- No numbers, titles, dates, or skills were added. Every number in the output (40%, 25, 3, 2, 30, 10, 60 fps) comes from `input/master.tex`.
